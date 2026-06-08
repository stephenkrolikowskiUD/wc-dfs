# World Cup Player Form Pull v1
#
# Pulls recent international form from API-Football and writes the Player_Form
# tab used by WCEnginev1.py and index.html.

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

import gspread
import requests
from google.auth import default
from google.oauth2.service_account import Credentials


SHEET_ID = "1ZijOHruRgILnyR4H_jJh3pQrU3A9PJepWMLtRf3Ie9g"
PICKS_SHEET_NAME = "Picks"
FORM_SHEET_NAME = "Player_Form"
API_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_LEAGUE_ID = 1
API_FOOTBALL_SEASON = 2026
FORM_SEASONS = (2025, 2024)
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
PLAYER_ID_CACHE_PATH = "player_id_cache.json"

INTL_COMPETITION_IDS = {
    1,    # World Cup
    4,    # Euro Championship
    9,    # Copa America
    10,   # International Friendlies
    13,   # CONCACAF Gold Cup
    21,   # Confed Cup
    22,   # CONCACAF Nations League
    29,   # World Cup Qualifiers - CONMEBOL
    30,   # World Cup Qualifiers - UEFA
    31,   # World Cup Qualifiers - AFC
    32,   # World Cup Qualifiers - CAF
    33,   # World Cup Qualifiers - CONCACAF
    34,   # World Cup Qualifiers - OFC
    480,  # Olympics Men
    537,  # UEFA Nations League
}

FORM_COLUMNS = [
    "Player_Name",
    "API_Football_ID",
    "Nationality",
    "Intl_Matches_Last_24mo",
    "Avg_Minutes",
    "Avg_Shots",
    "Avg_SOT",
    "Avg_Tackles",
    "Goals_Per_Match",
    "Goal_Scorer_Rate",
    "Last_5_Shots",
    "Last_5_Goals",
    "Last_Updated",
    "Notes",
]

API_KEY: str | None = None


def timestamp_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_secret(name: str, prompt_text: str | None = None, allow_missing: bool = False) -> str | None:
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
    if allow_missing:
        return None
    import getpass

    return getpass.getpass(prompt_text or f"Paste your {name}: ")


def api_headers() -> dict[str, str]:
    global API_KEY
    if not API_KEY:
        API_KEY = load_secret("API_FOOTBALL_KEY", "🔑 Paste your API-Football Key: ")
    if not API_KEY:
        raise RuntimeError("API_FOOTBALL_KEY is required for form pulls")
    return {"x-apisports-key": API_KEY}


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


def normalize_name(name: Any) -> str:
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[’'`\.]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return ""
    return value


def api_get(path: str, params: dict[str, Any] | None = None, max_retries: int = MAX_RETRIES) -> dict:
    url = f"{API_BASE}{path}"
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, headers=api_headers(), params=params or {}, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"   ⏳ API-Football rate limit — waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 5 * (attempt + 1)
                print(f"   ⏳ API-Football server error {resp.status_code} — waiting {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            wait = 5 * (attempt + 1)
            print(f"   ⚠️ API-Football request failed: {e} — retrying in {wait}s")
            time.sleep(wait)
    return {}


def load_player_id_cache() -> dict[str, dict]:
    if not os.path.exists(PLAYER_ID_CACHE_PATH):
        return {}
    with open(PLAYER_ID_CACHE_PATH) as f:
        return json.load(f)


def save_player_id_cache(cache: dict[str, dict]) -> None:
    with open(PLAYER_ID_CACHE_PATH, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def get_sheet_rows(sheet_name: str) -> list[dict]:
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        return []
    values = ws.get_all_values()
    if not values:
        return []
    headers = values[0]
    return [{headers[i]: vals[i] if i < len(vals) else "" for i in range(len(headers))} for vals in values[1:]]


def players_from_picks_sheet() -> list[dict]:
    rows = get_sheet_rows(PICKS_SHEET_NAME)
    players = {}
    for row in rows:
        name = str(row.get("Player", "")).strip()
        if not name:
            continue
        key = normalize_name(name)
        players.setdefault(
            key,
            {
                "name": name,
                "team": str(row.get("Team", "")).strip(),
                "nationality": str(row.get("Team", "")).strip(),
            },
        )
    return list(players.values())


def fetch_wc_squad_players() -> list[dict]:
    teams_payload = api_get("/teams", {"league": API_FOOTBALL_LEAGUE_ID, "season": API_FOOTBALL_SEASON})
    teams = teams_payload.get("response", []) if isinstance(teams_payload, dict) else []
    players: dict[str, dict] = {}
    print(f"🌎 Pulling WC squads for {len(teams)} team(s)")
    for item in teams:
        team = item.get("team", {}) or {}
        team_id = team.get("id")
        team_name = team.get("name", "")
        if not team_id:
            continue
        squad_payload = api_get("/players/squads", {"team": team_id})
        for squad in squad_payload.get("response", []) or []:
            for player in squad.get("players", []) or []:
                name = player.get("name") or ""
                if not name:
                    continue
                key = normalize_name(name)
                players[key] = {
                    "name": name,
                    "team": team_name,
                    "nationality": team_name,
                    "api_id": player.get("id"),
                }
        time.sleep(0.2)
    return list(players.values())


def candidate_score(candidate: dict, player_name: str, nationality: str = "") -> int:
    candidate_name = candidate.get("name") or candidate.get("firstname") or ""
    score = 0
    if normalize_name(candidate_name) == normalize_name(player_name):
        score += 100
    player_parts = set(normalize_name(player_name).split())
    candidate_parts = set(normalize_name(candidate_name).split())
    score += 10 * len(player_parts & candidate_parts)
    candidate_nationality = str(candidate.get("nationality") or "").lower()
    if nationality and normalize_name(nationality) in normalize_name(candidate_nationality):
        score += 20
    return score


def resolve_player_id(player: dict, cache: dict[str, dict]) -> dict | None:
    key = normalize_name(player.get("name", ""))
    if key in cache:
        return cache[key]

    if player.get("api_id"):
        cache[key] = {
            "id": player["api_id"],
            "name": player.get("name", ""),
            "nationality": player.get("nationality", ""),
        }
        return cache[key]

    search_name = str(player.get("name", "")).strip()
    lastname = search_name.split()[-1] if search_name else ""
    if not lastname:
        return None
    payload = api_get("/players/profiles", {"search": lastname})
    response = payload.get("response", []) if isinstance(payload, dict) else []
    candidates = []
    for item in response:
        candidate = item.get("player") or item
        candidates.append(candidate)
    if not candidates:
        print(f"   ⚠️ No player profile match for {search_name}")
        return None
    best = max(candidates, key=lambda c: candidate_score(c, search_name, player.get("nationality", "")))
    best_score = candidate_score(best, search_name, player.get("nationality", ""))
    if best_score < 20:
        print(f"   ⚠️ Weak player profile match for {search_name}: {best.get('name')} score={best_score}")
        return None
    cache[key] = {
        "id": best.get("id"),
        "name": best.get("name") or search_name,
        "nationality": best.get("nationality") or player.get("nationality", ""),
    }
    return cache[key]


def stat_num(container: dict, *path: str) -> float:
    value: Any = container
    for key in path:
        value = (value or {}).get(key) if isinstance(value, dict) else None
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def fetch_player_season_stats(player_id: int, season: int) -> list[dict]:
    payload = api_get("/players", {"id": player_id, "season": season})
    rows = payload.get("response", []) if isinstance(payload, dict) else []
    stats_rows = []
    for row in rows:
        for stat in row.get("statistics", []) or []:
            league_id = ((stat.get("league") or {}).get("id"))
            if league_id in INTL_COMPETITION_IDS:
                stats_rows.append(stat)
    return stats_rows


def summarize_form(player: dict, resolved: dict) -> dict:
    player_id = resolved.get("id")
    stats_rows = []
    for season in FORM_SEASONS:
        stats_rows.extend(fetch_player_season_stats(player_id, season))
        time.sleep(0.15)

    appearances = sum(stat_num(row, "games", "appearences") for row in stats_rows)
    minutes = sum(stat_num(row, "games", "minutes") for row in stats_rows)
    shots = sum(stat_num(row, "shots", "total") for row in stats_rows)
    sot = sum(stat_num(row, "shots", "on") for row in stats_rows)
    tackles = sum(stat_num(row, "tackles", "total") for row in stats_rows)
    goals = sum(stat_num(row, "goals", "total") for row in stats_rows)

    denom = appearances or 0
    notes = "Limited sample" if denom < 5 else ""
    goal_rate = min(goals / denom, 1.0) if denom else ""
    return {
        "Player_Name": player.get("name") or resolved.get("name") or "",
        "API_Football_ID": player_id,
        "Nationality": resolved.get("nationality") or player.get("nationality", ""),
        "Intl_Matches_Last_24mo": int(denom),
        "Avg_Minutes": round(minutes / denom, 2) if denom else "",
        "Avg_Shots": round(shots / denom, 2) if denom else "",
        "Avg_SOT": round(sot / denom, 2) if denom else "",
        "Avg_Tackles": round(tackles / denom, 2) if denom else "",
        "Goals_Per_Match": round(goals / denom, 3) if denom else "",
        # API-Football season aggregates do not expose per-match goal distribution.
        # This is a capped goals-per-match proxy until match-level form logs are added.
        "Goal_Scorer_Rate": round(goal_rate, 3) if denom else "",
        "Last_5_Shots": "",
        "Last_5_Goals": "",
        "Last_Updated": timestamp_utc_iso(),
        "Notes": notes,
    }


def safe_upload_form(rows: list[dict]) -> None:
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(FORM_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=FORM_SHEET_NAME, rows=max(len(rows) + 5, 100), cols=len(FORM_COLUMNS))
    ws.clear()
    values = [FORM_COLUMNS]
    values.extend([[clean_cell(row.get(col, "")) for col in FORM_COLUMNS] for row in rows])
    if values:
        ws.update("A1", values, value_input_option="USER_ENTERED")
    print(f"✅ Wrote {len(rows)} player form row(s) to {FORM_SHEET_NAME}")


def verify_international_leagues() -> None:
    print("🔎 Verifying World-country competition ids from API-Football")
    for league_type in ("cup", "league"):
        payload = api_get("/leagues", {"type": league_type, "country": "World"})
        for row in payload.get("response", []) or []:
            league = row.get("league", {}) or {}
            country = row.get("country", {}) or {}
            print(f"   {league.get('id')}: {league.get('name')} ({country.get('name')})")


def run(args: argparse.Namespace) -> list[dict]:
    cache = load_player_id_cache()
    if args.verify_leagues:
        verify_international_leagues()
        if not args.all and not args.picks_only:
            return []

    players = fetch_wc_squad_players() if args.all else players_from_picks_sheet()
    print(f"📋 Form candidates: {len(players)}")
    rows = []
    for idx, player in enumerate(players, start=1):
        resolved = resolve_player_id(player, cache)
        if not resolved or not resolved.get("id"):
            continue
        try:
            rows.append(summarize_form(player, resolved))
        except Exception as e:
            print(f"   ⚠️ Form pull failed for {player.get('name')}: {e}")
        if idx % 25 == 0:
            print(f"   Processed {idx}/{len(players)} players")
            save_player_id_cache(cache)
    save_player_id_cache(cache)
    rows = sorted(rows, key=lambda r: normalize_name(r.get("Player_Name", "")))
    if args.dry_run:
        print(f"🧪 Dry run — would write {len(rows)} rows")
        if rows:
            print(json.dumps(rows[0], indent=2))
    else:
        safe_upload_form(rows)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull World Cup player international form")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--all", action="store_true", help="Pull all WC squad players.")
    mode.add_argument("--picks-only", action="store_true", help="Pull only players currently present in Picks.")
    parser.add_argument("--verify-leagues", action="store_true", help="Print World-country league ids for audit.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write Player_Form.")
    args = parser.parse_args()
    if not args.verify_leagues and not args.all and not args.picks_only:
        parser.error("one of --all, --picks-only, or --verify-leagues is required")
    return args


if __name__ == "__main__":
    run(parse_args())
