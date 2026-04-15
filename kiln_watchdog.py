"""
Kiln Monitor Watchdog
=====================
Keeps kiln_monitor.py running. If it crashes or exits for any reason,
this script restarts it automatically.

Usage:
  python kiln_watchdog.py
"""

import subprocess
import time
import sys
import os

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kiln_monitor.py")
RESTART_DELAY = 10  # seconds to wait before restarting after a crash

print(f"👀 Watchdog started — monitoring {SCRIPT}")

while True:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] 🚀 Starting kiln monitor…")
    try:
        proc = subprocess.run([sys.executable, SCRIPT])
        exit_code = proc.returncode
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ⚠️  Monitor exited with code {exit_code}. Restarting in {RESTART_DELAY}s…")
    except Exception as e:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] ❌ Failed to start monitor: {e}. Retrying in {RESTART_DELAY}s…")

    time.sleep(RESTART_DELAY)
