# World Cup post-mortem analyzer
#
# Reads manually entered top Underdog rosters, enriches them with the model's
# Player_EFP view, then writes calibration and construction insights.

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone

import gspread

from WCDraftHelper import (
    EFP_SHEET_NAME,
    SHEET_ID,
    get_gspread_client,
    get_sheet_rows,
    normalize_name,
    safe_float,
    safe_upload,
)


TOP_ROSTERS_SHEET_NAME = "Top_Rosters"
POST_MORTEM_SHEET_NAME = "Post_Mortem_Analysis"

TOP_ROSTERS_COLUMNS = [
    "contest_date",
    "contest_type",
    "slate",
    "finishing_position",
    "total_score",
    "total_entries",
    "my_rank",
    "my_score",
    "roster_slot",
    "player_name",
    "player_team",
    "actual_points",
    "my_efp_rating",
    "my_tier",
    "i_drafted",
    "notes",
]

ANALYSIS_COLUMNS = [
    "Section",
    "Rank",
    "Player",
    "Team",
    "Metric",
    "Appearances",
    "My_Avg_EFP",
    "My_Avg_Tier",
    "Their_Avg_Points",
    "Value",
    "Details",
    "Generated_At",
]

TIER_ORDER = {"S": 1, "A": 2, "B": 3, "C": 4, "": 5}


def timestamp_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_bool(value: str) -> str:
    text = str(value or "").strip().upper()
    if text in {"TRUE", "YES", "Y", "1"}:
        return "TRUE"
    if text in {"FALSE", "NO", "N", "0"}:
        return "FALSE"
    return ""


def row_date(row: dict) -> datetime | None:
    raw = str(row.get("contest_date") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def normalize_row(row: dict) -> dict:
    out = {}
    lower = {str(k).strip().lower(): v for k, v in row.items()}
    for col in TOP_ROSTERS_COLUMNS:
        out[col] = lower.get(col.lower(), row.get(col, ""))
    return out


def position_from_slot(slot: str) -> str:
    text = str(slot or "").strip().upper()
    if text.startswith("G"):
        return "G"
    if text.startswith("D"):
        return "D"
    if text.startswith("MD") or text.startswith("M"):
        return "MD"
    if text.startswith("FW") or text.startswith("F"):
        return "FW"
    return text


def model_tiers(efp_rows: list[dict]) -> dict[tuple[str, str], str]:
    by_pos: dict[str, list[dict]] = defaultdict(list)
    for row in efp_rows:
        pos = str(row.get("Position") or "").strip().upper()
        if pos:
            by_pos[pos].append(row)
    tiers = {}
    for pos, rows in by_pos.items():
        ranked = sorted(rows, key=lambda r: -safe_float(r.get("EFP_Spread_Adjusted") or r.get("EFP_Per_Match"), 0.0))
        for idx, row in enumerate(ranked):
            tier = "S" if idx < 3 else "A" if idx < 8 else "B" if idx < 18 else "C"
            key = player_key(row.get("Player_Name", ""), row.get("Team", ""))
            tiers[key] = tier
    return tiers


def player_key(name: str, team: str) -> tuple[str, str]:
    return normalize_name(name), normalize_name(team)


def last_name_key(name: str, team: str) -> tuple[str, str]:
    parts = normalize_name(name).split()
    return (parts[-1] if parts else "", normalize_name(team))


def load_model_maps() -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], list[dict]], dict[tuple[str, str], str]]:
    rows = get_sheet_rows(EFP_SHEET_NAME)
    exact = {}
    by_last: dict[tuple[str, str], list[dict]] = defaultdict(list)
    tiers = model_tiers(rows)
    for row in rows:
        exact[player_key(row.get("Player_Name", ""), row.get("Team", ""))] = row
        by_last[last_name_key(row.get("Player_Name", ""), row.get("Team", ""))].append(row)
    return exact, by_last, tiers


def find_model_row(row: dict, exact: dict, by_last: dict) -> dict | None:
    key = player_key(row.get("player_name", ""), row.get("player_team", ""))
    if key in exact:
        return exact[key]
    matches = by_last.get(last_name_key(row.get("player_name", ""), row.get("player_team", "")), [])
    slot_pos = position_from_slot(row.get("roster_slot", ""))
    if slot_pos:
        pos_matches = [m for m in matches if str(m.get("Position") or "").upper() == slot_pos]
        if len(pos_matches) == 1:
            return pos_matches[0]
    if len(matches) == 1:
        return matches[0]
    return None


def ensure_top_rosters_sheet() -> None:
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(TOP_ROSTERS_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=TOP_ROSTERS_SHEET_NAME, rows=1000, cols=len(TOP_ROSTERS_COLUMNS))
        ws.update("A1", [TOP_ROSTERS_COLUMNS], value_input_option="USER_ENTERED")
        print(f"✅ Created {TOP_ROSTERS_SHEET_NAME} template")
        return
    headers = ws.row_values(1)
    if not headers:
        ws.update("A1", [TOP_ROSTERS_COLUMNS], value_input_option="USER_ENTERED")
        print(f"✅ Initialized {TOP_ROSTERS_SHEET_NAME} header")


def enrich_top_rosters(rows: list[dict]) -> list[dict]:
    exact, by_last, tiers = load_model_maps()
    enriched = []
    for raw in rows:
        row = normalize_row(raw)
        model = find_model_row(row, exact, by_last)
        if model:
            row["my_efp_rating"] = row.get("my_efp_rating") or model.get("EFP_Spread_Adjusted") or model.get("EFP_Per_Match") or model.get("EFP_Regressed") or ""
            tier = tiers.get(player_key(model.get("Player_Name", ""), model.get("Team", "")), "")
            row["my_tier"] = row.get("my_tier") or tier
        row["i_drafted"] = clean_bool(row.get("i_drafted")) or row.get("i_drafted", "")
        enriched.append(row)
    return enriched


def filter_rows(rows: list[dict], since: str = "", contest: str = "") -> list[dict]:
    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since).replace(tzinfo=timezone.utc)
    out = []
    for row in rows:
        if contest and str(row.get("contest_type", "")).strip().lower() != contest.strip().lower():
            continue
        if since_dt:
            dt = row_date(row)
            if dt and dt < since_dt:
                continue
        out.append(row)
    return out


def avg(values: list[float]) -> float:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else 0.0


def player_groups(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        name = str(row.get("player_name") or "").strip()
        team = str(row.get("player_team") or "").strip()
        if name:
            grouped[(name, team)].append(row)
    out = []
    for (name, team), items in grouped.items():
        tiers = [str(i.get("my_tier") or "").strip().upper() for i in items]
        tier_score = avg([TIER_ORDER.get(t, 5) for t in tiers])
        out.append(
            {
                "player": name,
                "team": team,
                "appearances": len(items),
                "avg_efp": avg([safe_float(i.get("my_efp_rating"), 0.0) for i in items]),
                "avg_tier_score": tier_score,
                "avg_tier": score_to_tier(tier_score),
                "avg_points": avg([safe_float(i.get("actual_points"), 0.0) for i in items]),
            }
        )
    return out


def score_to_tier(score: float) -> str:
    if score <= 1.25:
        return "S"
    if score <= 2.25:
        return "A"
    if score <= 3.25:
        return "B"
    if score <= 4.25:
        return "C"
    return "—"


def roster_key(row: dict) -> tuple[str, str, str, str]:
    return (
        str(row.get("contest_date") or ""),
        str(row.get("contest_type") or ""),
        str(row.get("slate") or ""),
        str(row.get("finishing_position") or ""),
    )


def analyze_construction(rows: list[dict]) -> list[dict]:
    rosters: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        rosters[roster_key(row)].append(row)
    if not rosters:
        return []
    max_stack = []
    teams_per_roster = []
    common_stacks = Counter()
    for roster in rosters.values():
        teams = [str(p.get("player_team") or "").strip() for p in roster if p.get("player_team")]
        team_counts = Counter(teams)
        if team_counts:
            max_stack.append(max(team_counts.values()))
            teams_per_roster.append(len(team_counts))
            for team, count in team_counts.items():
                common_stacks[f"{count} from {team}"] += 1
    rows_out = [
        ("Avg max same-team stack", round(avg(max_stack), 2), f"{len(rosters)} top roster(s) analyzed"),
        ("Avg teams represented", round(avg(teams_per_roster), 2), "Lower means more stacking; higher means more diversification"),
    ]
    for label, count in common_stacks.most_common(8):
        rows_out.append((f"Common stack: {label}", count, "Number of top rosters with this team stack"))
    return rows_out


def analysis_rows(rows: list[dict]) -> list[dict]:
    generated = timestamp_utc()
    groups = player_groups(rows)
    contests = {tuple(k[:3]) for k in [roster_key(r) for r in rows]}
    rosters = {roster_key(r) for r in rows}
    misses = sorted(groups, key=lambda g: (-g["appearances"], -g["avg_tier_score"], -g["avg_points"], g["player"]))[:25]
    hits = sorted(groups, key=lambda g: (-g["avg_efp"], -g["appearances"], -g["avg_points"], g["player"]))[:25]
    out = [
        {
            "Section": "Summary",
            "Rank": 1,
            "Metric": "Contests analyzed",
            "Value": len(contests),
            "Details": f"{len(rosters)} top roster(s), {len(rows)} player row(s)",
            "Generated_At": generated,
        },
        {
            "Section": "Summary",
            "Rank": 2,
            "Metric": "Biggest model miss",
            "Player": misses[0]["player"] if misses else "",
            "Team": misses[0]["team"] if misses else "",
            "Value": f"{misses[0]['appearances']} appearance(s), Tier {misses[0]['avg_tier']}" if misses else "",
            "Generated_At": generated,
        },
    ]
    for rank, item in enumerate(misses, start=1):
        out.append(format_player_row("Player Calibration Misses", rank, item, generated))
    for rank, item in enumerate(hits, start=1):
        out.append(format_player_row("Player Calibration Hits", rank, item, generated))
    for rank, (metric, value, details) in enumerate(analyze_construction(rows), start=1):
        out.append(
            {
                "Section": "Construction Patterns",
                "Rank": rank,
                "Metric": metric,
                "Value": value,
                "Details": details,
                "Generated_At": generated,
            }
        )
    return out


def format_player_row(section: str, rank: int, item: dict, generated: str) -> dict:
    return {
        "Section": section,
        "Rank": rank,
        "Player": item["player"],
        "Team": item["team"],
        "Metric": "top roster appearances",
        "Appearances": item["appearances"],
        "My_Avg_EFP": round(item["avg_efp"], 2),
        "My_Avg_Tier": item["avg_tier"],
        "Their_Avg_Points": round(item["avg_points"], 2),
        "Details": f"tier_score={round(item['avg_tier_score'], 2)}",
        "Generated_At": generated,
    }


def run(args: argparse.Namespace) -> list[dict]:
    ensure_top_rosters_sheet()
    raw_rows = get_sheet_rows(TOP_ROSTERS_SHEET_NAME)
    if not raw_rows:
        print(f"⚠️ {TOP_ROSTERS_SHEET_NAME} has no roster rows yet. Template is ready for manual entry.")
        safe_upload(args.output_tab, ANALYSIS_COLUMNS, [])
        return []
    enriched = enrich_top_rosters(raw_rows)
    safe_upload(TOP_ROSTERS_SHEET_NAME, TOP_ROSTERS_COLUMNS, enriched)
    scoped = filter_rows(enriched, args.since, args.contest)
    rows = analysis_rows(scoped)
    safe_upload(args.output_tab, ANALYSIS_COLUMNS, rows)
    print("\n📋 Post-mortem summary")
    print(f"   Top roster rows: {len(enriched)}")
    print(f"   Rows analyzed: {len(scoped)}")
    print(f"   Analysis rows: {len(rows)}")
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze top Underdog rosters against WC model ratings")
    parser.add_argument("--since", default="", help="Only analyze contests on/after YYYY-MM-DD")
    parser.add_argument("--contest", default="", help='Contest type filter, e.g. "Match Day Mania"')
    parser.add_argument("--output-tab", default=POST_MORTEM_SHEET_NAME)
    return parser.parse_args()


if __name__ == "__main__":
    run(parse_args())
