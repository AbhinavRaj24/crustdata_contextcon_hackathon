"""Settings and secrets. Keys come from the environment, .env, or the local key files (all git-ignored)."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _secret(env_name: str, filename: str) -> str | None:
    value = os.environ.get(env_name)
    if value:
        return value.strip()
    path = ROOT / filename
    if path.exists():
        return path.read_text().strip() or None
    return None


CRUSTDATA_API_KEY = _secret("CRUSTDATA_API_KEY", "crustdata_api.txt")
GEMINI_API_KEY = _secret("GEMINI_API_KEY", "gemini_api_key.txt")

DB_PATH = Path(os.environ.get("TERMSDESK_DB", ROOT / "termsdesk.db"))
POLICY_PATH = ROOT / "termsdesk" / "policy.yaml"

# REPLAY=1 answers every Crustdata call from the local cache only (demo without Wi-Fi).
REPLAY = os.environ.get("REPLAY") == "1"

# Every email to someone outside the team is delivered here instead (event rule: agents only talk to us).
SANDBOX_INBOX = os.environ.get("SANDBOX_INBOX", "sandbox@acme-supplies.test")
TEAM_DOMAINS = {d.strip().lower() for d in os.environ.get("TEAM_DOMAINS", "iitk.ac.in").split(",") if d.strip()}

SELLER_NAME = "Acme Supplies Pvt Ltd"
SELLER_EMAIL = "credit@acme-supplies.test"
CASE_CREDIT_CAP = float(os.environ.get("CASE_CREDIT_CAP", "25"))
