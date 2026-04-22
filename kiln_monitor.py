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
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from datetime import datetime

# ── CONFIG ─────────────────────────────────────────────────────────────────────

KILN_EMAIL    = os.environ.get("KILN_EMAIL", "")
KILN_PASSWORD = os.environ.get("KILN_PASSWORD", "")

SLACK_MEMBERS_URL    = os.environ.get("SLACK_MEMBERS_URL", "")
SLACK_LEADERSHIP_URL = os.environ.get("SLACK_LEADERSHIP_URL", "")

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

EXAMPLE_FIRINGS = [
    {
        "id": "example_1",
        "label": "Slow Bisque ^05 — 12 Mar 2025",
        "program": "Slow Bisque ^05",
        "peak": 1888,
        "duration": "14h 22m",
        "duration_to_peak": "8h 30m",
        "date": "2025-03-12",
        "history": [{"time": f"{i}m", "temp": t} for i, t in enumerate(
            [72,80,95,115,140,170,205,245,290,340,395,455,520,590,665,740,820,900,985,1070,
             1155,1245,1335,1425,1510,1590,1665,1735,1800,1850,1880,1888,1888,1875,1850,1815,
             1770,1715,1650,1580,1505,1425,1345,1265,1185,1110,1035,965,900,840,785,730,680,
             635,592,552,515,480,448,418,390,365,342,320,300,282,265,250,236,223,211,200,190,
             181,172,165,158,152,146,141,136,132,128,124,121]
        )],
    },
]

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

# ── HTML ───────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Kiln Monitor</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<script>
  (function(){
    try {
      var pref = localStorage.getItem('kilnTheme') || 'system';
      var dark = pref === 'dark' || (pref === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
      if (dark) document.documentElement.classList.add('dark');
    } catch(e) {}
  })();
</script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: system-ui, -apple-system, sans-serif; -webkit-tap-highlight-color: transparent; }
  html, body { height: 100%; }
  body { background: #f5f5f5; color: #111; display: flex; flex-direction: column; overflow: hidden; }
  header { padding: .5rem 1rem; display: flex; align-items: center; justify-content: space-between; gap: .5rem; flex-shrink: 0; border-bottom: 0.5px solid #e0e0e0; background: #fff; }
  .header-left { display: flex; align-items: center; gap: .5rem; min-width: 0; }
  .header-right { display: flex; align-items: center; gap: .5rem; flex-shrink: 0; }
  header h1 { font-size: .95rem; font-weight: 500; color: #444; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badge { display: inline-block; font-size: 11px; padding: 3px 9px; border-radius: 5px; white-space: nowrap; }
  .badge.idle     { background: #f0f0f0; color: #666; }
  .badge.firing   { background: #FFF3E0; color: #E65100; }
  .badge.cooling  { background: #E1F5FE; color: #0277BD; }
  .badge.complete { background: #E8F5E9; color: #2E7D32; }
  .badge.ready    { background: #E3F2FD; color: #1565C0; }
  .badge.error    { background: #FFEBEE; color: #C62828; }
  .updated { font-size: 11px; color: #888; white-space: nowrap; }
  .icon-btn { background: none; border: 1px solid #ddd; color: #555; font-size: 12px; padding: 4px 8px; border-radius: 5px; cursor: pointer; line-height: 1; }
  .icon-btn:hover { background: #f0f0f0; }
  .icon-btn:active { background: #e8e8e8; }
  .nav-link { font-size: 12px; color: #1565C0; text-decoration: none; padding: 4px 6px; }
  .nav-link:hover { text-decoration: underline; }
  .main { display: grid; grid-template-columns: 220px 1fr 220px; flex: 1; overflow: hidden; min-height: 0; }
  .info-col { background: #fff; border-right: 0.5px solid #e0e0e0; padding: .65rem .75rem; overflow-y: auto; display: flex; flex-direction: column; gap: .55rem; }
  .info-card { background: #f7f7f7; border-radius: 8px; padding: .45rem .65rem; }
  .info-card .label { font-size: 9px; color: #888; margin-bottom: 3px; text-transform: uppercase; letter-spacing: .04em; }
  .info-card .value { font-size: 16px; font-weight: 500; color: #222; word-break: break-word; }
  .info-card.primary { background: #FFF3E0; }
  .info-card.primary .label { color: #E65100; }
  .info-card.primary .value { font-size: 28px; font-weight: 600; color: #E65100; }
  .chart-area { display: flex; flex-direction: column; padding: .75rem; min-height: 0; min-width: 0; }
  .chart-title { font-size: 10px; color: #888; margin-bottom: .4rem; }
  .chart-wrap { flex: 1; position: relative; min-height: 0; }
  canvas { display: block; }
  .sidebar { border-left: 0.5px solid #e0e0e0; background: #fff; display: flex; flex-direction: column; overflow: hidden; }
  .sidebar-head { padding: .55rem .75rem .4rem; border-bottom: 0.5px solid #f0f0f0; flex-shrink: 0; display: flex; align-items: center; justify-content: space-between; gap: .35rem; }
  .sidebar-head h2 { font-size: .75rem; font-weight: 500; color: #888; }
  .firing-list { overflow-y: auto; flex: 1; -webkit-overflow-scrolling: touch; }
  .firing-item { padding: .55rem .75rem; border-bottom: 0.5px solid #f0f0f0; cursor: pointer; transition: background .15s; }
  .firing-item:hover { background: #fafafa; }
  .firing-item.active { background: #EEF4FF; }
  .firing-item .fi-label { font-size: 12px; font-weight: 500; color: #333; }
  .firing-item .fi-meta { font-size: 10px; color: #999; margin-top: 2px; }
  .firing-item .fi-badge { font-size: 9px; background: #f0f0f0; color: #555; padding: 1px 5px; border-radius: 4px; display: inline-block; margin-top: 3px; }
  .firing-item .fi-actions { display: none; gap: .35rem; margin-top: 6px; }
  body.editing .firing-item .fi-actions { display: flex; }
  .fi-action { font-size: 10px; padding: 3px 7px; border-radius: 4px; border: none; cursor: pointer; }
  .fi-action.delete { background: #FFEBEE; color: #C62828; }
  .fi-action.merge  { background: #E3F2FD; color: #1565C0; }
  .fi-action:disabled { opacity: .4; cursor: not-allowed; }
  @media (max-width: 800px) {
    body { overflow: auto; }
    .main { display: flex; flex-direction: column; height: auto; overflow: visible; }
    .info-col { border-right: none; border-bottom: 0.5px solid #e0e0e0; flex-direction: row; flex-wrap: wrap; gap: .35rem; padding: .5rem; }
    .info-col .info-card { flex: 1 1 calc(50% - .35rem); min-width: 0; }
    .info-col .info-card.primary { flex: 1 1 100%; }
    .chart-area { padding: .5rem; height: 60vw; min-height: 280px; }
    .sidebar { border-left: none; border-top: 0.5px solid #e0e0e0; max-height: 320px; }
  }
  html.dark body { background: #1a1a1a; color: #eee; }
  html.dark header, html.dark .info-col, html.dark .sidebar { background: #222; border-color: #333; }
  html.dark .info-card { background: #2a2a2a; }
  html.dark .info-card .value { color: #ddd; }
  html.dark .info-card.primary { background: #3E2800; }
  html.dark .info-card.primary .label { color: #FFB74D; }
  html.dark .info-card.primary .value { color: #FFB74D; }
  html.dark .badge.idle { background: #333; color: #aaa; }
  html.dark .badge.firing { background: #3E2800; color: #FFB74D; }
  html.dark .badge.cooling { background: #0D2A45; color: #64B5F6; }
  html.dark .badge.complete { background: #1B3A1F; color: #81C784; }
  html.dark .badge.ready { background: #0D2A45; color: #64B5F6; }
  html.dark .badge.error { background: #3B0000; color: #EF9A9A; }
  html.dark .updated { color: #888; }
  html.dark .icon-btn { background: #2a2a2a; border-color: #444; color: #ccc; }
  html.dark .icon-btn:hover { background: #333; }
  html.dark .nav-link { color: #64B5F6; }
  html.dark .sidebar-head h2 { color: #666; border-color: #2a2a2a; }
  html.dark .firing-item { border-color: #2a2a2a; }
  html.dark .firing-item:hover { background: #2a2a2a; }
  html.dark .firing-item.active { background: #0D2A45; }
  html.dark .firing-item .fi-label { color: #ddd; }
  html.dark .firing-item .fi-meta { color: #666; }
  html.dark .firing-item .fi-badge { background: #333; color: #aaa; }
  html.dark .chart-title { color: #666; }
  html.dark .fi-action.delete { background: #3B0000; color: #EF9A9A; }
  html.dark .fi-action.merge  { background: #0D2A45; color: #64B5F6; }
</style>
</head>
<body>
<header>
  <div class="header-left">
    <h1 id="kilnName">__KILN_NAME__</h1>
    <span class="badge __BADGE_CLASS__" id="statusBadge">__STATUS__</span>
  </div>
  <div class="header-right">
    <span class="updated" id="updatedAt" data-iso="__UPDATED_ISO__">__UPDATED__</span>
    <button class="icon-btn" id="refreshBtn" title="Refresh">⟳</button>
    <button class="icon-btn" id="themeBtn" title="Theme">◐</button>
    <a class="nav-link" href="/maintenance">Maintenance</a>
    <button class="icon-btn" id="updateBtn" title="Pull updates and restart">Update</button>
  </div>
</header>
<div class="main">
  <div class="info-col">
    <div class="info-card"><div class="label">Program / Status</div><div class="value">__PROGRAM_STATUS__</div></div>
    <div class="info-card"><div class="label">Zone 1</div><div class="value">__Z1__°F</div></div>
    <div class="info-card primary"><div class="label">Zone 2 (primary)</div><div class="value">__TEMP__°F</div></div>
    <div class="info-card"><div class="label">Zone 3</div><div class="value">__Z3__°F</div></div>
    <div class="info-card"><div class="label">Total Duration</div><div class="value">__DURATION__</div></div>
    <div class="info-card"><div class="label">Peak Temperature</div><div class="value">__PEAK__</div></div>
    <div class="info-card"><div class="label">Time to Peak</div><div class="value">__DURATION_TO_PEAK__</div></div>
  </div>
  <div class="chart-area">
    <div class="chart-title">Temperature (°F) over time</div>
    <div class="chart-wrap">
      <canvas id="kilnChart" role="img" aria-label="Kiln temperature over time"></canvas>
    </div>
  </div>
  <div class="sidebar">
    <div class="sidebar-head">
      <h2>Past firings</h2>
      <button class="icon-btn" id="editBtn" title="Edit past firings">Edit</button>
    </div>
    <div class="firing-list" id="firingList"></div>
  </div>
</div>
<script>
const allFirings = __ALL_FIRINGS__;
const liveFiring = { id: 'live', label: 'Live', history: __LIVE_HISTORY__, start_iso: __LIVE_START_ISO__ };
let activeId = 'live';
let editing = false;

function fmtClock(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  } catch(e) { return iso; }
}
function fmtElapsed(ms) {
  const total = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}
function fmtFullStamp(iso) {
  if (!iso) return '--';
  try {
    const d = new Date(iso);
    const date = d.toLocaleDateString('en-US', { month: 'short', day: '2-digit', year: 'numeric' });
    const time = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true, timeZoneName: 'short' });
    return date.replace(',', '') + ', ' + time;
  } catch(e) { return iso; }
}

function getFiring(id) {
  if (id === 'live') return liveFiring;
  return allFirings.find(x => x.id === id) || { history: [], start_iso: null };
}
function pointTimeMs(p, idx) {
  if (p.time && p.time.includes && p.time.includes('T')) {
    const t = Date.parse(p.time);
    if (!isNaN(t)) return t;
  }
  return idx * 60000;
}
function buildChart(id) {
  const firing = getFiring(id);
  const history = firing.history || [];
  const startMs = firing.start_iso ? Date.parse(firing.start_iso) : (history.length ? pointTimeMs(history[0], 0) : Date.now());

  const labels = history.map((p, i) => {
    if (p.time && p.time.includes && p.time.includes('T')) return fmtClock(p.time);
    return p.time || '';
  });
  const z2 = history.map(p => p.temp);
  const z1 = history.map(p => (p.z1 != null ? p.z1 : null));
  const z3 = history.map(p => (p.z3 != null ? p.z3 : null));
  const elapsedMs = history.map((p, i) => pointTimeMs(p, i) - startMs);

  if (window._kilnChart) window._kilnChart.destroy();
  window._kilnChart = new Chart(document.getElementById('kilnChart'), {
    type: 'line',
    data: {
      labels,
      datasets: [
        { label: 'Zone 1', data: z1, borderColor: 'rgba(120,120,120,0.45)', backgroundColor: 'transparent', borderWidth: 1, pointRadius: 0, pointHoverRadius: 3, tension: 0, spanGaps: true, order: 2 },
        { label: 'Zone 3', data: z3, borderColor: 'rgba(60,140,200,0.45)',  backgroundColor: 'transparent', borderWidth: 1, pointRadius: 0, pointHoverRadius: 3, tension: 0, spanGaps: true, order: 2 },
        { label: 'Zone 2', data: z2, borderColor: '#D85A30', backgroundColor: 'rgba(216,90,48,0.10)', fill: true, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4, tension: 0, order: 1 },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: true, position: 'top', labels: { font: { size: 10 }, boxWidth: 12 } },
        tooltip: {
          callbacks: {
            title: items => {
              if (!items.length) return '';
              const i = items[0].dataIndex;
              const wall = labels[i];
              const elapsed = fmtElapsed(elapsedMs[i]);
              return wall + '  (' + elapsed + ' into firing)';
            },
            label: c => c.dataset.label + ': ' + Math.round(c.parsed.y) + '°F',
          },
        },
      },
      scales: {
        x: { ticks: { maxTicksLimit: 8, font: { size: 9 }, color: '#888', autoSkip: true, maxRotation: 0 }, grid: { color: 'rgba(128,128,128,0.12)' }, title: { display: true, text: 'Time of day', font: { size: 10 }, color: '#888' } },
        y: { min: 0, max: 2400, ticks: { callback: v => v + '°F', font: { size: 9 }, color: '#888', stepSize: 400 }, grid: { color: 'rgba(128,128,128,0.12)' }, title: { display: true, text: 'Temperature (°F)', font: { size: 10 }, color: '#888' } },
      },
    },
  });
}
function canMergeWithPrevious(idx, sortedFirings) {
  if (idx <= 0) return false;
  const cur = sortedFirings[idx];
  const prev = sortedFirings[idx - 1];
  if (!cur.program || !prev.program) return false;
  return cur.program === prev.program;
}
function renderList() {
  const list = document.getElementById('firingList');
  list.innerHTML = '';
  const liveEl = document.createElement('div');
  liveEl.className = 'firing-item' + (activeId === 'live' ? ' active' : '');
  liveEl.innerHTML = '<div class="fi-label">🔴 Live</div><div class="fi-meta">Current firing</div>';
  liveEl.onclick = () => { activeId = 'live'; renderList(); buildChart('live'); };
  list.appendChild(liveEl);

  // Newest first
  const sorted = [...allFirings].slice().reverse();
  sorted.forEach((f, idx) => {
    const el = document.createElement('div');
    el.className = 'firing-item' + (activeId === f.id ? ' active' : '');
    const peak = f.peak != null ? f.peak + '°F' : '--';
    const dur  = f.duration || '--';
    const tp   = f.duration_to_peak ? ' · to peak: ' + f.duration_to_peak : '';
    el.innerHTML =
      '<div class="fi-label">' + (f.label || f.id) + '</div>' +
      '<div class="fi-meta">Peak: ' + peak + ' · ' + dur + tp + '</div>' +
      '<span class="fi-badge">' + (f.program || '—') + '</span>' +
      '<div class="fi-actions">' +
        '<button class="fi-action delete" data-id="' + f.id + '">Delete</button>' +
        '<button class="fi-action merge"  data-id="' + f.id + '" ' + (canMergeWithPrevious(idx, sorted) ? '' : 'disabled') + '>Merge ↑</button>' +
      '</div>';
    el.onclick = (ev) => {
      if (ev.target && ev.target.classList && ev.target.classList.contains('fi-action')) return;
      activeId = f.id; renderList(); buildChart(f.id);
    };
    list.appendChild(el);
  });

  // Wire up edit-mode actions
  list.querySelectorAll('.fi-action.delete').forEach(btn => {
    btn.onclick = async () => {
      const id = btn.getAttribute('data-id');
      if (!confirm('Delete this firing? This cannot be undone.')) return;
      const r = await fetch('/api/firings/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ id }) });
      if (r.ok) location.reload();
      else alert('Delete failed.');
    };
  });
  list.querySelectorAll('.fi-action.merge').forEach(btn => {
    btn.onclick = async () => {
      const id = btn.getAttribute('data-id');
      if (!confirm('Merge this firing into the previous one with the same program?\n\nData from the older firing above the newer firing\'s starting temp will be discarded.')) return;
      const r = await fetch('/api/firings/merge', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ id }) });
      if (r.ok) location.reload();
      else alert('Merge failed.');
    };
  });
}

function applyTheme(pref) {
  if (pref === 'dark' || (pref === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)) {
    document.documentElement.classList.add('dark');
  } else {
    document.documentElement.classList.remove('dark');
  }
}
function cycleTheme() {
  const cur = localStorage.getItem('kilnTheme') || 'system';
  const next = cur === 'system' ? 'light' : (cur === 'light' ? 'dark' : 'system');
  localStorage.setItem('kilnTheme', next);
  applyTheme(next);
  document.getElementById('themeBtn').title = 'Theme: ' + next;
}

// Init
document.getElementById('updatedAt').textContent = fmtFullStamp(document.getElementById('updatedAt').getAttribute('data-iso')) || document.getElementById('updatedAt').textContent;
document.getElementById('themeBtn').title = 'Theme: ' + (localStorage.getItem('kilnTheme') || 'system');
document.getElementById('themeBtn').onclick = cycleTheme;
document.getElementById('refreshBtn').onclick = () => location.reload();
document.getElementById('editBtn').onclick = () => {
  editing = !editing;
  document.body.classList.toggle('editing', editing);
  document.getElementById('editBtn').textContent = editing ? 'Done' : 'Edit';
};
document.getElementById('updateBtn').onclick = async () => {
  const status = document.getElementById('statusBadge').textContent.toLowerCase();
  let warn = 'Pull latest code from git and restart the monitor?';
  let confirmText = 'update';
  if (status.includes('firing') || status.includes('cooling')) {
    warn = '⚠️  A firing is currently active (' + status + ').\n\nUpdating will restart the monitor and may briefly interrupt logging. The current firing\'s in-progress data is snapshotted and will be restored after restart.\n\nType UPDATE to confirm:';
    const typed = prompt(warn, '');
    if (typed !== 'UPDATE') return;
  } else {
    if (!confirm(warn)) return;
  }
  try {
    const r = await fetch('/api/update', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}) });
    const j = await r.json();
    if (j.ok) {
      alert('Update started. The monitor will restart shortly. Page will reload in 20s.');
      setTimeout(() => location.reload(), 20000);
    } else {
      alert('Update failed: ' + (j.error || 'unknown'));
    }
  } catch(e) {
    alert('Update request failed: ' + e);
  }
};

renderList();
window.addEventListener('load', () => buildChart('live'));

// Auto-refresh every 60s, but pause while editing
setInterval(() => { if (!editing) location.reload(); }, 60000);
</script>
</body>
</html>"""


MAINTENANCE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kiln Maintenance</title>
<script>
  (function(){
    try {
      var pref = localStorage.getItem('kilnTheme') || 'system';
      var dark = pref === 'dark' || (pref === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
      if (dark) document.documentElement.classList.add('dark');
    } catch(e) {}
  })();
</script>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: system-ui, -apple-system, sans-serif; }
  body { background: #f5f5f5; color: #111; min-height: 100vh; padding: 1rem; }
  header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1rem; }
  header h1 { font-size: 1.1rem; font-weight: 500; color: #444; }
  .nav-link { font-size: 13px; color: #1565C0; text-decoration: none; }
  .nav-link:hover { text-decoration: underline; }
  .container { max-width: 720px; margin: 0 auto; }
  .panel { background: #fff; border-radius: 10px; padding: 1rem 1.25rem; margin-bottom: 1rem; border: 0.5px solid #e0e0e0; }
  .panel h2 { font-size: .85rem; color: #888; text-transform: uppercase; letter-spacing: .04em; margin-bottom: .8rem; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: .8rem 1rem; align-items: end; }
  .field { display: flex; flex-direction: column; gap: 4px; }
  .field label { font-size: 11px; color: #888; }
  .field input[type=date], .field input[type=number] { font-size: 14px; padding: .45rem .55rem; border: 1px solid #ccc; border-radius: 6px; background: #fff; color: #111; }
  .stat { font-size: 24px; font-weight: 600; color: #222; }
  .stat-row { display: flex; align-items: baseline; gap: .8rem; }
  .stat-row .label { font-size: 11px; color: #888; text-transform: uppercase; }
  .button-row { display: flex; gap: .5rem; margin-top: 1rem; flex-wrap: wrap; }
  button { font-size: 13px; padding: .5rem .9rem; border-radius: 6px; border: 1px solid #ccc; background: #fff; cursor: pointer; }
  button.primary { background: #1565C0; color: #fff; border-color: #1565C0; }
  button.danger  { background: #fff; color: #C62828; border-color: #FFCDD2; }
  button:hover { filter: brightness(0.97); }
  .relay-cycles { display: grid; grid-template-columns: repeat(4, 1fr); gap: .5rem; margin-top: .5rem; }
  .msg { font-size: 12px; color: #2E7D32; margin-top: .5rem; min-height: 1em; }
  .msg.error { color: #C62828; }
  @media (max-width: 600px) { .row { grid-template-columns: 1fr; } .relay-cycles { grid-template-columns: repeat(2, 1fr); } }
  html.dark body { background: #1a1a1a; color: #eee; }
  html.dark .panel { background: #222; border-color: #333; }
  html.dark .panel h2 { color: #888; }
  html.dark .field input { background: #2a2a2a; color: #eee; border-color: #444; }
  html.dark .stat { color: #ddd; }
  html.dark button { background: #2a2a2a; color: #ddd; border-color: #444; }
  html.dark button.primary { background: #1565C0; color: #fff; border-color: #1565C0; }
  html.dark button.danger { background: #2a2a2a; color: #EF9A9A; border-color: #5a2a2a; }
  html.dark .nav-link { color: #64B5F6; }
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>Kiln Maintenance</h1>
    <a class="nav-link" href="/">← Dashboard</a>
  </header>

  <div class="panel">
    <h2>Firing Counters</h2>
    <div class="stat-row" style="margin-bottom: .6rem;"><span class="stat" id="lifetime">__LIFETIME__</span><span class="label">Lifetime firings</span></div>
    <div class="stat-row"><span class="stat" id="sinceElement">__SINCE_ELEMENT__</span><span class="label">Firings since last element change</span></div>
    <div class="button-row">
      <button class="danger" id="resetLifetimeBtn">Adjust lifetime…</button>
      <button class="danger" id="resetSinceBtn">Reset since-element to 0</button>
    </div>
    <div class="msg" id="counterMsg"></div>
  </div>

  <div class="panel">
    <h2>Replacement Dates</h2>
    <form id="datesForm">
      <div class="row">
        <div class="field"><label>Elements</label><input type="date" name="element_replacement_date" value="__ELEMENT_DATE__"></div>
        <div class="field"><label>Thermocouple 1</label><input type="date" name="tc1_replacement_date" value="__TC1_DATE__"></div>
        <div class="field"><label>Thermocouple 2</label><input type="date" name="tc2_replacement_date" value="__TC2_DATE__"></div>
        <div class="field"><label>Thermocouple 3</label><input type="date" name="tc3_replacement_date" value="__TC3_DATE__"></div>
        <div class="field"><label>Relays (all 4)</label><input type="date" name="relay_replacement_date" value="__RELAY_DATE__"></div>
      </div>
      <div style="margin-top: 1rem;">
        <label style="font-size: 11px; color: #888;">Relay cycle counts at last replacement (1 / 2 / 3 / 4)</label>
        <div class="relay-cycles">
          <input type="number" min="0" name="relay_cycle_1" value="__RC1__">
          <input type="number" min="0" name="relay_cycle_2" value="__RC2__">
          <input type="number" min="0" name="relay_cycle_3" value="__RC3__">
          <input type="number" min="0" name="relay_cycle_4" value="__RC4__">
        </div>
        <div style="font-size: 11px; color: #888; margin-top: 4px;">Re-enter cycle counts whenever you update the relay replacement date.</div>
      </div>
      <div class="button-row">
        <button type="submit" class="primary">Save</button>
      </div>
      <div class="msg" id="datesMsg"></div>
    </form>
    <div style="font-size: 11px; color: #888; margin-top: .8rem;">Saving a new element replacement date will reset the firings-since-element-change counter to 0.</div>
  </div>
</div>
<script>
async function postJson(url, body) {
  const r = await fetch(url, { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
  return r.json();
}
document.getElementById('datesForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const fd = new FormData(ev.target);
  const payload = {
    element_replacement_date: fd.get('element_replacement_date') || null,
    tc1_replacement_date:     fd.get('tc1_replacement_date') || null,
    tc2_replacement_date:     fd.get('tc2_replacement_date') || null,
    tc3_replacement_date:     fd.get('tc3_replacement_date') || null,
    relay_replacement_date:   fd.get('relay_replacement_date') || null,
    relay_cycles_at_last_replacement: [
      parseInt(fd.get('relay_cycle_1') || '0', 10),
      parseInt(fd.get('relay_cycle_2') || '0', 10),
      parseInt(fd.get('relay_cycle_3') || '0', 10),
      parseInt(fd.get('relay_cycle_4') || '0', 10),
    ],
  };
  const msg = document.getElementById('datesMsg');
  msg.classList.remove('error'); msg.textContent = 'Saving…';
  const j = await postJson('/api/maintenance', payload);
  if (j.ok) { msg.textContent = 'Saved.'; setTimeout(() => location.reload(), 600); }
  else { msg.classList.add('error'); msg.textContent = j.error || 'Save failed.'; }
});
document.getElementById('resetSinceBtn').onclick = async () => {
  if (!confirm('Reset firings-since-element-change to 0?')) return;
  const j = await postJson('/api/maintenance', { firings_since_element_change: 0 });
  if (j.ok) location.reload();
};
document.getElementById('resetLifetimeBtn').onclick = async () => {
  const cur = document.getElementById('lifetime').textContent;
  const v = prompt('Set lifetime firing count to:', cur);
  if (v === null) return;
  const n = parseInt(v, 10);
  if (isNaN(n) || n < 0) { alert('Enter a non-negative integer.'); return; }
  const j = await postJson('/api/maintenance', { lifetime_firings: n });
  if (j.ok) location.reload();
};
</script>
</body>
</html>"""


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


def render_dashboard():
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

    all_firings = EXAMPLE_FIRINGS + past_firings
    live_history    = json.dumps(history)
    live_start_iso  = json.dumps(firing_start.isoformat() if firing_start else None)
    all_firings_js  = json.dumps(all_firings)

    html = DASHBOARD_HTML
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
    }
    for k, v in repls.items():
        html = html.replace(k, v)
    return html


def render_maintenance():
    m = maintenance
    rc = list(m.get("relay_cycles_at_last_replacement", [0, 0, 0, 0]))
    while len(rc) < 4:
        rc.append(0)
    html = MAINTENANCE_HTML
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

# ── HTTP HANDLER ───────────────────────────────────────────────────────────────

class KilnHandler(BaseHTTPRequestHandler):
    def _send(self, code, content_type, body_bytes):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        self.wfile.write(body_bytes)

    def _send_html(self, html, code=200):
        self._send(code, "text/html; charset=utf-8", html.encode("utf-8"))

    def _send_json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(obj).encode("utf-8"))

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send_html(render_dashboard())
        elif path == "/maintenance":
            self._send_html(render_maintenance())
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
                }
            self._send_json(snapshot)
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        path = urlparse(self.path).path
        data = self._read_json()
        try:
            if path == "/api/maintenance":
                self._send_json(handle_maintenance_update(data))
            elif path == "/api/firings/delete":
                self._send_json(handle_firing_delete(data))
            elif path == "/api/firings/merge":
                self._send_json(handle_firing_merge(data))
            elif path == "/api/update":
                self._send_json(handle_update())
            else:
                self._send(404, "text/plain", b"Not found")
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, code=500)

    def log_message(self, *args):
        pass


def run_server():
    server = HTTPServer(("0.0.0.0", WEB_PORT), KilnHandler)
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

    status_el = page.query_selector(".status-div ion-text")
    status = " ".join((status_el.inner_text() or "").split()) if status_el else "Unknown"

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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
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
                browser = p.chromium.launch(headless=True)
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
