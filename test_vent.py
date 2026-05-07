"""
Quick test for the IFTTT kiln vent email trigger.

Usage:
  python test_vent.py on
  python test_vent.py off
"""

import os
import sys
import smtplib
from email.message import EmailMessage
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


def send_vent_email(turn_on: bool):
    gmail_email        = os.environ.get("GMAIL_EMAIL")
    gmail_app_password = os.environ.get("GMAIL_APP_PASSWORD")

    if not gmail_email or not gmail_app_password:
        print("❌ GMAIL_EMAIL or GMAIL_APP_PASSWORD not set in .env")
        sys.exit(1)

    tag = "#TurnOnKilnVent" if turn_on else "#TurnOffKilnVent"
    print(f"Sending {tag} from {gmail_email} ...")

    msg = EmailMessage()
    msg["From"]    = gmail_email
    msg["To"]      = "trigger@applet.ifttt.com"
    msg["Subject"] = tag
    msg.set_content(tag)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(gmail_email, gmail_app_password)
        smtp.send_message(msg)

    print(f"✅ Done — {tag} sent.")


if __name__ == "__main__":
    load_env()

    if len(sys.argv) != 2 or sys.argv[1].lower() not in ("on", "off"):
        print("Usage: python test_vent.py on|off")
        sys.exit(1)

    send_vent_email(sys.argv[1].lower() == "on")
