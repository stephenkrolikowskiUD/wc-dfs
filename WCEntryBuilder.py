"""World Cup slip recommendation builder.

Reads the current WC pick slate and writes the best 3-, 4-, and 5-leg
Underdog-style slip recommendations to Google Sheets.
"""

from __future__ import annotations

import argparse
import itertools
import math
import re
import statistics
from datetime import datetime, timezone
from typing import Any

from WCDraftHelper import get_sheet_rows, safe_float, safe_upload


PICKS_CURRENT_SHEET_NAME = "Picks_Current"
SLIP_SHEET_NAME = "Slip_Recommendations"
SAME_GAME_SLIP_SHEET_NAME = "Same_Game_Slip_Recommendations"

SLIP_COLUMNS = [
    "Slip_Rank",
    "Slip_Size",
    "Payout_Multiplier",
    "Avg_Conviction",
    "Min_Conviction",
    "Teams_Represented",
    "SMASH_Count",
    "Slot_Number",
    "Player",
    "Team",
    "Prop",
    "Line",
    "Pick",
    "Tier",
    "Confidence",
    "Has_Form_Data",
    "Generated_At",
]

SAME_GAME_SLIP_COLUMNS = [
    "Fixture",
    "Slip_Rank",
    "Slip_Size",
    "Avg_Conviction",
    "Min_Conviction",
    "SMASH_Count",
    "Slot_Number",
    "Player",
    "Team",
    "Prop",
    "Line",
    "Pick",
    "Tier",
    "Confidence",
    "Generated_At",
]

PAYOUT_MULTIPLIERS = {3: "6x", 4: "10x", 5: "20x"}
SLIP_SIZES = (3, 4, 5)
SAME_GAME_SLIP_SIZES = (2, 3, 4)
TOP_SLIPS_PER_SIZE = 3
MIN_CONFIDENCE = 5.0
SAME_GAME_NARRATIVE_MULTIPLIER = 1.10
TIER_WEIGHTS = {"SMASH": 3.0, "STRONG": 2.0, "LEAN": 1.0}


def timestamp_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def normalize_tier(value: Any) -> str:
    tier = normalize_text(value).upper()
    return tier if tier in TIER_WEIGHTS else "LEAN"


def normalize_pick(value: Any) -> str:
    pick = normalize_text(value).upper()
    return "Under" if pick in {"UNDER", "U"} else "Over"


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = normalize_text(value).lower()
    return text in {"1", "true", "yes", "y"}


def has_form_data(row: dict[str, Any]) -> bool:
    if truthy(row.get("Has_Form_Data")):
        return True
    return safe_float(row.get("Intl_Sample") or row.get("Intl_Matches_Last_24mo")) > 0


def hydrate_pick(row: dict[str, Any]) -> dict[str, Any] | None:
    player = normalize_text(row.get("Player") or row.get("PLAYER_NAME") or row.get("player"))
    team = normalize_text(row.get("Team") or row.get("TEAM") or row.get("team"))
    opponent = normalize_text(row.get("Opponent") or row.get("OPPONENT") or row.get("opponent"))
    prop = normalize_text(row.get("Prop") or row.get("Metric") or row.get("METRIC"))
    pick = normalize_pick(row.get("Pick") or row.get("Lean") or row.get("PICK"))
    if not player or not team or not prop:
        return None

    confidence = safe_float(row.get("Confidence"), 0.0)
    tier = normalize_tier(row.get("Tier"))
    return {
        "Player": player,
        "Team": team,
        "Opponent": opponent,
        "Prop": prop,
        "Line": normalize_text(row.get("Line")),
        "Pick": pick,
        "Tier": tier,
        "Confidence": confidence,
        "Has_Form_Data": has_form_data(row),
    }


def fixture_key(pick: dict[str, Any]) -> tuple[str, str]:
    team = normalize_text(pick.get("Team"))
    opponent = normalize_text(pick.get("Opponent"))
    if not team or not opponent:
        return "", ""
    sides = sorted([team, opponent], key=normalize_key)
    return sides[0], sides[1]


def fixture_label_from_key(key: tuple[str, str]) -> str:
    return f"{key[0]} vs {key[1]}" if key[0] and key[1] else ""


def leg_conviction(pick: dict[str, Any]) -> float:
    tier_weight = TIER_WEIGHTS.get(str(pick.get("Tier", "")).upper(), 0.5)
    confidence = max(0.0, min(10.0, safe_float(pick.get("Confidence")))) / 10.0
    form_bonus = 1.15 if pick.get("Has_Form_Data") else 0.9
    return tier_weight * confidence * form_bonus


def valid_slip(slip: tuple[dict[str, Any], ...]) -> bool:
    teams = {normalize_key(pick.get("Team")) for pick in slip if pick.get("Team")}
    players = [normalize_key(pick.get("Player")) for pick in slip if pick.get("Player")]
    return len(teams) >= 2 and len(players) == len(set(players))


def score_slip(slip: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    leg_scores = [leg_conviction(pick) for pick in slip]
    return {
        "avg_conviction": statistics.mean(leg_scores),
        "min_conviction": min(leg_scores),
        "teams_represented": len({normalize_key(pick["Team"]) for pick in slip}),
        "smash_count": sum(1 for pick in slip if pick["Tier"] == "SMASH"),
    }


def valid_same_game_slip(slip: tuple[dict[str, Any], ...]) -> bool:
    players = [normalize_key(pick.get("Player")) for pick in slip if pick.get("Player")]
    if len(players) != len(set(players)):
        return False
    teams = {normalize_key(pick.get("Team")) for pick in slip if pick.get("Team")}
    return len(teams) >= 2


def score_same_game_slip(slip: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    leg_scores = [leg_conviction(pick) for pick in slip]
    return {
        "avg_conviction": statistics.mean(leg_scores) * SAME_GAME_NARRATIVE_MULTIPLIER,
        "min_conviction": min(leg_scores),
        "smash_count": sum(1 for pick in slip if pick["Tier"] == "SMASH"),
    }


def candidate_pool(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = [pick for pick in picks if safe_float(pick.get("Confidence")) >= MIN_CONFIDENCE]
    high_tier = [pick for pick in candidates if pick.get("Tier") in {"SMASH", "STRONG"}]
    if len(high_tier) >= max(SLIP_SIZES):
        candidates = high_tier
    return sorted(candidates, key=lambda p: (-leg_conviction(p), p["Player"], p["Prop"]))


def build_recommendations(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates = candidate_pool(picks)
    generated_at = timestamp_utc_iso()
    rows: list[dict[str, Any]] = []
    slip_rank = 1

    for size in SLIP_SIZES:
        scored = []
        for slip in itertools.combinations(candidates, size):
            if not valid_slip(slip):
                continue
            meta = score_slip(slip)
            scored.append((meta, slip))

        scored.sort(
            key=lambda item: (
                -item[0]["avg_conviction"],
                -item[0]["min_conviction"],
                -item[0]["smash_count"],
                -item[0]["teams_represented"],
            )
        )

        for meta, slip in scored[:TOP_SLIPS_PER_SIZE]:
            for slot, pick in enumerate(slip, start=1):
                rows.append(
                    {
                        "Slip_Rank": slip_rank,
                        "Slip_Size": size,
                        "Payout_Multiplier": PAYOUT_MULTIPLIERS[size],
                        "Avg_Conviction": round(meta["avg_conviction"], 3),
                        "Min_Conviction": round(meta["min_conviction"], 3),
                        "Teams_Represented": meta["teams_represented"],
                        "SMASH_Count": meta["smash_count"],
                        "Slot_Number": slot,
                        "Player": pick["Player"],
                        "Team": pick["Team"],
                        "Prop": pick["Prop"],
                        "Line": pick["Line"],
                        "Pick": pick["Pick"],
                        "Tier": pick["Tier"],
                        "Confidence": int(pick["Confidence"]) if float(pick["Confidence"]).is_integer() else pick["Confidence"],
                        "Has_Form_Data": "TRUE" if pick["Has_Form_Data"] else "FALSE",
                        "Generated_At": generated_at,
                    }
                )
            slip_rank += 1

    return rows


def build_same_game_recommendations(picks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    generated_at = timestamp_utc_iso()
    by_fixture: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pick in candidate_pool(picks):
        key = fixture_key(pick)
        if not all(key):
            continue
        by_fixture.setdefault(key, []).append(pick)

    rows: list[dict[str, Any]] = []
    for key, fixture_picks in sorted(by_fixture.items(), key=lambda item: fixture_label_from_key(item[0])):
        fixture = fixture_label_from_key(key)
        slip_rank = 1
        for size in SAME_GAME_SLIP_SIZES:
            scored = []
            if len(fixture_picks) < size:
                continue
            for slip in itertools.combinations(fixture_picks, size):
                if not valid_same_game_slip(slip):
                    continue
                meta = score_same_game_slip(slip)
                scored.append((meta, slip))

            scored.sort(
                key=lambda item: (
                    -item[0]["avg_conviction"],
                    -item[0]["min_conviction"],
                    -item[0]["smash_count"],
                )
            )

            for meta, slip in scored[:TOP_SLIPS_PER_SIZE]:
                for slot, pick in enumerate(slip, start=1):
                    rows.append(
                        {
                            "Fixture": fixture,
                            "Slip_Rank": slip_rank,
                            "Slip_Size": size,
                            "Avg_Conviction": round(meta["avg_conviction"], 3),
                            "Min_Conviction": round(meta["min_conviction"], 3),
                            "SMASH_Count": meta["smash_count"],
                            "Slot_Number": slot,
                            "Player": pick["Player"],
                            "Team": pick["Team"],
                            "Prop": pick["Prop"],
                            "Line": pick["Line"],
                            "Pick": pick["Pick"],
                            "Tier": pick["Tier"],
                            "Confidence": int(pick["Confidence"]) if float(pick["Confidence"]).is_integer() else pick["Confidence"],
                            "Generated_At": generated_at,
                        }
                    )
                slip_rank += 1
    return rows


def sample_picks() -> list[dict[str, Any]]:
    raw = [
        {"Player": "Santiago Gimenez", "Team": "Mexico", "Opponent": "South Africa", "Prop": "Shots", "Line": "2.5", "Pick": "Over", "Tier": "SMASH", "Confidence": 9, "Intl_Sample": 16},
        {"Player": "Edson Alvarez", "Team": "Mexico", "Opponent": "South Africa", "Prop": "Tackles", "Line": "2.5", "Pick": "Over", "Tier": "STRONG", "Confidence": 8, "Intl_Sample": 18},
        {"Player": "Raul Jimenez", "Team": "Mexico", "Opponent": "South Africa", "Prop": "SOT", "Line": "0.5", "Pick": "Over", "Tier": "STRONG", "Confidence": 7, "Intl_Sample": 20},
        {"Player": "Lyle Foster", "Team": "South Africa", "Opponent": "Mexico", "Prop": "Shots", "Line": "1.5", "Pick": "Over", "Tier": "SMASH", "Confidence": 8, "Intl_Sample": 10},
        {"Player": "Teboho Mokoena", "Team": "South Africa", "Opponent": "Mexico", "Prop": "Tackles", "Line": "1.5", "Pick": "Over", "Tier": "STRONG", "Confidence": 7, "Intl_Sample": 12},
        {"Player": "Patrik Schick", "Team": "Czechia", "Opponent": "South Korea", "Prop": "Goal Scorer", "Line": "0.5", "Pick": "Over", "Tier": "SMASH", "Confidence": 8, "Intl_Sample": 14},
        {"Player": "Son Heung-min", "Team": "South Korea", "Opponent": "Czechia", "Prop": "Shots", "Line": "2.5", "Pick": "Over", "Tier": "LEAN", "Confidence": 6, "Intl_Sample": 24},
    ]
    return [pick for pick in (hydrate_pick(row) for row in raw) if pick]


def run(dry_run: bool = False, sample: bool = False) -> list[dict[str, Any]]:
    if sample:
        picks = sample_picks()
        print(f"🧪 Loaded {len(picks)} sample pick(s)")
    else:
        rows = get_sheet_rows(PICKS_CURRENT_SHEET_NAME)
        picks = [pick for pick in (hydrate_pick(row) for row in rows) if pick]
        print(f"✅ Loaded {len(picks)} pick(s) from {PICKS_CURRENT_SHEET_NAME}")

    rows = build_recommendations(picks)
    same_game_rows = build_same_game_recommendations(picks)
    slip_count = len({row["Slip_Rank"] for row in rows})
    same_game_count = len({(row["Fixture"], row["Slip_Rank"]) for row in same_game_rows})
    print(f"🎫 Built {slip_count} cross-game slip recommendation(s), {len(rows)} leg row(s)")
    print(f"⚡ Built {same_game_count} same-game slip recommendation(s), {len(same_game_rows)} leg row(s)")

    if dry_run:
        for row in rows[: min(12, len(rows))]:
            print(row)
        for row in same_game_rows[: min(12, len(same_game_rows))]:
            print(row)
        return rows

    safe_upload(SLIP_SHEET_NAME, SLIP_COLUMNS, rows)
    safe_upload(SAME_GAME_SLIP_SHEET_NAME, SAME_GAME_SLIP_COLUMNS, same_game_rows)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Build World Cup slip recommendations.")
    parser.add_argument("--dry-run", action="store_true", help="Print rows without writing to Google Sheets")
    parser.add_argument("--sample", action="store_true", help="Use built-in sample picks instead of Google Sheets")
    args = parser.parse_args()
    run(dry_run=args.dry_run, sample=args.sample)


if __name__ == "__main__":
    main()
