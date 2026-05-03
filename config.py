import os
from pathlib import Path


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

KILN_EMAIL    = os.environ.get("KILN_EMAIL", "ceramics@thebodgery.org")
KILN_PASSWORD = os.environ.get("KILN_PASSWORD", "B0dgeryCeramics!")

SLACK_MEMBERS_URL    = os.environ.get("SLACK_MEMBERS_URL", "https://hooks.slack.com/triggers/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")
SLACK_LEADERSHIP_URL = os.environ.get("SLACK_LEADERSHIP_URL", "https://hooks.slack.com/triggers/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")

POLL_INTERVAL_SECONDS    = 60
WEB_PORT                 = 5000
ABLE_TO_UNLOAD_TEMP      = 425
READY_TO_UNLOAD_TEMP     = 200
HISTORY_FILE             = "kiln_firings.json"
MIN_FIRING_DURATION_HOURS = 12

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
