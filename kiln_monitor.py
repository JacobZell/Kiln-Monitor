"""
KilnAid Status Monitor
======================
Logs into kilnaid.bartinst.com, fires Slack webhooks on status changes,
and hosts a live temperature graph at http://localhost:5000

Setup:
  pip install playwright requests
  python -m playwright install

Usage:
  python kiln_monitor.py
"""

import time
import threading
import requests
import json
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from datetime import datetime
from pathlib import Path

# ── LOAD .env FILE ─────────────────────────────────────────────────────────────

def load_env():
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, value = line.partition("=")
                    os.environ.setdefault(key.strip(), value.strip())

load_env()

# ── CONFIG ─────────────────────────────────────────────────────────────────────

KILN_EMAIL    = os.environ.get("KILN_EMAIL", "ceramics@thebodgery.org")
KILN_PASSWORD = os.environ.get("KILN_PASSWORD", "B0dgeryCeramics!")

SLACK_MEMBERS_URL    = os.environ.get("SLACK_MEMBERS_URL", "https://hooks.slack.com/triggers/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")
SLACK_LEADERSHIP_URL = os.environ.get("SLACK_LEADERSHIP_URL", "https://hooks.slack.com/triggers/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")

POLL_INTERVAL_SECONDS = 60
WEB_PORT = 5000
ABLE_TO_UNLOAD_TEMP = 425
READY_TO_UNLOAD_TEMP = 200
HISTORY_FILE = "kiln_firings.json"
MIN_FIRING_DURATION_HOURS = 12

# ── URLS ───────────────────────────────────────────────────────────────────────

BASE_URL  = "https://kilnaid.bartinst.com"
LOGIN_URL = f"{BASE_URL}/home"

JS_CLICK_ION_BUTTON = """
const ionBtn = document.querySelector('ion-button');
if (ionBtn && ionBtn.shadowRoot) {
    const inner = ionBtn.shadowRoot.querySelector('button');
    if (inner) { inner.click(); }
} else if (ionBtn) {
    ionBtn.click();
}
"""

# ── PAST FIRINGS STORAGE ───────────────────────────────────────────────────────

EXAMPLE_FIRINGS = []

def load_past_firings():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_past_firings(firings):
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(firings, f)
    except Exception as e:
        print(f"Could not save firing history: {e}")

# ── SHARED STATE ───────────────────────────────────────────────────────────────

state = {
    "name": "Kiln",
    "status": "Idle",
    "temp": 0,
    "history": [],
    "firing_start": None,
    "peak_temp": 0,
    "last_updated": "--",
    "program": "",
    "elapsed": "",
    "z1": 0,
    "z3": 0,
}
state_lock = threading.Lock()
past_firings = load_past_firings()

# ── WEB SERVER ─────────────────────────────────────────────────────────────────

HTML_PAGE = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def build_html():
    with state_lock:
        s = dict(state)

    history = s["history"]
    status  = s["status"]
    temp    = s["temp"]
    peak    = f"{s['peak_temp']}°F" if s["peak_temp"] else "--"
    program = s.get("program", "--") or "--"
    z1      = s.get("z1", 0)
    z3      = s.get("z3", 0)

    elapsed_from_kiln = s.get("elapsed", "")
    if elapsed_from_kiln:
        duration = elapsed_from_kiln
    elif s["firing_start"] and status.lower() in ("firing", "complete"):
        secs = int((datetime.now() - s["firing_start"]).total_seconds())
        h, m = divmod(secs // 60, 60)
        duration = f"{h}h {m}m" if h else f"{m}m"
    else:
        duration = "--"

    if len(history) >= 2:
        diff = history[-1]["temp"] - history[-2]["temp"]
        rate = f"{'+' if diff>=0 else ''}{diff * 60}°F/h"
    else:
        rate = "--"

    sl = status.lower()
    if "firing" in sl:
        badge = "firing"
    elif "error" in sl:
        badge = "error"
    elif "complete" in sl and temp <= ABLE_TO_UNLOAD_TEMP:
        badge = "ready"
        status = "Ready to unload"
    elif "complete" in sl:
        badge = "complete"
    else:
        badge = "idle"

    all_firings = EXAMPLE_FIRINGS + past_firings
    live_history = json.dumps(history)
    all_firings_json = json.dumps(all_firings)

    html = HTML_PAGE
    html = html.replace("__KILN_NAME__", s["name"])
    html = html.replace("__STATUS__",    status)
    html = html.replace("__BADGE_CLASS__", badge)
    html = html.replace("__TEMP__",      str(temp))
    html = html.replace("__Z1__",        str(z1))
    html = html.replace("__Z3__",        str(z3))
    html = html.replace("__PEAK__",      peak)
    html = html.replace("__DURATION__",  duration)
    html = html.replace("__RATE__",      rate)
    html = html.replace("__PROGRAM__",   program)
    html = html.replace("__UPDATED__",   s["last_updated"])
    html = html.replace("__LIVE_HISTORY__", live_history)
    html = html.replace("__ALL_FIRINGS__",  all_firings_json)
    return html


def build_state_json():
    with state_lock:
        s = dict(state)

    history = s["history"]
    status  = s["status"]
    temp    = s["temp"]
    peak    = f"{s['peak_temp']}°F" if s["peak_temp"] else "--"
    program = s.get("program", "--") or "--"
    z1      = s.get("z1", 0)
    z3      = s.get("z3", 0)

    elapsed_from_kiln = s.get("elapsed", "")
    if elapsed_from_kiln:
        duration = elapsed_from_kiln
    elif s["firing_start"] and status.lower() in ("firing", "complete"):
        secs = int((datetime.now() - s["firing_start"]).total_seconds())
        h, m = divmod(secs // 60, 60)
        duration = f"{h}h {m}m" if h else f"{m}m"
    else:
        duration = "--"

    if len(history) >= 2:
        diff = history[-1]["temp"] - history[-2]["temp"]
        rate = f"{'+' if diff>=0 else ''}{diff * 60}°F/h"
    else:
        rate = "--"

    sl = status.lower()
    if "firing" in sl:
        badge = "firing"
    elif "error" in sl:
        badge = "error"
    elif "complete" in sl and temp <= ABLE_TO_UNLOAD_TEMP:
        badge = "ready"
        status = "Ready to unload"
    elif "complete" in sl:
        badge = "complete"
    else:
        badge = "idle"

    return json.dumps({
        "name": s["name"],
        "status": status,
        "badge": badge,
        "temp": temp,
        "z1": z1,
        "z3": z3,
        "peak": peak,
        "duration": duration,
        "rate": rate,
        "program": program,
        "last_updated": s["last_updated"],
        "history": history,
        "all_firings": EXAMPLE_FIRINGS + past_firings,
    })


class KilnHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/state":
            data = build_state_json().encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        html = build_html().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def do_DELETE(self):
        # Extract firing ID from path: DELETE /firing/<id>
        path = self.path
        if path.startswith("/firing/"):
            firing_id = path[len("/firing/"):]
            global past_firings
            original_len = len(past_firings)
            past_firings = [f for f in past_firings if f["id"] != firing_id]
            if len(past_firings) < original_len:
                save_past_firings(past_firings)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": true}')
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"ok": false, "error": "Not found"}')
        else:
            self.send_response(400)
            self.end_headers()

    def log_message(self, *args):
        pass


class KilnServer(HTTPServer):
    def handle_error(self, request, client_address):
        import sys
        if sys.exc_info()[0] in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


def run_server():
    server = KilnServer(("0.0.0.0", WEB_PORT), KilnHandler)
    print(f"🌐 Graph available at http://localhost:{WEB_PORT}")
    server.serve_forever()

# ── SLACK ──────────────────────────────────────────────────────────────────────

def send_slack_message(payload: dict, webhook_url: str):
    resp = requests.post(webhook_url, json=payload)
    if resp.status_code == 200:
        print(f"📨 Slack notified: {payload}")
    else:
        print(f"⚠️  Slack webhook returned {resp.status_code}: {resp.text}")

def notify(payload: dict, members: bool = False, leadership: bool = False):
    if members:
        send_slack_message(payload, SLACK_MEMBERS_URL)
    if leadership:
        send_slack_message(payload, SLACK_LEADERSHIP_URL)

# ── BROWSER HELPERS ────────────────────────────────────────────────────────────

def login(page):
    print("🔐 Logging in…")
    page.goto(LOGIN_URL, wait_until="networkidle")
    page.wait_for_selector("input.native-input", timeout=60_000)
    inputs = page.query_selector_all("input.native-input")
    if len(inputs) < 2:
        raise RuntimeError(f"Expected 2 login inputs, found {len(inputs)}.")
    inputs[0].click()
    inputs[0].fill(KILN_EMAIL)
    inputs[1].click()
    inputs[1].fill(KILN_PASSWORD)
    page.evaluate(JS_CLICK_ION_BUTTON)
    page.wait_for_url("**/kilns", timeout=60_000)
    page.wait_for_selector(".item-kiln-list", timeout=60_000)
    page.click("div.temperature")
    page.wait_for_url("**/kiln-tabs/status", timeout=60_000)
    print(f"✅ Logged in — now on {page.url}")


def read_kiln_status(page):
    if "/home" in page.url or "kiln-tabs/status" not in page.url:
        raise RuntimeError("session expired")

    # Status — scan page text for known keywords, robust to HTML changes
    # Strip label text that contains status keywords but isn't the status itself
    body_text = page.inner_text("body").lower()
    body_text = body_text.replace("elapsed firing time", "")
    if "firing" in body_text:
        status = "Firing"
    elif "complete" in body_text:
        status = "Complete"
    elif "error" in body_text:
        status = "Error"
    elif "idle" in body_text:
        status = "Idle"
    elif "delay" in body_text:
        status = "Delay"
    else:
        status = "Unknown"

    # Program and elapsed time
    program = ""
    elapsed = ""
    for item in page.query_selector_all("ion-item"):
        labels = item.query_selector_all("ion-label")
        if len(labels) < 2:
            continue
        label_text = (labels[0].inner_text() or "").strip()
        if "Program" in label_text:
            program = (labels[1].inner_text() or "").strip()
        elif "Elapsed Firing Time" in label_text:
            elapsed = (labels[1].inner_text() or "").strip()

    # Zone 2 — primary temperature
    zone2_el = page.query_selector("ion-text.kiln-temp-large")
    zone2_str = (zone2_el.inner_text() or "0").replace("°F","").replace("°C","").replace("\xa0","").strip() if zone2_el else "0"
    try:
        zone2 = int(zone2_str)
    except ValueError:
        zone2 = 0

    # Zone 1 and Zone 3
    zone1, zone3 = 0, 0
    for header in page.query_selector_all("ion-card-header"):
        zone_label_el = header.query_selector("ion-text:not(.tempLabel)")
        temp_label_el = header.query_selector("ion-text.tempLabel")
        if not zone_label_el or not temp_label_el:
            continue
        zone_label = (zone_label_el.inner_text() or "").strip()
        temp_val = (temp_label_el.inner_text() or "0").replace("°F","").replace("°C","").replace("\xa0","").strip()
        try:
            t = int(temp_val)
        except ValueError:
            t = 0
        if "Zone 1" in zone_label:
            zone1 = t
        elif "Zone 3" in zone_label:
            zone3 = t

    # Thermocouple check
    zones = {"Zone 1": zone1, "Zone 2": zone2, "Zone 3": zone3}
    broken = []
    if zone1 > 0 and zone2 > 0 and zone3 > 0:
        for zone_name, val, avg_others in [
            ("Zone 1", zone1, (zone2 + zone3) / 2),
            ("Zone 2", zone2, (zone1 + zone3) / 2),
            ("Zone 3", zone3, (zone1 + zone2) / 2),
        ]:
            if abs(val - avg_others) >= 100:
                broken.append(zone_name)

    # Kiln name
    title_el = page.query_selector("ion-title, .header-kiln-name")
    name_raw = (title_el.inner_text() or "").strip() if title_el else ""
    name = name_raw if name_raw and name_raw.lower() not in ("login", "") else "Ceramics Kiln"

    return {name: {
        "status": status,
        "temp": zone2,
        "temp_str": f"{zone2}°F",
        "program": program,
        "elapsed": elapsed,
        "zones": zones,
        "broken": broken,
        "z1": zone1,
        "z3": zone3,
    }}

# ── MAIN LOOP ──────────────────────────────────────────────────────────────────

def main():
    global past_firings
    print("🔥 KilnAid Monitor starting…")

    t = threading.Thread(target=run_server, daemon=True)
    t.start()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=[
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-gpu",
            "--single-process",
        ])
        page = browser.new_context().new_page()

        try:
            login(page)
        except Exception as e:
            print(f"❌ Login failed: {e}")
            browser.close()
            return

        last_statuses = {}
        first_run = True
        browser_start = time.time()
        BROWSER_RESTART_INTERVAL = 24 * 60 * 60

        while True:
            try:
                kilns = read_kiln_status(page)

                for name, data in kilns.items():
                    status   = data["status"]
                    temp     = data["temp"]
                    temp_str = data["temp_str"]
                    program  = data.get("program", "")
                    elapsed  = data.get("elapsed", "")
                    zones    = data.get("zones", {})
                    broken   = data.get("broken", [])
                    z1       = data.get("z1", 0)
                    z3       = data.get("z3", 0)
                    prev     = last_statuses.get(name)
                    prev_broken = last_statuses.get(f"{name}_broken", [])

                    print(f"[{time.strftime('%H:%M:%S')}] {name}: {status} | Z1:{z1}°F Z2:{temp}°F Z3:{z3}°F | {program}")

                    # Thermocouple alerts - only when firing
                    if not first_run and broken and broken != prev_broken and "firing" in status.lower():
                        zone_readings = ", ".join(f"{z}: {zones[z]}°F" for z in zones)
                        notify({"KilnStatus": f"⚠️ *Thermocouple alert on {name}!* {', '.join(broken)} thermocouple may be faulty. Readings: {zone_readings}"}, leadership=True)
                        last_statuses[f"{name}_broken"] = broken
                    elif not first_run and not broken and prev_broken:
                        last_statuses[f"{name}_broken"] = []

                    # Update shared state
                    with state_lock:
                        state["name"]         = name
                        state["status"]       = status
                        state["temp"]         = temp
                        state["z1"]           = z1
                        state["z3"]           = z3
                        state["program"]      = program
                        state["elapsed"]      = elapsed
                        state["last_updated"] = datetime.now().strftime("%d %b %Y, %H:%M:%S")

                        if "firing" in status.lower() and not state["firing_start"]:
                            state["firing_start"] = datetime.now()
                            state["history"] = []
                            state["peak_temp"] = 0

                        if "idle" in status.lower() and state["firing_start"]:
                            secs = int((datetime.now() - state["firing_start"]).total_seconds())
                            if state["history"] and secs >= MIN_FIRING_DURATION_HOURS * 3600:
                                h, m = divmod(secs // 60, 60)
                                firing_record = {
                                    "id": state["firing_start"].strftime("firing_%Y%m%d_%H%M"),
                                    "label": f"{program or 'Firing'} — {state['firing_start'].strftime('%d %b %Y')}",
                                    "program": program,
                                    "peak": state["peak_temp"],
                                    "duration": f"{h}h {m}m" if h else f"{m}m",
                                    "date": state["firing_start"].strftime("%Y-%m-%d"),
                                    "history": state["history"],
                                }
                                past_firings.append(firing_record)
                                save_past_firings(past_firings)
                                print(f"💾 Firing saved: {firing_record['label']}")
                            elif state["history"]:
                                print(f"⏭️  Firing too short ({secs//3600}h {(secs%3600)//60}m) — not saved.")
                            state["firing_start"] = None

                        if state["firing_start"] or "complete" in status.lower():
                            point_label = elapsed if elapsed else datetime.now().strftime("%H:%M")
                            state["history"].append({"time": point_label, "temp": temp})
                            if len(state["history"]) > 1500:
                                state["history"] = state["history"][-1500:]
                            if temp > state["peak_temp"]:
                                state["peak_temp"] = temp

                    # Slack notifications
                    was_ready   = last_statuses.get(f"{name}_ready", False)
                    is_complete = "complete" in status.lower()
                    is_able     = is_complete and temp <= ABLE_TO_UNLOAD_TEMP
                    is_ready    = is_complete and temp <= READY_TO_UNLOAD_TEMP

                    if first_run:
                        last_statuses[name] = status
                        last_statuses[f"{name}_ready"] = is_able

                    if not first_run and (status != prev or (is_able and not was_ready)):
                        prog = f" ({program})" if program else ""
                        if "firing" in status.lower():
                            notify({"KilnStatus": f"🔥 *{name} is now firing{prog}!* The kiln has started a firing cycle."}, members=True)
                        elif is_ready:
                            notify({"KilnStatus": f"🏺 *{name} is ready to unload!* The {program or 'firing'} has finished and cooled to {temp_str} — safe to open and unload."}, members=True)
                        elif is_able:
                            notify({"KilnStatus": f"🏺 *{name} is able to be unloaded!* The {program or 'firing'} has finished and cooled to {temp_str} — safe to open."}, leadership=True)
                        elif is_complete:
                            notify({"KilnStatus": f"✅ *{name} firing complete{prog}!* Reached target temperature. Currently cooling at {temp_str}."}, leadership=True)
                        elif "idle" in status.lower():
                            notify({"KilnStatus": f"💤 *{name} has been unloaded and is now idle.* Current temp: {temp_str}"}, members=True)
                        elif "error" in status.lower():
                            notify({"KilnStatus": f"🚨 *{name} has an error{prog}!* The kiln has reported an error and may need attention. Current temp: {temp_str}"}, leadership=True)
                        else:
                            notify({"KilnStatus": f"ℹ️ *{name} status update:* {status}{prog} — Current temp: {temp_str}"}, leadership=True)

                        last_statuses[name] = status
                        last_statuses[f"{name}_ready"] = is_able

            except PWTimeout:
                print("⚠️  Timeout reading page, will retry.")
                time.sleep(30)
            except RuntimeError as e:
                if "session" in str(e):
                    print("🔄 Session expired, re-logging in…")
                    for attempt in range(5):
                        try:
                            login(page)
                            break
                        except Exception as le:
                            wait = 60 * (attempt + 1)
                            print(f"Re-login attempt {attempt+1} failed: {le}. Retrying in {wait}s…")
                            time.sleep(wait)
                else:
                    print(f"Error: {e}")
            except Exception as e:
                import traceback
                print(f"Unexpected error: {e}")
                traceback.print_exc()
                time.sleep(30)

            first_run = False

            if time.time() - browser_start > BROWSER_RESTART_INTERVAL:
                print("🔄 Scheduled browser restart (24h uptime)…")
                try:
                    browser.close()
                except Exception:
                    pass
                browser = p.chromium.launch(headless=True, args=[
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                    "--disable-gpu",
                    "--single-process",
                ])
                page = browser.new_context().new_page()
                try:
                    login(page)
                except Exception as e:
                    print(f"Re-login after restart failed: {e}")
                browser_start = time.time()
                first_run = True

            time.sleep(POLL_INTERVAL_SECONDS)

        browser.close()

if __name__ == "__main__":
    main()