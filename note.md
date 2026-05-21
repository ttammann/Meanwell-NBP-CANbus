# Running NPB charger app on Ubuntu

Full setup including the SocketCAN side which the macOS demo mode skips.

## 1. System dependencies

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip can-utils
```

`can-utils` gives you `cansend`, `candump`, `ip` for CAN — useful for
debugging.

## 2. CAN hardware setup

How you bring up the CAN interface depends on your adapter:

**Built-in CAN controller** (Raspberry Pi with MCP2515 hat, BeagleBone,
etc.) — usually configured at boot via `/boot/config.txt` or device tree.
Check with `ip link show`. You should see something like `can0` listed.

**USB-CAN adapter — `gs_usb` family** (Canable v1/v2 with Candlelight
firmware, Geschwister Schneider, many cheap dongles):

```bash
sudo ip link set can0 up type can bitrate 250000
ip -details link show can0   # verify it's UP
```

**USB-CAN adapter — `slcan` family** (Canable v1 with original slcan
firmware):

```bash
sudo slcand -o -c -s5 /dev/ttyACM0 can0   # -s5 = 250 kbps
sudo ip link set can0 up
```

**PEAK / Kvaser** — install vendor drivers, interfaces are typically
`pcan0` or `kvaser0` rather than `can0`.

To make `can0` come up automatically on boot, add it to
`/etc/network/interfaces.d/can0`:

```
auto can0
iface can0 inet manual
  pre-up /sbin/ip link set $IFACE type can bitrate 250000
  up /sbin/ifconfig $IFACE up
  down /sbin/ifconfig $IFACE down
```

(or use a systemd-networkd `.network` file if your Ubuntu version uses
that.)

## 3. Verify the CAN bus before involving the app

Confirm the charger is actually answering before debugging the Python
side:

```bash
candump can0 &                       # listen for CAN frames
cansend can0 000C0103##2 0000        # send an OPERATION read request
# you should see the charger reply with its current state
kill %1
```

If `candump` shows nothing, the issue is wiring/termination/bitrate, not
the app. (Common: missing 120Ω termination resistor at one or both ends
of the bus.)

## 4. Install and run the app

```bash
unzip npb-charger.zip
cd npb-charger
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

CLI smoke test:

```bash
python charger_app.py check
# OK = charger answered, NO RESPONSE = no answer (re-check CAN setup)
```

Web UI:

```bash
python charger_web.py
# Starting on http://0.0.0.0:8080 (CAN can0 @ 250000, id 0xC0103)
```

Open `http://localhost:8080` (or `http://<this-machine-ip>:8080` from
another device on the LAN). Click **Reload from charger** to pull the
current values into the table.

## 5. Optional: permission to bring up CAN without sudo

By default `ip link set can0 up` requires root. If you want the app to
run unprivileged (e.g. as a user, not root), bring `can0` up at boot via
the `/etc/network/interfaces.d/` snippet above and then the app just
uses the already-up interface — no privilege needed. The app itself
never calls `ip` or anything that needs root.

## 6. Optional: run as a systemd service

If you want the web UI to start at boot and restart on failure, drop
this in `/etc/systemd/system/npb-charger.service`:

```ini
[Unit]
Description=NPB charger web UI
After=network.target

[Service]
Type=simple
User=tom
WorkingDirectory=/home/tom/npb-charger
ExecStart=/home/tom/npb-charger/.venv/bin/python charger_web.py --port 8080
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now npb-charger
sudo systemctl status npb-charger
journalctl -u npb-charger -f       # tail the logs
```

## Common Ubuntu-specific gotchas

- **`pip install` fails with "externally-managed-environment"** on
  Ubuntu 23.04+ — that's why we used a venv. Don't `sudo pip install`
  system-wide.
- **`can0` exists but `cansend` says "Network is down"** — you forgot
  `sudo ip link set can0 up`.
- **Port 8080 already in use** — change with `--port 8088` (or whatever).
- **Browser on another machine can't connect** — the app binds to
  `0.0.0.0` by default so it accepts external connections, but `ufw`
  might be blocking. Either `sudo ufw allow 8080` or run
  `--host 127.0.0.1` and SSH-tunnel.
- **No CAN hardware yet, want to play with the UI** — `python
  charger_web.py --demo` works the same way it does on macOS.

If `python charger_app.py check` returns `NO RESPONSE` but `candump`
shows the charger talking when you `cansend` manually, the issue is
likely the CAN ID — check the address bits A0/A1 on CN71 and adjust
with `--can-id 0xC0103` (the default assumes both pins open = address 0).
