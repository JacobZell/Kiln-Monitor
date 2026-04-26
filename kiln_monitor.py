"""
KilnAid Status Monitor
======================
Logs into kilnaid.bartinst.com, fires Slack webhooks on status changes,
and hosts a live temperature dashboard at http://localhost:5000

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
import sys
import secrets
import hmac
import subprocess
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, quote
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from datetime import datetime, timedelta

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

# Admin password gate. Leave unset on a Pi to disable maintenance/update entirely.
KILN_ADMIN_PASS      = os.environ.get("KILN_ADMIN_PASS", "")
SESSION_TTL_SECONDS  = 60 * 60   # 1 hour

POLL_INTERVAL_SECONDS = 60
WEB_PORT              = 5000

# Slack thresholds
ABLE_TO_UNLOAD_TEMP   = 425
READY_TO_UNLOAD_TEMP  = 200

# Firing lifecycle thresholds
COOLDOWN_END_TEMP            = 300   # °F — firing ends when temp falls to this
COUNTED_FIRING_THRESHOLD     = 1000  # °F — firing must reach this to count toward maintenance
PEAK_REACHED_BUFFER          = 100   # °F — peak must exceed COOLDOWN_END_TEMP+this for cooldown end to trigger
ABANDONED_FIRING_TIMEOUT_S   = 30 * 60  # if status idle and never got hot, abandon after 30 min

# File paths
SCRIPT_DIR          = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE        = os.path.join(SCRIPT_DIR, "kiln_firings.json")
MAINTENANCE_FILE    = os.path.join(SCRIPT_DIR, "kiln_maintenance.json")
CURRENT_FIRING_FILE = os.path.join(SCRIPT_DIR, "current_firing.json")

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

# ── DEFAULT MAINTENANCE STATE ──────────────────────────────────────────────────

DEFAULT_MAINTENANCE = {
    "lifetime_firings": 0,
    "firings_since_element_change": 0,
    "element_replacement_date": None,
    "tc1_replacement_date": None,
    "tc2_replacement_date": None,
    "tc3_replacement_date": None,
    "relay_replacement_date": None,
    "relay_cycles_at_last_replacement": [0, 0, 0, 0],
}

# ── PAST FIRINGS / MAINTENANCE / CURRENT FIRING STORAGE ────────────────────────

def _load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️  Could not read {path}: {e}")
    return default

def _save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except Exception as e:
        print(f"⚠️  Could not write {path}: {e}")

def load_past_firings():
    return _load_json(HISTORY_FILE, [])

def save_past_firings(firings):
    _save_json(HISTORY_FILE, firings)

def load_maintenance():
    data = _load_json(MAINTENANCE_FILE, None)
    if data is None:
        return dict(DEFAULT_MAINTENANCE)
    merged = dict(DEFAULT_MAINTENANCE)
    merged.update(data)
    if not isinstance(merged.get("relay_cycles_at_last_replacement"), list):
        merged["relay_cycles_at_last_replacement"] = [0, 0, 0, 0]
    while len(merged["relay_cycles_at_last_replacement"]) < 4:
        merged["relay_cycles_at_last_replacement"].append(0)
    return merged

def save_maintenance(m):
    _save_json(MAINTENANCE_FILE, m)

def load_current_firing():
    return _load_json(CURRENT_FIRING_FILE, None)

def save_current_firing(snapshot):
    _save_json(CURRENT_FIRING_FILE, snapshot)

def clear_current_firing():
    try:
        if os.path.exists(CURRENT_FIRING_FILE):
            os.remove(CURRENT_FIRING_FILE)
    except Exception as e:
        print(f"⚠️  Could not clear current_firing: {e}")

# ── SHARED STATE ───────────────────────────────────────────────────────────────

state = {
    "name": "Kiln",
    "status": "Idle",
    "temp": 0,
    "z1": 0,
    "z3": 0,
    "history": [],
    "firing_start": None,         # datetime
    "peak_temp": 0,
    "peak_temp_time": None,       # datetime when peak was last set
    "counted": False,             # has this firing crossed COUNTED_FIRING_THRESHOLD?
    "last_updated": "--",
    "last_updated_iso": "",
    "program": "",
    "elapsed": "",
    "commit_sha": "",
}
state_lock = threading.Lock()
past_firings = load_past_firings()
maintenance  = load_maintenance()

# Inter-thread signals
update_in_progress = threading.Event()

# ── GIT / VERSION HELPERS ──────────────────────────────────────────────────────

def get_commit_sha():
    try:
        out = subprocess.run(
            ["git", "-C", SCRIPT_DIR, "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""

state["commit_sha"] = get_commit_sha()

# ── FIRING LIFECYCLE ───────────────────────────────────────────────────────────

def _start_firing(now, program):
    state["firing_start"]   = now
    state["history"]        = []
    state["peak_temp"]      = 0
    state["peak_temp_time"] = None
    state["counted"]        = False
    print(f"▶️  Firing started at {now.isoformat()} (program: {program or '?'})")

def _append_firing_point(now, temp, z1, z3, elapsed_label):
    point = {
        "time":     now.isoformat(),     # canonical wall-clock timestamp
        "elapsed":  elapsed_label or "", # KilnAid-reported elapsed (may freeze)
        "temp":     temp,
        "z1":       z1,
        "z3":       z3,
    }
    state["history"].append(point)
    if len(state["history"]) > 1500:
        state["history"] = state["history"][-1500:]
    if temp > state["peak_temp"]:
        state["peak_temp"]      = temp
        state["peak_temp_time"] = now
    if temp >= COUNTED_FIRING_THRESHOLD:
        state["counted"] = True

def _finalize_firing(reason="cooled"):
    """Save the active firing to past_firings, update maintenance, clear state."""
    global past_firings, maintenance

    if not state["firing_start"]:
        return

    start    = state["firing_start"]
    end      = datetime.now()
    history  = state["history"]
    peak     = state["peak_temp"]
    peak_t   = state["peak_temp_time"]
    counted  = state["counted"]
    program  = state.get("program", "") or "Firing"

    if not history:
        print(f"⚠️  Firing finalized with empty history (reason: {reason}); discarding")
        state["firing_start"]   = None
        state["peak_temp"]      = 0
        state["peak_temp_time"] = None
        state["counted"]        = False
        clear_current_firing()
        return

    total_seconds = int((end - start).total_seconds())
    h, m = divmod(total_seconds // 60, 60)
    duration_str = f"{h}h {m}m" if h else f"{m}m"

    if peak_t:
        peak_seconds = int((peak_t - start).total_seconds())
        ph, pm = divmod(peak_seconds // 60, 60)
        duration_to_peak_str = f"{ph}h {pm}m" if ph else f"{pm}m"
    else:
        duration_to_peak_str = "--"

    record = {
        "id":               start.strftime("firing_%Y%m%d_%H%M"),
        "label":            f"{program} — {start.strftime('%d %b %Y')}",
        "program":          program,
        "peak":             peak,
        "duration":         duration_str,
        "duration_to_peak": duration_to_peak_str,
        "date":             start.strftime("%Y-%m-%d"),
        "start_iso":        start.isoformat(),
        "end_iso":          end.isoformat(),
        "counted":          counted,
        "history":          history,
    }
    past_firings.append(record)
    save_past_firings(past_firings)

    if counted:
        maintenance["lifetime_firings"]            = int(maintenance.get("lifetime_firings", 0)) + 1
        maintenance["firings_since_element_change"] = int(maintenance.get("firings_since_element_change", 0)) + 1
        save_maintenance(maintenance)
        print(f"💾 Counted firing saved: {record['label']} (peak {peak}°F, {duration_str})")
    else:
        print(f"💾 Uncounted firing saved: {record['label']} (peak {peak}°F, {duration_str}) — below {COUNTED_FIRING_THRESHOLD}°F threshold")

    state["firing_start"]   = None
    state["peak_temp"]      = 0
    state["peak_temp_time"] = None
    state["counted"]        = False
    state["history"]        = []
    clear_current_firing()

def _snapshot_current_firing():
    """Persist the in-progress firing so it can be restored after a power loss."""
    if not state["firing_start"]:
        return
    snap = {
        "firing_start":   state["firing_start"].isoformat(),
        "peak_temp":      state["peak_temp"],
        "peak_temp_time": state["peak_temp_time"].isoformat() if state["peak_temp_time"] else None,
        "counted":        state["counted"],
        "program":        state.get("program", ""),
        "history":        state["history"],
    }
    save_current_firing(snap)

def restore_current_firing_if_valid(initial_status, initial_temp):
    """On startup, restore an active firing snapshot if the kiln still seems to be firing."""
    snap = load_current_firing()
    if not snap:
        return False
    try:
        firing_start = datetime.fromisoformat(snap["firing_start"])
    except Exception:
        clear_current_firing()
        return False

    # Only restore if either the kiln still reports firing, or temp is meaningfully above cooldown.
    is_firing = "firing" in (initial_status or "").lower()
    is_warm   = initial_temp > COOLDOWN_END_TEMP
    if not (is_firing or is_warm):
        print(f"ℹ️  Discarding stale firing snapshot (status={initial_status}, temp={initial_temp}°F)")
        clear_current_firing()
        return False

    state["firing_start"]   = firing_start
    state["history"]        = snap.get("history", []) or []
    state["peak_temp"]      = snap.get("peak_temp", 0) or 0
    pt = snap.get("peak_temp_time")
    state["peak_temp_time"] = datetime.fromisoformat(pt) if pt else None
    state["counted"]        = bool(snap.get("counted", False))
    print(f"🔄 Restored active firing from snapshot (started {firing_start.isoformat()}, {len(state['history'])} points)")
    return True

# ── HTML TEMPLATES (loaded from disk on each render) ──────────────────────────

TEMPLATE_DIR = Path(__file__).parent

def _read_template(name):
    return (TEMPLATE_DIR / name).read_text(encoding="utf-8")

# ── ADMIN SESSIONS ─────────────────────────────────────────────────────────────
# In-memory session store: {token: expiry_datetime}. Reset on process restart.

_session_lock = threading.Lock()
_sessions = {}   # token -> datetime
SESSION_COOKIE_NAME = "kiln_admin"

def _purge_expired_sessions(now=None):
    now = now or datetime.now()
    with _session_lock:
        expired = [t for t, exp in _sessions.items() if exp <= now]
        for t in expired:
            _sessions.pop(t, None)

def _create_session():
    token = secrets.token_urlsafe(32)
    expiry = datetime.now() + timedelta(seconds=SESSION_TTL_SECONDS)
    with _session_lock:
        _sessions[token] = expiry
    return token, expiry

def _is_valid_session(token):
    if not token:
        return False
    _purge_expired_sessions()
    with _session_lock:
        exp = _sessions.get(token)
    return bool(exp and exp > datetime.now())

def _drop_session(token):
    if not token:
        return
    with _session_lock:
        _sessions.pop(token, None)

def _check_admin_password(supplied):
    if not KILN_ADMIN_PASS:
        return False  # fail closed when admin unconfigured
    if not isinstance(supplied, str):
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), KILN_ADMIN_PASS.encode("utf-8"))


# ── RENDER ─────────────────────────────────────────────────────────────────────

def _classify_status(status_text, temp):
    sl = (status_text or "").lower()
    if "firing" in sl:
        return "firing", status_text
    if "error" in sl:
        return "error", status_text
    if "complete" in sl and temp <= ABLE_TO_UNLOAD_TEMP:
        return "ready", "Ready to unload"
    if "complete" in sl:
        return "complete", status_text
    # If no longer firing but we still have an active firing, we're in cooldown
    with state_lock:
        active = state["firing_start"] is not None
    if active and temp > COOLDOWN_END_TEMP:
        return "cooling", "Cooling"
    return "idle", status_text or "Idle"


def render_dashboard(is_admin=False):
    with state_lock:
        s = dict(state)
        history = list(s["history"])
        firing_start = s["firing_start"]
        peak_t = s["peak_temp_time"]

    status_text = s["status"]
    temp        = s["temp"]
    z1          = s.get("z1", 0)
    z3          = s.get("z3", 0)
    program     = s.get("program") or "—"

    badge_class, display_status = _classify_status(status_text, temp)

    if firing_start:
        elapsed_s = int((datetime.now() - firing_start).total_seconds())
        h, m = divmod(elapsed_s // 60, 60)
        duration = f"{h}h {m}m" if h else f"{m}m"
    else:
        duration = "--"

    if firing_start and peak_t:
        ptp = int((peak_t - firing_start).total_seconds())
        ph, pm = divmod(ptp // 60, 60)
        duration_to_peak = f"{ph}h {pm}m" if ph else f"{pm}m"
    else:
        duration_to_peak = "--"

    peak = f"{s['peak_temp']}°F" if s["peak_temp"] else "--"

    program_status = f"{program}"
    if display_status and display_status.lower() not in program.lower():
        program_status = f"{program} · {display_status}"

    live_history    = json.dumps(history)
    live_start_iso  = json.dumps(firing_start.isoformat() if firing_start else None)
    all_firings_js  = json.dumps(past_firings)

    html = _read_template("index.html")
    repls = {
        "__KILN_NAME__":       s["name"],
        "__STATUS__":          display_status,
        "__BADGE_CLASS__":     badge_class,
        "__TEMP__":            str(temp),
        "__Z1__":              str(z1),
        "__Z3__":              str(z3),
        "__PEAK__":            peak,
        "__DURATION__":        duration,
        "__DURATION_TO_PEAK__": duration_to_peak,
        "__PROGRAM_STATUS__":  program_status,
        "__UPDATED__":         s["last_updated"],
        "__UPDATED_ISO__":     s["last_updated_iso"],
        "__LIVE_HISTORY__":    live_history,
        "__LIVE_START_ISO__":  live_start_iso,
        "__ALL_FIRINGS__":     all_firings_js,
        "__IS_ADMIN__":        "true" if is_admin else "false",
    }
    for k, v in repls.items():
        html = html.replace(k, v)
    return html


def render_maintenance():
    m = maintenance
    rc = list(m.get("relay_cycles_at_last_replacement", [0, 0, 0, 0]))
    while len(rc) < 4:
        rc.append(0)
    html = _read_template("maintenance.html")
    repls = {
        "__LIFETIME__":      str(m.get("lifetime_firings", 0)),
        "__SINCE_ELEMENT__": str(m.get("firings_since_element_change", 0)),
        "__ELEMENT_DATE__":  m.get("element_replacement_date") or "",
        "__TC1_DATE__":      m.get("tc1_replacement_date") or "",
        "__TC2_DATE__":      m.get("tc2_replacement_date") or "",
        "__TC3_DATE__":      m.get("tc3_replacement_date") or "",
        "__RELAY_DATE__":    m.get("relay_replacement_date") or "",
        "__RC1__":           str(rc[0]),
        "__RC2__":           str(rc[1]),
        "__RC3__":           str(rc[2]),
        "__RC4__":           str(rc[3]),
    }
    for k, v in repls.items():
        html = html.replace(k, v)
    return html


def render_login(error="", subtitle=None):
    if subtitle is None:
        subtitle = "Sign in to access maintenance and updates."
    if not KILN_ADMIN_PASS:
        subtitle = "Admin functions are disabled — KILN_ADMIN_PASS is not set on this server."
    html = _read_template("login.html")
    repls = {
        "__SUBTITLE__": subtitle,
        "__ERROR__":    error,
    }
    for k, v in repls.items():
        html = html.replace(k, v)
    return html

# ── HTTP HANDLER ───────────────────────────────────────────────────────────────

class KilnHandler(BaseHTTPRequestHandler):
    def _send(self, code, content_type, body_bytes, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_html(self, html, code=200, extra_headers=None):
        self._send(code, "text/html; charset=utf-8", html.encode("utf-8"), extra_headers=extra_headers)

    def _send_json(self, obj, code=200, extra_headers=None):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"), extra_headers=extra_headers)

    def _redirect(self, location, extra_headers=None):
        headers = [("Location", location)] + list(extra_headers or [])
        self._send(303, "text/plain", b"", extra_headers=headers)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    # ─── auth helpers ───────────────────────────────────────────────────────────

    def _get_session_token(self):
        raw = self.headers.get("Cookie", "")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
            morsel = jar.get(SESSION_COOKIE_NAME)
            return morsel.value if morsel else None
        except Exception:
            return None

    def _is_admin(self):
        return _is_valid_session(self._get_session_token())

    def _require_admin_html(self):
        """For HTML routes: returns True if admin, else redirects to /login and returns False."""
        if self._is_admin():
            return True
        next_path = quote(self.path, safe="")
        self._redirect(f"/login?next={next_path}")
        return False

    def _require_admin_api(self):
        """For API routes: returns True if admin, else 401 JSON and returns False."""
        if self._is_admin():
            return True
        self._send_json({"ok": False, "error": "unauthorized"}, code=401)
        return False

    @staticmethod
    def _session_cookie(token, max_age=SESSION_TTL_SECONDS):
        return ("Set-Cookie", f"{SESSION_COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}")

    @staticmethod
    def _clear_cookie():
        return ("Set-Cookie", f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")

    # ─── routes ─────────────────────────────────────────────────────────────────

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(render_dashboard(is_admin=self._is_admin()))
        elif path == "/maintenance":
            if not self._require_admin_html():
                return
            self._send_html(render_maintenance())
        elif path == "/login":
            self._send_html(render_login())
        elif path == "/logout":
            tok = self._get_session_token()
            _drop_session(tok)
            self._redirect("/", extra_headers=[self._clear_cookie()])
        elif path == "/api/state":
            with state_lock:
                snapshot = {
                    "name":   state["name"],
                    "status": state["status"],
                    "temp":   state["temp"],
                    "z1":     state.get("z1", 0),
                    "z3":     state.get("z3", 0),
                    "peak":   state["peak_temp"],
                    "last_updated_iso": state["last_updated_iso"],
                    "commit_sha": state.get("commit_sha", ""),
                    "is_admin": self._is_admin(),
                }
            self._send_json(snapshot)
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()
        try:
            if path == "/api/login":
                supplied = (data or {}).get("password", "")
                if not KILN_ADMIN_PASS:
                    self._send_json({"ok": False, "error": "Admin functions are disabled on this server."}, code=503)
                    return
                if _check_admin_password(supplied):
                    token, _ = _create_session()
                    self._send_json({"ok": True}, extra_headers=[self._session_cookie(token)])
                else:
                    self._send_json({"ok": False, "error": "Wrong password."}, code=401)
                return

            # Everything else under /api/ that mutates is admin-gated
            if path == "/api/maintenance":
                if not self._require_admin_api(): return
                self._send_json(handle_maintenance_update(data))
            elif path == "/api/firings/delete":
                if not self._require_admin_api(): return
                self._send_json(handle_firing_delete(data))
            elif path == "/api/firings/merge":
                if not self._require_admin_api(): return
                self._send_json(handle_firing_merge(data))
            elif path == "/api/update":
                if not self._require_admin_api(): return
                self._send_json(handle_update())
            else:
                self._send(404, "text/plain", b"Not found")
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=500)

    def log_message(self, *args):
        pass


class KilnServer(HTTPServer):
    """Suppress noisy stack traces from clients dropping the connection mid-response."""
    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return
        super().handle_error(request, client_address)


def run_server():
    server = KilnServer(("0.0.0.0", WEB_PORT), KilnHandler)
    print(f"🌐 Dashboard available at http://localhost:{WEB_PORT}")
    server.serve_forever()

# ── API HANDLERS ───────────────────────────────────────────────────────────────

def handle_maintenance_update(data):
    global maintenance
    if not isinstance(data, dict):
        return {"ok": False, "error": "expected object"}

    new_m = dict(maintenance)
    prev_element_date = new_m.get("element_replacement_date")

    for key in (
        "element_replacement_date",
        "tc1_replacement_date",
        "tc2_replacement_date",
        "tc3_replacement_date",
        "relay_replacement_date",
    ):
        if key in data:
            v = data[key]
            new_m[key] = v if (v is None or (isinstance(v, str) and v.strip())) else None

    if "lifetime_firings" in data:
        try:
            new_m["lifetime_firings"] = max(0, int(data["lifetime_firings"]))
        except (TypeError, ValueError):
            pass
    if "firings_since_element_change" in data:
        try:
            new_m["firings_since_element_change"] = max(0, int(data["firings_since_element_change"]))
        except (TypeError, ValueError):
            pass
    if "relay_cycles_at_last_replacement" in data:
        cycles = data["relay_cycles_at_last_replacement"]
        if isinstance(cycles, list):
            cleaned = []
            for c in cycles[:4]:
                try:
                    cleaned.append(max(0, int(c)))
                except (TypeError, ValueError):
                    cleaned.append(0)
            while len(cleaned) < 4:
                cleaned.append(0)
            new_m["relay_cycles_at_last_replacement"] = cleaned

    # Auto-reset since-element counter when element date changes to a new value
    if (
        "element_replacement_date" in data
        and new_m.get("element_replacement_date") != prev_element_date
        and "firings_since_element_change" not in data
    ):
        new_m["firings_since_element_change"] = 0

    save_maintenance(new_m)
    maintenance.clear()
    maintenance.update(new_m)
    return {"ok": True}


def handle_firing_delete(data):
    global past_firings
    fid = (data or {}).get("id")
    if not fid:
        return {"ok": False, "error": "missing id"}
    before = len(past_firings)
    past_firings = [f for f in past_firings if f.get("id") != fid]
    if len(past_firings) == before:
        return {"ok": False, "error": "firing not found"}
    save_past_firings(past_firings)
    return {"ok": True}


def _point_temp(p):
    try:
        return float(p.get("temp"))
    except (TypeError, ValueError):
        return None


def handle_firing_merge(data):
    """Merge a firing into the chronologically-previous firing with the same program.
    Older firing's points whose temp >= the newer firing's starting temp are dropped.
    Combined record uses the older firing's start metadata and the merged history."""
    global past_firings
    fid = (data or {}).get("id")
    if not fid:
        return {"ok": False, "error": "missing id"}

    # Sort chronologically by start time (or fallback to date)
    def _sort_key(f):
        return f.get("start_iso") or f.get("date") or ""

    sorted_firings = sorted(past_firings, key=_sort_key)
    idx = next((i for i, f in enumerate(sorted_firings) if f.get("id") == fid), -1)
    if idx <= 0:
        return {"ok": False, "error": "no previous firing to merge with"}

    newer = sorted_firings[idx]
    older = sorted_firings[idx - 1]
    if (newer.get("program") or "") != (older.get("program") or "") or not newer.get("program"):
        return {"ok": False, "error": "programs do not match"}

    new_history = newer.get("history", []) or []
    if not new_history:
        return {"ok": False, "error": "newer firing has no history"}

    new_start_temp = _point_temp(new_history[0])
    if new_start_temp is None:
        return {"ok": False, "error": "newer firing has no usable starting temp"}

    old_history = older.get("history", []) or []
    kept_old = [p for p in old_history if (_point_temp(p) is not None and _point_temp(p) < new_start_temp)]

    merged_history = kept_old + new_history
    peak = max((_point_temp(p) or 0 for p in merged_history), default=0)

    # Try to compute total duration if we have ISO timestamps
    duration_str = older.get("duration") or "--"
    try:
        if kept_old and kept_old[0].get("time", "").find("T") >= 0 and new_history[-1].get("time", "").find("T") >= 0:
            t0 = datetime.fromisoformat(kept_old[0]["time"])
            t1 = datetime.fromisoformat(new_history[-1]["time"])
            total = int((t1 - t0).total_seconds())
            h, m = divmod(total // 60, 60)
            duration_str = f"{h}h {m}m" if h else f"{m}m"
    except Exception:
        pass

    merged = dict(older)
    merged["history"]  = merged_history
    merged["peak"]     = int(peak) if peak else older.get("peak", 0)
    merged["duration"] = duration_str
    merged["label"]    = older.get("label") or merged.get("label")
    # Mark merged
    merged["merged_from"] = (older.get("merged_from") or []) + [newer.get("id")]

    # Replace older with merged, drop newer
    new_list = []
    for f in past_firings:
        if f.get("id") == older.get("id"):
            new_list.append(merged)
        elif f.get("id") == newer.get("id"):
            continue
        else:
            new_list.append(f)
    past_firings = new_list
    save_past_firings(past_firings)
    return {"ok": True}


def handle_update():
    """Trigger git pull and exit; the watchdog restarts us."""
    if update_in_progress.is_set():
        return {"ok": False, "error": "update already in progress"}
    update_in_progress.set()

    def _do_update():
        try:
            print("⬇️  Update requested — running git pull…")
            r = subprocess.run(
                ["git", "-C", SCRIPT_DIR, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )
            print(r.stdout)
            if r.returncode != 0:
                print(f"❌ git pull failed: {r.stderr}")
            else:
                print("✅ git pull complete — exiting for watchdog restart")
        except Exception as e:
            print(f"❌ Update error: {e}")
        finally:
            # Snapshot any active firing so we can resume after restart
            with state_lock:
                _snapshot_current_firing()
            time.sleep(1)
            os._exit(0)

    threading.Thread(target=_do_update, daemon=True).start()
    return {"ok": True, "message": "update started"}

# ── SLACK ──────────────────────────────────────────────────────────────────────

def send_slack_message(payload, webhook_url):
    if not webhook_url:
        print(f"📨 (slack disabled) {payload}")
        return
    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200:
            print(f"📨 Slack notified: {payload}")
        else:
            print(f"⚠️  Slack webhook returned {resp.status_code}: {resp.text}")
    except Exception as e:
        print(f"⚠️  Slack post failed: {e}")

def notify(payload, members=False, leadership=False):
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

    # Status — scan page text for known keywords (robust to HTML structure changes).
    # Strip the "Elapsed Firing Time" label first so it doesn't always trigger "firing".
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

    zone2_el = page.query_selector("ion-text.kiln-temp-large")
    zone2_str = (zone2_el.inner_text() or "0").replace("°F", "").replace("°C", "").replace("\xa0", "").strip() if zone2_el else "0"
    try:
        zone2 = int(zone2_str)
    except ValueError:
        zone2 = 0

    zone1, zone3 = 0, 0
    for header in page.query_selector_all("ion-card-header"):
        zone_label_el = header.query_selector("ion-text:not(.tempLabel)")
        temp_label_el = header.query_selector("ion-text.tempLabel")
        if not zone_label_el or not temp_label_el:
            continue
        zone_label = (zone_label_el.inner_text() or "").strip()
        temp_val = (temp_label_el.inner_text() or "0").replace("°F", "").replace("°C", "").replace("\xa0", "").strip()
        try:
            t = int(temp_val)
        except ValueError:
            t = 0
        if "Zone 1" in zone_label:
            zone1 = t
        elif "Zone 3" in zone_label:
            zone3 = t

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
    print("🔥 KilnAid Monitor starting…")

    threading.Thread(target=run_server, daemon=True).start()

    chromium_args = [
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-gpu",
        "--single-process",
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=chromium_args)
        page    = browser.new_context().new_page()

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
                    now      = datetime.now()

                    print(f"[{time.strftime('%H:%M:%S')}] {name}: {status} | Z1:{z1}°F Z2:{temp}°F Z3:{z3}°F | {program}")

                    # Thermocouple alerts — only when actively firing (not during cooldown/unload)
                    if not first_run and broken and broken != prev_broken and "firing" in status.lower():
                        zone_readings = ", ".join(f"{z}: {zones[z]}°F" for z in zones)
                        notify({"KilnStatus": f"⚠️ *Thermocouple alert on {name}!* {', '.join(broken)} thermocouple may be faulty. Readings: {zone_readings}"}, leadership=True)
                        last_statuses[f"{name}_broken"] = broken
                    elif not first_run and not broken and prev_broken:
                        last_statuses[f"{name}_broken"] = []

                    # Update shared state + drive firing lifecycle
                    is_firing_status = "firing" in status.lower()
                    with state_lock:
                        state["name"]             = name
                        state["status"]           = status
                        state["temp"]             = temp
                        state["z1"]               = z1
                        state["z3"]               = z3
                        state["program"]          = program
                        state["elapsed"]          = elapsed
                        state["last_updated"]     = now.strftime("%d %b %Y, %H:%M:%S")
                        state["last_updated_iso"] = now.astimezone().isoformat()

                        # On first run, attempt to restore an in-progress firing snapshot
                        if first_run:
                            restore_current_firing_if_valid(status, temp)

                        # Start firing if status flipped to firing and no active firing
                        if is_firing_status and not state["firing_start"]:
                            _start_firing(now, program)

                        # Append a point if we have an active firing
                        if state["firing_start"]:
                            _append_firing_point(now, temp, z1, z3, elapsed)
                            _snapshot_current_firing()

                            # End-of-firing checks
                            peak = state["peak_temp"]
                            age_s = (now - state["firing_start"]).total_seconds()

                            ended = False
                            # Normal cooldown end: peak got meaningfully hot, now back at/below cooldown threshold
                            if peak > COOLDOWN_END_TEMP + PEAK_REACHED_BUFFER and temp <= COOLDOWN_END_TEMP:
                                ended = True
                                end_reason = "cooled below threshold"
                            # Abandoned: never really got hot, status idle, and 30+ min elapsed
                            elif (
                                peak <= COOLDOWN_END_TEMP + PEAK_REACHED_BUFFER
                                and age_s >= ABANDONED_FIRING_TIMEOUT_S
                                and "idle" in status.lower()
                            ):
                                ended = True
                                end_reason = "abandoned (never reached temp)"

                            if ended:
                                _finalize_firing(reason=end_reason)

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
                        elif is_able:
                            notify({"KilnStatus": f"🏺 *{name} is able to be unloaded!* The {program or 'firing'} has finished and cooled to {temp_str} — safe to open and unload."}, leadership=True)
                        elif is_ready:
                            notify({"KilnStatus": f"🏺 *{name} is ready to unload!* The {program or 'firing'} has finished and cooled to {temp_str} — safe to open and unload."}, members=True)
                        elif is_complete:
                            notify({"KilnStatus": f"✅ *{name} firing complete{prog}!* Reached target temperature. Currently cooling at {temp_str}."}, leadership=True)
                        elif "idle" in status.lower() and prev and "complete" in prev.lower():
                            notify({"KilnStatus": f"💤 *{name} has been unloaded and is now idle.* Current temp: {temp_str}"}, members=True)
                        elif "error" in status.lower():
                            notify({"KilnStatus": f"🚨 *{name} has an error{prog}!* The kiln has reported an error and may need attention. Current temp: {temp_str}"}, leadership=True)
                        elif "not connected" in status.lower():
                            notify({"KilnStatus": f"⚠️ *{name} is not connected!* The monitoring system cannot communicate with the kiln."}, leadership=True)
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
                browser = p.chromium.launch(headless=True, args=chromium_args)
                page = browser.new_context().new_page()
                try:
                    login(page)
                except Exception as e:
                    print(f"Re-login after restart failed: {e}")
                browser_start = time.time()
                # Do not reset first_run — the restored snapshot has already been processed if applicable

            time.sleep(POLL_INTERVAL_SECONDS)

        browser.close()


if __name__ == "__main__":
    main()
