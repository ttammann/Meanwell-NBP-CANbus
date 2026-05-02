# Mean Well NPB charger CAN controller

CLI and web UI for Mean Well NPB-450 / 750 / 1200 / 1700 chargers over CAN
bus, implementing the protocol from section 6 of the manual.  Designed
around a 16-cell LiFePO4 (LFP) battery on an NPB-1700-48, but everything
range-related lives in one dictionary at the top of `charger_app.py` so
adapting to a different chemistry or model is a one-file edit.

## Files

| file              | purpose                                                |
|-------------------|--------------------------------------------------------|
| `charger_app.py`  | charger client + command-line interface                |
| `charger_web.py`  | Flask web UI (imports `charger_app`)                   |
| `requirements.txt`| Python dependencies                                    |
| `preview.html`    | static preview of the web UI (no server / no hardware) |
| `README.md`       | this file                                              |

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# CLI on Linux with socketcan (Pi, etc.)
python3 charger_app.py read

# Web UI on Linux with socketcan
python3 charger_web.py
# then open http://localhost:8080

# Web UI on macOS or any machine without CAN hardware (simulated charger)
python3 charger_web.py --demo
```

## Command-line interface

```bash
python3 charger_app.py --help        # full help with examples
python3 charger_app.py list          # list every register, no CAN traffic
python3 charger_app.py check         # probe the bus
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
--channel can0          SocketCAN channel name or device path (default: can0)
--bitrate 250000        CAN bitrate (default: 250000)
--can-id 0xC0103        29-bit CAN arbitration ID (default: 0xC0103)
--interface socketcan   python-can backend.  On macOS use a USB-CAN adapter:
                        slcan, gs_usb, pcan, kvaser, etc.
```

## Web UI

```bash
python3 charger_web.py [--demo] [--host 0.0.0.0] [--port 8080] [CAN options]
```

Then open `http://<host>:8080`.

The page is split into five sections, top to bottom:

1. **Charge curve** — focal point.  Editable table of the bulk current,
   CV/FV voltage, taper, and three stage timeouts; below it a friendly row
   of checkboxes for `curve_config` (charger mode, temp comp, restart-on-low)
   plus a restart voltage input that auto-greys when restart is off.
2. **Status row** — slim coloured pill showing charge stage (idle, CC, CV,
   float, fully charged, fault) plus inline chips for any active flags.
3. **Live readings** — `V_in`, internal temp, `V_out`, `I_out`, computed
   power.  `V_out` / `I_out` / power show "standby" when no current is
   flowing — they're only meaningful while charging.
4. **Device info** — model, manufacturer, serial, firmware, manufacture date,
   origin (read from the `MFR_*` ASCII registers, always available).
5. **Activity log** — running record of reads, writes, errors.

**The page starts blank.**  Click **Reload from charger** to populate
the table with what's currently in the unit; from there, edit any row
and click **Apply changes** to write only the dirty fields back, in one
OFF/ON cycle.  The friendly checkbox row stays disabled until you've
done a reload, since toggling individual `curve_config` bits requires
knowing the existing register state to preserve bits the UI doesn't
expose.

**Clearing a field skips that register.**  If you reload values and
then empty an input box, the row turns dashed-grey ("X skipped pending")
and the Apply batch will not include that register — whatever the
charger currently has is left untouched.  This is intentionally distinct
from typing `0`, which is a real value the firmware interprets specially
(e.g. `0` for a timeout means "disabled" if the firmware accepts it;
for a timeout register with a `60` minimum, typing `0` is rejected as
out-of-range).

### macOS / hardware-free testing (`--demo`)

`socketcan` is Linux-only, so the default mode won't work on macOS.  Use
`--demo` to run against an in-process simulated charger:

```bash
python3 charger_web.py --demo
```

The `FakeBus` class in `charger_web.py` answers reads with realistic
values that drift slightly between polls when the output is on, and
accepts writes that round-trip through in-memory state.  All endpoints
behave identically to the real hardware, so the UI can be developed and
tested without anything attached.

When you're ready to drive a real charger from a Mac, get a USB-CAN
adapter (Canable / Geschwister Schneider / PEAK / Kvaser) and run with
the matching python-can backend:

```bash
python3 charger_web.py --interface slcan --channel /dev/tty.usbmodem1101
```

### HTTP API (all JSON)

| route                  | method | purpose                                       |
|------------------------|--------|-----------------------------------------------|
| `/api/registers`       | GET    | static metadata for every register            |
| `/api/read?names=…`    | GET    | read named registers (or all if omitted)      |
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
