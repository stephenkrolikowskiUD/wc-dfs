# World Cup Draft Helper v1
#
# Builds Underdog World Pup draft recommendations from the existing Player_Form
# tab. v1 is intentionally honest: EFP uses shots, SOT, tackles, and goals from
# Player_Form plus light priors for missing UD scoring fields.

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import statistics
import unicodedata
from datetime import datetime, timezone
from typing import Any

import gspread
from google.auth import default
from google.oauth2.service_account import Credentials


SHEET_ID = "1ZijOHruRgILnyR4H_jJh3pQrU3A9PJepWMLtRf3Ie9g"
FORM_SHEET_NAME = "Player_Form"
EFP_SHEET_NAME = "Player_EFP"
SURVIVAL_SHEET_NAME = "Team_Survival"
DRAFT_SHEET_NAME = "Draft_Recommendations"
SQUAD_CACHE_PATH = "squad_cache.json"

ROSTER_SIZE = 12
MAX_PLAYERS_PER_TEAM = 4
DEFAULT_SIMULATIONS = 1000
DEFAULT_RANDOM_SEEDS = 8

EFP_COLUMNS = [
    "Player_Name",
    "API_Football_ID",
    "Team",
    "Position",
    "EFP_Per_Match",
    "Intl_Sample",
    "Notes",
]

SURVIVAL_COLUMNS = [
    "Team",
    "API_Football_ID",
    "Expected_Matches_Remaining",
    "Confidence",
    "Source",
]

DRAFT_COLUMNS = [
    "Recommendation_Rank",
    "Slot",
    "Player",
    "Team",
    "Position",
    "EFP_Per_Match",
    "Expected_Matches",
    "ETFP",
    "Notes",
]

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
    "greece": "greece",
    "iran": "iran",
    "ir iran": "iran",
    "italy": "italy",
    "ivory coast": "cote d ivoire",
    "cote divoire": "cote d ivoire",
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
    "turkiye": "turkiye",
    "ukraine": "ukraine",
    "united states": "usa",
    "usa": "usa",
    "us": "usa",
    "uruguay": "uruguay",
    "wales": "wales",
}

# v1 survival proxy. These are intentionally broad tiers, not bookmaker-grade
# futures. Replace with Odds API outrights or group-path probabilities later.
TEAM_EXPECTED_MATCHES = {
    "argentina": 6.3,
    "brazil": 6.3,
    "france": 6.3,
    "england": 6.0,
    "spain": 6.0,
    "portugal": 5.8,
    "germany": 5.8,
    "netherlands": 5.5,
    "belgium": 5.2,
    "italy": 5.0,
    "uruguay": 5.0,
    "colombia": 5.0,
    "croatia": 4.8,
    "mexico": 4.7,
    "united states": 4.7,
    "usa": 4.7,
    "morocco": 4.6,
    "switzerland": 4.5,
    "denmark": 4.4,
    "senegal": 4.4,
    "japan": 4.3,
    "austria": 4.2,
    "serbia": 4.1,
    "poland": 4.0,
    "ecuador": 4.0,
    "south korea": 3.9,
    "turkiye": 3.9,
    "ukraine": 3.8,
    "sweden": 3.8,
    "norway": 3.8,
    "canada": 3.7,
    "australia": 3.7,
    "ghana": 3.7,
    "nigeria": 3.7,
    "scotland": 3.6,
    "costa rica": 3.5,
    "panama": 3.4,
    "paraguay": 3.4,
    "peru": 3.4,
    "chile": 3.4,
    "saudi arabia": 3.3,
    "south africa": 3.3,
    "tunisia": 3.3,
    "egypt": 3.3,
    "qatar": 3.2,
    "new zealand": 3.2,
    "iran": 3.2,
    "wales": 3.2,
}

POSITION_MAP = {
    "goalkeeper": "G",
    "g": "G",
    "defender": "D",
    "d": "D",
    "midfielder": "MD",
    "midfield": "MD",
    "m": "MD",
    "md": "MD",
    "attacker": "FW",
    "forward": "FW",
    "fw": "FW",
}


def timestamp_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_cell(val: Any) -> Any:
    if val is None:
        return ""
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return ""
    return val


def normalize_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[’'`\.]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_team_name(name: str) -> str:
    norm = normalize_name(name)
    return TEAM_NAME_ALIASES.get(norm, norm)


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if math.isnan(float(value)) or math.isinf(float(value)):
            return default
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def position_code(raw: str) -> str:
    return POSITION_MAP.get(normalize_name(raw), "")


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
        print(f"⚠️ {SQUAD_CACHE_PATH} not found — using Player_Form-only fallback")
        return {}
    with open(SQUAD_CACHE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def build_squad_indexes(squad_cache: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    by_id: dict[str, dict] = {}
    teams: dict[str, dict] = {}
    for team_key, team in (squad_cache or {}).items():
        team_name = team.get("team_name") or team_key
        team_id = team.get("team_id", "")
        canonical = canonical_team_name(team_name)
        teams[canonical] = {"Team": team_name, "API_Football_ID": team_id}
        for player in team.get("players", []) or []:
            api_id = str(player.get("api_id") or "")
            if not api_id:
                continue
            by_id[api_id] = {
                "Team": team_name,
                "Team_ID": team_id,
                "Position": position_code(player.get("position", "")),
                "Player_Name": player.get("name", ""),
            }
    return by_id, teams


def infer_position_from_form(row: dict) -> str:
    # Last-resort fallback for local dry runs without squad_cache.json.
    shots = safe_float(row.get("Avg_Shots"))
    tackles = safe_float(row.get("Avg_Tackles"))
    goals = safe_float(row.get("Goals_Per_Match"))
    if shots >= 1.8 or goals >= 0.20:
        return "FW"
    if tackles >= 1.8:
        return "D"
    return "MD"


def compute_efp_per_match(form_row: dict, position: str) -> tuple[float, str]:
    shots = safe_float(form_row.get("Avg_Shots"))
    sot = safe_float(form_row.get("Avg_SOT"))
    tackles = safe_float(form_row.get("Avg_Tackles"))
    goals = safe_float(form_row.get("Goals_Per_Match"))
    sample = safe_float(form_row.get("Intl_Matches_Last_24mo"))
    shots_off = max(shots - sot, 0.0)

    if position == "G":
        # Player_Form does not yet include saves, wins, clean sheets, or goals conceded.
        efp = 4.0
        note = "GK tie-break: ranked by intl caps (EFP stubbed pending v1.1); v1 excludes saves, wins, goals conceded, and clean sheets"
        return round(efp, 2), note

    assist_proxy = goals * 0.5
    efp = (
        goals * 8.0
        + assist_proxy * 4.0
        + sot * 2.0
        + shots_off * 1.0
        + tackles * 0.5
    )
    notes = ["EFP v1 excludes chances, crosses, passes, and clean-sheet bonuses"]
    if sample < 5:
        notes.append("limited international sample")
    if not position:
        notes.append("position inferred")
    return round(max(efp, 0.0), 2), "; ".join(notes)


def compute_player_efp_rows(form_rows: list[dict], squad_by_id: dict[str, dict]) -> list[dict]:
    rows = []
    for row in form_rows:
        api_id = str(row.get("API_Football_ID") or "").strip()
        squad = squad_by_id.get(api_id, {})
        team = squad.get("Team") or row.get("Team") or row.get("Nationality") or ""
        position = squad.get("Position") or position_code(row.get("Position", "")) or infer_position_from_form(row)
        efp, notes = compute_efp_per_match(row, position)
        rows.append(
            {
                "Player_Name": row.get("Player_Name", ""),
                "API_Football_ID": api_id,
                "Team": team,
                "Position": position,
                "EFP_Per_Match": efp,
                "Intl_Sample": int(safe_float(row.get("Intl_Matches_Last_24mo"))),
                "Notes": notes,
            }
        )
    rows = [r for r in rows if r["Player_Name"]]
    return sorted(rows, key=lambda r: (canonical_team_name(r.get("Team", "")), r.get("Position", ""), -safe_float(r.get("EFP_Per_Match"))))


def expected_matches_for_team(team: str) -> tuple[float, str, str]:
    canonical = canonical_team_name(team)
    if canonical in TEAM_EXPECTED_MATCHES:
        val = TEAM_EXPECTED_MATCHES[canonical]
        confidence = "medium" if val >= 4.0 else "low"
        return val, confidence, "FIFA_ranking_proxy"
    return 3.2, "low", "FIFA_ranking_proxy_default"


def compute_team_survival_rows(teams: dict[str, dict], efp_rows: list[dict]) -> list[dict]:
    all_teams = dict(teams)
    for row in efp_rows:
        canonical = canonical_team_name(row.get("Team", ""))
        if canonical:
            all_teams.setdefault(canonical, {"Team": row.get("Team", ""), "API_Football_ID": ""})
    rows = []
    for canonical, item in sorted(all_teams.items(), key=lambda kv: kv[1].get("Team", kv[0])):
        team = item.get("Team") or canonical
        expected, confidence, source = expected_matches_for_team(team)
        rows.append(
            {
                "Team": team,
                "API_Football_ID": item.get("API_Football_ID", ""),
                "Expected_Matches_Remaining": expected,
                "Confidence": confidence,
                "Source": source,
            }
        )
    return rows


def team_survival_map(survival_rows: list[dict]) -> dict[str, float]:
    return {
        canonical_team_name(row.get("Team", "")): safe_float(row.get("Expected_Matches_Remaining"), 3.2)
        for row in survival_rows
    }


def candidate_from_efp(row: dict, survival: dict[str, float]) -> dict:
    team = row.get("Team", "")
    expected_matches = survival.get(canonical_team_name(team), 3.2)
    efp = safe_float(row.get("EFP_Per_Match"))
    return {
        "player": row.get("Player_Name", ""),
        "api_id": row.get("API_Football_ID", ""),
        "team": team,
        "position": row.get("Position", ""),
        "efp": efp,
        "expected_matches": expected_matches,
        "etfp": efp * expected_matches,
        "notes": row.get("Notes", ""),
        "sample": int(safe_float(row.get("Intl_Sample"))),
    }


def roster_counts(roster: list[dict]) -> dict[str, int]:
    counts = {"G": 0, "D": 0, "MD": 0, "FW": 0}
    for player in roster:
        pos = player.get("position")
        if pos in counts:
            counts[pos] += 1
    return counts


def is_valid_roster(roster: list[dict]) -> bool:
    if len(roster) != ROSTER_SIZE:
        return False
    counts = roster_counts(roster)
    outfield = counts["D"] + counts["MD"] + counts["FW"]
    if counts["G"] < 1 or counts["D"] < 1 or counts["MD"] < 1 or counts["FW"] < 2 or outfield < 5:
        return False
    team_counts: dict[str, int] = {}
    for player in roster:
        team_counts[player["team"]] = team_counts.get(player["team"], 0) + 1
        if team_counts[player["team"]] > MAX_PLAYERS_PER_TEAM:
            return False
    return True


def can_add_player(roster: list[dict], candidate: dict) -> bool:
    if any(p["api_id"] == candidate["api_id"] and p["api_id"] for p in roster):
        return False
    if any(p["player"] == candidate["player"] and p["team"] == candidate["team"] for p in roster):
        return False
    if sum(1 for p in roster if p["team"] == candidate["team"]) >= MAX_PLAYERS_PER_TEAM:
        return False
    return candidate.get("position") in {"G", "D", "MD", "FW"}


def expected_team_periods(expected_matches: float) -> list[float]:
    # Group stage is three matches in one scoring period. Five knockout periods
    # are modeled from expected extra matches.
    extra = max(expected_matches - 3.0, 0.0)
    periods = [3.0]
    for idx in range(5):
        periods.append(min(max(extra - idx, 0.0), 1.0))
    return periods


def auto_lineup_score(players: list[dict], period_multiplier: float) -> float:
    if not players or period_multiplier <= 0:
        return 0.0
    used: set[int] = set()

    def take_best(pos: str, count: int = 1) -> list[dict]:
        pool = [(idx, p) for idx, p in enumerate(players) if p.get("position") == pos and idx not in used]
        if pos == "G":
            pool = sorted(pool, key=lambda item: gk_sort_key(item[1]))
        else:
            pool = sorted(pool, key=lambda item: item[1]["efp"], reverse=True)
        chosen = []
        for idx, player in pool[:count]:
            used.add(idx)
            chosen.append(player)
        return chosen

    lineup = []
    lineup.extend(take_best("G"))
    lineup.extend(take_best("D"))
    lineup.extend(take_best("MD"))
    lineup.extend(take_best("FW", 2))
    flex_pool = sorted(
        [(idx, p) for idx, p in enumerate(players) if p.get("position") in {"D", "MD", "FW"} and idx not in used],
        key=lambda item: item[1]["efp"],
        reverse=True,
    )
    if flex_pool:
        lineup.append(flex_pool[0][1])
    return sum(p["efp"] for p in lineup) * period_multiplier


def expected_roster_score(roster: list[dict]) -> float:
    score = 0.0
    for period_idx in range(6):
        active = []
        for player in roster:
            periods = expected_team_periods(player["expected_matches"])
            prob_or_matches = periods[period_idx] if period_idx < len(periods) else 0.0
            if prob_or_matches > 0:
                adjusted = dict(player)
                adjusted["efp"] = player["efp"] * prob_or_matches
                active.append(adjusted)
        if not active:
            continue
        score += auto_lineup_score(active, 1.0)
    return score


def simulate_extra_rounds(expected_matches: float, rng: random.Random) -> int:
    extra = max(expected_matches - 3.0, 0.0)
    whole = int(math.floor(extra))
    frac = extra - whole
    return min(5, whole + (1 if rng.random() < frac else 0))


def simulate_roster_score(roster: list[dict], n_simulations: int = DEFAULT_SIMULATIONS, seed: int = 0) -> float:
    rng = random.Random(seed)
    total_scores = []
    for _ in range(n_simulations):
        extra_by_team = {
            canonical_team_name(player["team"]): simulate_extra_rounds(player["expected_matches"], rng)
            for player in roster
        }
        total = 0.0
        for period_idx in range(6):
            if period_idx == 0:
                active = roster
                multiplier = 3.0
            else:
                active = [
                    player
                    for player in roster
                    if extra_by_team.get(canonical_team_name(player["team"]), 0) >= period_idx
                ]
                multiplier = 1.0
            total += auto_lineup_score(active, multiplier)
        total_scores.append(total)
    return statistics.mean(total_scores) if total_scores else 0.0


def sorted_candidates(candidates: list[dict], rng: random.Random) -> list[dict]:
    return sorted(candidates, key=lambda p: p["etfp"] * rng.uniform(0.92, 1.08), reverse=True)


def gk_sort_key(player: dict) -> tuple:
    """
    Goalkeepers are on a stubbed EFP baseline in v1.
    Break ties by international sample so likely #1 keepers beat backups.
    """
    return (-player["efp"], -player.get("sample", 0), -player.get("expected_matches", 0), player.get("player", ""))


def build_greedy_roster(candidates: list[dict], seed: int) -> list[dict]:
    rng = random.Random(seed)
    ordered = sorted_candidates(candidates, rng)
    gk_ordered = sorted([c for c in candidates if c.get("position") == "G"], key=gk_sort_key)
    roster: list[dict] = []

    def add_best(predicate, count: int, pool: list[dict] | None = None) -> None:
        nonlocal roster
        for cand in pool or ordered:
            if len([p for p in roster if predicate(p)]) >= count:
                return
            if predicate(cand) and can_add_player(roster, cand):
                roster.append(cand)

    add_best(lambda p: p["position"] == "G", 1, gk_ordered)
    add_best(lambda p: p["position"] == "D", 1)
    add_best(lambda p: p["position"] == "MD", 1)
    add_best(lambda p: p["position"] == "FW", 2)
    add_best(lambda p: p["position"] in {"D", "MD", "FW"}, 5)

    for cand in ordered:
        if len(roster) >= ROSTER_SIZE:
            break
        if can_add_player(roster, cand):
            roster.append(cand)
    return roster


def optimize_roster(candidates: list[dict], seed: int) -> list[dict]:
    roster = build_greedy_roster(candidates, seed)
    if len(roster) < ROSTER_SIZE:
        return roster
    best_score = expected_roster_score(roster)
    for _ in range(3):
        improved = False
        drafted_keys = {(p["api_id"], p["player"], p["team"]) for p in roster}
        for out_idx, outgoing in enumerate(list(roster)):
            for incoming in candidates:
                key = (incoming["api_id"], incoming["player"], incoming["team"])
                if key in drafted_keys:
                    continue
                trial = list(roster)
                trial[out_idx] = incoming
                if not is_valid_roster(trial):
                    continue
                score = expected_roster_score(trial)
                if score > best_score + 0.01:
                    roster = trial
                    best_score = score
                    improved = True
                    break
            if improved:
                break
        if not improved:
            break
    return roster


def roster_signature(roster: list[dict]) -> tuple:
    return tuple(sorted((p.get("api_id") or p["player"], p["team"]) for p in roster))


def build_recommendations(candidates: list[dict], simulations: int, seeds: int) -> list[dict]:
    valid_candidates = [
        c
        for c in candidates
        if c["player"] and c["team"] and c["position"] in {"G", "D", "MD", "FW"} and c["efp"] > 0
    ]
    print(f"🧮 Draft candidate pool: {len(valid_candidates)} player(s)")
    rosters = []
    seen = set()
    for seed in range(seeds):
        roster = optimize_roster(valid_candidates, seed)
        if not is_valid_roster(roster):
            print(f"   ⚠️ Seed {seed} could not produce a valid 12-player roster")
            continue
        sig = roster_signature(roster)
        if sig in seen:
            continue
        seen.add(sig)
        score = simulate_roster_score(roster, n_simulations=simulations, seed=1000 + seed)
        rosters.append({"roster": roster, "score": score})
        print(f"   Seed {seed}: simulated score {score:.2f}")
    rosters = sorted(rosters, key=lambda r: r["score"], reverse=True)[:3]
    return rosters


def assign_slots(roster: list[dict]) -> list[tuple[str, dict]]:
    remaining = sorted(roster, key=lambda p: p["efp"], reverse=True)
    assigned: list[tuple[str, dict]] = []

    def pop_best(pos: str) -> dict | None:
        matches = [(idx, player) for idx, player in enumerate(remaining) if player["position"] == pos]
        if not matches:
            return None
        if pos == "G":
            idx, _ = min(matches, key=lambda item: gk_sort_key(item[1]))
        else:
            idx, _ = max(matches, key=lambda item: item[1]["efp"])
        return remaining.pop(idx)

    for slot, pos in [("G", "G"), ("D", "D"), ("MD", "MD"), ("FW", "FW"), ("FW", "FW")]:
        player = pop_best(pos)
        if player:
            assigned.append((slot, player))
    flex_candidates = [(idx, p) for idx, p in enumerate(remaining) if p["position"] in {"D", "MD", "FW"}]
    if flex_candidates:
        idx, player = max(flex_candidates, key=lambda item: item[1]["efp"])
        assigned.append(("FLEX", player))
        remaining.pop(idx)
    bench_num = 1
    for player in sorted(remaining, key=lambda p: p["etfp"], reverse=True):
        assigned.append((f"BENCH {bench_num}", player))
        bench_num += 1
    return assigned


def recommendation_rows(recommendations: list[dict]) -> list[dict]:
    rows = []
    for rank, rec in enumerate(recommendations, start=1):
        assigned = assign_slots(rec["roster"])
        for slot, player in assigned:
            rows.append(
                {
                    "Recommendation_Rank": rank,
                    "Slot": slot,
                    "Player": player["player"],
                    "Team": player["team"],
                    "Position": player["position"],
                    "EFP_Per_Match": round(player["efp"], 2),
                    "Expected_Matches": round(player["expected_matches"], 2),
                    "ETFP": round(player["etfp"], 2),
                    "Notes": f"Sim score {rec['score']:.1f}; {player['notes']}",
                }
            )
    return rows


def sample_form_rows() -> list[dict]:
    return [
        {"Player_Name": "Kylian Mbappe", "API_Football_ID": "1", "Nationality": "France", "Position": "FW", "Intl_Matches_Last_24mo": "12", "Avg_Shots": "3.4", "Avg_SOT": "1.6", "Avg_Tackles": "0.2", "Goals_Per_Match": "0.7"},
        {"Player_Name": "Jude Bellingham", "API_Football_ID": "2", "Nationality": "England", "Position": "MD", "Intl_Matches_Last_24mo": "11", "Avg_Shots": "2.0", "Avg_SOT": "0.8", "Avg_Tackles": "1.7", "Goals_Per_Match": "0.25"},
        {"Player_Name": "Vinicius Junior", "API_Football_ID": "3", "Nationality": "Brazil", "Position": "FW", "Intl_Matches_Last_24mo": "9", "Avg_Shots": "2.8", "Avg_SOT": "1.1", "Avg_Tackles": "0.4", "Goals_Per_Match": "0.33"},
        {"Player_Name": "Edson Alvarez", "API_Football_ID": "4", "Nationality": "Mexico", "Position": "MD", "Intl_Matches_Last_24mo": "16", "Avg_Shots": "1.25", "Avg_SOT": "0.25", "Avg_Tackles": "2.8", "Goals_Per_Match": "0.05"},
        {"Player_Name": "Achraf Hakimi", "API_Football_ID": "5", "Nationality": "Morocco", "Position": "D", "Intl_Matches_Last_24mo": "10", "Avg_Shots": "1.2", "Avg_SOT": "0.3", "Avg_Tackles": "2.1", "Goals_Per_Match": "0.1"},
        {"Player_Name": "Alisson", "API_Football_ID": "6", "Nationality": "Brazil", "Position": "G", "Intl_Matches_Last_24mo": "8", "Avg_Shots": "0", "Avg_SOT": "0", "Avg_Tackles": "0", "Goals_Per_Match": "0"},
        {"Player_Name": "Christian Pulisic", "API_Football_ID": "7", "Nationality": "United States", "Position": "FW", "Intl_Matches_Last_24mo": "13", "Avg_Shots": "2.5", "Avg_SOT": "1.0", "Avg_Tackles": "0.7", "Goals_Per_Match": "0.31"},
        {"Player_Name": "Bruno Fernandes", "API_Football_ID": "8", "Nationality": "Portugal", "Position": "MD", "Intl_Matches_Last_24mo": "14", "Avg_Shots": "2.4", "Avg_SOT": "0.9", "Avg_Tackles": "1.2", "Goals_Per_Match": "0.28"},
        {"Player_Name": "Virgil van Dijk", "API_Football_ID": "9", "Nationality": "Netherlands", "Position": "D", "Intl_Matches_Last_24mo": "10", "Avg_Shots": "0.8", "Avg_SOT": "0.2", "Avg_Tackles": "1.9", "Goals_Per_Match": "0.1"},
        {"Player_Name": "Pedri", "API_Football_ID": "10", "Nationality": "Spain", "Position": "MD", "Intl_Matches_Last_24mo": "9", "Avg_Shots": "1.1", "Avg_SOT": "0.3", "Avg_Tackles": "1.5", "Goals_Per_Match": "0.08"},
        {"Player_Name": "Lionel Messi", "API_Football_ID": "11", "Nationality": "Argentina", "Position": "FW", "Intl_Matches_Last_24mo": "10", "Avg_Shots": "3.0", "Avg_SOT": "1.3", "Avg_Tackles": "0.3", "Goals_Per_Match": "0.6"},
        {"Player_Name": "Theo Hernandez", "API_Football_ID": "12", "Nationality": "France", "Position": "D", "Intl_Matches_Last_24mo": "8", "Avg_Shots": "1.0", "Avg_SOT": "0.3", "Avg_Tackles": "1.7", "Goals_Per_Match": "0.1"},
    ]


def run(args: argparse.Namespace) -> list[dict]:
    squad_cache = load_squad_cache()
    squad_by_id, squad_teams = build_squad_indexes(squad_cache)
    form_rows = sample_form_rows() if args.sample else get_sheet_rows(FORM_SHEET_NAME)
    if not form_rows:
        raise RuntimeError("Player_Form is empty. Run WCFormPull.py before WCDraftHelper.py.")

    efp_rows = compute_player_efp_rows(form_rows, squad_by_id)
    survival_rows = compute_team_survival_rows(squad_teams, efp_rows)
    survival = team_survival_map(survival_rows)
    candidates = [candidate_from_efp(row, survival) for row in efp_rows]
    recommendations = build_recommendations(candidates, args.simulations, args.seeds)
    draft_rows = recommendation_rows(recommendations)

    print("\n📊 Draft helper summary")
    print(f"   Player_EFP rows: {len(efp_rows)}")
    print(f"   Team_Survival rows: {len(survival_rows)}")
    print(f"   Draft recommendation rows: {len(draft_rows)}")
    if draft_rows:
        top = draft_rows[0]
        print(f"   Top slot: {top['Slot']} — {top['Player']} ({top['Team']}) ETFP {top['ETFP']}")

    if args.dry_run:
        print("\n🧪 Dry run complete — skipped Google Sheets writes")
        if draft_rows:
            print(json.dumps(draft_rows[:3], indent=2))
    else:
        safe_upload(EFP_SHEET_NAME, EFP_COLUMNS, efp_rows)
        safe_upload(SURVIVAL_SHEET_NAME, SURVIVAL_COLUMNS, survival_rows)
        safe_upload(DRAFT_SHEET_NAME, DRAFT_COLUMNS, draft_rows)
    return draft_rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build World Cup draft helper recommendations")
    parser.add_argument("--dry-run", action="store_true", help="Compute locally without writing to Google Sheets.")
    parser.add_argument("--sample", action="store_true", help="Use built-in sample Player_Form rows for offline smoke testing.")
    parser.add_argument("--simulations", type=int, default=DEFAULT_SIMULATIONS, help="Monte Carlo simulations per roster.")
    parser.add_argument("--seeds", type=int, default=DEFAULT_RANDOM_SEEDS, help="Greedy+swap random seeds to try.")
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
