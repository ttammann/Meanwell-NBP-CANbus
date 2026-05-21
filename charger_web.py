#!/usr/bin/env python3
"""Web control panel for the Mean Well NPB charger.

Wraps charger_app.MeanWellCharger in a small Flask app and serves a
single-page HTML control panel with live readings, status flags, and
editable curve parameters.

  pip install flask
  python3 charger_web.py [--channel can0] [--bitrate 250000]
                         [--can-id 0xC0103] [--host 0.0.0.0] [--port 8080]

Then open http://<host>:8080 in a browser.
"""
import argparse
import json
import logging
import os
import queue
import random
import signal
import sys
import threading
import time
from functools import wraps
from logging.handlers import RotatingFileHandler

import can
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from charger_app import (
    MeanWellCharger, REGISTERS, RANGES,
    FAULT_BITS, CHG_STATUS_BITS, SYSTEM_STATUS_BITS,
    _decode_bits, _decode_curve_config, _decode_system_config,
)

log = logging.getLogger("npb")

# Flask discovers ./templates and ./static next to this module by default.
app = Flask(__name__)
_lock = threading.Lock()
charger: MeanWellCharger | None = None  # set in main()

# Args captured from CLI in main() so the bus watchdog can rebuild the
# bus with the same settings if it goes down (USB unplug, slcand crash).
_bus_args: dict | None = None


# ---------------------------------------------------------------------------
# Simulated CAN bus for --demo mode
# ---------------------------------------------------------------------------

class FakeBus:
    """In-process CAN bus that pretends to be an NPB-1700-48.

    Lets the web UI run on machines without CAN hardware (e.g. macOS, where
    socketcan does not exist).  Reads return state with mild random drift on
    the live-monitoring registers; writes update state in place.  Interface
    matches the subset of python-can's Bus that MeanWellCharger uses:
    .send(msg), .recv(timeout), .shutdown()."""

    def __init__(self):
        self._state = {
            0x00: 1,          # OPERATION: ON
            0x40: 0x0000,     # FAULT_STATUS: clean
            0x50: 2304,       # READ_VIN  -> 230.4 V
            0x60: 5418,       # READ_VOUT -> 54.18 V
            0x61: 1482,       # READ_IOUT -> 14.82 A
            0x62: 354,        # READ_TEMP -> 35.4 C
            0xB0: 1500, 0xB1: 5520, 0xB2: 5520, 0xB3: 500,
            0xB4: 0x0884,     # CURVE_CONFIG: charger + temp comp + RSTE
            0xB5: 900, 0xB6: 60, 0xB7: 60,
            0xB8: 0x0004,     # CHG_STATUS: CVM (constant voltage)
            0xB9: 4800,       # CHG_RST_VBAT -> 48.0 V
            0xC1: 0x0002,     # SYSTEM_STATUS: DC_OK
            0xC2: 0x0002,     # SYSTEM_CONFIG: power_on=ON
        }
        # ASCII payloads for MFR_* commands (each up to 6 bytes per code).
        self._strings = {
            0x80: b"MEAN-W",  0x81: b"ELL\x00\x00\x00",          # mfr_id  = "MEAN-WELL"
            0x82: b"NPB-17",  0x83: b"00-48\x00",                # model   = "NPB-1700-48"
            0x84: b"DEMO00",  0x85: b"01\x00\x00\x00\x00",       # serial  = "DEMO0001"
            0x86: b"250114",                                     # date    = "250114"
            0x87: b"V01.05",                                     # firmware = "V01.05"
            0x88: b"TW\x00",                                     # location = "TW"
        }
        self._last_request = None
        self._lock = threading.Lock()
        self._rand = random.Random()

    def _drift(self, code, base):
        """Small random fluctuation so the dashboard doesn't look frozen."""
        if code == 0x60: return max(0, int(base + self._rand.uniform(-3, 3)))    # vout
        if code == 0x61: return max(0, int(base + self._rand.uniform(-15, 15)))  # iout
        if code == 0x62: return int(base + self._rand.uniform(-2, 2))            # temp
        if code == 0x50: return int(base + self._rand.uniform(-1, 1))            # vin
        return base

    def send(self, msg):
        with self._lock:
            data = list(msg.data)
            code = data[0]
            if len(data) <= 2:
                self._last_request = code
            else:
                if len(data) >= 4:
                    self._state[code] = data[2] | (data[3] << 8)
                else:
                    self._state[code] = data[2]

    def recv(self, timeout=None):
        with self._lock:
            code = self._last_request
            if code is None:
                return None
            # Clear it now so a subsequent recv (e.g. the drain loop in
            # _request) returns None instead of replaying the same frame.
            self._last_request = None
            # ASCII string registers (MFR_*) come from a separate map.
            if code in self._strings:
                payload = [code, 0] + list(self._strings[code].ljust(6, b"\x00")[:6])
                return can.Message(arbitration_id=0xC0103, data=payload, is_extended_id=True)
            if code not in self._state:
                return None
            val = self._state[code]
            # live drift, but only when the output is actually on
            if self._state[0x00] == 1 and code in (0x50, 0x60, 0x61, 0x62):
                val = self._drift(code, val)
            elif self._state[0x00] == 0 and code in (0x60, 0x61):
                val = 0  # output off -> no V/I
            if code == 0x00:
                payload = [code, 0, val, 0, 0, 0, 0, 0]
            else:
                payload = [code, 0, val & 0xFF, (val >> 8) & 0xFF, 0, 0, 0, 0]
        return can.Message(arbitration_id=0xC0103, data=payload, is_extended_id=True)

    def shutdown(self):
        pass


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def safe_can(fn):
    """Catch CAN/value errors and turn them into JSON 4xx/5xx responses."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, KeyError) as e:
            return jsonify({"ok": False, "error": str(e)}), 400
        except can.CanError as e:
            log.warning("CAN error in %s: %s", fn.__name__, e)
            resp = jsonify({"ok": False, "error": f"CAN error: {e}"})
            resp.status_code = 503
            # Hint clients to back off briefly rather than hammering on
            # a flapping bus (e.g. mid-reconnect after USB unplug).
            resp.headers["Retry-After"] = "3"
            return resp
    return wrapper


@app.after_request
def _security_headers(resp):
    """Conservative defaults for a LAN tool.  Google Fonts is allow-listed
    because the UI uses Fraunces / Inter / JetBrains Mono from it; if you
    self-host the fonts you can tighten this further."""
    # SSE responses are stream-style; setting these is harmless but the
    # Cache-Control we already set on /api/stream takes precedence.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "frame-ancestors 'none'"
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

_ROOT = os.path.dirname(os.path.abspath(__file__))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/preview")
@app.route("/preview.html")
def preview_page():
    """Static UI with mocked API (preview-mock.js); same port as the live app."""
    return send_from_directory(_ROOT, "preview.html")


@app.route("/api/registers")
def api_registers():
    """Static metadata for the UI to render its forms."""
    return jsonify({
        name: {
            "code":     reg.code,
            "scale":    reg.scale,
            "size":     reg.size,
            "unit":     reg.unit,
            "writable": reg.writable,
            "desc":     reg.desc,
            "range":    list(RANGES[name]) if name in RANGES else None,
        }
        for name, reg in REGISTERS.items()
    })


@app.route("/api/read")
@safe_can
def api_read():
    arg = request.args.get("names", "")
    names = [n.strip() for n in arg.split(",") if n.strip()] or list(REGISTERS.keys())
    # Reject unknown names up front so the caller knows something's wrong
    # instead of getting a quietly-incomplete dict back.
    unknown = [n for n in names if n not in REGISTERS]
    if unknown:
        return jsonify({"ok": False,
                        "error": f"unknown register(s): {', '.join(unknown)}",
                        "known": sorted(REGISTERS)}), 400
    out = {}
    with _lock:
        for name in names:
            raw, scaled = charger.read_register(name)
            out[name] = {"raw": raw, "value": scaled}
    return jsonify(out)


@app.route("/api/status")
@safe_can
def api_status():
    out = {}
    with _lock:
        for key, bits in (("fault_status",  FAULT_BITS),
                          ("chg_status",    CHG_STATUS_BITS),
                          ("system_status", SYSTEM_STATUS_BITS)):
            raw, _ = charger.read_register(key)
            out[key] = {
                "raw":   raw,
                "flags": _decode_bits(raw or 0, bits) if raw is not None else [],
            }
        for key, decoder in (("curve_config",  _decode_curve_config),
                             ("system_config", _decode_system_config)):
            raw, _ = charger.read_register(key)
            out[key] = {
                "raw":     raw,
                "decoded": decoder(raw) if raw is not None else None,
            }
    return jsonify(out)


@app.route("/api/write", methods=["POST"])
@safe_can
def api_write():
    """Write a batch of registers, then re-read each one and return the
    post-write state.  Returning what we requested *and* what landed lets
    the UI detect rounding / clamping / silent failures instead of
    optimistically displaying "saved!" for a value the firmware rejected.
    """
    body = request.get_json(force=True) or {}
    raw_settings = body.get("settings", {})
    cycle = bool(body.get("cycle", True))
    settings = list(raw_settings.items())
    with _lock:
        was_on = charger.write_many(settings, cycle=cycle)
        # Re-read so the caller sees what actually committed.  Same lock
        # acquisition — we hold it across both phases so a concurrent
        # write can't interleave.
        post = {}
        for name, _v in settings:
            raw, scaled = charger.read_register(name)
            post[name] = {"raw": raw, "value": scaled}
    return jsonify({
        "ok":         True,
        "wrote":      dict(settings),
        "post":       post,
        "cycled":     bool(cycle and was_on),
        "was_on":     was_on,
    })


@app.route("/api/on", methods=["POST"])
@safe_can
def api_on():
    with _lock:
        charger.set_on()
    return jsonify({"ok": True})


@app.route("/api/off", methods=["POST"])
@safe_can
def api_off():
    with _lock:
        charger.set_off()
    return jsonify({"ok": True})


@app.route("/api/device_info")
@safe_can
def api_device_info():
    """Identity + always-readable info (no battery / output current required)."""
    with _lock:
        return jsonify(charger.device_info())


@app.route("/api/operation")
@safe_can
def api_operation():
    """Cheap single-register read for the UI to ask "is the output on right
    now?" before committing changes.  Used by the Apply confirmation modal
    so we can warn the user when Apply is about to power-cycle the output
    vs. when it'll just write while OFF."""
    with _lock:
        raw, _ = charger.read_register("operation")
    return jsonify({"operation": raw, "on": raw == 1})


# ---------------------------------------------------------------------------
# Server-Sent Events stream — single broadcaster, many subscribers
# ---------------------------------------------------------------------------
#
# Replaces a per-client read loop with one shared "broadcaster" thread
# that polls the CAN bus on a fixed cadence and fans each `state` event
# out to every connected EventSource via per-subscriber queue.Queue.
#
# Win in three dimensions:
#   * CPU / thread count: N browsers -> 1 polling thread instead of N
#   * CAN traffic: 1 read per tick total, instead of N
#   * Lock contention on _lock: 1 acquirer per tick, predictable cadence
#
# We also fold in:
#   * Disconnect hysteresis — flip to "disconnected" only after 2
#     consecutive failed reads, so a transient single-frame timeout
#     during a busy write doesn't flap the UI.
#   * CAN bus auto-reconnect — after N consecutive failures, tear down
#     the python-can Bus and re-build it with the same args.  Survives
#     USB unplug / slcand restart without a process restart.
#   * Per-read latency in the payload (ms), so the UI can show "connected · 28 ms".

STREAM_HEARTBEAT_S    = 3.0  # broadcaster cadence (also CAN read interval)
DISCONNECT_THRESHOLD  = 2    # consecutive read failures before flipping UI
RECONNECT_THRESHOLD   = 4    # consecutive CAN errors before bus rebuild
SUBSCRIBER_QUEUE_MAX  = 32   # cap per-client backlog; older events drop silently


class StateBroadcaster:
    """One shared CAN poller; N SSE subscribers.

    Each subscriber gets a bounded queue.Queue.  The poller puts the
    latest state event into every queue every STREAM_HEARTBEAT_S seconds;
    subscriber generators drain their own queue and yield SSE bytes.

    Failure model:
      * `read_register` returns (None, None) on timeout — counted as
        a "no-response" tick, not a CAN error.
      * `can.CanError` is a harder failure (bus dropped, USB unplugged,
        slcand died, etc.) — after RECONNECT_THRESHOLD in a row, we
        rebuild the python-can Bus and reset the counters.
      * UI sees `connected=False` only after DISCONNECT_THRESHOLD
        consecutive no-response *or* error ticks, so a single hiccup
        doesn't blink the chip.
    """

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._subs_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="npb-broadcaster",
                                        daemon=True)
        self._fail_streak = 0
        self._err_streak  = 0
        self._last_payload: dict | None = None

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        # Wake every subscriber so their generators can exit cleanly.
        with self._subs_lock:
            for q in self._subs:
                try: q.put_nowait(None)
                except queue.Full: pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_MAX)
        with self._subs_lock:
            self._subs.append(q)
        # Push the last-known state so a freshly-connected browser doesn't
        # have to wait up to STREAM_HEARTBEAT_S for its first event.
        if self._last_payload is not None:
            try: q.put_nowait(self._last_payload)
            except queue.Full: pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._subs_lock:
            try: self._subs.remove(q)
            except ValueError: pass

    def _try_reconnect(self):
        """Rebuild the python-can Bus when the current one is wedged.
        Called from the poll loop after RECONNECT_THRESHOLD failures."""
        global charger
        if _bus_args is None or _bus_args.get("demo"):
            return  # nothing to rebuild in demo mode
        log.warning("CAN bus appears down (%d consecutive errors); "
                    "attempting reconnect…", self._err_streak)
        try:
            with _lock:
                old = charger
                charger = MeanWellCharger(
                    channel=_bus_args["channel"],
                    bitrate=_bus_args["bitrate"],
                    can_id =_bus_args["can_id"],
                    interface=_bus_args["interface"],
                    recv_timeout=0.5)
                try: old.shutdown()
                except Exception: pass
            log.info("CAN bus rebuilt successfully")
            self._err_streak = 0
        except Exception as e:
            log.error("CAN reconnect failed: %s", e)
            # Counter not reset — we'll try again next cycle.

    def _read_once(self) -> dict:
        """One CAN read, with latency timing and failure accounting."""
        t0 = time.monotonic()
        try:
            with _lock:
                raw, _ = charger.read_register("operation")
            latency_ms = (time.monotonic() - t0) * 1000.0
            if raw is None:
                self._fail_streak += 1
                self._err_streak  = 0
                return {
                    "connected":   self._fail_streak < DISCONNECT_THRESHOLD
                                   and self._last_payload is not None
                                   and self._last_payload.get("connected", False),
                    "operation":   None,
                    "latency_ms":  None,
                    "fail_streak": self._fail_streak,
                    "ts":          time.time(),
                }
            # Success — reset both streaks.
            self._fail_streak = 0
            self._err_streak  = 0
            return {
                "connected":   True,
                "operation":   raw,
                "latency_ms":  round(latency_ms, 1),
                "fail_streak": 0,
                "ts":          time.time(),
            }
        except can.CanError as e:
            self._err_streak  += 1
            self._fail_streak += 1
            log.debug("CAN read error (%d/%d): %s",
                      self._err_streak, RECONNECT_THRESHOLD, e)
            if self._err_streak >= RECONNECT_THRESHOLD:
                self._try_reconnect()
            return {
                "connected":   self._fail_streak < DISCONNECT_THRESHOLD
                               and self._last_payload is not None
                               and self._last_payload.get("connected", False),
                "operation":   None,
                "latency_ms":  None,
                "error":       f"CAN error: {e}",
                "fail_streak": self._fail_streak,
                "ts":          time.time(),
            }

    def _run(self):
        while not self._stop.is_set():
            payload = self._read_once()
            self._last_payload = payload
            with self._subs_lock:
                dead = []
                for q in self._subs:
                    try:
                        q.put_nowait(payload)
                    except queue.Full:
                        # Subscriber is too slow — drop the oldest event
                        # and try once more.  If still full, give up on
                        # this tick (the next one will come 3 s later).
                        try:
                            q.get_nowait()
                            q.put_nowait(payload)
                        except (queue.Empty, queue.Full):
                            dead.append(q)
                for q in dead:
                    # Subscriber will close itself on the next iteration
                    # of its generator; we just stop trying to feed it.
                    try: self._subs.remove(q)
                    except ValueError: pass
            self._stop.wait(STREAM_HEARTBEAT_S)


_broadcaster: StateBroadcaster | None = None


def _stream_events(q: queue.Queue):
    """Generator: yield SSE-formatted bytes for one subscriber.

    Sentinel value `None` (pushed on shutdown) terminates the loop.  We
    also unsubscribe in `finally` so a client disconnect via the
    underlying TCP close (GeneratorExit) doesn't leak the queue."""
    try:
        while True:
            payload = q.get()
            if payload is None:
                return  # broadcaster shutting down
            yield f"event: state\ndata: {json.dumps(payload)}\n\n"
    finally:
        if _broadcaster is not None:
            _broadcaster.unsubscribe(q)


@app.route("/api/stream")
def api_stream():
    q = _broadcaster.subscribe()
    resp = Response(_stream_events(q), mimetype="text/event-stream")
    # Disable buffering — important when the app is fronted by nginx, and
    # harmless when it isn't.
    resp.headers["Cache-Control"]    = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


# ---------------------------------------------------------------------------
# logging + lifecycle
# ---------------------------------------------------------------------------

def _setup_logging(log_path: str | None):
    """Stream to stdout (always) + rotating file (when --log-file given)."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S")
    # Stream handler (stdout — picked up by docker logs / journald).
    if not any(isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
               for h in root.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)
    if log_path:
        fh = RotatingFileHandler(log_path, maxBytes=512 * 1024, backupCount=3)
        fh.setFormatter(fmt)
        root.addHandler(fh)
        log.info("Logging to %s (rotating, 512 KiB × 3)", log_path)


def _install_signal_handlers():
    """Cleanly tear down the broadcaster + CAN bus on SIGTERM/SIGINT.

    Docker sends SIGTERM on `docker stop`; without this the CAN socket
    leaks until the kernel reaps it, and the broadcaster thread keeps
    polling for ~10 s during the grace period."""
    def _shutdown(signum, _frame):
        log.info("signal %d received — shutting down", signum)
        try:
            if _broadcaster is not None:
                _broadcaster.stop()
        finally:
            try:
                if charger is not None:
                    charger.shutdown()
            finally:
                # os._exit doesn't flush stdio — push the "shutting
                # down" log line out so docker logs / journald sees it.
                logging.shutdown()
                sys.stdout.flush(); sys.stderr.flush()
                # Flask dev server doesn't have a graceful shutdown hook,
                # so we exit hard.  All clean-up that matters is done.
                os._exit(0)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _shutdown)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--channel", default="can0",
                    help="CAN channel name (default: can0)")
    ap.add_argument("--bitrate", type=int, default=250000)
    ap.add_argument("--can-id", type=lambda x: int(x, 0), default=0xC0103)
    ap.add_argument("--interface", default="socketcan",
                    help="python-can interface (default: socketcan; on macOS try "
                         "'slcan', 'pcan', 'kvaser', or 'gs_usb' for your adapter)")
    ap.add_argument("--demo", action="store_true",
                    help="Run with a simulated charger; no CAN hardware needed. "
                         "Use this on macOS or for UI development.")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--log-file", default=os.environ.get("NPB_LOG_FILE"),
                    help="optional rotating-file log path (also goes to stdout)")
    args = ap.parse_args()

    _setup_logging(args.log_file)

    global charger, _bus_args, _broadcaster
    _bus_args = {
        "channel":   args.channel,
        "bitrate":   args.bitrate,
        "can_id":    args.can_id,
        "interface": args.interface,
        "demo":      args.demo,
    }
    if args.demo:
        log.info("DEMO MODE: simulated charger, no real CAN bus")
        charger = MeanWellCharger(can_id=args.can_id, bus=FakeBus(), recv_timeout=0.5)
    else:
        charger = MeanWellCharger(channel=args.channel,
                                  bitrate=args.bitrate,
                                  can_id=args.can_id,
                                  interface=args.interface,
                                  recv_timeout=0.5)
        log.info("CAN: %s ch=%s @ %d id=0x%X",
                 args.interface, args.channel, args.bitrate, args.can_id)

    _broadcaster = StateBroadcaster()
    _broadcaster.start()
    _install_signal_handlers()

    log.info("Starting on http://%s:%d", args.host, args.port)
    try:
        app.run(host=args.host, port=args.port, threaded=True, debug=False,
                use_reloader=False)
    finally:
        _broadcaster.stop()
        charger.shutdown()


if __name__ == "__main__":
    main()
