# Mean Well NPB charger CAN controller

CLI and web UI for Mean Well NPB-450 / 750 / 1200 / 1700 chargers over CAN
bus, implementing the protocol from section 6 of the manual.  Designed
around a 16-cell LiFePO4 (LFP) battery on an NPB-1700-48, but everything
range-related lives in one dictionary at the top of `charger_app.py` so
adapting to a different chemistry or model is a one-file edit.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Then pick your platform below. **SocketCAN (`can0`) is Linux-only.** On
macOS and Windows you either run **`--demo`** (simulated charger) or talk
to a USB-CAN adapter with the matching python-can backend (`slcan`,
`gs_usb`, `pcan`, …).

## macOS

| Goal | How |
|------|-----|
| **UI / dev, no hardware** | Docker **or** native `--demo` (see below) |
| **Real charger over USB-CAN** | **Native Python on the Mac** — not Docker |

### Docker (demo only)

Docker Desktop on macOS **cannot** pass through USB serial devices
(`/dev/cu.usbmodem…`). Use the demo profile:

```bash
docker compose --profile demo up --build
# → http://localhost:8080
```

### Native — simulated charger

```bash
python3 charger_web.py --demo
# → http://localhost:8080
```

### Native — real USB-CAN adapter

macOS names serial ports **`/dev/cu.usbmodem…`** (use the `cu.*` device,
not `tty.*`). There is no `/dev/ttyACM0` — that path is Linux.

Find your device after plugging in the adapter:

```bash
ls /dev/cu.usb*
```

Probe the bus, then start the web UI (default bitrate **250000** for NPB):

```bash
python3 charger_app.py \
  --interface slcan \
  --channel /dev/cu.usbmodem207838884D4D1 \
  check

python3 charger_web.py \
  --interface slcan \
  --channel /dev/cu.usbmodem207838884D4D1 \
  --bitrate 250000
# → http://localhost:8080  →  click "Reload from charger"
```

Do **not** run `slcand` on macOS for this app — python-can opens the
serial port directly. If `slcan` fails, your stick may use another
backend (e.g. `--interface gs_usb` for candleLight firmware).

## Windows

The web UI and CLI run on Windows. **SocketCAN (`socketcan`) is Linux-only**, so on
Windows you have three practical options:

1. **`--demo`** — full UI with a simulated charger (no hardware):

   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python charger_web.py --demo
   ```

2. **USB-CAN adapter** — install the vendor driver, then use the matching
   python-can backend, for example:

   ```powershell
   python charger_web.py --interface slcan --channel COM3
   python charger_web.py --interface pcan --channel PCAN_USBBUS1
   ```

   Use `python -m can.interfaces` or the python-can docs to list backends
   available on your machine.

3. **WSL2 + Linux** — run the app inside WSL with SocketCAN or USB passthrough
   if you need the same `can0` workflow as a Pi.

Docker Desktop on Windows can run the **`demo`** profile only; real CAN
profiles need a Linux host (see [Docker](#docker)).

## Linux

Default backend is **SocketCAN** (`can0`). Bring the interface up, then
run the CLI or web UI:

```bash
# Native CAN (Pi MCP2515, Jetson, PEAK kernel driver, …)
sudo ip link set can0 up type can bitrate 250000

# Or USB-CAN via slcand (Canable, CANtact, …) — creates can0 on Linux
sudo slcand -o -c -s5 /dev/ttyACM0 can0   # -s5 = 250 kbit/s
sudo ip link set can0 up

python3 charger_app.py read
python3 charger_web.py
# → http://localhost:8080
```

## Docker

Three compose profiles; **only `demo` works on macOS / Windows**.

| Profile | Host | Needs |
|---------|------|--------|
| **`demo`** | macOS, Windows, Linux | Nothing — simulated charger |
| **`charger`** | Linux only | `can0` up on the host (see Linux above) |
| **`usb-can`** | Linux only | USB tty passthrough (e.g. `/dev/ttyACM0`) |

```bash
# Demo — use this on macOS (and anywhere without CAN hardware)
docker compose --profile demo up --build
# → http://localhost:8080

# Linux: SocketCAN (can0 must already exist on the host)
docker compose --profile charger up -d --build
# env: NPB_CHANNEL=can0  NPB_BITRATE=250000  NPB_CAN_ID=0xC0103

# Linux: USB-CAN without slcand (python-can slcan backend inside container)
NPB_USB_DEVICE=/dev/ttyACM0 NPB_USB_INTERFACE=slcan \
  docker compose --profile usb-can up -d --build
```

Profile **`charger`** uses host networking and talks to **`can0`**. Profile
**`usb-can`** maps a host **`/dev/tty*`** into the container — do not run
`slcand` on that same device at the same time. Both profiles require
**Linux**; Docker Desktop on Mac/Windows does not expose USB serial devices
reliably enough for **`usb-can`**.

The image is non-root, ~80 MB. See `docker-compose.yml` for env vars.

## Command-line interface

```bash
python3 charger_app.py --help        # full help with examples
python3 charger_app.py list          # list every register, no CAN traffic
python3 charger_app.py check         # probe the bus (exit 0 = OK, 1 = no response)
python3 charger_app.py read          # read every register
python3 charger_app.py read curve_cc curve_cv curve_fv
python3 charger_app.py status        # decoded fault / charge / config bitfields
python3 charger_app.py on            # remote ON
python3 charger_app.py off           # remote OFF
```

`write` takes any number of `KEY=VALUE` pairs and wraps the whole batch in
a single OFF/ON cycle (the manual requires remote-OFF for B0–B9 to commit
to EEPROM):

```bash
python3 charger_app.py write curve_cc=15 curve_cv=55.2 curve_fv=55.2 curve_tc=5
python3 charger_app.py write curve_config=0x0884 chg_rst_vbat=48
python3 charger_app.py write curve_fv_timeout=60   # optional: 1-hour float window
```

Bitfield registers (`curve_config`, `system_config`) accept either decimal
or `0x...` hex.  Use `--no-cycle` to skip the OFF/ON wrapping.

### CAN options (CLI and web)

```
--channel can0          SocketCAN name (Linux) or device path (USB backends)
--bitrate 250000        CAN bitrate (default: 250000)
--can-id 0xC0103        29-bit CAN arbitration ID (default: 0xC0103)
--interface socketcan   python-can backend (default). USB adapters: slcan,
                        gs_usb, pcan, kvaser, … — see platform sections above.
```

## Web UI

```bash
python3 charger_web.py [--demo] [--host 0.0.0.0] [--port 8080] [CAN options]
```

Then open `http://<host>:8080`.

The UI is deliberately configuration-only — the charger is not assumed
to be connected to a battery while you're talking to it, so there's no
live-telemetry panel.  Four sections, top to bottom:

1. **Preview charge curve** — live SVG mirror of the manual's 3-stage
   diagram (page 44).  Stages labelled *bulk / absorption / float*;
   voltage and current axes with rotated titles; charcoal + slate
   curves.  On **real hardware**, the plot stays empty until **Reload from
   charger**. In **`--demo`**, a suggested 16S LFP preview may appear after
   5 s if auto-reload has not finished yet. Annotation pills and a traveller
   dot follow once CC, CV, FV, and TC are available.
2. **Settings charge curve** — editable table + friendly `curve_config`
   checkboxes + optional raw-hex editor for power users.  **Apply**
   opens a confirmation modal listing every change and whether the
   output will be power-cycled (only if it was ON).  **Discard** /
   **Esc** revert pending edits.  Empty fields are skipped on Apply
   (see the `?` hint).  Keyboard: **⌘/Ctrl+S** apply, **⇧⌘R** reload, **Esc** discard. **Export** /
**Import** JSON backups of curve settings (export after Reload; import can
load a file as draft before the first Reload).

Exported file shape:

```json
{
  "version": 1,
  "exported_at": "2026-05-21T12:00:00.000Z",
  "source": "npb-console",
  "settings": {
    "curve_cc": 15.0,
    "curve_cv": 55.2,
    "curve_config": 2180,
    "chg_rst_vbat": 48.0
  }
}
```
3. **Device info** — model, serial, firmware, etc. from `MFR_*` registers.
4. **Activity log** — persisted in `localStorage` across refreshes.

The header shows **connected** (with CAN latency), **status pills** from
`/api/status` (CCM, CVM, OTP, DC_OK, … — hover for the full label), and
the **output** switch.

**Connection status** uses **Server-Sent Events** (`/api/stream`): one
shared server-side CAN poller fans out to every browser tab.  Each tick
reads operation plus fault/charge/system status for the header pills.
The chip shows `connected · <latency ms>` and only flips to disconnected
after **two** consecutive failed reads.  On a **real CAN** link (charger /
USB-CAN), settings load only when you click **Reload from charger** (or
after Apply re-reads the unit) — no invented defaults.  In **`--demo`**
mode the UI auto-reloads once from the simulated bus on connect, with a
**5 s** suggested-preview fallback if that read has not completed yet.
`/api/device_info` caches identity strings (~55 s) but refreshes live
VIN/temp on every request.  Post-write verification reads are paced outside the
global CAN lock (same as batch `/api/read`).  The server auto-reconnects
the python-can bus after repeated `CanError`s
(USB unplug / `slcand` restart) without restarting the container.

**Safety on Apply:** `write_many()` preserves the pre-write output
state — if the output was OFF, Apply leaves it OFF instead of silently
turning it back on.  The API re-reads every written register and
reports `post` values so the UI can flag firmware clamping.

Styling is a two-tone grey palette (white cards on `#f6f6f7` background,
single slate accent).  Templates live in `templates/`, assets in
`static/` — no JS framework.

**Single-system focus.**  This UI assumes a 48 V / 55.2 V 16S LFP
system on an NPB-1700-48.  The `RANGES` dict in `charger_app.py`
enforces the absolute voltage limits (42.0 – 58.4 V) and the curve
preview's Y-axes are fixed to that window.  Editing those would
require coordinated changes in both files.

**The page starts blank on real hardware.**  Click **Reload from charger**
to read the unit; **Apply** is enabled only after a successful Reload.
A failed Reload (any register timeout) does not enable Apply or fill
substitute values.  If the CAN link drops, the form shows a stale warning
and Apply/Export/Import are disabled until you Reload successfully.  **`--demo`** auto-reloads from `FakeBus` on first
connect; if that read fails, a **5 s** suggested 16S LFP preview may appear
(preview only — config row stays locked until Reload succeeds).

**Clearing a field skips that register.**  If you reload values and
then empty an input box, the row turns dashed-grey ("X skipped pending")
and the Apply batch will not include that register — whatever the
charger currently has is left untouched.  This is intentionally distinct
from typing `0`, which is a real value the firmware interprets specially
(e.g. `0` for a timeout means "disabled" if the firmware accepts it;
for a timeout register with a `60` minimum, typing `0` is rejected as
out-of-range).

### Static preview (`preview.html`)

The mock UI uses `preview-mock.js` and does not talk to CAN even when
served by Flask.

**If `charger_web.py` is already running** (e.g. on port 8088), open the
preview on that same port — do not start another server on 8088:

```text
http://127.0.0.1:8088/preview.html
```

(`python3 -m http.server 8088` while Flask is on 8088 will not work: the
port is taken and `/preview.html` is not a file listing.)

**If nothing is listening**, from the repo root:

```bash
python3 serve_preview.py
# → http://127.0.0.1:8090/preview.html  (root / redirects here)
```

Or any free port: `python3 -m http.server 8090` then open
`/preview.html` (must run the command in the project root so `static/`
resolves).

`preview.html` loads `static/css/main.css` and `static/js/main.js` plus
`preview-data.js` (register metadata from `charger_app`) and
`preview-mock.js` (in-memory `/api/*` + SSE).  Writes update the mock
state only; no CAN traffic.  Regenerate `static/js/preview-data.js` after changing `REGISTERS` /
`RANGES` (same values as `FakeBus` demo reads):

```bash
.venv/bin/python <<'PY'
import json
from pathlib import Path
from charger_app import REGISTERS, RANGES
registers = {n: {"code": r.code, "scale": r.scale, "size": r.size,
    "unit": r.unit, "writable": r.writable, "desc": r.desc,
    "range": list(RANGES[n]) if n in RANGES else None}
    for n, r in REGISTERS.items()}
reads = {"operation": {"raw": 1, "value": 1}, "curve_cc": {"raw": 1500, "value": 15.0},
    "curve_cv": {"raw": 5520, "value": 55.2}, "curve_fv": {"raw": 5520, "value": 55.2},
    "curve_tc": {"raw": 500, "value": 5.0}, "curve_cc_timeout": {"raw": 900, "value": 900},
    "curve_cv_timeout": {"raw": 60, "value": 60}, "curve_fv_timeout": {"raw": 60, "value": 60},
    "curve_config": {"raw": 2180, "value": 2180}, "chg_rst_vbat": {"raw": 4800, "value": 48.0}}
Path("static/js/preview-data.js").write_text(
    "window.PREVIEW_REGISTERS = " + json.dumps(registers, indent=2) + ";\n"
    + "window.PREVIEW_READS = " + json.dumps(reads, indent=2) + ";\n")
PY
```

The **`--demo`** / **`FakeBus`** path (native or Docker) simulates a
charger in-process for UI development — see [macOS](#macos) and
[Docker](#docker).

## Files

| file                      | purpose                                                  |
|---------------------------|----------------------------------------------------------|
| `charger_app.py`          | charger client + command-line interface                  |
| `charger_web.py`          | Flask web UI (imports `charger_app`)                     |
| `templates/index.html`    | single-page HTML for the web UI                          |
| `preview.html`            | static UI preview (mock API; see below)                  |
| `serve_preview.py`        | local static server on port 8090 when Flask is not up    |
| `static/css/main.css`     | UI styles                                                |
| `static/js/main.js`       | UI behaviour: curve preview, form, SSE stream            |
| `static/js/preview-mock.js` | fetch/EventSource shim for `preview.html`              |
| `static/js/preview-data.js` | demo register metadata + reads (generated from app)    |
| `tests/test_decoders.py`  | pytest: bitfield decoders                                |
| `tests/test_runtime.py`   | pytest: FakeBus, write_many, SSE broadcaster             |
| `requirements.txt`        | runtime Python dependencies                              |
| `requirements-dev.txt`    | adds `pytest` on top of `requirements.txt`               |
| `Dockerfile` + `docker-compose.yml` | containerised deployment (demo / real CAN / USB) |
| `README.md`               | this file                                                |

## Tests

Bitfield decoders, `FakeBus` round-trips, `write_many` cycle preservation,
range validation, and the SSE `StateBroadcaster` all have unit tests:

```bash
pip install -r requirements-dev.txt
pytest tests/ -v
```

83 tests, runs in <2 s.  No CAN hardware required.

## HTTP API (all JSON)

| route                  | method | purpose                                       |
|------------------------|--------|-----------------------------------------------|
| `/api/health`          | GET    | `{ok, demo, connected}` for scripts / healthchecks |
| `/api/registers`       | GET    | static metadata for every register            |
| `/api/read?names=…`    | GET    | read named registers (paced batch, one lock per read) |
| `/api/status`          | GET    | decoded fault / charge / config bitfields     |
| `/api/device_info`     | GET    | model, serial, firmware, etc.                 |
| `/api/write`           | POST   | `{settings: {curve_cc: 15, …}, cycle: true}`  |
| `/api/on` / `/api/off` | POST   | direct master ON/OFF                          |

All CAN access is serialized through a single `threading.Lock`, so
multiple browser sessions plus polling can run concurrently without
colliding on the bus.

## Suggested 16S LFP values

If you're charging a 16-cell LiFePO4 pack and want a starting point,
these values work well:

| register             | value         | purpose                                              |
|----------------------|---------------|------------------------------------------------------|
| `curve_cc`           | 15.0 A        | bulk-charge current                                  |
| `curve_cv`           | 55.2 V        | constant-voltage target (3.45 V/cell)                |
| `curve_fv`           | 55.2 V        | float voltage equal to CV (LFP needs no Vfloat drop) |
| `curve_tc`           | 5.0 A         | taper current — transition CV → FV                   |
| `curve_config`       | `0x0884`      | charger mode + temp comp + restart-on-Vbat enable    |
| `chg_rst_vbat`       | 48.0 V        | restart from CC when pack drops below 48 V           |

You can write all of these in one shot from the CLI:

```bash
python3 charger_app.py write curve_cc=15 curve_cv=55.2 curve_fv=55.2 \
                             curve_tc=5 curve_config=0x0884 chg_rst_vbat=48
```

Or set each via the web UI's editable table after clicking "Reload from
charger".

The stage timeouts (CC / CV / FV) are deliberately not on this list.
The CV stage exits naturally on taper (current dropping to ~10 % of
rated), which transitions to float automatically — no need to involve
the `CVTSSE` / `CVTOE` timeout machinery, which the manual documents
inconsistently.  All you need is `RSTE` set in `curve_config` so the
restart voltage in B9 is honoured.

## Registers

`charger_app.py list` prints the full table; the most useful ones:

| name                       | code      | rw | notes                                 |
|----------------------------|-----------|----|---------------------------------------|
| `operation`                | 0x00      | rw | 0 = OFF, 1 = ON (master switch)       |
| `vout_set` / `iout_set`    | 0x20/0x30 | rw | PSU-mode V/I setpoint                 |
| `fault_status`             | 0x40      | ro | OTP / OVP / OLP / SHORT / AC_FAIL …   |
| `read_vin`                 | 0x50      | ro | AC input voltage (always available)   |
| `read_vout` / `read_iout`  | 0x60/0x61 | ro | live output (zero in standby)         |
| `read_temp`                | 0x62      | ro | internal temperature (always)         |
| `curve_cc`                 | 0xB0      | rw | bulk-charge current                   |
| `curve_cv`                 | 0xB1      | rw | boost / CV voltage                    |
| `curve_fv`                 | 0xB2      | rw | float voltage                         |
| `curve_tc`                 | 0xB3      | rw | taper current (CV → FV transition)    |
| `curve_config`             | 0xB4      | rw | bitfield: curve, temp-comp, mode, restart |
| `curve_cc/cv/fv_timeout`   | 0xB5–B7   | rw | minutes per stage (range: 60–64800)   |
| `chg_status`               | 0xB8      | ro | FULLM / CCM / CVM / FVM / timeouts    |
| `chg_rst_vbat`             | 0xB9      | rw | restart-charge battery voltage        |
| `system_status`            | 0xC1      | ro | DC_OK / INITIAL_STATE / EEPER         |
| `system_config`            | 0xC2      | rw | power-on state, EEPROM-write disable  |

Manufacturer ASCII strings (model, serial, firmware, etc.) are at
0x80–0x88; they're exposed via `MeanWellCharger.device_info()` and the
`/api/device_info` endpoint rather than the generic register table since
they return text rather than 16-bit ints.

## CURVE_CONFIG (0xB4) bits

The web UI exposes the three most useful bits as friendly checkboxes; for
direct writes the full bit map is:

CURVE_CONFIG is a 16-bit register.  The manual splits it into low and
high bytes (each row below shows the 16-bit word position):

**Low byte** (bits 0–7):

| word bit | name   | meaning                                                  |
|----------|--------|----------------------------------------------------------|
| 0–1      | CUVS   | 00 = customised, 01–11 = preset curve 1–3 (DIP-driven)   |
| 2–3      | TCS    | 00 off, 01 −3 mV/°C/cell (default), 10 −4, 11 −5         |
| 5        | CVTSSE | 0 = on CV timeout cut output off, 1 = enter float        |
| 7        | CUVE   | 0 = PSU mode, 1 = charger mode (default)                 |

**High byte** (bits 8–15, but the manual numbers them 0–7 within the high
byte; word-bit numbers shown here for clarity):

| word bit | name   | meaning                                                  |
|----------|--------|----------------------------------------------------------|
| 8        | CVTOE  | enable CV-stage timeout indication                       |
| 9        | CCTOE  | enable CC-stage timeout indication                       |
| 10       | FVTOE  | enable FV-stage timeout indication                       |
| 11       | RSTE   | enable restart-on-Vbat — required before B9 takes effect |

Factory default is `0x0084`.  Setting `0x0884` (default + RSTE) is the
typical configuration for charging with auto-restart enabled.  The web
UI exposes the most useful bits as checkboxes — charger mode, temp comp
off, enter float after CV (CVTSSE), restart-on-Vbat (RSTE).

A note on CV-timeout behavior — the manual is genuinely inconsistent
about whether the `*TOE` "timeout indication enable" bits gate the
timeout itself or merely the status flag in `CHG_STATUS`.  The safe
default is to leave the CC and CV timeouts unset (or at the firmware
default) — the CV stage then exits on taper and transitions to float
automatically without involving the timeout path.  If you do set a CV
timeout explicitly (writing `curve_cv_timeout`) and then see the charger
cutting off rather than entering float, set the **CVTSSE** bit
(or tick "Enter float after CV" in the UI) — that is exactly the
scenario it's designed for.

## Limits worth knowing

- **Voltages**: validation is set for a 16S LFP pack — 58.4 V max
  (16 × 3.65 V).  Edit `RANGES` in `charger_app.py` for a different cell
  count or chemistry.
- **Timeouts**: per the manual's CANBus value-range table (page 35), the
  firmware accepts 60 – 64 800 minutes.  Writes outside this window are
  rejected at the API layer.  (The NFC-app section on page 37 says 1–6000
  minutes; that's a documentation contradiction — the CANBus table is
  authoritative for CAN-driven writes.)
- **EEPROM commit timing**: B0–B9 only take effect when the output is OFF
  (master switch off or remote-OFF input pulled low).  `write_many()` and
  the web UI's "Apply changes" wrap every batch in OFF / write / ON for
  this reason.

## Notes from the field

A few things that took real testing to figure out, plus some things in
the manual that will trip you up.  Documenting them here so the next
person doesn't have to repeat the work.

### The `*TOE` bits gate the timeout itself, not just the status flag

This is the big one.  The manual describes `CVTOE`, `CCTOE`, and `FVTOE`
(curve_config high byte, bits 0–2) as "timeout indication enable" bits,
which is genuinely ambiguous — it could mean either:

1. The bit gates the timeout machinery itself (i.e. CV will only time
   out if CVTOE is set), or
2. The timeout always runs once you write a value into `curve_*_timeout`,
   and these bits only gate whether the corresponding `*TOF` flag in
   `CHG_STATUS` is raised.

Interpretation (2) is what the *prose* in the manual suggests.
Interpretation (1) is what the firmware actually does.

Empirical evidence: with `curve_fv_timeout = 60` and `FVTOE = 0`
(`curve_config = 0x0884`), the charger floated for 70+ minutes without
timing out.  With `FVTOE = 1` (`curve_config = 0x0C84`), the charger cut
off at exactly 60 minutes.  Same timeout register value, different bit,
different behavior.

**Implication**: setting any `curve_*_timeout` register without also
setting the corresponding `*TOE` bit in `curve_config` is a no-op.  The
web UI exposes the three TOE checkboxes inline next to each timeout
field for exactly this reason — you can't have one without the other.

### CV stage exits on taper without involving the timeout

The CV stage doesn't need a timeout to end.  When the charging current
naturally drops to roughly 10% of rated (about 8.5 A on the 1700-48 in
practice — this threshold is firmware-internal and not exposed as a
register), the CV stage completes and the charger transitions to either
float (if `CVTSSE = 1` or via the `*TOE`/`CVTSSE` interaction described
above) or cut-off (if `CVTSSE = 0`).

For a typical LFP setup, leaving the CC and CV timeouts disabled (TOE
bits clear) and only enabling the FV timeout is sufficient: CV ends
naturally on taper, the charger goes into float, and the FV timeout
gives the BMS a known-bounded top-balance window before cut-off.

### `CURVE_TC` (taper current, B3) is not the CV-exit threshold

The register named "taper current" *sounds* like it should be the
threshold below which CV completes, but in practice the CV-exit
threshold appears to be firmware-internal (~10% of rated).  Setting
`CURVE_TC` to a different value did not change when CV ended.  The
register's actual role in the firmware is unclear from the manual; we
include it in the optimized profile at 5 A for completeness but don't
rely on its exact value.

### Manual chapter 6 has real typos — trust diagrams over prose

Several issues in the CANBus protocol chapter cost real time.  Rely on
the bit-position diagrams, not the prose descriptions, when they
disagree:

- **Three different bits all labeled "CCTOE" in prose** (page 52).  The
  bit-position diagram correctly shows them as `CVTOE` (bit 0),
  `CCTOE` (bit 1), `FVTOE` (bit 2).  The prose copy-pasted "CCTOE"
  three times with mismatched descriptions.

- **TCS values listed as `01 = -3mV`, `01 = -4mV`, `01 = -5mV`** —
  the second and third should be `10` and `11`.

- **CVTSSE and RSTE descriptions appear under SYSTEM_CONFIG** on
  page 53, even though both bits belong to CURVE_CONFIG.  Easy to
  miss.

- **CURVE_CONFIG factory default is contradictory**: page 35 says
  `0004h`, page 56 worked example says `0x0084`.  The latter (charger
  mode + temp comp on) matches the behavior we observed.

- **Timeout range is contradictory**: page 35 (CANBus value-range
  table) says `60–64800` minutes, page 37 (NFC app section) says
  `1–6000`.  CAN writes appear to accept the wider range; the app
  enforces 60–64800.

- **Register names differ between pages**: command list (page 27) calls
  them `CURVE_CC / CURVE_CV / CURVE_FV / CURVE_TC`; value-range table
  (page 62) calls them `CURVE_ICHG / CURVE_VBST / CURVE_VFLOAT /
  CURVE_ITAPER`.  The CAN command codes are identical; only the names
  vary.  This codebase uses the page-27 names.

- **`VOUT_SET` shown as `0x0002`** in the page-62 value-range table —
  pure typo, the correct code is `0x0020` (matches every other place
  in the manual).

- **`SYSTEM_CONFIG` table shows `CAN_CTRL` and `EEP_CONFIG` bits** that
  are never described in the prose.  These appear to be reserved /
  unused but are not documented as such.

### `MFR_*` ASCII strings can include null padding

The manufacturer-info registers (`0x80`–`0x88`) return up to 6 ASCII
bytes per command, padded with `\x00`.  Two-half strings like the
12-character serial number are read in two requests and concatenated.
Implementations need to pace requests at ≥20 ms apart (manual section
6.1) and validate that response frames echo the requested command code
— without these guards, rapid back-to-back reads can return stale
frames from previous requests, producing garbled output.

### Empty / zero-padded MFR fields render literally

Some units return `"000"` (literal ASCII zeros) or all-null bytes for
fields like `MFR_LOCATION` or unpopulated parts of the serial.  The
web UI treats values matching `^[0\s]+$` as placeholders and shows
"unavailable" in italic instead, but the underlying bytes really are
those values — the firmware appears not to distinguish "empty" from
"zero" for these fields.

## License

BSD 3-Clause — see `LICENSE`.

## Acknowledgments

This project was developed with significant assistance from
[Claude](https://claude.ai), Anthropic's AI assistant, particularly
for reverse-engineering chapter 6 of the NPB CANBus protocol manual,
disambiguating its various typos and contradictions, and building the
web UI.  The author retains copyright; AI assistance is acknowledged
here for transparency.

## Disclaimer

This software writes to a battery charger over CAN bus.  Misconfigured
charge parameters can damage your battery, void warranties, or in
extreme cases cause fires.  The author and contributors accept no
liability — see the `LICENSE` file.  Validate any change against your
specific battery chemistry, cell count, and BMS specifications before
applying it to a real system.
