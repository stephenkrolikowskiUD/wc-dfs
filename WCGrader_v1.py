# World Cup Pick Grader v1
#
# FotMob stat mapping chosen for v1:
# - Shots   -> "Total shots" first, fallback "Shots"
# - SOT     -> "Shots on target"
# - Passes  -> "Accurate passes" first. If FotMob exposes "Accurate passes" as
#              "42/50 (84%)", the numerator is used. Fallback: "Passes".
# - Tackles -> "Tackles won" first, fallback "Tackles"
#
# These keys are intentionally auditable because FotMob's unofficial JSON can
# drift by competition or app release. Verify on the first completed WC match.
#
# TODO: fallback to API-Football if FotMob breaks.

from __future__ import annotations

import json
import math
import os
import re
import time
import argparse
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
FOTMOB_MATCH_DETAILS = "https://www.fotmob.com/api/matchDetails?matchId={match_id}"
FOTMOB_MATCHES_BY_DATE = "https://www.fotmob.com/api/matches?date={yyyymmdd}"
WC_LEAGUE_ID = 77  # FotMob World Cup league ID — verify at runtime; may shift between editions.

RESULT_COL = "Result"
ACTUAL_COL = "Actual"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
MATCH_TIME_TOLERANCE_HOURS = 8
EASTERN = pytz.timezone("America/New_York")

PROP_STAT_KEYS = {
    "SHOTS": ("Total shots", "Shots"),
    "SOT": ("Shots on target",),
    "PASSES": ("Accurate passes", "Passes", "Total passes"),
    "TACKLES": ("Tackles won", "Tackles"),
}

# Canonical names are deliberately broad. FotMob and Odds API may use country
# names, abbreviations, or federation-style names.
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


def now_est() -> datetime:
    return datetime.now(EASTERN)


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[’'`\.]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_team_name(name: Any) -> str:
    norm = normalize_text(name)
    return TEAM_NAME_ALIASES.get(norm, norm)


def normalize_player_name(name: Any) -> str:
    return normalize_text(name)


def normalize_prop(prop: Any) -> str:
    raw = str(prop or "").strip().upper()
    compact = re.sub(r"\s+", "", raw)
    if compact in {"SOT", "SHOTSONTARGET", "SHOTS_ON_TARGET"}:
        return "SOT"
    if compact == "SHOTS":
        return "SHOTS"
    if compact == "PASSES":
        return "PASSES"
    if compact == "TACKLES":
        return "TACKLES"
    return compact


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
    # FotMob often formats stat values as "42/50 (84%)". For the selected
    # pass/tackle markets, the first number is the count we need.
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


def format_fotmob_date(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d")


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

        print("Authenticating with Google...")
        colab_auth.authenticate_user()
        creds, _ = default(scopes=scopes)
        print("✅ Google auth via Colab")
        return gspread.authorize(creds)
    except Exception as e:
        raise RuntimeError("Google auth unavailable. Set GOOGLE_SERVICE_ACCOUNT_JSON or run in Colab.") from e


def request_json(url: str) -> dict:
    headers = {
        "User-Agent": "Mozilla/5.0 WCGrader/1.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://www.fotmob.com/",
    }
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code in {429, 500, 502, 503, 504}:
                wait = 5 * (attempt + 1)
                print(f"   ⏳ FotMob {resp.status_code} — retrying in {wait}s")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if attempt == MAX_RETRIES - 1:
                raise
            wait = 5 * (attempt + 1)
            print(f"   ⚠️ FotMob request failed: {e} — retrying in {wait}s")
            time.sleep(wait)
    return {}


MATCHES_CACHE: dict[str, dict] = {}
DETAILS_CACHE: dict[int, dict] = {}
MATCH_ID_CACHE: dict[tuple[str, str, str], int | None] = {}


def get_nested(obj: dict, paths: list[tuple[str, ...]], default=None):
    for path in paths:
        cur = obj
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                cur = None
                break
            cur = cur[key]
        if cur not in (None, ""):
            return cur
    return default


def league_matches_world_cup(match: dict) -> bool:
    league = match.get("league") or match.get("parentLeague") or match.get("tournament") or {}
    league_id = (
        match.get("leagueId")
        or match.get("primaryLeagueId")
        or league.get("id")
        or league.get("primaryId")
        or league.get("leagueId")
    )
    if str(league_id) == str(WC_LEAGUE_ID):
        return True
    league_name = normalize_text(league.get("name") or league.get("localizedName") or match.get("leagueName"))
    return "world cup" in league_name


def extract_team_names(match: dict) -> tuple[str, str]:
    home = get_nested(
        match,
        [
            ("home", "name"),
            ("homeTeam", "name"),
            ("home", "shortName"),
            ("homeTeam", "shortName"),
        ],
        "",
    )
    away = get_nested(
        match,
        [
            ("away", "name"),
            ("awayTeam", "name"),
            ("away", "shortName"),
            ("awayTeam", "shortName"),
        ],
        "",
    )
    return str(home or ""), str(away or "")


def extract_match_id(match: dict) -> int | None:
    raw = match.get("id") or match.get("matchId") or match.get("fixtureId")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def extract_match_time(match: dict) -> datetime | None:
    raw = (
        match.get("status", {}).get("utcTime")
        if isinstance(match.get("status"), dict)
        else None
    )
    raw = raw or match.get("utcTime") or match.get("time") or match.get("kickoffTime") or match.get("startTime")
    return parse_datetime_utc(raw)


def fetch_matches_by_date(yyyymmdd: str) -> dict:
    if yyyymmdd not in MATCHES_CACHE:
        MATCHES_CACHE[yyyymmdd] = request_json(FOTMOB_MATCHES_BY_DATE.format(yyyymmdd=yyyymmdd))
    return MATCHES_CACHE[yyyymmdd]


def iter_matches(payload: Any):
    if isinstance(payload, list):
        for item in payload:
            yield from iter_matches(item)
    elif isinstance(payload, dict):
        if any(k in payload for k in ("home", "homeTeam")) and any(k in payload for k in ("away", "awayTeam")):
            yield payload
        for key in ("matches", "fixtures", "events", "allMatches"):
            if key in payload:
                yield from iter_matches(payload[key])
        for league_key in ("leagues", "sections"):
            for section in payload.get(league_key, []) or []:
                yield from iter_matches(section)


def resolve_fotmob_match_id(team_home: str, team_away: str, kickoff_utc: str) -> int | None:
    """
    Given Odds API team names + kickoff time, find FotMob match_id.
    Fetch /matches?date=YYYYMMDD, filter to WC league, fuzzy-match team names
    (handle 'United States' vs 'USA', 'South Korea' vs 'Korea Republic', etc.)
    """
    kickoff = parse_datetime_utc(kickoff_utc)
    if not kickoff:
        print(f"   ⚠️ Invalid kickoff time for match resolution: {kickoff_utc}")
        return None
    home_key = canonical_team_name(team_home)
    away_key = canonical_team_name(team_away)
    cache_key = (home_key, away_key, kickoff.isoformat())
    if cache_key in MATCH_ID_CACHE:
        return MATCH_ID_CACHE[cache_key]

    payload = fetch_matches_by_date(format_fotmob_date(kickoff))
    candidates = []
    for match in iter_matches(payload):
        if not league_matches_world_cup(match):
            continue
        home, away = extract_team_names(match)
        h_key, a_key = canonical_team_name(home), canonical_team_name(away)
        teams_match = {home_key, away_key} == {h_key, a_key}
        if not teams_match:
            continue
        match_time = extract_match_time(match)
        time_score = 999999
        if match_time:
            time_score = abs((match_time - kickoff).total_seconds())
        if time_score <= MATCH_TIME_TOLERANCE_HOURS * 3600:
            candidates.append((time_score, match, home, away))

    candidates.sort(key=lambda item: item[0])
    if not candidates:
        print(f"   ⚠️ No FotMob match for {team_home} vs {team_away} at {kickoff_utc}")
        MATCH_ID_CACHE[cache_key] = None
        return None
    match_id = extract_match_id(candidates[0][1])
    print(f"   🔎 Resolved FotMob match_id={match_id} for {team_home} vs {team_away}")
    MATCH_ID_CACHE[cache_key] = match_id
    return match_id


def fetch_match_details(match_id: int) -> dict:
    if match_id not in DETAILS_CACHE:
        DETAILS_CACHE[match_id] = request_json(FOTMOB_MATCH_DETAILS.format(match_id=match_id))
    return DETAILS_CACHE[match_id]


def collect_player_names(obj: Any, names: dict[str, str]) -> None:
    if isinstance(obj, dict):
        pid = obj.get("id") or obj.get("playerId") or obj.get("participantId")
        name = obj.get("name") or obj.get("fullName") or obj.get("shortName")
        if isinstance(name, dict):
            name = name.get("name") or name.get("default")
        if pid is not None and name:
            names[str(pid)] = str(name)
        for val in obj.values():
            collect_player_names(val, names)
    elif isinstance(obj, list):
        for item in obj:
            collect_player_names(item, names)


def stat_items_from_blob(blob: Any):
    if isinstance(blob, dict):
        if any(k in blob for k in ("key", "title", "name", "stat", "value", "displayValue")):
            yield blob
        for key in ("stats", "stat", "items", "data"):
            if key in blob:
                yield from stat_items_from_blob(blob[key])
    elif isinstance(blob, list):
        for item in blob:
            yield from stat_items_from_blob(item)


def stat_item_name(item: dict) -> str:
    raw = item.get("key") or item.get("title") or item.get("name") or item.get("stat") or item.get("label")
    if isinstance(raw, dict):
        raw = raw.get("key") or raw.get("name") or raw.get("default")
    return str(raw or "")


def stat_item_value(item: dict) -> Any:
    return (
        item.get("value")
        if "value" in item
        else item.get("displayValue")
        if "displayValue" in item
        else item.get("statValue")
        if "statValue" in item
        else item.get("val")
    )


def extract_player_actuals(details: dict) -> dict[str, dict]:
    names_by_id: dict[str, str] = {}
    collect_player_names(details.get("content", {}), names_by_id)
    player_stats = ((details.get("content") or {}).get("playerStats") or {})
    actuals: dict[str, dict] = {}
    if isinstance(player_stats, list):
        iterable = [(str(item.get("id") or item.get("playerId") or idx), item) for idx, item in enumerate(player_stats)]
    elif isinstance(player_stats, dict):
        iterable = [(str(pid), blob) for pid, blob in player_stats.items()]
    else:
        iterable = []

    for pid, blob in iterable:
        player_name = names_by_id.get(pid, "")
        if isinstance(blob, dict):
            raw_name = blob.get("name") or blob.get("playerName") or blob.get("fullName")
            if isinstance(raw_name, dict):
                raw_name = raw_name.get("name") or raw_name.get("default")
            player_name = player_name or str(raw_name or "")
        if not player_name:
            continue
        flat_stats = {}
        for item in stat_items_from_blob(blob):
            name = stat_item_name(item)
            value = stat_item_value(item)
            if name:
                flat_stats[normalize_text(name)] = safe_float(value)

        prop_values = {}
        for prop, key_options in PROP_STAT_KEYS.items():
            actual = None
            for key in key_options:
                actual = flat_stats.get(normalize_text(key))
                if actual is not None:
                    break
            if actual is not None:
                prop_values[prop] = actual
        if prop_values:
            prop_values["PLAYER_ID"] = pid
            prop_values["PLAYER_NAME"] = player_name
            actuals[normalize_player_name(player_name)] = prop_values
    return actuals


def fetch_actuals(picks: list[dict]) -> dict[str, dict]:
    actuals_by_pick_key: dict[str, dict] = {}
    match_groups: dict[int, list[dict]] = {}
    for pick in picks:
        match_id = resolve_fotmob_match_id(
            pick.get("Team", ""),
            pick.get("Opponent", ""),
            pick.get("Game_Time", ""),
        )
        if match_id is None:
            actuals_by_pick_key[pick["_row_key"]] = {"status": "PENDING", "actual": None}
            continue
        match_groups.setdefault(match_id, []).append(pick)

    for match_id, group in match_groups.items():
        details = fetch_match_details(match_id)
        player_actuals = extract_player_actuals(details)
        for pick in group:
            player_key = normalize_player_name(pick.get("Player", ""))
            prop = normalize_prop(pick.get("Prop", ""))
            player_row = player_actuals.get(player_key)
            if not player_row:
                print(f"   ⚠️ match_id={match_id}: player not found: {pick.get('Player')}")
                actuals_by_pick_key[pick["_row_key"]] = {"status": "PENDING", "actual": None, "match_id": match_id}
                continue
            actual = player_row.get(prop)
            if actual is None:
                print(f"   ⚠️ match_id={match_id} player_id={player_row.get('PLAYER_ID')}: missing stat {prop} for {pick.get('Player')}")
                actuals_by_pick_key[pick["_row_key"]] = {"status": "PENDING", "actual": None, "match_id": match_id}
                continue
            print(f"   ✅ match_id={match_id} player_id={player_row.get('PLAYER_ID')} {pick.get('Player')} {prop}={actual}")
            actuals_by_pick_key[pick["_row_key"]] = {"status": "OK", "actual": actual, "match_id": match_id, "player_id": player_row.get("PLAYER_ID")}
    return actuals_by_pick_key


def grade_result(actual: float | None, line: float | None, pick: str) -> str:
    if actual is None or line is None:
        return "PENDING"
    if actual == line:
        return "PUSH"
    lean = str(pick or "").strip().upper()
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
    if result in {"HIT", "MISS", "PUSH"}:
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
    # Still allow retroactive grading for old blank rows. This satisfies the
    # "skip recent match days" guard without blocking first-run backfills.
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
        line = safe_float(row.get("Line"))
        actual = info.get("actual")
        result = grade_result(actual, line, row.get("Pick"))
        graded.append(
            {
                "_sheet_row": row["_sheet_row"],
                "Player": row.get("Player", ""),
                "Prop": row.get("Prop", ""),
                "Pick": row.get("Pick", ""),
                "Line": row.get("Line", ""),
                RESULT_COL: result,
                ACTUAL_COL: "" if actual is None else actual,
                "match_id": info.get("match_id", ""),
                "player_id": info.get("player_id", ""),
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
    ws, headers, rows = load_picks_from_sheet()
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
    parser.add_argument("--alias-check", action="store_true", help="Validate team aliases and exit without Google/FotMob calls.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    validate_team_aliases()
    if not args.alias_check:
        grade_picks()
