#!/usr/bin/env python3
"""Mean Well NPB-series CAN-bus charger CLI.

Talks to NPB-450 / 750 / 1200 / 1700 chargers per the CANBus protocol in
section 6 of the NPB-NPP installation manual.  All commands are 16-bit
addresses with low byte first, then up to 6 data bytes (LSB first).
"""
import argparse
import sys
import time
from dataclasses import dataclass

import can


# ---------------------------------------------------------------------------
# Register table
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Register:
    code: int
    scale: float   # multiply raw int by this to get human units; 1 = bitfield/int
    size: int      # data byte count: 1 or 2
    unit: str
    writable: bool
    desc: str


REGISTERS = {
    "operation":        Register(0x00, 1,    1, "",    True,  "ON/OFF control (0=off, 1=on)"),
    "vout_set":         Register(0x20, 0.01, 2, "V",   True,  "Output voltage setpoint (PSU mode)"),
    "iout_set":         Register(0x30, 0.01, 2, "A",   True,  "Output current setpoint (PSU mode)"),
    "fault_status":     Register(0x40, 1,    2, "",    False, "Fault status (bitfield)"),
    "read_vin":         Register(0x50, 0.1,  2, "V",   False, "Input voltage"),
    "read_vout":        Register(0x60, 0.01, 2, "V",   False, "Output voltage measurement"),
    "read_iout":        Register(0x61, 0.01, 2, "A",   False, "Output current measurement"),
    "read_temp":        Register(0x62, 0.1,  2, "C",   False, "Internal ambient temperature"),
    "curve_cc":         Register(0xB0, 0.01, 2, "A",   True,  "Charge current (CC stage)"),
    "curve_cv":         Register(0xB1, 0.01, 2, "V",   True,  "Boost / CV voltage"),
    "curve_fv":         Register(0xB2, 0.01, 2, "V",   True,  "Float voltage"),
    "curve_tc":         Register(0xB3, 0.01, 2, "A",   True,  "Taper current (CV->FV transition)"),
    "curve_config":     Register(0xB4, 1,    2, "",    True,  "Curve config bitfield (e.g. 0x0884)"),
    "curve_cc_timeout": Register(0xB5, 1,    2, "min", True,  "CC stage timeout"),
    "curve_cv_timeout": Register(0xB6, 1,    2, "min", True,  "CV stage timeout"),
    "curve_fv_timeout": Register(0xB7, 1,    2, "min", True,  "FV stage timeout"),
    "chg_status":       Register(0xB8, 1,    2, "",    False, "Charging status (bitfield)"),
    "chg_rst_vbat":     Register(0xB9, 0.01, 2, "V",   True,  "Restart-charge battery voltage"),
    "system_status":    Register(0xC1, 1,    2, "",    False, "System status (bitfield)"),
    "system_config":    Register(0xC2, 1,    2, "",    True,  "System config (bitfield)"),
}

# (lo, hi) inclusive validation ranges for writable scaled registers.  Voltage
# ceilings are set for a 16-cell LFP pack (16 * 3.65 V = 58.4 V absolute max);
# widen / lower here for a different chemistry or cell count.
RANGES = {
    "vout_set":         (42.0, 58.4),
    "iout_set":         (5.0,  25.0),
    "curve_cc":         (5.0,  25.0),
    "curve_cv":         (42.0, 58.4),
    "curve_fv":         (42.0, 58.4),
    "curve_tc":         (0.5,  7.5),
    "curve_cc_timeout": (60,   64800),
    "curve_cv_timeout": (60,   64800),
    "curve_fv_timeout": (60,   64800),
    "chg_rst_vbat":     (0.0,  58.4),
}

# Bitfield decoders: (bit_index_in_16bit_word, name, description-when-set)
FAULT_BITS = [
    (1, "OTP",     "over-temperature"),
    (2, "OVP",     "over-voltage"),
    (3, "OLP",     "over-load"),
    (4, "SHORT",   "short-circuit"),
    (5, "AC_FAIL", "AC abnormal"),
    # Bit 6 (OP_OFF) is informational status, not a fault — output is just off.
    # Already shown in the header master switch, so we don't repeat it here.
    (7, "HI_TEMP", "internal high temp"),
]

CHG_STATUS_BITS = [
    (0,  "FULLM",       "fully charged"),
    (1,  "CCM",         "in CC mode"),
    (2,  "CVM",         "in CV mode"),
    (3,  "FVM",         "in float mode"),
    (6,  "WAKEUP_STOP", "wake-up unfinished"),
    (7,  "HI_TEMP",     "internal high temp"),
    (10, "NTCER",       "temp-comp circuit fault"),
    (11, "BTNC",        "no battery detected"),
    (13, "CCTOF",       "CC mode timed out"),
    (14, "CVTOF",       "CV mode timed out"),
    (15, "FVTOF",       "float mode timed out"),
]

SYSTEM_STATUS_BITS = [
    (1, "DC_OK",         "DC output normal"),
    (5, "INITIAL_STATE", "in initial state"),
    (6, "EEPER",         "EEPROM access error"),
]


def _decode_bits(value, table):
    return [f"{name} ({desc})" for bit, name, desc in table if value & (1 << bit)]


def _decode_curve_config(v):
    cuvs = {0: "customised", 1: "preset 1", 2: "preset 2", 3: "preset 3"}
    tcs  = {0: "off", 1: "-3mV/C/cell", 2: "-4mV/C/cell", 3: "-5mV/C/cell"}
    parts = [
        f"curve={cuvs[v & 0b11]}",
        f"temp_comp={tcs[(v >> 2) & 0b11]}",
        f"cv_timeout_action={'enter float' if v & (1 << 5) else 'cut-off'}",
        f"mode={'charger' if v & (1 << 7) else 'PSU'}",
        f"cv_timeout_en={'on' if v & (1 << 8) else 'off'}",
        f"cc_timeout_en={'on' if v & (1 << 9) else 'off'}",
        f"fv_timeout_en={'on' if v & (1 << 10) else 'off'}",
        f"restart_en={'on' if v & (1 << 11) else 'off'}",
    ]
    return ", ".join(parts)


def _decode_system_config(v):
    op_init = {0: "OFF", 1: "ON", 2: "last setting", 3: "reserved"}
    return ", ".join([
        f"power_on_state={op_init[(v >> 1) & 0b11]}",
        f"eeprom_writes={'disabled' if v & (1 << 10) else 'enabled'}",
    ])


# ---------------------------------------------------------------------------
# Charger client
# ---------------------------------------------------------------------------

class MeanWellCharger:
    """Mean Well NPB-series CAN-bus charger client (one class, all logic)."""

    def __init__(self, channel="can0", bitrate=250000, can_id=0xC0103,
                 recv_timeout=1.0, interface="socketcan", bus=None):
        # Allow a pre-built bus to be injected (used by demo / fake mode).
        # Otherwise build a real one — `interface` lets non-Linux callers
        # pick e.g. "slcan", "pcan", "kvaser", "gs_usb", or "virtual".
        self._bus = bus if bus is not None else can.Bus(
            interface=interface, channel=channel, bitrate=bitrate)
        self._id = can_id
        self._timeout = recv_timeout

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.shutdown()

    def shutdown(self):
        try:
            self._bus.shutdown()
        except Exception:
            pass

    # --- low-level CAN ----------------------------------------------------

    def _send(self, code, data):
        msg = can.Message(
            arbitration_id=self._id,
            data=[code & 0xFF, (code >> 8) & 0xFF, *data],
            is_extended_id=True,
        )
        self._bus.send(msg)

    def _request(self, code):
        """Send a 2-byte read request; return the data bytes that follow the
        echoed command code in the response.

        Failure semantics — important for the SSE broadcaster to behave
        correctly:
          * a response that times out / doesn't echo our code returns None
            (the bus is healthy, the charger just didn't answer this read)
          * a `can.CanError` from `_bus.send` or the response `_bus.recv`
            propagates up to the caller (the bus itself is broken — USB
            unplug, slcand crashed, kernel socket closed, etc.).  The
            broadcaster catches that and triggers a bus rebuild.

        The drain-pre-read is wrapped in its own try/except: errors there
        are not actionable and we'd rather just attempt the real request
        than fail the call because of buffer-cleanup quirks.

        Per manual §6.1, the protocol requires a matching command echo in
        the response.  We validate it here to avoid mis-attributing a stale
        or out-of-order frame to the wrong register read.

        Total wait is bounded by ``self._timeout`` (a single deadline),
        not ``N × self._timeout`` per skipped frame.  A misbehaving bus
        that floods us with the wrong-coded frames cannot hold _lock for
        more than ``recv_timeout`` per call."""
        # Drain up to a few stale frames that may be sitting in the
        # receive buffer (unsolicited frames, or leftovers from a previous
        # request that timed out).  Bounded so a misbehaving bus can't
        # hang us; errors here are intentionally swallowed.
        try:
            for _ in range(8):
                stale = self._bus.recv(0)
                if stale is None:
                    break
        except can.CanError:
            pass  # drain hiccup is fine; the real send/recv below is what matters

        # send + response recv — errors here propagate so the broadcaster
        # can distinguish "bus broken" from "no answer".
        self._bus.send(can.Message(
            arbitration_id=self._id,
            data=[code & 0xFF, (code >> 8) & 0xFF],
            is_extended_id=True,
        ))
        deadline = time.monotonic() + self._timeout
        # Belt + braces sanity bound on iterations even if the bus is
        # spamming wrong-coded frames faster than `time.monotonic()` ticks.
        attempts = 16
        while attempts > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            resp = self._bus.recv(remaining)
            if not resp or len(resp.data) < 2:
                return None
            if resp.data[0] == (code & 0xFF) and resp.data[1] == ((code >> 8) & 0xFF):
                if len(resp.data) < 3:
                    return None
                return list(resp.data[2:])
            attempts -= 1
        return None

    # --- generic register access -----------------------------------------

    def read_register(self, name):
        """Read register `name`; return (raw_int, scaled_value) or (None, None)."""
        reg = REGISTERS[name]
        data = self._request(reg.code)
        if data is None or len(data) < reg.size:
            return None, None
        if reg.size == 1:
            raw = data[0]
        else:
            raw = data[0] | (data[1] << 8)
        scaled = raw * reg.scale if reg.scale != 1 else raw
        return raw, scaled

    def write_register(self, name, value):
        """Write `value` (in real units) to register `name`."""
        reg = REGISTERS[name]
        if not reg.writable:
            raise ValueError(f"register {name!r} is read-only")
        if name in RANGES:
            lo, hi = RANGES[name]
            if not (lo <= value <= hi):
                raise ValueError(f"{name} must be in [{lo}, {hi}] (got {value})")
        raw = int(value) if reg.scale == 1 else int(round(value / reg.scale))
        if reg.size == 1:
            self._send(reg.code, [raw & 0xFF])
        else:
            self._send(reg.code, [raw & 0xFF, (raw >> 8) & 0xFF])

    # --- manufacturer / firmware info (ASCII payload registers) ----------

    def _read_string_block(self, code, length):
        """MFR_* registers return up to 6 ASCII bytes per command code,
        padded with 0x00.  Strip nulls and decode.  Bus errors are
        squashed to None — device_info is best-effort by design."""
        try:
            data = self._request(code)
        except can.CanError:
            return None
        if data is None:
            return None
        chunk = bytes(data[:length])
        return chunk.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip() or None

    def device_identity(self, read_hook=None):
        """Manufacturer / model / serial strings — no live telemetry."""
        def _do_read(fn):
            if read_hook is not None:
                return read_hook(fn)
            time.sleep(self.INTER_WRITE_DELAY_S)
            return fn()

        def _read_string(code, length):
            return _do_read(lambda: self._read_string_block(code, length))

        mfr_id    = (_read_string(0x80, 6) or "") + (_read_string(0x81, 6) or "")
        mfr_model = (_read_string(0x82, 6) or "") + (_read_string(0x83, 6) or "")
        serial    = (_read_string(0x84, 6) or "") + (_read_string(0x85, 6) or "")
        date      =  _read_string(0x86, 6)
        revision  =  _read_string(0x87, 6)
        location  =  _read_string(0x88, 3)
        return {
            "manufacturer": mfr_id.strip()    or None,
            "model":        mfr_model.strip() or None,
            "serial":       serial.strip()    or None,
            "location":     location,
            "firmware":     revision,
            "made":         date,
        }

    def device_info(self, read_hook=None):
        """Return charger identity + always-readable info as a dict.
        Every key may be None if that read failed/timed out.

        Optional ``read_hook(fn)`` wraps each CAN read (e.g. the web UI
        acquires a lock per read and sleeps between reads *outside* the
        lock so SSE / Apply are not blocked for the whole ~250 ms)."""
        out = self.device_identity(read_hook=read_hook)
        def _do_read(fn):
            if read_hook is not None:
                return read_hook(fn)
            time.sleep(self.INTER_WRITE_DELAY_S)
            return fn()
        try:
            _, vin  = _do_read(lambda: self.read_register("read_vin")[1])
        except can.CanError:
            vin = None
        try:
            _, temp = _do_read(lambda: self.read_register("read_temp")[1])
        except can.CanError:
            temp = None
        out["vin"] = vin
        out["temp"] = temp
        return out

    # --- ON/OFF / probe ---------------------------------------------------

    def set_off(self):
        self._send(0x00, [0x00])

    def set_on(self):
        self._send(0x00, [0x01])

    def bus_ok(self):
        # CanError ("bus broken") is the strongest possible "not OK"; the
        # CLI `check` subcommand wants a simple yes/no, not a stack trace.
        try:
            raw, _ = self.read_register("operation")
        except can.CanError:
            return False
        return raw is not None and raw in (0, 1)

    # --- batched writes ---------------------------------------------------

    # Manual section 6.1 requires >=20 ms between successive requests.
    # The previous value (50 ms) was conservative cargo-cult; 25 ms is
    # comfortably within spec and makes a 7-register Apply visibly snappier
    # (~175 ms instead of ~350 ms blocking _lock in the request handler).
    INTER_WRITE_DELAY_S = 0.025

    def write_many(self, settings, cycle=True):
        """Write a list of (name, value) settings.

        B0..B9 only commit while the remote output is OFF (manual §6.5), so
        we briefly cycle OFF/ON around the writes.  The original output
        state is *preserved*: if the user had the output OFF before Apply,
        we leave it OFF afterwards instead of silently energising the
        charger.  This is a real safety property — a connected battery
        suddenly seeing charge current is not OK.

        Returns the original operation state (0/1) so callers can surface
        the cycle in the UI (or skip it entirely on a read-only preview).
        """
        for name, value in settings:
            reg = REGISTERS[name]
            if not reg.writable:
                raise ValueError(f"register {name!r} is read-only")
            if name in RANGES:
                lo, hi = RANGES[name]
                if not (lo <= value <= hi):
                    raise ValueError(
                        f"{name} must be in [{lo}, {hi}] (got {value})")
        was_on = None
        if cycle:
            raw, _ = self.read_register("operation")
            if raw is None:
                raise ValueError(
                    "cannot determine output state (operation read failed); "
                    "refusing write with power-cycle — retry or use --no-cycle "
                    "only if you are certain the output is OFF")
            was_on = (raw == 1)
            if was_on:
                self.set_off()
                time.sleep(self.INTER_WRITE_DELAY_S)
        try:
            for name, value in settings:
                self.write_register(name, value)
                time.sleep(self.INTER_WRITE_DELAY_S)
        finally:
            # Only re-energise if the user already had it on.  If the
            # output was off (or we couldn't determine state), leave it
            # off — the writes still commit because B0..B9 don't need
            # the output to be on, only "not on while writing".
            if cycle and was_on:
                self.set_on()
        return was_on

    def set_restart_voltage(self, v):
        """Enable restart-on-Vbat and set the trigger voltage.  0x0884 in
        CURVE_CONFIG = charger mode + -3mV/C/cell temp comp + restart
        enable bit, which the manual requires before B9 is honoured."""
        self.write_many([
            ("curve_config", 0x0884),
            ("chg_rst_vbat", v),
        ])

    # --- pretty printers --------------------------------------------------

    def print_register(self, name):
        reg = REGISTERS[name]
        raw, value = self.read_register(name)
        if raw is None:
            print(f"  {name:<18} ---")
            return
        if reg.scale == 1:
            print(f"  {name:<18} {raw} {reg.unit:<3}  (0x{raw:04X})")
        else:
            print(f"  {name:<18} {value:>7.2f} {reg.unit:<3}  (raw={raw})")

    def print_status(self):
        def line(label, raw, decoded, empty_text):
            if raw is None:
                print(f"  {label:<14} (no response)")
            else:
                items = ", ".join(decoded) if decoded else empty_text
                print(f"  {label:<14} 0x{raw:04X}  {items}")

        print("Status:")
        raw, _ = self.read_register("fault_status")
        line("fault_status", raw, _decode_bits(raw or 0, FAULT_BITS), "all clear")

        raw, _ = self.read_register("chg_status")
        line("chg_status", raw, _decode_bits(raw or 0, CHG_STATUS_BITS), "idle")

        raw, _ = self.read_register("system_status")
        line("system_status", raw, _decode_bits(raw or 0, SYSTEM_STATUS_BITS), "normal")

        raw, _ = self.read_register("curve_config")
        if raw is None:
            print(f"  {'curve_config':<14} (no response)")
        else:
            print(f"  {'curve_config':<14} 0x{raw:04X}  {_decode_curve_config(raw)}")

        raw, _ = self.read_register("system_config")
        if raw is None:
            print(f"  {'system_config':<14} (no response)")
        else:
            print(f"  {'system_config':<14} 0x{raw:04X}  {_decode_system_config(raw)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_value(name, raw):
    """Parse a CLI value: hex (0x...) -> int, else float for scaled regs,
    int for bitfield/integer regs."""
    if raw.lower().startswith("0x"):
        return int(raw, 16)
    if name in REGISTERS and REGISTERS[name].scale == 1:
        return int(raw, 0)
    return float(raw)


def _parse_kv(arg):
    if "=" not in arg:
        raise argparse.ArgumentTypeError(f"expected KEY=VALUE, got {arg!r}")
    k, v = arg.split("=", 1)
    if k not in REGISTERS:
        raise argparse.ArgumentTypeError(
            f"unknown register {k!r} (try `charger_app.py list`)")
    if not REGISTERS[k].writable:
        raise argparse.ArgumentTypeError(f"register {k!r} is read-only")
    try:
        value = _parse_value(k, v)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad value for {k}: {e}")
    return (k, value)


EPILOG = """\
examples:
  charger_app.py check
  charger_app.py read
  charger_app.py read curve_cc curve_cv curve_fv
  charger_app.py write curve_cc=15 curve_cv=55.2 curve_fv=55.2 curve_tc=5
  charger_app.py write curve_config=0x0884 chg_rst_vbat=48
  charger_app.py write curve_cc_timeout=900 curve_cv_timeout=60 curve_fv_timeout=60
  charger_app.py status
  charger_app.py list

notes:
  - `write` accepts any number of KEY=VALUE pairs; a single OFF/ON cycle
    wraps the whole batch (use --no-cycle to skip).
  - integer / bitfield registers accept decimal or 0x... hex values.
  - `list` shows every known register and whether it's writable.
"""


def _build_parser():
    p = argparse.ArgumentParser(
        prog="charger_app.py",
        description="Mean Well NPB-series CAN-bus charger CLI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    p.add_argument("--channel", default="can0",
                   help="SocketCAN channel (default: can0)")
    p.add_argument("--bitrate", type=int, default=250000,
                   help="CAN bitrate in bps (default: 250000)")
    p.add_argument("--can-id", type=lambda x: int(x, 0), default=0xC0103,
                   help="29-bit CAN arbitration ID (default: 0xC0103)")
    p.add_argument("--interface", default="socketcan",
                   help="python-can interface (default: socketcan; on macOS try "
                        "'slcan', 'pcan', 'kvaser', or 'gs_usb' for your adapter)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("check",     help="probe the bus and report whether the charger answered")
    sub.add_parser("on",        help="turn the output ON")
    sub.add_parser("off",       help="turn the output OFF")
    sub.add_parser("status",    help="read & decode all bitfield/status registers")
    sub.add_parser("list",      help="list all known registers (no CAN traffic)")

    pr = sub.add_parser("read",
                        help="read one or more registers (default: all)")
    pr.add_argument("registers", nargs="*",
                    help="register names; empty = read everything")

    pw = sub.add_parser("write",
                        help="write one or more KEY=VALUE settings in a single batch")
    pw.add_argument("settings", nargs="+", type=_parse_kv, metavar="KEY=VALUE",
                    help="e.g. curve_cc=15 curve_cv=55.2 curve_config=0x0884")
    pw.add_argument("--no-cycle", action="store_true",
                    help="skip the OFF/ON output cycle around the writes")
    return p


def _cmd_list():
    print(f"{'name':<18} {'code':<6} {'scale':<7} {'unit':<5} {'rw':<3} description")
    print("-" * 78)
    for name, reg in REGISTERS.items():
        rw = "rw" if reg.writable else "ro"
        scale = "" if reg.scale == 1 else f"{reg.scale}"
        print(f"{name:<18} 0x{reg.code:02X}   {scale:<7} {reg.unit:<5} {rw:<3} {reg.desc}")


def main(argv=None):
    args = _build_parser().parse_args(argv)

    if args.command == "list":
        _cmd_list()
        return 0

    with MeanWellCharger(channel=args.channel,
                         bitrate=args.bitrate,
                         can_id=args.can_id,
                         interface=args.interface) as mw:
        if args.command == "check":
            ok = mw.bus_ok()
            print("OK" if ok else "NO RESPONSE")
            return 0 if ok else 1

        elif args.command == "on":
            mw.set_on()
            print("output ON")

        elif args.command == "off":
            mw.set_off()
            print("output OFF")

        elif args.command == "status":
            mw.print_status()

        elif args.command == "read":
            names = args.registers or list(REGISTERS.keys())
            unknown = [n for n in names if n not in REGISTERS]
            if unknown:
                sys.exit(f"unknown registers: {', '.join(unknown)}")
            for name in names:
                mw.print_register(name)

        elif args.command == "write":
            mw.write_many(args.settings, cycle=not args.no_cycle)
            print("After write:")
            for name, _ in args.settings:
                mw.print_register(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
