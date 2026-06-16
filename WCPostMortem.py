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
    "slate_archetype",
    "favorite_team",
    "is_mine",
]

ANALYSIS_COLUMNS = [
    "Section",
    "Rank",
    "Slate",
    "Archetype",
    "Player",
    "Team",
    "Metric",
    "Appearances",
    "My_Avg_EFP",
    "My_Avg_Tier",
    "Their_Avg_Points",
    "Value",
    "N_Slates",
    "Avg_Winning_Score",
    "Avg_Max_Stack",
    "Leverage_Position",
    "Gap",
    "Underdog_GK_Pct",
    "Warning",
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
    out["slate_archetype"] = str(out.get("slate_archetype") or "").strip() or "unknown"
    out["favorite_team"] = str(out.get("favorite_team") or "").strip()
    out["is_mine"] = clean_bool(out.get("is_mine")) or "FALSE"
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
        return
    missing = [col for col in TOP_ROSTERS_COLUMNS if col not in headers]
    if missing:
        ws.update("A1", [headers + missing], value_input_option="USER_ENTERED")
        print(f"✅ Added {TOP_ROSTERS_SHEET_NAME} columns: {', '.join(missing)}")


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


def slate_key(row: dict) -> tuple[str, str, str]:
    return (
        str(row.get("contest_date") or ""),
        str(row.get("contest_type") or ""),
        str(row.get("slate") or ""),
    )


def is_mine_row(row: dict) -> bool:
    return clean_bool(row.get("is_mine")) == "TRUE"


def top_finish_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if not is_mine_row(row)]


def own_rows(rows: list[dict]) -> list[dict]:
    return [row for row in rows if is_mine_row(row)]


def group_by_roster(rows: list[dict]) -> dict[tuple[str, str, str, str], list[dict]]:
    rosters: dict[tuple[str, str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        rosters[roster_key(row)].append(row)
    return rosters


def group_by_slate(rows: list[dict]) -> dict[tuple[str, str, str], list[dict]]:
    slates: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for row in rows:
        slates[slate_key(row)].append(row)
    return slates


def analyze_construction(rows: list[dict]) -> list[dict]:
    rosters = group_by_roster(rows)
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


def avg_max_stack(rows: list[dict]) -> float:
    rosters = group_by_roster(rows)
    stacks = []
    for roster in rosters.values():
        teams = [str(p.get("player_team") or "").strip() for p in roster if p.get("player_team")]
        counts = Counter(teams)
        if counts:
            stacks.append(max(counts.values()))
    return round(avg(stacks), 2)


def avg_winning_score(rows: list[dict]) -> float:
    scores = {}
    for row in rows:
        if str(row.get("finishing_position") or "").strip() != "1":
            continue
        key = roster_key(row)
        score = safe_float(row.get("total_score"))
        if score is not None:
            scores[key] = score
    return round(avg(list(scores.values())), 2)


def top_misses(rows: list[dict], n: int = 5) -> list[dict]:
    groups = player_groups(rows)
    return sorted(groups, key=lambda g: (-g["appearances"], -g["avg_tier_score"], -g["avg_points"], g["player"]))[:n]


def compute_leverage_position(rows: list[dict]) -> tuple[str, float]:
    by_position: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        pos = position_from_slot(row.get("roster_slot", ""))
        pts = safe_float(row.get("actual_points"))
        if pos and pts is not None:
            by_position[pos].append(pts)
    gaps = {}
    for pos, points in by_position.items():
        sorted_pts = sorted(points, reverse=True)
        if len(sorted_pts) >= 3:
            gaps[pos] = round(sorted_pts[0] - sorted_pts[2], 2)
    if not gaps:
        return "", 0.0
    pos = max(gaps, key=gaps.get)
    return pos, gaps[pos]


def leverage_rows(rows: list[dict], generated: str) -> list[dict]:
    out = []
    for rank, (key, slate_rows) in enumerate(sorted(group_by_slate(rows).items()), start=1):
        pos, gap = compute_leverage_position(slate_rows)
        contest_date, contest_type, slate = key
        out.append(
            {
                "Section": "Leverage Position Per Slate",
                "Rank": rank,
                "Slate": slate,
                "Metric": contest_type,
                "Value": contest_date,
                "Leverage_Position": pos,
                "Gap": gap,
                "Details": "Actual-points gap between #1 and #3 top-five player at the position",
                "Generated_At": generated,
            }
        )
    return out


def analyze_by_archetype(rows: list[dict], generated: str) -> list[dict]:
    out = []
    by_archetype: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        archetype = str(row.get("slate_archetype") or "unknown").strip() or "unknown"
        by_archetype[archetype].append(row)
    for rank, (archetype, archetype_rows) in enumerate(sorted(by_archetype.items()), start=1):
        slate_count = len(group_by_slate(archetype_rows))
        leverage = [compute_leverage_position(slate_rows)[0] for slate_rows in group_by_slate(archetype_rows).values()]
        dominant = Counter([pos for pos in leverage if pos]).most_common(1)
        misses = top_misses(archetype_rows, 1)
        out.append(
            {
                "Section": "Slate Archetype Breakdown",
                "Rank": rank,
                "Archetype": archetype,
                "N_Slates": slate_count,
                "Avg_Winning_Score": avg_winning_score(archetype_rows),
                "Avg_Max_Stack": avg_max_stack(archetype_rows),
                "Leverage_Position": dominant[0][0] if dominant else "",
                "Player": misses[0]["player"] if misses else "",
                "Team": misses[0]["team"] if misses else "",
                "Details": "sample_top_miss",
                "Generated_At": generated,
            }
        )
    return out


def player_exposure(rows: list[dict]) -> Counter:
    return Counter(str(row.get("player_name") or "").strip() for row in rows if row.get("player_name"))


def portfolio_concentration_stats(rows: list[dict]) -> dict:
    rosters = group_by_roster(rows)
    n_rosters = len(rosters)
    appearances = player_exposure(rows)
    top_three = appearances.most_common(3)
    denominator = max(n_rosters * 3, 1)
    concentration = sum(count for _, count in top_three) / denominator
    return {
        "n_rosters": n_rosters,
        "top_3_player_exposure_pct": concentration,
        "n_unique_players_used": len(appearances),
        "top_players": ", ".join(f"{player} ({count})" for player, count in top_three),
    }


def portfolio_rows(rows: list[dict], generated: str) -> list[dict]:
    out = []
    top_by_contest = group_by_slate(top_finish_rows(rows))
    mine_by_contest = group_by_slate(own_rows(rows))
    rank = 1
    for key, my_contest_rows in sorted(mine_by_contest.items()):
        my_stats = portfolio_concentration_stats(my_contest_rows)
        top_stats = portfolio_concentration_stats(top_by_contest.get(key, []))
        warning = my_stats["top_3_player_exposure_pct"] + 0.15 < top_stats["top_3_player_exposure_pct"]
        contest_date, contest_type, slate = key
        out.append(
            {
                "Section": "Portfolio Concentration",
                "Rank": rank,
                "Slate": slate,
                "Metric": contest_type,
                "Value": contest_date,
                "Appearances": my_stats["n_rosters"],
                "My_Avg_EFP": round(my_stats["top_3_player_exposure_pct"], 3),
                "Their_Avg_Points": round(top_stats["top_3_player_exposure_pct"], 3),
                "Warning": "LOW_CONCENTRATION" if warning else "",
                "Details": (
                    f"mine_unique={my_stats['n_unique_players_used']}; "
                    f"top5_unique={top_stats['n_unique_players_used']}; "
                    f"mine_top={my_stats['top_players']}; top5_top={top_stats['top_players']}"
                ),
                "Generated_At": generated,
            }
        )
        rank += 1
    return out


def underdog_gk_rows(rows: list[dict], generated: str) -> list[dict]:
    slate_signals = []
    out = []
    for key, slate_rows in sorted(group_by_slate(rows).items()):
        gks = [
            r
            for r in slate_rows
            if position_from_slot(r.get("roster_slot", "")) == "G" and str(r.get("favorite_team") or "").strip()
        ]
        if not gks:
            continue
        underdog = [r for r in gks if normalize_name(r.get("player_team", "")) != normalize_name(r.get("favorite_team", ""))]
        pct = len(underdog) / len(gks)
        slate_signals.append(pct)
        contest_date, contest_type, slate = key
        out.append(
            {
                "Section": "Underdog GK Pattern",
                "Rank": len(out) + 2,
                "Slate": slate,
                "Metric": contest_type,
                "Value": contest_date,
                "Appearances": len(gks),
                "Underdog_GK_Pct": round(pct, 3),
                "Details": f"{len(underdog)} of {len(gks)} top-five GK rows were underdogs",
                "Generated_At": generated,
            }
        )
    if slate_signals:
        hit_slates = sum(1 for pct in slate_signals if pct > 0)
        avg_pct = avg(slate_signals)
        confirmed = len(slate_signals) >= 5 and avg_pct >= 0.6
        out.insert(
            0,
            {
                "Section": "Underdog GK Pattern",
                "Rank": 1,
                "Metric": "Underdog GK win rate",
                "Value": f"{hit_slates} of {len(slate_signals)} slates",
                "Underdog_GK_Pct": round(avg_pct, 3),
                "Warning": "CONFIRMED_EDGE" if confirmed else "",
                "Details": "Slate counted when at least one top-five GK was from the underdog team",
                "Generated_At": generated,
            },
        )
    return out


def analysis_rows(rows: list[dict]) -> list[dict]:
    generated = timestamp_utc()
    top_rows = top_finish_rows(rows)
    groups = player_groups(top_rows)
    contests = {tuple(k[:3]) for k in [roster_key(r) for r in top_rows]}
    rosters = {roster_key(r) for r in top_rows}
    misses = sorted(groups, key=lambda g: (-g["appearances"], -g["avg_tier_score"], -g["avg_points"], g["player"]))[:25]
    hits = sorted(groups, key=lambda g: (-g["avg_efp"], -g["appearances"], -g["avg_points"], g["player"]))[:25]
    underdog_rows = underdog_gk_rows(top_rows, generated)
    underdog_summary = underdog_rows[0] if underdog_rows else {}
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
        {
            "Section": "Summary",
            "Rank": 3,
            "Metric": "Underdog GK win rate",
            "Value": underdog_summary.get("Value", "—"),
            "Underdog_GK_Pct": underdog_summary.get("Underdog_GK_Pct", ""),
            "Warning": underdog_summary.get("Warning", ""),
            "Generated_At": generated,
        },
    ]
    for rank, item in enumerate(misses, start=1):
        out.append(format_player_row("Player Calibration Misses", rank, item, generated))
    for rank, item in enumerate(hits, start=1):
        out.append(format_player_row("Player Calibration Hits", rank, item, generated))
    for rank, (metric, value, details) in enumerate(analyze_construction(top_rows), start=1):
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
    out.extend(analyze_by_archetype(top_rows, generated))
    out.extend(leverage_rows(top_rows, generated))
    out.extend(portfolio_rows(rows, generated))
    out.extend(underdog_rows)
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
