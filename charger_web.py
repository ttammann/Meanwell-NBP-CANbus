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
import random
import threading
from functools import wraps

import can
from flask import Flask, jsonify, request, Response

from charger_app import (
    MeanWellCharger, REGISTERS, RANGES,
    FAULT_BITS, CHG_STATUS_BITS, SYSTEM_STATUS_BITS,
    _decode_bits, _decode_curve_config, _decode_system_config,
)

app = Flask(__name__)
_lock = threading.Lock()
charger: MeanWellCharger | None = None  # set in main()


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
            return jsonify({"ok": False, "error": f"CAN error: {e}"}), 503
    return wrapper


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


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
    out = {}
    with _lock:
        for name in names:
            if name not in REGISTERS:
                continue
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
    body = request.get_json(force=True) or {}
    raw_settings = body.get("settings", {})
    cycle = bool(body.get("cycle", True))
    settings = list(raw_settings.items())
    with _lock:
        charger.write_many(settings, cycle=cycle)
    return jsonify({"ok": True, "wrote": dict(settings)})


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


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NPB charger console</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,300;9..144,400;9..144,500;9..144,600&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {
    --paper:       #faf6ee;
    --paper-2:     #f4eee2;
    --paper-3:     #ede5d2;
    --card:        #ffffff;
    --ink:         #1a1816;
    --ink-2:       #4a463f;
    --ink-3:       #7a7368;
    --ink-4:       #b5ae9f;
    --line:        #e5dec9;
    --line-2:      #d6cdb3;
    --accent:      #2d6a4f;
    --accent-2:    #1b4332;
    --accent-soft: #d8e6df;
    --warn:        #b8861c;
    --warn-soft:   #f3e7c4;
    --danger:      #9b3232;
    --danger-soft: #f1d9d9;
    --info:        #335c81;
    --info-soft:   #d8e2ec;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: "Inter", system-ui, sans-serif;
    font-size: 14px;
    font-feature-settings: "ss01", "cv11";
    -webkit-font-smoothing: antialiased;
  }
  body {
    background-image:
      radial-gradient(ellipse 800px 600px at 100% 0%, rgba(45,106,79,0.04), transparent),
      radial-gradient(ellipse 600px 400px at 0% 100%, rgba(184,134,28,0.03), transparent);
    min-height: 100vh;
    padding: 32px 24px 60px;
  }
  .container { max-width: 1180px; margin: 0 auto; }

  /* ---------------- header ---------------- */
  header {
    display: grid;
    grid-template-columns: auto 1fr auto;
    align-items: center;
    gap: 32px;
    padding: 4px 0 28px;
    margin-bottom: 28px;
    border-bottom: 1px solid var(--line);
  }
  .brand {
    font-family: "Fraunces", Georgia, serif;
    font-weight: 400;
    font-size: 32px;
    letter-spacing: -0.02em;
    line-height: 1;
    font-variation-settings: "opsz" 144;
    color: var(--ink);
  }
  .brand em { font-style: italic; color: var(--accent); font-weight: 400; }
  .subtitle {
    font-size: 12px;
    color: var(--ink-3);
    letter-spacing: 0.04em;
    margin-top: 6px;
  }
  .header-actions {
    display: flex; align-items: center; gap: 14px;
  }
  .conn {
    display: inline-flex; align-items: center; gap: 8px;
    font-size: 12px; font-weight: 500;
    color: var(--ink-3);
    padding: 7px 14px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 100px;
  }
  .conn .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--ink-4);
    transition: all 0.2s;
  }
  .conn.ok  .dot { background: var(--accent); box-shadow: 0 0 0 3px rgba(45,106,79,0.15); }
  .conn.err .dot { background: var(--danger); box-shadow: 0 0 0 3px rgba(155,50,50,0.15); }
  .conn.ok  { color: var(--accent-2); }
  .conn.err { color: var(--danger); }

  /* ---------------- buttons ---------------- */
  button {
    font: 500 13px/1 "Inter", sans-serif;
    cursor: pointer; padding: 10px 18px;
    background: var(--card);
    color: var(--ink);
    border: 1px solid var(--line-2);
    border-radius: 6px;
    transition: all 0.12s;
  }
  button:hover:not(:disabled) {
    background: var(--paper-2);
    border-color: var(--ink-4);
  }
  button:active:not(:disabled) { transform: translateY(1px); }
  button.primary {
    background: var(--accent); color: #f7f9f6; border-color: var(--accent);
  }
  button.primary:hover:not(:disabled) {
    background: var(--accent-2); border-color: var(--accent-2);
  }
  button:disabled { opacity: 0.4; cursor: not-allowed; }

  /* master switch */
  .master {
    display: inline-flex; align-items: center; gap: 12px;
    padding: 7px 14px 7px 16px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 100px;
    font-size: 12px; font-weight: 500;
    color: var(--ink-2);
  }
  .master .lbl { user-select: none; }
  .switch { position: relative; display: inline-block; width: 38px; height: 22px; }
  .switch input { display: none; }
  .switch .track {
    position: absolute; inset: 0; cursor: pointer;
    background: var(--paper-3);
    border-radius: 11px;
    transition: all 0.2s;
  }
  .switch .track::after {
    content: ""; position: absolute; top: 3px; left: 3px;
    width: 16px; height: 16px; border-radius: 50%;
    background: white;
    box-shadow: 0 1px 3px rgba(0,0,0,0.15);
    transition: all 0.2s;
  }
  .switch input:checked + .track { background: var(--accent); }
  .switch input:checked + .track::after { transform: translateX(16px); }
  .switch input:disabled + .track { opacity: 0.5; cursor: not-allowed; }

  /* ---------------- panels ---------------- */
  .panel {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    padding: 22px 26px 24px;
    margin-bottom: 20px;
  }
  .panel h2 {
    font-family: "Fraunces", Georgia, serif;
    font-weight: 400;
    font-size: 18px;
    letter-spacing: -0.01em;
    color: var(--ink);
    margin: 0 0 18px;
    padding-bottom: 14px;
    border-bottom: 1px solid var(--line);
    display: flex; justify-content: space-between; align-items: baseline;
    gap: 12px;
  }
  .panel h2 em { color: var(--accent); font-style: italic; }
  .panel h2 .meta {
    font-family: "JetBrains Mono", monospace;
    font-size: 11px;
    color: var(--ink-4);
    font-weight: 400;
    font-style: normal;
    letter-spacing: 0;
  }

  /* ---------------- settings table (charge curve, focal point) -------- */
  table.settings {
    width: 100%; border-collapse: collapse;
    font-size: 13px;
  }
  table.settings tr { border-bottom: 1px solid var(--line); }
  table.settings tr:last-child { border-bottom: none; }
  table.settings td { padding: 12px 8px; vertical-align: middle; }
  table.settings td.desc {
    color: var(--ink-2);
  }
  table.settings td.desc .name {
    font-family: "JetBrains Mono", monospace; font-size: 11px;
    color: var(--ink-4); display: block; margin-top: 2px;
  }
  table.settings input {
    width: 120px; padding: 8px 12px;
    background: var(--paper);
    color: var(--ink);
    border: 1px solid var(--line-2); border-radius: 5px;
    font-family: "JetBrains Mono", monospace;
    font-variant-numeric: tabular-nums;
    text-align: right; font-size: 13px;
    transition: all 0.12s;
  }
  table.settings input:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(45,106,79,0.12);
    background: var(--card);
  }
  table.settings input.changed {
    border-color: var(--warn); color: #6f5012;
    background: var(--warn-soft);
  }
  table.settings input.invalid {
    border-color: var(--danger); color: var(--danger);
    background: var(--danger-soft);
  }
  table.settings input.skipped {
    border-color: var(--ink-4); color: var(--ink-3);
    background: var(--paper-2);
    border-style: dashed;
  }
  table.settings input.skipped::placeholder {
    color: var(--ink-3);
    font-style: italic;
  }
  table.settings .unit-cell {
    color: var(--ink-3); padding-left: 10px;
    font-size: 12px; font-weight: 500;
    width: 40px;
  }
  table.settings .range-cell {
    color: var(--ink-4);
    font-family: "JetBrains Mono", monospace; font-size: 11px;
    padding-left: 14px;
  }
  table.settings .raw-cell {
    color: var(--ink-4);
    font-family: "JetBrains Mono", monospace; font-size: 11px;
    text-align: right;
  }

  .actions {
    display: flex; align-items: center; gap: 12px;
    margin-top: 18px; padding-top: 18px;
    border-top: 1px solid var(--line);
  }
  .actions .spacer { flex: 1; }
  .actions .pending {
    font-size: 12px; font-weight: 500;
    color: #6f5012;
  }
  .actions .pending.invalid { color: var(--danger); }

  /* ---------------- charge config row (checkboxes + restart V) -------- */
  .config-row {
    display: flex; flex-wrap: wrap; align-items: center;
    gap: 24px;
    margin-top: 18px; padding: 18px 4px 4px;
    border-top: 1px dashed var(--line-2);
    transition: opacity 0.15s;
  }
  .config-row.unloaded {
    opacity: 0.4;
    pointer-events: none;
  }
  .config-row.unloaded::after {
    content: "Reload from charger to edit";
    font-family: "Fraunces", Georgia, serif;
    font-style: italic;
    font-size: 12px;
    color: var(--ink-3);
    pointer-events: auto;
  }
  .config-row .row-lbl {
    font-family: "Fraunces", Georgia, serif;
    font-style: italic;
    font-size: 14px;
    color: var(--ink-2);
    margin-right: 4px;
  }
  .check {
    display: inline-flex; align-items: center; gap: 9px;
    cursor: pointer; user-select: none;
    font-size: 13px; color: var(--ink-2);
  }
  .check input { position: absolute; opacity: 0; pointer-events: none; }
  .check .box {
    width: 18px; height: 18px; border-radius: 4px;
    border: 1.5px solid var(--line-2);
    background: var(--paper);
    display: inline-flex; align-items: center; justify-content: center;
    transition: all 0.12s;
    flex-shrink: 0;
  }
  .check .box::after {
    content: ""; width: 10px; height: 10px;
    background: transparent;
    clip-path: polygon(14% 50%, 0% 65%, 40% 100%, 100% 25%, 86% 12%, 38% 70%);
    transition: background 0.12s;
  }
  .check:hover .box { border-color: var(--ink-4); }
  .check input:checked + .box {
    background: var(--accent); border-color: var(--accent);
  }
  .check input:checked + .box::after { background: white; }
  .check input:focus-visible + .box {
    box-shadow: 0 0 0 3px rgba(45,106,79,0.18);
  }
  .check.changed .box { border-color: var(--warn); background: var(--warn-soft); }
  .check.changed input:checked + .box { background: var(--warn); border-color: var(--warn); }
  .check .hint {
    color: var(--ink-4); font-size: 11px;
    font-style: italic; font-family: "Fraunces", serif;
    margin-left: 2px;
  }
  .restart-group {
    display: inline-flex; align-items: center; gap: 9px;
    transition: opacity 0.15s;
  }
  .restart-group.disabled { opacity: 0.45; }
  .restart-group input.num {
    width: 78px; padding: 6px 10px;
    background: var(--paper);
    color: var(--ink);
    border: 1px solid var(--line-2); border-radius: 5px;
    font-family: "JetBrains Mono", monospace;
    font-variant-numeric: tabular-nums;
    text-align: right; font-size: 13px;
    transition: all 0.12s;
  }
  .restart-group input.num:focus {
    outline: none;
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(45,106,79,0.12);
    background: var(--card);
  }
  .restart-group input.num.changed {
    border-color: var(--warn); color: #6f5012; background: var(--warn-soft);
  }
  .restart-group input.num.invalid {
    border-color: var(--danger); color: var(--danger); background: var(--danger-soft);
  }
  .restart-group input.num:disabled {
    cursor: not-allowed; background: var(--paper-3);
  }
  .restart-group .unit {
    color: var(--ink-3); font-size: 12px; font-weight: 500;
  }

  /* ---------------- live strip (compact, secondary) ------------------- */
  .live-strip {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
    gap: 0;
  }
  .live-cell {
    padding: 4px 18px;
    border-right: 1px solid var(--line);
  }
  .live-cell:last-child { border-right: none; }
  .live-cell .lbl {
    font-size: 10px;
    color: var(--ink-3);
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .live-cell .val {
    font-family: "Fraunces", Georgia, serif;
    font-weight: 400;
    font-size: 22px;
    line-height: 1;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.01em;
    font-variation-settings: "opsz" 144;
  }
  .live-cell .val.dim {
    color: var(--ink-4);
    font-style: italic;
    font-size: 16px;
  }
  .live-cell .unit {
    font-family: "Inter", sans-serif;
    font-size: 12px;
    font-weight: 500;
    color: var(--ink-3);
    margin-left: 4px;
  }
  @media (max-width: 720px) {
    .live-strip { grid-template-columns: repeat(2, 1fr); }
    .live-cell { border-right: none; border-bottom: 1px solid var(--line); padding: 10px 14px; }
  }

  /* ---------------- status row (compact chips) ------------------------ */
  .status-row {
    display: flex; align-items: center; flex-wrap: wrap;
    gap: 14px;
    padding: 14px 22px;
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 10px;
    margin-bottom: 20px;
  }
  .status-row .stage-pill {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 6px 14px;
    border-radius: 100px;
    font-family: "Fraunces", Georgia, serif;
    font-style: italic; font-size: 15px;
    background: var(--paper-2); color: var(--ink-3);
    border: 1px solid var(--line);
  }
  .status-row .stage-pill::before {
    content: ""; width: 8px; height: 8px; border-radius: 50%;
    background: var(--ink-4);
  }
  .status-row .stage-pill.idle    { /* keep default */ }
  .status-row .stage-pill.cc      { background: var(--warn-soft); color: #6f5012; border-color: rgba(184,134,28,0.3); }
  .status-row .stage-pill.cc::before { background: var(--warn); }
  .status-row .stage-pill.cv      { background: var(--info-soft); color: var(--info); border-color: rgba(51,92,129,0.3); }
  .status-row .stage-pill.cv::before { background: var(--info); }
  .status-row .stage-pill.float,
  .status-row .stage-pill.full    { background: var(--accent-soft); color: var(--accent-2); border-color: rgba(45,106,79,0.3); }
  .status-row .stage-pill.float::before,
  .status-row .stage-pill.full::before { background: var(--accent); }
  .status-row .stage-pill.fault   { background: var(--danger-soft); color: var(--danger); border-color: rgba(155,50,50,0.3); }
  .status-row .stage-pill.fault::before { background: var(--danger); }
  .status-row .sep {
    width: 1px; height: 18px; background: var(--line);
  }
  .status-row .flags {
    display: flex; flex-wrap: wrap; gap: 6px;
  }
  .status-row .flag {
    padding: 3px 10px; border-radius: 100px;
    font-family: "JetBrains Mono", monospace;
    font-size: 11px;
    background: var(--danger-soft); color: var(--danger);
  }
  .status-row .flag.warn { background: var(--warn-soft); color: #6f5012; }
  .status-row .silent {
    font-family: "Fraunces", Georgia, serif;
    font-style: italic;
    color: var(--ink-4);
    font-size: 13px;
  }

  /* ---------------- device info ---------------- */
  .info-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 14px 28px;
  }
  .info-cell .lbl {
    font-size: 10px;
    color: var(--ink-3);
    letter-spacing: 0.05em;
    text-transform: uppercase;
    margin-bottom: 4px;
  }
  .info-cell .val {
    font-family: "JetBrains Mono", monospace;
    font-size: 14px;
    color: var(--ink);
  }
  .info-cell .val.empty {
    font-family: "Fraunces", Georgia, serif;
    font-style: italic;
    color: var(--ink-4);
    font-size: 13px;
  }
  .info-cell .val.serif {
    font-family: "Fraunces", Georgia, serif;
    font-style: normal;
    font-size: 16px;
    letter-spacing: -0.01em;
  }

  .config-summary {
    margin-top: 16px; padding-top: 14px;
    border-top: 1px dashed var(--line-2);
    font-size: 12px;
    color: var(--ink-3);
    line-height: 1.7;
  }
  .config-summary .key {
    color: var(--ink); font-weight: 500;
  }
  .config-summary code {
    color: var(--accent-2);
    background: var(--accent-soft);
    padding: 1px 7px; border-radius: 4px;
    font-family: "JetBrains Mono", monospace;
    font-size: 11px;
  }

  /* ---------------- log ---------------- */
  #log {
    font-family: "JetBrains Mono", monospace; font-size: 12px;
    background: var(--paper-2);
    border-radius: 8px;
    padding: 12px 16px;
    max-height: 200px; overflow-y: auto;
    border: 1px solid var(--line);
  }
  #log .row {
    padding: 3px 0; color: var(--ink-2);
    display: flex; gap: 14px;
  }
  #log .row.err { color: var(--danger); }
  #log .row.ok  { color: var(--accent-2); }
  #log .row .ts {
    color: var(--ink-4); flex-shrink: 0;
    font-variant-numeric: tabular-nums;
  }
  #log:empty::before {
    content: "no activity yet";
    color: var(--ink-4); font-style: italic;
    font-family: "Fraunces", Georgia, serif;
  }

  @keyframes flash {
    0%   { color: var(--accent); }
    100% { color: var(--ink); }
  }
  .live-cell .val.flash { animation: flash 0.7s ease-out; }
</style>
</head>
<body>
<div class="container">

  <header>
    <div>
      <div class="brand">npb <em>console</em></div>
      <div class="subtitle">Mean Well intelligent battery charger</div>
    </div>
    <div></div>
    <div class="header-actions">
      <span id="conn" class="conn"><span class="dot"></span><span id="conn-text">connecting</span></span>
      <label class="master">
        <span class="lbl">output</span>
        <span class="switch">
          <input type="checkbox" id="onoff" disabled>
          <span class="track"></span>
        </span>
      </label>
    </div>
  </header>

  <!-- charge curve: focal point -->
  <div class="panel">
    <h2>Charge <em>curve</em><span class="meta">B0–B9 / C2</span></h2>
    <table class="settings"><tbody id="settings"></tbody></table>
    <div class="config-row unloaded" id="config-row">
      <span class="row-lbl">Config</span>
      <label class="check" id="check-charger-wrap">
        <input type="checkbox" id="cfg-charger">
        <span class="box"></span>
        <span>Charger mode</span>
        <span class="hint">(off = constant V/I PSU)</span>
      </label>
      <label class="check" id="check-tcoff-wrap">
        <input type="checkbox" id="cfg-tempoff">
        <span class="box"></span>
        <span>Temp comp off</span>
        <span class="hint">(default −3 mV/°C/cell when unticked)</span>
      </label>
      <label class="check" id="check-float-wrap">
        <input type="checkbox" id="cfg-float">
        <span class="box"></span>
        <span>Enter float after CV</span>
        <span class="hint">(off = output cuts when CV tapers)</span>
      </label>
      <label class="check" id="check-restart-wrap">
        <input type="checkbox" id="cfg-restart">
        <span class="box"></span>
        <span>Restart when V drops below</span>
      </label>
      <span class="restart-group disabled" id="restart-group">
        <input class="num" type="number" id="cfg-restart-v" step="0.1" min="0" max="58.4" disabled>
        <span class="unit">V</span>
      </span>
    </div>
    <div class="actions">
      <button class="primary" id="reload">Reload from charger</button>
      <button id="apply" disabled>Apply changes</button>
      <span class="spacer"></span>
      <span class="pending" id="dirty-count"></span>
    </div>
  </div>

  <!-- compact status row: stage pill + any active warning/fault chips -->
  <div class="status-row" id="status-row">
    <span class="stage-pill idle" id="stage-pill">idle</span>
    <span class="sep"></span>
    <span class="silent" id="status-msg">no faults · all flags nominal</span>
    <span class="flags" id="flags-inline"></span>
  </div>

  <!-- compact live strip: only useful when actually charging -->
  <div class="panel">
    <h2>Live <em>readings</em><span class="meta" id="readings-ts">––:––:––</span></h2>
    <div class="live-strip">
      <div class="live-cell">
        <div class="lbl">V input</div>
        <div><span class="val" id="r-vin">–</span><span class="unit">V</span></div>
      </div>
      <div class="live-cell">
        <div class="lbl">Internal temp</div>
        <div><span class="val" id="r-temp">–</span><span class="unit">°C</span></div>
      </div>
      <div class="live-cell">
        <div class="lbl">V output</div>
        <div><span class="val" id="r-vout">–</span><span class="unit">V</span></div>
      </div>
      <div class="live-cell">
        <div class="lbl">I output</div>
        <div><span class="val" id="r-iout">–</span><span class="unit">A</span></div>
      </div>
      <div class="live-cell">
        <div class="lbl">Power</div>
        <div><span class="val" id="r-power">–</span><span class="unit">W</span></div>
      </div>
    </div>
  </div>

  <!-- device info: model, serial, firmware -->
  <div class="panel">
    <h2>Device <em>info</em></h2>
    <div class="info-grid">
      <div class="info-cell">
        <div class="lbl">Model</div>
        <div class="val serif" id="info-model">–</div>
      </div>
      <div class="info-cell">
        <div class="lbl">Manufacturer</div>
        <div class="val" id="info-mfr">–</div>
      </div>
      <div class="info-cell">
        <div class="lbl">Serial</div>
        <div class="val" id="info-serial">–</div>
      </div>
      <div class="info-cell">
        <div class="lbl">Firmware</div>
        <div class="val" id="info-fw">–</div>
      </div>
      <div class="info-cell">
        <div class="lbl">Manufacture date</div>
        <div class="val" id="info-date">–</div>
      </div>
      <div class="info-cell">
        <div class="lbl">Origin</div>
        <div class="val" id="info-loc">–</div>
      </div>
    </div>
    <div class="config-summary" id="config-summary">––</div>
  </div>

  <!-- activity log -->
  <div class="panel">
    <h2>Activity <em>log</em></h2>
    <div id="log"></div>
  </div>

</div>

<script>
// Settings shown in the editable table.  curve_config (0xB4) and chg_rst_vbat
// (0xB9) are NOT in this list — they're driven by the friendly checkbox/input
// row underneath the table.
const SETTING_KEYS = [
  'curve_cc', 'curve_cv', 'curve_fv', 'curve_tc',
  'curve_cc_timeout', 'curve_cv_timeout', 'curve_fv_timeout',
];
// Always read these two alongside the table values so we can populate
// the friendly row + preserve unmapped curve_config bits on write.
const FRIENDLY_KEYS = ['curve_config', 'chg_rst_vbat'];

// CURVE_CONFIG bit masks the friendly UI exposes.  Anything outside these
// masks is preserved verbatim on write.
const CFG_TCS_MASK = 0x000C;   // bits 2-3: temp comp slope (00=off, 01=-3mV)
const CFG_CVTSSE   = 0x0020;   // bit 5:   CV completion behavior (0=cut off, 1=float)
const CFG_CUVE     = 0x0080;   // bit 7:   charger mode
const CFG_RSTE     = 0x0800;   // bit 11:  restart-on-Vbat enable

let registers = {};
let currentValues = {};
let currentCurveConfig = null;     // raw 16-bit value last seen on the wire
let currentRestartV    = null;     // last seen chg_rst_vbat in volts

function log(msg, kind) {
  const row = document.createElement('div');
  row.className = 'row' + (kind ? ' ' + kind : '');
  const ts = new Date().toTimeString().slice(0,8);
  row.innerHTML = `<span class="ts">${ts}</span><span>${msg}</span>`;
  const lg = document.getElementById('log');
  lg.insertBefore(row, lg.firstChild);
  while (lg.children.length > 60) lg.removeChild(lg.lastChild);
}

function setConn(state, text) {
  const el = document.getElementById('conn');
  el.className = 'conn ' + state;
  document.getElementById('conn-text').textContent = text;
}

async function fetchJSON(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) {
    const err = await r.json().catch(() => ({error: r.statusText}));
    throw new Error(err.error || r.statusText);
  }
  return r.json();
}

function fmt(v, decimals) {
  if (v === null || v === undefined) return '–';
  return Number(v).toFixed(decimals ?? 2);
}

function flashUpdate(el, newText, dim) {
  if (el.textContent !== newText) {
    el.textContent = newText;
    el.classList.remove('flash');
    void el.offsetWidth;
    el.classList.add('flash');
  }
  el.classList.toggle('dim', !!dim);
}

function parseInputValue(text, reg) {
  const t = text.trim();
  // Bitfield-style registers (scale=1, no unit) accept hex (0x...) or decimal.
  // Plain integer registers (scale=1, with unit, e.g. timeouts in min) are decimal.
  if (reg.scale === 1) {
    if (!reg.unit && t.toLowerCase().startsWith('0x')) return parseInt(t, 16);
    return parseInt(t, 10);
  }
  return parseFloat(t);
}

function formatInputValue(value, reg) {
  if (value === null || value === undefined) return '';
  if (reg.scale === 1) {
    // Bitfield (no unit) -> hex, integer (with unit) -> plain decimal
    return reg.unit
      ? String(Math.round(Number(value)))
      : '0x' + Number(value).toString(16).toUpperCase().padStart(4, '0');
  }
  return Number(value).toFixed(reg.scale < 1 ? 2 : 0);
}

function buildSettingsTable() {
  const tbody = document.getElementById('settings');
  tbody.innerHTML = '';
  for (const name of SETTING_KEYS) {
    const reg = registers[name];
    if (!reg) continue;
    const row = document.createElement('tr');
    // Hex input only for "true" bitfields (scale=1 AND no unit).  Integer
    // registers like timeouts (scale=1, unit='min') get a normal number input.
    const isHex = reg.scale === 1 && !reg.unit;
    const inputType = isHex ? 'text' : 'number';
    const step = (reg.scale && reg.scale < 1) ? '0.01' : '1';
    const range = reg.range || [];
    const rangeText = range.length === 2 ? `${range[0]} – ${range[1]}` : '';
    const minMax = (range.length === 2 && !isHex) ? `min="${range[0]}" max="${range[1]}"` : '';
    row.innerHTML = `
      <td class="desc">${reg.desc}<span class="name">${name}</span></td>
      <td><input type="${inputType}" data-name="${name}" step="${step}" ${minMax}></td>
      <td class="unit-cell">${reg.unit || ''}</td>
      <td class="range-cell">${rangeText}</td>
      <td class="raw-cell" data-raw="${name}"></td>
    `;
    tbody.appendChild(row);
  }
  tbody.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('input', () => {
      const name = inp.dataset.name;
      const orig = currentValues[name];
      const reg = registers[name];
      // Always start clean
      inp.classList.remove('invalid', 'changed', 'skipped');
      // Empty value = skip this register on Apply.  This is intentionally
      // distinct from typing "0" (which is a real value the firmware
      // interprets specially for timeouts: 0 = disable).
      if (inp.value.trim() === '') {
        inp.classList.add('skipped');
        inp.title = 'Empty — this register will be skipped on Apply (current charger value preserved)';
        updateDirtyCount();
        return;
      }
      inp.title = '';
      let parsed;
      try { parsed = parseInputValue(inp.value, reg); } catch { parsed = NaN; }
      if (Number.isNaN(parsed)) {
        inp.classList.add('invalid');
      } else if (reg.range && (parsed < reg.range[0] || parsed > reg.range[1])) {
        inp.classList.add('invalid');
      } else if (orig === undefined || Math.abs(parsed - orig) > 1e-9) {
        inp.classList.add('changed');
      }
      updateDirtyCount();
    });
  });
}

function updateDirtyCount() {
  const tableDirty   = document.querySelectorAll('#settings input.changed').length;
  const tableBad     = document.querySelectorAll('#settings input.invalid').length;
  const tableSkipped = document.querySelectorAll('#settings input.skipped').length;
  const cfgDirty     = configRowDirtyCount();
  const cfgBad       = configRowInvalidCount();
  const dirty   = tableDirty + cfgDirty;
  const invalid = tableBad + cfgBad;
  const span  = document.getElementById('dirty-count');
  const apply = document.getElementById('apply');
  span.classList.toggle('invalid', invalid > 0);
  if (invalid) {
    span.textContent = `${invalid} invalid value${invalid===1?'':'s'}`;
    apply.disabled = true;
  } else if (dirty || tableSkipped) {
    const parts = [];
    if (dirty)        parts.push(`${dirty} change${dirty===1?'':'s'}`);
    if (tableSkipped) parts.push(`${tableSkipped} skipped`);
    span.textContent = parts.join(' · ') + ' pending';
    apply.disabled = !dirty;   // skipped-only = nothing to write, but allow visual review
  } else {
    span.textContent = '';
    apply.disabled = true;
  }
}

// ---------- friendly config row (CURVE_CONFIG checkboxes + restart V) ------

function decodeCurveConfig(raw) {
  // raw is the 16-bit register value last read from the charger
  return {
    chargerMode:  (raw & CFG_CUVE) !== 0,
    tempCompOff:  (raw & CFG_TCS_MASK) === 0,   // ticked = comp disabled
    floatAfterCV: (raw & CFG_CVTSSE) !== 0,
    restartEn:    (raw & CFG_RSTE) !== 0,
  };
}

function readConfigRow() {
  return {
    chargerMode:  document.getElementById('cfg-charger').checked,
    tempCompOff:  document.getElementById('cfg-tempoff').checked,
    floatAfterCV: document.getElementById('cfg-float').checked,
    restartEn:    document.getElementById('cfg-restart').checked,
    restartV:     parseFloat(document.getElementById('cfg-restart-v').value),
  };
}

function paintConfigRow(values) {
  // Apply UI state from a decoded config object (+ optional restartV)
  document.getElementById('cfg-charger').checked = values.chargerMode;
  document.getElementById('cfg-tempoff').checked = values.tempCompOff;
  document.getElementById('cfg-float').checked   = values.floatAfterCV;
  document.getElementById('cfg-restart').checked = values.restartEn;
  if (typeof values.restartV === 'number') {
    document.getElementById('cfg-restart-v').value = values.restartV.toFixed(1);
  }
  applyRestartGroupState();
  refreshConfigRowChangedClasses();
}

function applyRestartGroupState() {
  const enabled = document.getElementById('cfg-restart').checked;
  const grp = document.getElementById('restart-group');
  const inp = document.getElementById('cfg-restart-v');
  grp.classList.toggle('disabled', !enabled);
  inp.disabled = !enabled;
}

function refreshConfigRowChangedClasses() {
  if (currentCurveConfig === null) return;
  const orig = decodeCurveConfig(currentCurveConfig);
  const cur  = readConfigRow();
  document.getElementById('check-charger-wrap')
    .classList.toggle('changed', cur.chargerMode !== orig.chargerMode);
  document.getElementById('check-tcoff-wrap')
    .classList.toggle('changed', cur.tempCompOff !== orig.tempCompOff);
  document.getElementById('check-float-wrap')
    .classList.toggle('changed', cur.floatAfterCV !== orig.floatAfterCV);
  document.getElementById('check-restart-wrap')
    .classList.toggle('changed', cur.restartEn !== orig.restartEn);
  const vInp = document.getElementById('cfg-restart-v');
  vInp.classList.remove('invalid', 'changed');
  if (cur.restartEn) {
    if (Number.isNaN(cur.restartV) || cur.restartV < 0 || cur.restartV > 58.4) {
      vInp.classList.add('invalid');
    } else if (currentRestartV === null
               || Math.abs(cur.restartV - currentRestartV) > 1e-6) {
      vInp.classList.add('changed');
    }
  }
}

function configRowDirtyCount() {
  return document.querySelectorAll('#config-row .check.changed').length
       + document.querySelectorAll('#config-row input.num.changed').length;
}
function configRowInvalidCount() {
  return document.querySelectorAll('#config-row input.num.invalid').length;
}

function buildCurveConfigWrite() {
  // Compose a new curve_config 16-bit value from the friendly UI, preserving
  // any bits we don't expose (CUVS, timeout enables, etc.).
  if (currentCurveConfig === null) return null;
  const cur = readConfigRow();
  const masksWeOwn = CFG_TCS_MASK | CFG_CVTSSE | CFG_CUVE | CFG_RSTE;
  let v = currentCurveConfig & ~masksWeOwn;
  if (cur.chargerMode)  v |= CFG_CUVE;
  // tempCompOff ticked = bits 2-3 = 00; unticked = restore -3mV/C/cell (01)
  if (!cur.tempCompOff) v |= 0x0004;
  if (cur.floatAfterCV) v |= CFG_CVTSSE;
  if (cur.restartEn)    v |= CFG_RSTE;
  return v & 0xFFFF;
}

function gatherConfigRowWrites() {
  const out = {};
  if (currentCurveConfig === null) return out;
  const orig = decodeCurveConfig(currentCurveConfig);
  const cur  = readConfigRow();
  const cfgChanged =
    cur.chargerMode  !== orig.chargerMode ||
    cur.tempCompOff  !== orig.tempCompOff ||
    cur.floatAfterCV !== orig.floatAfterCV ||
    cur.restartEn    !== orig.restartEn;
  if (cfgChanged) out.curve_config = buildCurveConfigWrite();
  if (cur.restartEn
      && !Number.isNaN(cur.restartV)
      && (currentRestartV === null
          || Math.abs(cur.restartV - currentRestartV) > 1e-6)) {
    out.chg_rst_vbat = cur.restartV;
  }
  return out;
}

// Wire up the five config-row controls
['cfg-charger', 'cfg-tempoff', 'cfg-float', 'cfg-restart'].forEach(id => {
  document.getElementById(id).addEventListener('change', () => {
    if (id === 'cfg-restart') applyRestartGroupState();
    refreshConfigRowChangedClasses();
    updateDirtyCount();
  });
});
document.getElementById('cfg-restart-v').addEventListener('input', () => {
  refreshConfigRowChangedClasses();
  updateDirtyCount();
});

let userToggling = 0;

async function refreshReadings() {
  try {
    const data = await fetchJSON('/api/read?names=read_vin,read_vout,read_iout,read_temp,operation');
    flashUpdate(document.getElementById('r-vin'),  fmt(data.read_vin?.value, 1));
    flashUpdate(document.getElementById('r-temp'), fmt(data.read_temp?.value, 1));
    // V_out / I_out / power: only meaningful when actually flowing.
    // Mark dim and show "—" when output is off OR values are null/zero.
    const opOn = data.operation?.value === 1;
    const vout = data.read_vout?.value;
    const iout = data.read_iout?.value;
    const flowing = opOn && vout != null && iout != null && (iout > 0.05 || vout > 1);
    if (flowing) {
      flashUpdate(document.getElementById('r-vout'),  fmt(vout), false);
      flashUpdate(document.getElementById('r-iout'),  fmt(iout), false);
      flashUpdate(document.getElementById('r-power'), fmt(vout * iout, 1), false);
    } else {
      flashUpdate(document.getElementById('r-vout'),  'standby', true);
      flashUpdate(document.getElementById('r-iout'),  'standby', true);
      flashUpdate(document.getElementById('r-power'), 'standby', true);
    }
    document.getElementById('readings-ts').textContent = new Date().toTimeString().slice(0,8);
    if (data.operation?.value !== null && data.operation?.value !== undefined
        && Date.now() - userToggling > 2500) {
      const sw = document.getElementById('onoff');
      sw.disabled = false;
      sw.checked = data.operation.value === 1;
    }
    setConn('ok', 'connected');
  } catch (e) {
    setConn('err', 'no response');
  }
}

async function refreshStatus() {
  try {
    const s = await fetchJSON('/api/status');
    const cf = s.chg_status.flags || [];
    const fl = s.fault_status.flags || [];

    // stage pill
    let label = 'idle', cls = 'idle';
    if (fl.length) { label = 'fault'; cls = 'fault'; }
    else if (cf.some(f => f.startsWith('FULLM'))) { label = 'fully charged'; cls = 'full'; }
    else if (cf.some(f => f.startsWith('FVM')))   { label = 'float';            cls = 'float'; }
    else if (cf.some(f => f.startsWith('CVM')))   { label = 'constant voltage'; cls = 'cv'; }
    else if (cf.some(f => f.startsWith('CCM')))   { label = 'constant current'; cls = 'cc'; }
    const pill = document.getElementById('stage-pill');
    pill.textContent = label;
    pill.className = 'stage-pill ' + cls;

    // inline flag chips
    const otherFlags = cf.filter(f => !f.match(/^(CCM|CVM|FVM|FULLM)/));
    const inline = document.getElementById('flags-inline');
    const msg    = document.getElementById('status-msg');
    if (fl.length || otherFlags.length) {
      msg.style.display = 'none';
      inline.innerHTML =
        fl.map(f => `<span class="flag">${f}</span>`).join('') +
        otherFlags.map(f => `<span class="flag warn">${f}</span>`).join('');
    } else {
      msg.style.display = '';
      inline.innerHTML = '';
    }

    // config summary lives under device info
    const cs = document.getElementById('config-summary');
    let html = '';
    if (s.curve_config.raw !== null) {
      html += `<div><span class="key">curve_config</span> <code>0x${s.curve_config.raw.toString(16).toUpperCase().padStart(4,'0')}</code> · ${s.curve_config.decoded}</div>`;
    }
    if (s.system_config.raw !== null) {
      html += `<div style="margin-top:6px"><span class="key">system_config</span> · ${s.system_config.decoded}</div>`;
    }
    cs.innerHTML = html || '––';
  } catch (e) { /* silent */ }
}

async function refreshDeviceInfo() {
  try {
    const d = await fetchJSON('/api/device_info');
    const setVal = (id, v, fallback='unavailable') => {
      const el = document.getElementById(id);
      if (v) {
        el.textContent = v;
        el.classList.remove('empty');
      } else {
        el.textContent = fallback;
        el.classList.add('empty');
      }
    };
    setVal('info-model',  d.model);
    setVal('info-mfr',    d.manufacturer);
    setVal('info-serial', d.serial);
    setVal('info-fw',     d.firmware);
    setVal('info-loc',    d.location);
    // pretty-print the date if it's a YYMMDD string
    let dateText = d.made;
    if (dateText && /^\d{6}$/.test(dateText)) {
      dateText = `20${dateText.slice(0,2)}-${dateText.slice(2,4)}-${dateText.slice(4,6)}`;
    }
    setVal('info-date', dateText);
  } catch (e) { /* silent — device info isn't critical to UI */ }
}

async function loadRegisters() {
  // Just fetch metadata and render empty rows.  The user explicitly clicks
  // "Reload from charger" to populate values.
  registers = await fetchJSON('/api/registers');
  buildSettingsTable();
}

async function reloadFromCharger() {
  try {
    const allKeys = SETTING_KEYS.concat(FRIENDLY_KEYS);
    const data = await fetchJSON('/api/read?names=' + allKeys.join(','));
    // table rows
    for (const name of SETTING_KEYS) {
      const v = data[name];
      if (!v || v.value === null) continue;
      currentValues[name] = v.value;
      const inp = document.querySelector(`#settings input[data-name="${name}"]`);
      if (inp) {
        const formatted = formatInputValue(v.value, registers[name]);
        inp.value = formatted;
        // Placeholder shows the current value so if the user clears the
        // field (= "skip on Apply"), they can see what's being preserved.
        inp.placeholder = `(skip — ${formatted})`;
        inp.classList.remove('changed', 'invalid', 'skipped');
        inp.title = '';
      }
      const raw = document.querySelector(`#settings [data-raw="${name}"]`);
      if (raw) raw.textContent = `raw=${v.raw}`;
    }
    // friendly config row
    if (data.curve_config && data.curve_config.raw !== null) {
      currentCurveConfig = data.curve_config.raw;
    }
    if (data.chg_rst_vbat && data.chg_rst_vbat.value !== null) {
      currentRestartV = data.chg_rst_vbat.value;
    }
    if (currentCurveConfig !== null) {
      const decoded = decodeCurveConfig(currentCurveConfig);
      paintConfigRow({...decoded, restartV: currentRestartV ?? 48.0});
      document.getElementById('config-row').classList.remove('unloaded');
    }
    updateDirtyCount();
    log('reloaded settings from charger', 'ok');
  } catch (e) {
    log('reload failed: ' + e.message, 'err');
  }
}

async function applyChanges() {
  const dirtyTable = document.querySelectorAll('#settings input.changed');
  const cfgWrites  = gatherConfigRowWrites();
  if (!dirtyTable.length && !Object.keys(cfgWrites).length) return;
  const settings = {};
  for (const inp of dirtyTable) {
    const name = inp.dataset.name;
    settings[name] = parseInputValue(inp.value, registers[name]);
  }
  Object.assign(settings, cfgWrites);
  const apply = document.getElementById('apply');
  apply.disabled = true;
  try {
    log('writing: ' + Object.entries(settings).map(([k,v]) => {
      const reg = registers[k];
      // Hex log line only for bitfield registers (scale=1, no unit).
      return reg && reg.scale === 1 && !reg.unit
        ? `${k}=0x${v.toString(16).toUpperCase().padStart(4,'0')}`
        : `${k}=${v}`;
    }).join(', '));
    await fetchJSON('/api/write', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ settings })
    });
    log('write OK', 'ok');
    await reloadFromCharger();
  } catch (e) {
    log('write failed: ' + e.message, 'err');
    apply.disabled = false;
  }
}

document.getElementById('apply').addEventListener('click', applyChanges);
document.getElementById('reload').addEventListener('click', reloadFromCharger);

document.getElementById('onoff').addEventListener('change', async (e) => {
  const target = e.target.checked;
  userToggling = Date.now();
  e.target.disabled = true;
  try {
    await fetchJSON(target ? '/api/on' : '/api/off', { method: 'POST' });
    log('output ' + (target ? 'ON' : 'OFF'), 'ok');
  } catch (err) {
    log('toggle failed: ' + err.message, 'err');
    e.target.checked = !target;
  } finally {
    e.target.disabled = false;
  }
});

loadRegisters().catch(e => log('register load failed: ' + e.message, 'err'));
refreshDeviceInfo();
refreshReadings();
refreshStatus();
setInterval(refreshReadings, 2000);
setInterval(refreshStatus, 5000);
// Device info doesn't really change; refresh occasionally in case we
// reconnect to a different unit.
setInterval(refreshDeviceInfo, 60000);
</script>
</body>
</html>
"""


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
    args = ap.parse_args()

    global charger
    if args.demo:
        print("--- DEMO MODE: simulated charger, no real CAN bus ---")
        charger = MeanWellCharger(can_id=args.can_id, bus=FakeBus(), recv_timeout=0.5)
    else:
        charger = MeanWellCharger(channel=args.channel,
                                  bitrate=args.bitrate,
                                  can_id=args.can_id,
                                  interface=args.interface,
                                  recv_timeout=0.5)
        print(f"CAN: {args.interface} ch={args.channel} @ {args.bitrate} id=0x{args.can_id:X}")

    print(f"Starting on http://{args.host}:{args.port}")
    try:
        app.run(host=args.host, port=args.port, threaded=True, debug=False)
    finally:
        charger.shutdown()


if __name__ == "__main__":
    main()
