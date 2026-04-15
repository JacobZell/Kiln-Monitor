# Kiln Monitor

A monitoring tool for The Bodgery's ceramics kiln. Logs into KilnAid, tracks the kiln's status and temperature in real time, sends Slack notifications to members and leadership, and hosts a live temperature graph accessible from any device on the network.

---

## What it does

- Logs into [kilnaid.bartinst.com](https://kilnaid.bartinst.com) automatically
- Polls the kiln status page every 60 seconds
- Reads kiln status, program name, elapsed firing time, and all three zone temperatures
- Detects faulty thermocouples (any zone 100°F+ from the average of the other two)
- Sends Slack notifications to the right audience when something changes
- Hosts a live temperature graph at `http://localhost:5000`
- Saves every completed firing to `kiln_firings.json` for historical review
- Restarts automatically via a watchdog script if it ever crashes

---

## Slack notifications

| Event | Members channel | Leadership channel |
|---|---|---|
| Kiln started firing | ✅ | |
| Kiln unloaded / idle | ✅ | |
| Firing complete (cooling) | | ✅ |
| Ready to unload (≤425°F) | | ✅ |
| Kiln error | | ✅ |
| Thermocouple alert | | ✅ |

Notifications are suppressed on startup — only sent when the status actually changes.

---

## Web interface

Open `http://localhost:5000` (or your machine's IP on port 5000) in any browser.

- **Live view** — current temperature, all three zones, program, duration, rate of change
- **Temperature graph** — full firing curve from start to cool-down, straight-line point graph
- **Past firings sidebar** — click any past firing to load its curve into the graph
- **Mobile friendly** — responsive layout that works on phones and tablets
- **Dark mode** — follows your device's system preference

To access from outside your local network see the [Remote Access](#remote-access) section below.

---

## Setup

### Requirements

- Python 3.9+
- pip

### Install dependencies

```bash
pip install playwright requests
python -m playwright install
```

On a Raspberry Pi use:
```bash
pip install playwright requests --break-system-packages
python -m playwright install
```

### Configuration

Open `kiln_monitor.py` and update the config section at the top:

```python
KILN_EMAIL    = "ceramics@thebodgery.org"
KILN_PASSWORD = "your_password"

SLACK_MEMBERS_URL    = "https://hooks.slack.com/triggers/..."
SLACK_LEADERSHIP_URL = "https://hooks.slack.com/triggers/..."

POLL_INTERVAL_SECONDS = 60   # how often to check (seconds)
WEB_PORT = 5000              # port for the web graph
READY_TO_UNLOAD_TEMP = 425   # °F threshold for unload notification
HISTORY_FILE = "kiln_firings.json"  # where past firings are saved
```

---

## Running

### Normal run

```bash
python kiln_monitor.py
```

### Recommended — run via watchdog (auto-restarts on crash)

```bash
python kiln_watchdog.py
```

The watchdog monitors the main script and restarts it automatically if it ever exits for any reason.

### Keep running after closing the terminal (Windows)

```powershell
start /b pythonw kiln_watchdog.py
```

---

## Raspberry Pi setup

### Install system dependencies

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install python3-pip chromium-browser -y
pip install playwright requests --break-system-packages
python3 -m playwright install
```

### Increase swap (recommended for Pi 3B)

```bash
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile
# Change CONF_SWAPSIZE=100 to CONF_SWAPSIZE=1024
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

### Auto-start on reboot

```bash
crontab -e
```

Add this line:

```
@reboot sleep 30 && python3 /home/pi/kiln_watchdog.py >> /home/pi/kiln.log 2>&1
```

The `sleep 30` gives the Pi time to connect to WiFi before starting.

### Monitor the log

```bash
tail -f /home/pi/kiln.log
```

### Find the Pi's IP address

```bash
hostname -I
```

Then access the graph at `http://<pi-ip>:5000` from any device on the same network.

---

## Remote access

To make the graph accessible from outside your local network:

### Option 1 — Cloudflare Tunnel (recommended, free)

Requires a Cloudflare account and a domain name. Gives a permanent public URL.

```bash
# Install cloudflared on the Pi
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 -o cloudflared
chmod +x cloudflared
sudo mv cloudflared /usr/local/bin

# Set up tunnel
cloudflared tunnel login
cloudflared tunnel create kiln-monitor
cloudflared tunnel route dns kiln-monitor kiln.yourdomain.com
cloudflared tunnel run --url http://localhost:5000 kiln-monitor
```

### Option 2 — Ngrok (quick, no domain needed)

```bash
pip install ngrok
ngrok http 5000
```

Gives a temporary public URL that changes on each restart. Paid tier ($8/mo) gives a fixed URL.

### Option 3 — Port forwarding

Forward port 5000 on your router to your Pi's local IP. Access via your public IP address. Free but your IP may change — pair with [DuckDNS](https://www.duckdns.org) for a stable address.

---

## Firing history

Completed firings are automatically saved to `kiln_firings.json` when the kiln goes idle. This file persists across restarts and is loaded back in on startup.

To store in a specific directory on the Pi:

```python
HISTORY_FILE = "/home/pi/kiln_data/kiln_firings.json"
```

Create the folder first:

```bash
mkdir -p /home/pi/kiln_data
```

---

## Reliability features

- **Browser restart** — Chromium is restarted every 24 hours to prevent memory leaks
- **Session recovery** — if the KilnAid session expires, the script re-logs in automatically (up to 5 retries with increasing delays)
- **Watchdog** — `kiln_watchdog.py` restarts the monitor if it crashes
- **History cap** — temperature history is capped at 1500 entries per firing to prevent unbounded memory growth
- **Retry backoff** — on network errors the script pauses before retrying rather than hammering the server

---

## File structure

```
kiln_monitor.py     — main monitoring script
kiln_watchdog.py    — auto-restart watchdog
kiln_firings.json   — saved firing history (created automatically)
kiln.log            — log output when run via crontab
README.md           — this file
```

---

## Troubleshooting

**Login keeps failing**
- Check your KilnAid credentials in the config
- Try increasing the timeout: `timeout=60_000` is already set, but the Pi 3B can be slow to launch Chromium

**No kilns found**
- The KilnAid page may have changed its HTML structure
- Run with a screenshot debug to see what the browser is actually seeing

**Port 5000 already in use**
- Change `WEB_PORT = 5000` to another value like `5001` or `5500`

**Slack not receiving messages**
- Confirm the webhook URLs are correct and the Slack workflows are active
- Check the console output for HTTP response codes
