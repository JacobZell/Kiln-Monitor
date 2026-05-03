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
_DIR = Path(__file__).parent
HISTORY_FILE     = _DIR / "kiln_firings.json"
MAINTENANCE_FILE = _DIR / "kiln_maintenance.json"
SNAPSHOT_FILE    = _DIR / "current_firing.json"
MAINT_PATH = "/maint"
MIN_PEAK_TO_COUNT = 1000
FIRING_END_TEMP = 300
FIRING_PEAK_THRESHOLD = 400
ABANDONED_MIN_MINUTES = 30

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

def load_maintenance():
    if os.path.exists(MAINTENANCE_FILE):
        try:
            with open(MAINTENANCE_FILE) as f:
                data = json.load(f)
            if isinstance(data, list):
                return {"records": data, "lifetime_offset": 0}
            return data
        except Exception:
            pass
    return {"records": [], "lifetime_offset": 0}

def save_maintenance(maint):
    try:
        with open(MAINTENANCE_FILE, "w") as f:
            json.dump(maint, f)
    except Exception as e:
        print(f"Could not save maintenance records: {e}")

def save_snapshot():
    with state_lock:
        if not state["firing_start"]:
            return
        snap = {
            "firing_start":   state["firing_start"].isoformat(),
            "history":        list(state["history"]),
            "peak_temp":      state["peak_temp"],
            "peak_temp_time": state["peak_temp_time"].isoformat() if state["peak_temp_time"] else None,
            "has_peaked":     state["has_peaked"],
            "program":        state["program"],
        }
    try:
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(snap, f)
    except Exception as e:
        print(f"Could not save snapshot: {e}")

def clear_snapshot():
    try:
        if os.path.exists(SNAPSHOT_FILE):
            os.remove(SNAPSHOT_FILE)
    except Exception:
        pass

def restore_snapshot():
    if not os.path.exists(SNAPSHOT_FILE):
        return False
    try:
        with open(SNAPSHOT_FILE) as f:
            snap = json.load(f)
        fs = datetime.fromisoformat(snap["firing_start"])
        if (datetime.now() - fs).total_seconds() > 48 * 3600 or snap.get("peak_temp", 0) == 0:
            clear_snapshot()
            return False
        ptt = datetime.fromisoformat(snap["peak_temp_time"]) if snap.get("peak_temp_time") else None
        with state_lock:
            state["firing_start"]   = fs
            state["history"]        = snap.get("history", [])
            state["peak_temp"]      = snap.get("peak_temp", 0)
            state["peak_temp_time"] = ptt
            state["has_peaked"]     = snap.get("has_peaked", False)
            state["program"]        = snap.get("program", "")
        print(f"🔄 Snapshot restored: {fs.isoformat()}, peak {snap['peak_temp']}°F, {len(snap.get('history', []))} pts")
        return True
    except Exception as e:
        print(f"Could not restore snapshot: {e}")
        clear_snapshot()
        return False

# ── SHARED STATE ───────────────────────────────────────────────────────────────

state = {
    "name": "Kiln",
    "status": "Idle",
    "temp": 0,
    "history": [],
    "firing_start": None,
    "peak_temp": 0,
    "peak_temp_time": None,
    "has_peaked": False,
    "last_updated": "",
    "program": "",
    "elapsed": "",
    "z1": 0,
    "z3": 0,
}
state_lock = threading.Lock()
past_firings = load_past_firings()
maintenance_data = load_maintenance()

# ── WEB SERVER ─────────────────────────────────────────────────────────────────

HTML_PAGE  = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")
MAINT_HTML = (Path(__file__).parent / "maintenance.html").read_text(encoding="utf-8")


def _compute_display(s):
    history = s["history"]
    status  = s["status"]
    temp    = s["temp"]
    peak    = f"{s['peak_temp']}°F" if s["peak_temp"] else "--"
    program = s.get("program", "--") or "--"
    z1      = s.get("z1", 0)
    z3      = s.get("z3", 0)

    fs  = s.get("firing_start")
    ptt = s.get("peak_temp_time")

    if fs and status.lower() in ("firing", "complete"):
        secs = int((datetime.now() - fs).total_seconds())
        h, m = divmod(secs // 60, 60)
        duration = f"{h}h {m}m" if h else f"{m}m"
    else:
        duration = "--"

    if fs and ptt:
        ps = int((ptt - fs).total_seconds())
        ph, pm = divmod(ps // 60, 60)
        duration_to_peak = f"{ph}h {pm}m" if ph else f"{pm}m"
    else:
        duration_to_peak = "--"

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

    return dict(history=history, status=status, temp=temp, peak=peak, program=program,
                z1=z1, z3=z3, duration=duration, duration_to_peak=duration_to_peak,
                rate=rate, badge=badge)


def build_html():
    with state_lock:
        s = dict(state)

    d = _compute_display(s)
    all_firings      = EXAMPLE_FIRINGS + past_firings
    live_history     = json.dumps(d["history"])
    all_firings_json = json.dumps(all_firings)

    html = HTML_PAGE
    html = html.replace("__KILN_NAME__",       s["name"])
    html = html.replace("__STATUS__",          d["status"])
    html = html.replace("__BADGE_CLASS__",     d["badge"])
    html = html.replace("__TEMP__",            str(d["temp"]))
    html = html.replace("__Z1__",              str(d["z1"]))
    html = html.replace("__Z3__",              str(d["z3"]))
    html = html.replace("__PEAK__",            d["peak"])
    html = html.replace("__DURATION__",        d["duration"])
    html = html.replace("__DURATION_TO_PEAK__", d["duration_to_peak"])
    html = html.replace("__RATE__",            d["rate"])
    html = html.replace("__PROGRAM__",         d["program"])
    html = html.replace("__UPDATED__",         s["last_updated"] or "")
    html = html.replace("__LIVE_HISTORY__",    live_history)
    html = html.replace("__ALL_FIRINGS__",     all_firings_json)
    return html


def build_state_json():
    with state_lock:
        s = dict(state)

    d = _compute_display(s)
    return json.dumps({
        "name":             s["name"],
        "status":           d["status"],
        "badge":            d["badge"],
        "temp":             d["temp"],
        "z1":               d["z1"],
        "z3":               d["z3"],
        "peak":             d["peak"],
        "duration":         d["duration"],
        "duration_to_peak": d["duration_to_peak"],
        "rate":             d["rate"],
        "program":          d["program"],
        "last_updated":     s["last_updated"],
        "history":          d["history"],
        "all_firings":      EXAMPLE_FIRINGS + past_firings,
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
        if self.path in (MAINT_PATH, MAINT_PATH + "/"):
            page = MAINT_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
            return
        if self.path == MAINT_PATH + "/data":
            counted = sum(1 for f in past_firings if f.get("counted", True))
            payload = json.dumps({
                "firings":         EXAMPLE_FIRINGS + past_firings,
                "replacements":    maintenance_data["records"],
                "total_firings":   counted + maintenance_data.get("lifetime_offset", 0),
                "lifetime_offset": maintenance_data.get("lifetime_offset", 0),
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        html = build_html().encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _json_resp(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length)) if length else {}

    def do_POST(self):
        global maintenance_data, past_firings

        if self.path == MAINT_PATH + "/replacement":
            try:
                payload   = self._read_body()
                component = payload.get("component", "").strip()
                notes     = payload.get("notes", "").strip()
                if not component:
                    return self._json_resp(400, {"ok": False, "error": "component required"})
                counted = sum(1 for f in past_firings if f.get("counted", True))
                total   = counted + maintenance_data.get("lifetime_offset", 0)
                record  = {
                    "id":           f"repl_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
                    "component":    component,
                    "notes":        notes,
                    "date":         datetime.now().strftime("%Y-%m-%d"),
                    "firing_count": total,
                }
                maintenance_data["records"].append(record)
                save_maintenance(maintenance_data)
                self._json_resp(200, {"ok": True, "record": record})
            except Exception as e:
                self._json_resp(500, {"ok": False, "error": str(e)})

        elif self.path == MAINT_PATH + "/adjust_count":
            try:
                payload = self._read_body()
                offset  = int(payload.get("lifetime_offset", 0))
                maintenance_data["lifetime_offset"] = offset
                save_maintenance(maintenance_data)
                self._json_resp(200, {"ok": True, "lifetime_offset": offset})
            except Exception as e:
                self._json_resp(500, {"ok": False, "error": str(e)})

        elif self.path == "/firing/merge":
            try:
                payload   = self._read_body()
                older_id  = payload.get("older_id", "")
                newer_id  = payload.get("newer_id", "")
                older     = next((f for f in past_firings if f["id"] == older_id), None)
                newer     = next((f for f in past_firings if f["id"] == newer_id), None)
                if not older or not newer:
                    return self._json_resp(404, {"ok": False, "error": "firing not found"})
                if older.get("program") != newer.get("program"):
                    return self._json_resp(400, {"ok": False, "error": "programs differ"})
                newer_start = newer["history"][0]["temp"] if newer.get("history") else 0
                trimmed     = [p for p in older.get("history", []) if p["temp"] < newer_start]
                merged_hist = trimmed + newer.get("history", [])
                all_temps   = [p["temp"] for p in merged_hist if p.get("temp")]
                merged_peak = max(all_temps) if all_temps else older["peak"]
                if merged_hist:
                    try:
                        t0 = datetime.fromisoformat(merged_hist[0]["ts"])
                        t1 = datetime.fromisoformat(merged_hist[-1]["ts"])
                        ds = int((t1 - t0).total_seconds())
                        dh, dm = divmod(ds // 60, 60)
                        merged_dur = f"{dh}h {dm}m" if dh else f"{dm}m"
                    except Exception:
                        merged_dur = older["duration"]
                else:
                    merged_dur = older["duration"]
                older["history"] = merged_hist
                older["peak"]    = merged_peak
                older["duration"] = merged_dur
                past_firings = [f for f in past_firings if f["id"] != newer_id]
                save_past_firings(past_firings)
                self._json_resp(200, {"ok": True, "merged": older})
            except Exception as e:
                self._json_resp(500, {"ok": False, "error": str(e)})

        elif self.path == MAINT_PATH + "/update":
            import subprocess
            try:
                payload = self._read_body()
                confirm = payload.get("confirm", "")
                with state_lock:
                    is_active = state["firing_start"] is not None
                if is_active and confirm != "UPDATE":
                    return self._json_resp(200, {"ok": False, "needs_confirm": True})
                if is_active:
                    save_snapshot()
                result = subprocess.run(
                    ["git", "pull", "--ff-only"],
                    capture_output=True, text=True,
                    cwd=str(Path(__file__).parent)
                )
                if result.returncode != 0:
                    return self._json_resp(200, {"ok": False, "error": result.stderr.strip()})
                self._json_resp(200, {"ok": True, "output": result.stdout.strip()})
            except Exception as e:
                self._json_resp(500, {"ok": False, "error": str(e)})
                return
            import os as _os
            _os._exit(0)

        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
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
        elif path.startswith(MAINT_PATH + "/replacement/"):
            global maintenance_data
            repl_id      = path[len(MAINT_PATH + "/replacement/"):]
            orig         = len(maintenance_data["records"])
            maintenance_data["records"] = [r for r in maintenance_data["records"] if r["id"] != repl_id]
            if len(maintenance_data["records"]) < orig:
                save_maintenance(maintenance_data)
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

    def log_message(self, *_):
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
    if restore_snapshot():
        print("   (Active firing state restored from snapshot)")

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
                        state["last_updated"] = datetime.now().isoformat(timespec="seconds")

                        # Start firing
                        if "firing" in status.lower() and not state["firing_start"]:
                            state["firing_start"]   = datetime.now()
                            state["history"]        = []
                            state["peak_temp"]      = 0
                            state["peak_temp_time"] = None
                            state["has_peaked"]     = False

                        # Track peak
                        if state["firing_start"] and temp > state["peak_temp"]:
                            state["peak_temp"]      = temp
                            state["peak_temp_time"] = datetime.now()
                            if temp >= FIRING_PEAK_THRESHOLD:
                                state["has_peaked"] = True

                        # End firing: cooled below FIRING_END_TEMP after peaking
                        if state["has_peaked"] and temp <= FIRING_END_TEMP and state["firing_start"]:
                            fs   = state["firing_start"]
                            secs = int((datetime.now() - fs).total_seconds())
                            h, m = divmod(secs // 60, 60)
                            ptt  = state["peak_temp_time"]
                            if ptt:
                                ps = int((ptt - fs).total_seconds())
                                ph, pm = divmod(ps // 60, 60)
                                dtp = f"{ph}h {pm}m" if ph else f"{pm}m"
                            else:
                                dtp = "--"
                            counted = state["peak_temp"] >= MIN_PEAK_TO_COUNT
                            rec = {
                                "id":               fs.strftime("firing_%Y%m%d_%H%M"),
                                "label":            f"{program or 'Firing'} — {fs.strftime('%d %b %Y')}",
                                "program":          program,
                                "peak":             state["peak_temp"],
                                "duration":         f"{h}h {m}m" if h else f"{m}m",
                                "duration_to_peak": dtp,
                                "date":             fs.strftime("%Y-%m-%d"),
                                "history":          list(state["history"]),
                                "counted":          counted,
                            }
                            past_firings.append(rec)
                            save_past_firings(past_firings)
                            clear_snapshot()
                            print(f"💾 Saved: {rec['label']} (peak {state['peak_temp']}°F, {'counted' if counted else 'uncounted'})")
                            state["firing_start"]   = None
                            state["has_peaked"]     = False
                            state["peak_temp_time"] = None

                        # Abandoned: went idle without reaching peak threshold
                        elif "idle" in status.lower() and state["firing_start"] and not state["has_peaked"]:
                            secs = int((datetime.now() - state["firing_start"]).total_seconds())
                            if secs >= ABANDONED_MIN_MINUTES * 60:
                                print(f"⏭️  Abandoned ({secs//60}m, peak {state['peak_temp']}°F) — not saved.")
                            state["firing_start"]   = None
                            state["peak_temp_time"] = None

                        # Append history while firing active
                        if state["firing_start"]:
                            state["history"].append({
                                "ts":   datetime.now().isoformat(timespec="seconds"),
                                "temp": temp,
                                "z1":   z1,
                                "z3":   z3,
                            })
                            if len(state["history"]) > 1500:
                                state["history"] = state["history"][-1500:]

                    save_snapshot()

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

if __name__ == "__main__":
    main()