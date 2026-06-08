# World Cup Pick Grader v1
#
# API-Football stat mapping chosen for v1:
# - Shots       -> statistics.shots.total
# - SOT         -> statistics.shots.on
# - Tackles     -> statistics.tackles.total; milestone alternate, HIT when actual >= line
# - Goal Scorer -> statistics.goals.total; HIT when goals >= 1
# - Goals       -> statistics.goals.total; milestone alternate, HIT when actual >= line
#
# API-Football returns null when a player recorded no action. For players with
# minutes > 0, null is treated as 0. Players with 0/no minutes are marked DNP.

from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

import gspread
import pytz
import requests
from google.auth import default
from google.oauth2.service_account import Credentials


SHEET_ID = "1ZijOHruRgILnyR4H_jJh3pQrU3A9PJepWMLtRf3Ie9g"
SHEET_NAME = "Picks"
API_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_LEAGUE_ID = 1
API_FOOTBALL_SEASON = 2026

RESULT_COL = "Result"
ACTUAL_COL = "Actual"
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
MATCH_TIME_TOLERANCE_HOURS = 2
FIXTURE_CACHE_TTL_SECONDS = 24 * 60 * 60
EASTERN = pytz.timezone("America/New_York")


def resolve_cache_dir() -> str:
    for path in (os.path.expanduser("~/.dfs_engines_cache"), os.path.join(os.getcwd(), ".cache")):
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except OSError:
            continue
    return "/tmp"


CACHE_DIR = resolve_cache_dir()

# Canonical names are deliberately broad. API-Football and Odds API may use
# country names, abbreviations, or federation-style names.
TEAM_NAME_ALIASES = {
    "argentina": "argentina",
    "australia": "australia",
    "austria": "austria",
    "belgium": "belgium",
    "brazil": "brazil",
    "canada": "canada",
    "chile": "chile",
    "colombia": "colombia",
    "costa rica": "costa rica",
    "croatia": "croatia",
    "czech republic": "czechia",
    "czechia": "czechia",
    "denmark": "denmark",
    "ecuador": "ecuador",
    "egypt": "egypt",
    "england": "england",
    "france": "france",
    "germany": "germany",
    "ghana": "ghana",
    "iran": "iran",
    "ir iran": "iran",
    "islamic republic of iran": "iran",
    "italy": "italy",
    "ivory coast": "cote d ivoire",
    "cote d'ivoire": "cote d ivoire",
    "côte d’ivoire": "cote d ivoire",
    "cote d ivoire": "cote d ivoire",
    "japan": "japan",
    "korea republic": "south korea",
    "south korea": "south korea",
    "republic of korea": "south korea",
    "mexico": "mexico",
    "morocco": "morocco",
    "netherlands": "netherlands",
    "holland": "netherlands",
    "new zealand": "new zealand",
    "nigeria": "nigeria",
    "norway": "norway",
    "panama": "panama",
    "paraguay": "paraguay",
    "peru": "peru",
    "poland": "poland",
    "portugal": "portugal",
    "qatar": "qatar",
    "saudi arabia": "saudi arabia",
    "scotland": "scotland",
    "senegal": "senegal",
    "serbia": "serbia",
    "slovakia": "slovakia",
    "slovenia": "slovenia",
    "south africa": "south africa",
    "spain": "spain",
    "sweden": "sweden",
    "switzerland": "switzerland",
    "tunisia": "tunisia",
    "turkey": "turkiye",
    "türkiye": "turkiye",
    "turkiye": "turkiye",
    "ukraine": "ukraine",
    "united states": "usa",
    "usa": "usa",
    "us": "usa",
    "u s a": "usa",
    "uruguay": "uruguay",
    "wales": "wales",
}

API_KEY: str | None = None


def now_est() -> datetime:
    return datetime.now(EASTERN)


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
        raise RuntimeError("API_FOOTBALL_KEY is required for World Cup grading")
    return {"x-apisports-key": API_KEY}


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[’'`\.]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_name(name: str) -> str:
    """Strip diacritics, lowercase, collapse spaces for matching."""
    return normalize_text(name)


def canonical_team_name(name: Any) -> str:
    norm = normalize_text(name)
    return TEAM_NAME_ALIASES.get(norm, norm)


def normalize_player_name(name: Any) -> str:
    return normalize_name(str(name or ""))


def normalize_prop(prop: Any) -> str:
    raw = str(prop or "").strip().upper()
    compact = re.sub(r"\s+", "", raw)
    if compact in {"SOT", "SHOTSONTARGET", "SHOTS_ON_TARGET"}:
        return "SOT"
    if compact == "SHOTS":
        return "Shots"
    if compact == "TACKLES":
        return "Tackles"
    if compact in {"GOALSCORER", "GOALSCORERANYTIME", "ANYTIMEGOALSCORER"}:
        return "Goal Scorer"
    if compact == "GOALS":
        return "Goals"
    return str(prop or "").strip()


def safe_float(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if math.isnan(float(value)) or math.isinf(float(value)):
            return default
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text or text.upper() in {"N/A", "NA", "NONE", "NULL", "DNP", "-"}:
        return default
    first_num = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not first_num:
        return default
    try:
        return float(first_num.group(0))
    except ValueError:
        return default


def parse_datetime_utc(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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


def fixture_cache_path() -> str:
    return os.path.join(CACHE_DIR, f"WC_api_football_fixtures_{API_FOOTBALL_SEASON}.json")


def fetch_all_fixtures_cached() -> list[dict]:
    path = fixture_cache_path()
    if os.path.exists(path) and (time.time() - os.path.getmtime(path)) < FIXTURE_CACHE_TTL_SECONDS:
        with open(path) as f:
            data = json.load(f)
        print(f"💾 API-Football fixtures cache hit ({len(data)} fixtures)")
        return data
    payload = api_get(
        "/fixtures",
        {"league": API_FOOTBALL_LEAGUE_ID, "season": API_FOOTBALL_SEASON},
    )
    fixtures = payload.get("response", []) if isinstance(payload, dict) else []
    with open(path, "w") as f:
        json.dump(fixtures, f)
    print(f"✅ Cached {len(fixtures)} API-Football WC fixture(s)")
    return fixtures


def fixture_kickoff(fixture: dict) -> datetime | None:
    date_raw = ((fixture.get("fixture") or {}).get("date") or "")
    return parse_datetime_utc(date_raw)


def fixture_team_names(fixture: dict) -> tuple[str, str]:
    teams = fixture.get("teams") or {}
    home = ((teams.get("home") or {}).get("name") or "")
    away = ((teams.get("away") or {}).get("name") or "")
    return home, away


def resolve_api_football_fixture_id(team_home: str, team_away: str, kickoff_utc: str) -> int | None:
    """
    Given Odds API team names + kickoff time, find API-Football fixture_id.
    Cache fixture list per-day to avoid burning quota.
    """
    kickoff = parse_datetime_utc(kickoff_utc)
    if not kickoff:
        print(f"   ⚠️ Missing kickoff for fixture resolution: {team_home} vs {team_away}")
        return None

    target_teams = {canonical_team_name(team_home), canonical_team_name(team_away)}
    candidates = []
    for fixture in fetch_all_fixtures_cached():
        start = fixture_kickoff(fixture)
        if not start or abs((start - kickoff).total_seconds()) > MATCH_TIME_TOLERANCE_HOURS * 3600:
            continue
        api_home, api_away = fixture_team_names(fixture)
        api_teams = {canonical_team_name(api_home), canonical_team_name(api_away)}
        if target_teams == api_teams:
            candidates.append(fixture)

    if not candidates:
        print(f"   ⚠️ No API-Football fixture for {team_home} vs {team_away} at {kickoff_utc}")
        return None

    fixture = candidates[0]
    fixture_id = int((fixture.get("fixture") or {}).get("id"))
    api_home, api_away = fixture_team_names(fixture)
    print(f"   🔎 Resolved API-Football fixture_id={fixture_id} for {api_home} vs {api_away}")
    return fixture_id


PLAYER_STATS_CACHE: dict[int, dict[str, dict]] = {}


def fetch_fixture_player_stats(fixture_id: int) -> dict[str, dict]:
    """
    Returns {normalized_player_name: stats_dict} for both teams.
    """
    if fixture_id in PLAYER_STATS_CACHE:
        return PLAYER_STATS_CACHE[fixture_id]
    payload = api_get("/fixtures/players", {"fixture": fixture_id})
    data = payload.get("response", []) if isinstance(payload, dict) else []
    result: dict[str, dict] = {}
    for team in data:
        for player_entry in team.get("players", []) or []:
            player_blob = player_entry.get("player") or {}
            name = player_blob.get("name") or ""
            if not name:
                continue
            stats = (player_entry.get("statistics") or [{}])[0] or {}
            row = {
                "PLAYER_ID": player_blob.get("id", ""),
                "PLAYER_NAME": name,
                "stats": stats,
            }
            result[normalize_player_name(name)] = row
    PLAYER_STATS_CACHE[fixture_id] = result
    return result


def extract_stat(stats: dict, prop: str) -> int | None:
    """
    Extract numeric stat for a given prop type.
    Returns 0 when value is None and player played minutes.
    Returns None only if player didn't play at all.
    """
    games = stats.get("games", {}) or {}
    minutes = safe_float(games.get("minutes"), 0) or 0
    if minutes == 0:
        return None

    prop_norm = normalize_prop(prop)
    if prop_norm == "Shots":
        return int((stats.get("shots", {}) or {}).get("total") or 0)
    if prop_norm == "SOT":
        return int((stats.get("shots", {}) or {}).get("on") or 0)
    if prop_norm == "Tackles":
        return int((stats.get("tackles", {}) or {}).get("total") or 0)
    if prop_norm == "Goal Scorer":
        goals = int((stats.get("goals", {}) or {}).get("total") or 0)
        return 1 if goals >= 1 else 0
    if prop_norm == "Goals":
        return int((stats.get("goals", {}) or {}).get("total") or 0)
    raise ValueError(f"Unknown prop: {prop}")


def fetch_actuals(picks: list[dict]) -> dict[str, dict]:
    actuals_by_pick_key: dict[str, dict] = {}
    fixture_groups: dict[int, list[dict]] = {}
    for pick in picks:
        fixture_id = resolve_api_football_fixture_id(
            pick.get("Team", ""),
            pick.get("Opponent", ""),
            pick.get("Game_Time", ""),
        )
        if fixture_id is None:
            actuals_by_pick_key[pick["_row_key"]] = {"status": "PENDING", "actual": None}
            continue
        fixture_groups.setdefault(fixture_id, []).append(pick)

    for fixture_id, group in fixture_groups.items():
        stats_by_player = fetch_fixture_player_stats(fixture_id)
        api_names = [row.get("PLAYER_NAME", "") for row in stats_by_player.values()]
        for pick in group:
            player_key = normalize_player_name(pick.get("Player", ""))
            player_row = stats_by_player.get(player_key)
            if not player_row:
                print(
                    f"   ❌ No API-Football player match for pick: {pick.get('Player')} "
                    f"in fixture {fixture_id}. API-Football names: {api_names[:10]}"
                )
                actuals_by_pick_key[pick["_row_key"]] = {"status": "PENDING", "actual": None, "fixture_id": fixture_id}
                continue
            try:
                actual = extract_stat(player_row.get("stats") or {}, pick.get("Prop", ""))
            except ValueError as e:
                print(f"   ⚠️ fixture_id={fixture_id} player={player_row.get('PLAYER_NAME')}: {e}")
                actuals_by_pick_key[pick["_row_key"]] = {"status": "PENDING", "actual": None, "fixture_id": fixture_id}
                continue
            if actual is None:
                print(f"   ℹ️ fixture_id={fixture_id} player_id={player_row.get('PLAYER_ID')}: {player_row.get('PLAYER_NAME')} DNP")
                actuals_by_pick_key[pick["_row_key"]] = {
                    "status": "DNP",
                    "actual": None,
                    "fixture_id": fixture_id,
                    "player_id": player_row.get("PLAYER_ID"),
                    "player_name": player_row.get("PLAYER_NAME"),
                }
                continue
            print(
                f"   ✅ fixture_id={fixture_id} player_id={player_row.get('PLAYER_ID')} "
                f"{player_row.get('PLAYER_NAME')} {pick.get('Prop')}={actual}"
            )
            actuals_by_pick_key[pick["_row_key"]] = {
                "status": "OK",
                "actual": actual,
                "fixture_id": fixture_id,
                "player_id": player_row.get("PLAYER_ID"),
                "player_name": player_row.get("PLAYER_NAME"),
            }
    return actuals_by_pick_key


def grade_result(actual: float | None, line: float | None, pick: str, prop: Any = "") -> str:
    if actual is None or line is None:
        return "PENDING"
    prop_norm = normalize_prop(prop)
    lean = str(pick or "").strip().upper()
    if prop_norm in {"Tackles", "Goal Scorer", "Goals"}:
        if lean == "UNDER":
            return "HIT" if actual < line else "MISS"
        return "HIT" if actual >= line else "MISS"
    if actual == line:
        return "PUSH"
    if lean == "UNDER":
        return "HIT" if actual < line else "MISS"
    return "HIT" if actual > line else "MISS"


def load_picks_from_sheet() -> tuple[gspread.Worksheet, list[str], list[dict]]:
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(SHEET_NAME)
    values = ws.get_all_values()
    if not values:
        return ws, [], []
    headers = values[0]
    rows = []
    for sheet_row, vals in enumerate(values[1:], start=2):
        row = {headers[i]: vals[i] if i < len(vals) else "" for i in range(len(headers))}
        row["_sheet_row"] = sheet_row
        row["_row_key"] = f"{sheet_row}:{row.get('Player','')}:{row.get('Prop','')}:{row.get('Line','')}"
        rows.append(row)
    return ws, headers, rows


def pick_is_gradable(row: dict, now_utc: datetime | None = None) -> bool:
    result = str(row.get(RESULT_COL, "") or "").strip().upper()
    if result in {"HIT", "MISS", "PUSH", "DNP"}:
        return False
    game_time = parse_datetime_utc(row.get("Game_Time"))
    if not game_time:
        return False
    now_utc = now_utc or datetime.now(timezone.utc)
    return game_time < now_utc - timedelta(minutes=90)


def skip_if_no_recent_completed_matches(rows: list[dict]) -> bool:
    now_utc = datetime.now(timezone.utc)
    recent = []
    for row in rows:
        game_time = parse_datetime_utc(row.get("Game_Time"))
        if game_time and now_utc - timedelta(hours=4) <= game_time <= now_utc:
            recent.append(row)
    if recent:
        return False
    old_blanks = [r for r in rows if pick_is_gradable(r, now_utc)]
    if old_blanks:
        print(f"ℹ️ No WC matches completed in last 4h, but {len(old_blanks)} older blank pick(s) need retroactive grading")
        return False
    print("⏭️ No WC matches completed in the last 4 hours and no retroactive blanks to grade")
    return True


def build_graded_rows(picks: list[dict], actuals: dict[str, dict]) -> list[dict]:
    graded = []
    for row in picks:
        info = actuals.get(row["_row_key"], {"status": "PENDING", "actual": None})
        if info.get("status") == "DNP":
            result = "DNP"
            actual = None
        else:
            line = safe_float(row.get("Line"))
            actual = info.get("actual")
            result = grade_result(actual, line, row.get("Pick"), row.get("Prop"))
        graded.append(
            {
                "_sheet_row": row["_sheet_row"],
                "Player": row.get("Player", ""),
                "Prop": row.get("Prop", ""),
                "Pick": row.get("Pick", ""),
                "Line": row.get("Line", ""),
                RESULT_COL: result,
                ACTUAL_COL: "" if actual is None else actual,
                "fixture_id": info.get("fixture_id", ""),
                "player_id": info.get("player_id", ""),
                "player_name": info.get("player_name", ""),
            }
        )
    return graded


def write_results(graded: list[dict]) -> None:
    if not graded:
        print("⏭️ No graded rows to write")
        return
    ws, headers, _ = load_picks_from_sheet()
    if RESULT_COL not in headers or ACTUAL_COL not in headers:
        raise RuntimeError(f"Missing {RESULT_COL}/{ACTUAL_COL} columns in {SHEET_NAME}")
    result_col = headers.index(RESULT_COL) + 1
    actual_col = headers.index(ACTUAL_COL) + 1
    updates = []
    for row in graded:
        sheet_row = row["_sheet_row"]
        updates.append({"range": gspread.utils.rowcol_to_a1(sheet_row, result_col), "values": [[row[RESULT_COL]]]})
        updates.append({"range": gspread.utils.rowcol_to_a1(sheet_row, actual_col), "values": [[row[ACTUAL_COL]]]})
    ws.batch_update(updates)
    print(f"✅ Wrote Result/Actual for {len(graded)} row(s)")


def grade_picks() -> None:
    print("=" * 60)
    print("🌎 WORLD CUP PICK GRADER v1")
    print("=" * 60)
    print(f"🕐 Run time: {now_est().strftime('%Y-%m-%d %I:%M:%S %p EST')}")
    _, headers, rows = load_picks_from_sheet()
    if not rows:
        print("⏭️ No picks found")
        return
    for required in ("Player", "Team", "Opponent", "Prop", "Line", "Pick", "Game_Time", RESULT_COL, ACTUAL_COL):
        if required not in headers:
            raise RuntimeError(f"Missing required column: {required}")
    if skip_if_no_recent_completed_matches(rows):
        return
    gradable = [row for row in rows if pick_is_gradable(row)]
    print(f"📋 Picks loaded: {len(rows)} | gradable blanks: {len(gradable)}")
    if not gradable:
        print("⏭️ No blank completed picks to grade")
        return
    actuals = fetch_actuals(gradable)
    graded = build_graded_rows(gradable, actuals)
    write_results(graded)
    counts = {}
    for row in graded:
        counts[row[RESULT_COL]] = counts.get(row[RESULT_COL], 0) + 1
    print(f"📊 Grade summary: {counts}")
    print("=" * 60)
    print("✅ WORLD CUP PICK GRADER COMPLETE")
    print("=" * 60)


def validate_team_aliases() -> None:
    required_examples = [
        "United States",
        "USA",
        "South Korea",
        "Korea Republic",
        "Czech Republic",
        "Czechia",
        "Turkey",
        "Türkiye",
        "Ivory Coast",
        "Côte d’Ivoire",
    ]
    missing = [name for name in required_examples if not canonical_team_name(name)]
    if missing:
        raise RuntimeError(f"Alias validation failed: {missing}")
    print(f"✅ Team alias smoke test passed ({len(TEAM_NAME_ALIASES)} aliases)")


def parse_args():
    parser = argparse.ArgumentParser(description="World Cup pick grader")
    parser.add_argument("--alias-check", action="store_true", help="Validate team aliases and exit without Google/API-Football calls.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    validate_team_aliases()
    if not args.alias_check:
        grade_picks()
