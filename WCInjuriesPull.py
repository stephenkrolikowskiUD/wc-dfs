"""Pull current World Cup injury data from API-Football.

Writes a flat Player_Injuries tab keyed by API-Football player ID. The rest of
the WC stack treats missing rows as healthy.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

import gspread
import requests
from google.auth import default
from google.oauth2.service_account import Credentials


SHEET_ID = "1ZijOHruRgILnyR4H_jJh3pQrU3A9PJepWMLtRf3Ie9g"
INJURY_SHEET_NAME = "Player_Injuries"
SQUAD_CACHE_PATH = "squad_cache.json"
API_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_SEASON = 2026
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3

INJURY_COLUMNS = [
    "Player_Name",
    "API_Football_ID",
    "Team",
    "Injury_Type",
    "Reason",
    "Affected_Fixture_ID",
    "Affected_Fixture_Date",
    "Status_Score",
    "Last_Updated",
    "Notes",
]

API_KEY: str | None = None


def timestamp_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_cell(value: Any) -> Any:
    return "" if value is None else value


def load_secret(name: str, prompt_text: str | None = None) -> str:
    env_val = os.environ.get(name)
    if env_val:
        print(f"🔐 Loaded {name} from environment")
        return env_val
    try:
        from google.colab import userdata

        colab_val = userdata.get(name)
        if colab_val:
            print(f"🔐 Loaded {name} from Colab userdata")
            return colab_val
    except Exception:
        pass
    import getpass

    return getpass.getpass(prompt_text or f"Paste your {name}: ")


def api_headers() -> dict[str, str]:
    global API_KEY
    if not API_KEY:
        API_KEY = load_secret("API_FOOTBALL_KEY", "🔑 Paste your API-Football Key: ")
    return {"x-apisports-key": API_KEY}


def api_get(path: str, params: dict[str, Any]) -> dict:
    url = f"{API_BASE}{path}"
    last_error: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(url, headers=api_headers(), params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < MAX_RETRIES:
                wait = 2**attempt
                print(f"   ⏳ API retry {attempt}/{MAX_RETRIES} for {path} in {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_error = exc
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise
    raise RuntimeError(f"API request failed: {path}") from last_error


def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    svc_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or os.environ.get("GSPREAD_SERVICE_ACCOUNT_JSON")
    if svc_json:
        creds = Credentials.from_service_account_info(json.loads(svc_json), scopes=scopes)
        print("✅ Google auth via service account env")
        return gspread.authorize(creds)
    try:
        from google.colab import auth as colab_auth

        colab_auth.authenticate_user()
        creds, _ = default(scopes=scopes)
        print("✅ Google auth via Colab")
        return gspread.authorize(creds)
    except Exception as e:
        raise RuntimeError("Google auth unavailable. Set GOOGLE_SERVICE_ACCOUNT_JSON or run in Colab.") from e


def safe_upload(sheet_name: str, columns: list[str], rows: list[dict]) -> None:
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows=max(len(rows) + 5, 100), cols=len(columns))
    ws.clear()
    values = [columns]
    values.extend([[clean_cell(row.get(col, "")) for col in columns] for row in rows])
    ws.update("A1", values, value_input_option="USER_ENTERED")
    print(f"✅ Wrote {len(rows)} row(s) to {sheet_name}")


def load_squad_cache() -> dict:
    if not os.path.exists(SQUAD_CACHE_PATH):
        raise RuntimeError(f"{SQUAD_CACHE_PATH} is required. Run WCFormPull.py --build-squads first.")
    with open(SQUAD_CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def status_score(injury_type: Any) -> float:
    text = str(injury_type or "").strip().lower()
    if "missing" in text or "out" in text or "suspended" in text:
        return 0.0
    if "questionable" in text or "doubtful" in text or "day" in text:
        return 0.5
    return 0.5 if text else 1.0


def flatten_injury(item: dict[str, Any], team_name: str, updated_at: str) -> dict[str, Any]:
    player = item.get("player", {}) or {}
    fixture = item.get("fixture", {}) or {}
    injury_type = item.get("type") or item.get("status") or ""
    reason = item.get("reason") or ""
    return {
        "Player_Name": player.get("name", ""),
        "API_Football_ID": player.get("id", ""),
        "Team": team_name,
        "Injury_Type": injury_type,
        "Reason": reason,
        "Affected_Fixture_ID": fixture.get("id", ""),
        "Affected_Fixture_Date": fixture.get("date", ""),
        "Status_Score": status_score(injury_type),
        "Last_Updated": updated_at,
        "Notes": "API-Football /injuries",
    }


def pull_injuries() -> list[dict[str, Any]]:
    squad_cache = load_squad_cache()
    updated_at = timestamp_utc_iso()
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    print(f"🏥 Pulling injuries for {len(squad_cache)} WC team(s)")
    for team in squad_cache.values():
        team_id = team.get("team_id")
        team_name = team.get("team_name") or ""
        if not team_id:
            continue
        payload = api_get("/injuries", {"team": team_id, "season": API_FOOTBALL_SEASON})
        response = payload.get("response", []) if isinstance(payload, dict) else []
        print(f"   {team_name}: {len(response)} record(s)")
        for item in response:
            row = flatten_injury(item, team_name, updated_at)
            key = (
                str(row.get("API_Football_ID")),
                str(row.get("Affected_Fixture_ID")),
                str(row.get("Reason")),
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
        time.sleep(0.2)
    rows.sort(key=lambda r: (r.get("Team", ""), r.get("Player_Name", ""), r.get("Affected_Fixture_Date", "")))
    return rows


def sample_rows() -> list[dict[str, Any]]:
    now = timestamp_utc_iso()
    return [
        {
            "Player_Name": "Emiliano Martinez",
            "API_Football_ID": "123",
            "Team": "Argentina",
            "Injury_Type": "Questionable",
            "Reason": "Finger Fracture",
            "Affected_Fixture_ID": "sample-fixture",
            "Affected_Fixture_Date": "2026-06-16T19:00:00Z",
            "Status_Score": 0.5,
            "Last_Updated": now,
            "Notes": "sample",
        },
        {
            "Player_Name": "Leonardo Balerdi",
            "API_Football_ID": "456",
            "Team": "Argentina",
            "Injury_Type": "Missing Fixture",
            "Reason": "Muscle Injury",
            "Affected_Fixture_ID": "sample-fixture",
            "Affected_Fixture_Date": "2026-06-16T19:00:00Z",
            "Status_Score": 0.0,
            "Last_Updated": now,
            "Notes": "sample",
        },
    ]


def run(dry_run: bool = False, sample: bool = False) -> list[dict[str, Any]]:
    rows = sample_rows() if sample else pull_injuries()
    print(f"✅ Injury rows prepared: {len(rows)}")
    if dry_run:
        print(json.dumps(rows[:10], indent=2, ensure_ascii=False))
    else:
        safe_upload(INJURY_SHEET_NAME, INJURY_COLUMNS, rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Pull World Cup injury data into Google Sheets.")
    parser.add_argument("--dry-run", action="store_true", help="Pull and print rows without writing to Sheets.")
    parser.add_argument("--sample", action="store_true", help="Use sample rows for offline smoke testing.")
    args = parser.parse_args()
    run(dry_run=args.dry_run, sample=args.sample)


if __name__ == "__main__":
    main()
