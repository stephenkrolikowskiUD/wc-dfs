# World Cup Daily Draft Prep v1
#
# Builds the Daily_Slate tab for Underdog Match Day Mania style drafts:
# 4-person snake, 6 players, no bench, one exact slate.

from __future__ import annotations

import argparse
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

import requests

from WCDraftHelper import (
    FORM_SHEET_NAME,
    build_squad_indexes,
    canonical_team_name,
    compute_player_efp_rows,
    get_sheet_rows,
    load_squad_cache,
    normalize_name,
    safe_float,
    safe_upload,
)


API_BASE = "https://v3.football.api-sports.io"
API_FOOTBALL_LEAGUE_ID = 1
API_FOOTBALL_SEASON = 2026
REQUEST_TIMEOUT = 15
MAX_RETRIES = 3
DAILY_SLATE_SHEET_NAME = "Daily_Slate"

DAILY_SLATE_COLUMNS = [
    "Player_Name",
    "API_Football_ID",
    "Team",
    "Opponent",
    "Position",
    "EFP_Per_Match",
    "Start_Prob",
    "Adjusted_EFP",
    "Tier",
    "Kickoff_Time",
    "Source",
    "Notes",
]

TEAM_ALIASES = {
    "arg": "argentina",
    "argentina": "argentina",
    "aus": "australia",
    "australia": "australia",
    "aut": "austria",
    "austria": "austria",
    "bel": "belgium",
    "belgium": "belgium",
    "bra": "brazil",
    "brazil": "brazil",
    "can": "canada",
    "canada": "canada",
    "chi": "chile",
    "chile": "chile",
    "col": "colombia",
    "colombia": "colombia",
    "crc": "costa rica",
    "costa rica": "costa rica",
    "cro": "croatia",
    "croatia": "croatia",
    "cze": "czechia",
    "czechia": "czechia",
    "czech republic": "czechia",
    "den": "denmark",
    "denmark": "denmark",
    "ecu": "ecuador",
    "ecuador": "ecuador",
    "egy": "egypt",
    "egypt": "egypt",
    "eng": "england",
    "england": "england",
    "fra": "france",
    "france": "france",
    "ger": "germany",
    "germany": "germany",
    "gha": "ghana",
    "ghana": "ghana",
    "gre": "greece",
    "greece": "greece",
    "irn": "iran",
    "iran": "iran",
    "ita": "italy",
    "italy": "italy",
    "jpn": "japan",
    "japan": "japan",
    "kor": "south korea",
    "korea republic": "south korea",
    "south korea": "south korea",
    "mex": "mexico",
    "mexico": "mexico",
    "mar": "morocco",
    "morocco": "morocco",
    "ned": "netherlands",
    "netherlands": "netherlands",
    "nzl": "new zealand",
    "new zealand": "new zealand",
    "nga": "nigeria",
    "nigeria": "nigeria",
    "nor": "norway",
    "norway": "norway",
    "pan": "panama",
    "panama": "panama",
    "par": "paraguay",
    "paraguay": "paraguay",
    "per": "peru",
    "peru": "peru",
    "pol": "poland",
    "poland": "poland",
    "por": "portugal",
    "portugal": "portugal",
    "qat": "qatar",
    "qatar": "qatar",
    "ksa": "saudi arabia",
    "saudi arabia": "saudi arabia",
    "sco": "scotland",
    "scotland": "scotland",
    "sen": "senegal",
    "senegal": "senegal",
    "srb": "serbia",
    "serbia": "serbia",
    "svk": "slovakia",
    "slovakia": "slovakia",
    "svn": "slovenia",
    "slovenia": "slovenia",
    "rsa": "south africa",
    "south africa": "south africa",
    "esp": "spain",
    "spain": "spain",
    "swe": "sweden",
    "sweden": "sweden",
    "sui": "switzerland",
    "switzerland": "switzerland",
    "tun": "tunisia",
    "tunisia": "tunisia",
    "tur": "turkiye",
    "turkey": "turkiye",
    "turkiye": "turkiye",
    "ukr": "ukraine",
    "ukraine": "ukraine",
    "usa": "usa",
    "united states": "usa",
    "uru": "uruguay",
    "uruguay": "uruguay",
    "wal": "wales",
    "wales": "wales",
}

API_KEY: str | None = None


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
        raise RuntimeError("API_FOOTBALL_KEY is required in confirmed mode")
    return {"x-apisports-key": API_KEY}


def api_get(path: str, params: dict | None = None, max_retries: int = MAX_RETRIES) -> dict:
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


def team_key(raw: str) -> str:
    norm = normalize_name(raw)
    return TEAM_ALIASES.get(norm, canonical_team_name(raw))


def parse_slate(slate: str) -> list[tuple[str, str]]:
    games = []
    for chunk in str(slate or "").split(","):
        text = chunk.strip()
        if not text:
            continue
        parts = re.split(r"\s*(?:@|vs|v)\s*", text, flags=re.IGNORECASE)
        if len(parts) != 2:
            raise ValueError(f"Could not parse slate game: {text!r}. Use form RSA@MEX,CZE@KOR")
        away, home = team_key(parts[0]), team_key(parts[1])
        games.append((away, home))
    if not games:
        raise ValueError("--slate is required, e.g. RSA@MEX,CZE@KOR")
    return games


def opponent_map_from_games(games: list[tuple[str, str]]) -> dict[str, str]:
    out = {}
    for away, home in games:
        out[away] = home
        out[home] = away
    return out


def display_team_name(team_key_value: str, squad_teams: dict[str, dict]) -> str:
    return (squad_teams.get(team_key_value) or {}).get("Team") or team_key_value.title()


def load_player_pool(slate_games: list[tuple[str, str]]) -> tuple[list[dict], dict[str, dict]]:
    squad_cache = load_squad_cache()
    squad_by_id, squad_teams = build_squad_indexes(squad_cache)
    form_rows = get_sheet_rows(FORM_SHEET_NAME)
    efp_rows = compute_player_efp_rows(form_rows, squad_by_id)
    slate_teams = set(opponent_map_from_games(slate_games).keys())
    players = []
    for row in efp_rows:
        api_id = str(row.get("API_Football_ID") or "").strip()
        squad = squad_by_id.get(api_id, {})
        team = squad.get("Team") or row.get("Team") or row.get("Nationality") or ""
        canonical = team_key(team)
        if canonical not in slate_teams:
            continue
        position = row.get("Position", "")
        if not position:
            continue
        avg_minutes = safe_float(row.get("Avg_Minutes"))
        players.append(
            {
                "Player_Name": row.get("Player_Name", ""),
                "API_Football_ID": api_id,
                "Team": display_team_name(canonical, squad_teams) or team,
                "Team_Key": canonical,
                "Position": position,
                "EFP_Per_Match": safe_float(row.get("EFP_Regressed") or row.get("EFP_Per_Match")),
                "EFP_Raw": safe_float(row.get("EFP_Raw")),
                "Intl_Sample": int(safe_float(row.get("Intl_Sample"))),
                "Avg_Minutes": avg_minutes,
                "Notes": row.get("Notes", ""),
            }
        )
    return players, squad_teams


def pre_xi_start_prob(avg_minutes: float) -> float:
    if avg_minutes >= 75:
        return 0.95
    if avg_minutes >= 58:
        return 0.75
    if avg_minutes >= 25:
        return 0.35
    if avg_minutes > 0:
        return 0.15
    return 0.05


def start_multiplier(start_prob: float) -> float:
    if start_prob >= 0.95:
        return 1.0
    if start_prob >= 0.65:
        return 0.85
    if start_prob >= 0.30:
        return 0.30
    return 0.05


def adjusted_score(efp: float, start_prob: float) -> float:
    return round(efp * start_multiplier(start_prob), 2)


def load_fixtures() -> list[dict]:
    payload = api_get("/fixtures", {"league": API_FOOTBALL_LEAGUE_ID, "season": API_FOOTBALL_SEASON})
    return payload.get("response", []) if isinstance(payload, dict) else []


def fixture_teams(fixture: dict) -> tuple[str, str]:
    teams = fixture.get("teams", {}) or {}
    home = canonical_team_name(((teams.get("home") or {}).get("name")) or "")
    away = canonical_team_name(((teams.get("away") or {}).get("name")) or "")
    return away, home


def resolve_fixtures_for_slate(slate_games: list[tuple[str, str]]) -> dict[str, dict]:
    fixtures = load_fixtures()
    resolved: dict[str, dict] = {}
    wanted = {tuple(sorted(game)): game for game in slate_games}
    for item in fixtures:
        away, home = fixture_teams(item)
        key = tuple(sorted((away, home)))
        if key not in wanted:
            continue
        fixture = item.get("fixture", {}) or {}
        kickoff = fixture.get("date", "")
        fixture_id = fixture.get("id")
        for team in wanted[key]:
            resolved[team] = {"fixture_id": fixture_id, "kickoff": kickoff}
    for game in slate_games:
        for team in game:
            if team not in resolved:
                print(f"   ⚠️ Could not resolve fixture for {team}")
    return resolved


def fetch_lineup_status(fixture_id: int | str) -> dict[str, str]:
    payload = api_get("/fixtures/lineups", {"fixture": fixture_id})
    rows = payload.get("response", []) if isinstance(payload, dict) else []
    status: dict[str, str] = {}
    for team in rows:
        for section, label in (("startXI", "starter"), ("substitutes", "sub")):
            for item in team.get(section, []) or []:
                player = item.get("player", {}) or {}
                pid = str(player.get("id") or "")
                name = normalize_name(player.get("name", ""))
                if pid:
                    status[pid] = label
                if name:
                    status[name] = label
    return status


def confirmed_start_prob(player: dict, lineup_status: dict[str, str]) -> tuple[float, str]:
    status = lineup_status.get(str(player.get("API_Football_ID") or "")) or lineup_status.get(normalize_name(player["Player_Name"]))
    if status == "starter":
        return 1.0, "confirmed starter"
    if status == "sub":
        return 0.30, "confirmed substitute"
    return 0.05, "not listed in confirmed XI"


def daily_sort_key(row: dict) -> tuple:
    adjusted = -safe_float(row.get("Adjusted_EFP"))
    if row.get("Position") == "G":
        return (adjusted, -safe_float(row.get("Intl_Sample")), row.get("Player_Name", ""))
    return (adjusted, row.get("Player_Name", ""))


def tier_daily_slate(rows: list[dict]) -> list[dict]:
    for pos in ("G", "D", "MD", "FW"):
        pos_rows = sorted([r for r in rows if r["Position"] == pos], key=daily_sort_key)
        for idx, row in enumerate(pos_rows):
            if idx < 3:
                row["Tier"] = "S"
            elif idx < 8:
                row["Tier"] = "A"
            elif idx < 18:
                row["Tier"] = "B"
            else:
                row["Tier"] = "C"
    return rows


def build_daily_slate_rows(slate: str, mode: str) -> list[dict]:
    games = parse_slate(slate)
    opponent_map = opponent_map_from_games(games)
    players, squad_teams = load_player_pool(games)
    fixture_map: dict[str, dict] = {}
    lineup_by_fixture: dict[str, dict[str, str]] = {}
    if mode == "confirmed":
        fixture_map = resolve_fixtures_for_slate(games)
        for info in {str(v.get("fixture_id")): v for v in fixture_map.values() if v.get("fixture_id")}.values():
            fixture_id = str(info["fixture_id"])
            lineup_by_fixture[fixture_id] = fetch_lineup_status(fixture_id)
            print(f"   ✅ Loaded lineups for fixture {fixture_id}: {len(lineup_by_fixture[fixture_id])} player keys")

    rows = []
    for player in players:
        team = player["Team_Key"]
        opponent = opponent_map.get(team, "")
        kickoff = (fixture_map.get(team) or {}).get("kickoff", "")
        if mode == "confirmed":
            fixture_id = str((fixture_map.get(team) or {}).get("fixture_id") or "")
            start_prob, start_note = confirmed_start_prob(player, lineup_by_fixture.get(fixture_id, {}))
        else:
            start_prob = pre_xi_start_prob(player["Avg_Minutes"])
            start_note = f"pre-XI minutes proxy ({player['Avg_Minutes']:.1f} avg min)"
        adjusted = adjusted_score(player["EFP_Per_Match"], start_prob)
        notes = f"{start_note}; {player['Notes']}"
        rows.append(
            {
                "Player_Name": player["Player_Name"],
                "API_Football_ID": player["API_Football_ID"],
                "Team": player["Team"],
                "Opponent": display_team_name(opponent, squad_teams) if opponent else "",
                "Position": player["Position"],
                "EFP_Per_Match": player["EFP_Per_Match"],
                "Start_Prob": round(start_prob, 2),
                "Adjusted_EFP": adjusted,
                "Tier": "C",
                "Kickoff_Time": kickoff,
                "Source": mode,
                "Notes": notes,
                "Intl_Sample": player.get("Intl_Sample", 0),
            }
        )
    rows = tier_daily_slate(rows)
    return sorted(rows, key=lambda r: (r["Position"], daily_sort_key(r), r["Team"], r["Player_Name"]))


def sample_rows() -> list[dict]:
    rows = [
        {"Player_Name": "Luis Malagon", "API_Football_ID": "1", "Team": "Mexico", "Opponent": "South Africa", "Position": "G", "EFP_Per_Match": 4.0, "Start_Prob": 0.95, "Adjusted_EFP": 4.0, "Tier": "S", "Kickoff_Time": "2026-06-11T19:00:00+00:00", "Source": "pre-xi", "Notes": "sample"},
        {"Player_Name": "Edson Alvarez", "API_Football_ID": "2", "Team": "Mexico", "Opponent": "South Africa", "Position": "MD", "EFP_Per_Match": 3.1, "Start_Prob": 0.95, "Adjusted_EFP": 3.1, "Tier": "S", "Kickoff_Time": "2026-06-11T19:00:00+00:00", "Source": "pre-xi", "Notes": "sample"},
        {"Player_Name": "Santiago Gimenez", "API_Football_ID": "3", "Team": "Mexico", "Opponent": "South Africa", "Position": "FW", "EFP_Per_Match": 5.8, "Start_Prob": 0.75, "Adjusted_EFP": 4.93, "Tier": "S", "Kickoff_Time": "2026-06-11T19:00:00+00:00", "Source": "pre-xi", "Notes": "sample"},
        {"Player_Name": "Cesar Montes", "API_Football_ID": "4", "Team": "Mexico", "Opponent": "South Africa", "Position": "D", "EFP_Per_Match": 2.2, "Start_Prob": 0.95, "Adjusted_EFP": 2.2, "Tier": "S", "Kickoff_Time": "2026-06-11T19:00:00+00:00", "Source": "pre-xi", "Notes": "sample"},
        {"Player_Name": "Teboho Mokoena", "API_Football_ID": "5", "Team": "South Africa", "Opponent": "Mexico", "Position": "MD", "EFP_Per_Match": 3.4, "Start_Prob": 0.95, "Adjusted_EFP": 3.4, "Tier": "S", "Kickoff_Time": "2026-06-11T19:00:00+00:00", "Source": "pre-xi", "Notes": "sample"},
        {"Player_Name": "Percy Tau", "API_Football_ID": "6", "Team": "South Africa", "Opponent": "Mexico", "Position": "FW", "EFP_Per_Match": 4.6, "Start_Prob": 0.75, "Adjusted_EFP": 3.91, "Tier": "S", "Kickoff_Time": "2026-06-11T19:00:00+00:00", "Source": "pre-xi", "Notes": "sample"},
    ]
    return rows


def run(args: argparse.Namespace) -> list[dict]:
    if args.sample:
        rows = sample_rows()
    else:
        rows = build_daily_slate_rows(args.slate, args.mode)
    print("\n📊 Daily draft summary")
    print(f"   Mode: {args.mode}")
    print(f"   Players: {len(rows)}")
    print(f"   Teams: {sorted({r['Team'] for r in rows})}")
    by_pos: dict[str, int] = {}
    for row in rows:
        by_pos[row["Position"]] = by_pos.get(row["Position"], 0) + 1
    print(f"   Position counts: {by_pos}")
    if rows:
        top = max(rows, key=lambda r: safe_float(r["Adjusted_EFP"]))
        print(f"   Top player: {top['Player_Name']} ({top['Team']}) {top['Adjusted_EFP']} adj EFP")
    if args.dry_run:
        print("\n🧪 Dry run complete — skipped Google Sheets write")
        print(json.dumps(rows[:6], indent=2))
    else:
        safe_upload(DAILY_SLATE_SHEET_NAME, DAILY_SLATE_COLUMNS, rows)
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare World Cup daily draft slate")
    parser.add_argument("--slate", default="", help='Slate games, e.g. "RSA@MEX,CZE@KOR"')
    parser.add_argument("--mode", choices=["pre-xi", "confirmed"], default="pre-xi")
    parser.add_argument("--dry-run", action="store_true", help="Compute without writing Daily_Slate.")
    parser.add_argument("--sample", action="store_true", help="Use built-in sample rows for offline smoke testing.")
    args = parser.parse_args()
    if not args.sample and not args.slate:
        parser.error("--slate is required unless --sample is used")
    return args


if __name__ == "__main__":
    run(parse_args())
