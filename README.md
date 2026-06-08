# World Cup DFS Dashboard Engine

A v1 World Cup player-prop pick engine built from the same mental model as the existing MLB/NBA/NHL/WNBA DFS dashboards: pull slate data, collect prop markets, ask Gemini for ranked picks, and write a clean pick sheet for grading and dashboard work.

## What It Builds

`WCEnginev1.py` pulls upcoming World Cup fixtures and player prop odds for:

- Shots
- Shots on Target
- Tackles milestone lines
- Goal Scorer Anytime
- Goals milestone lines

The first live market probe showed that World Cup passes are not booked by US sportsbooks on The Odds API, and standard tackle markets are not broadly available. The engine therefore uses the live bookable market set: standard Shots/SOT/Goal Scorer plus alternate milestone Shots/SOT/Tackles/Goals.

It sends those fixtures and props to Gemini, validates the returned picks, and writes them to the `Picks` tab in Google Sheets.

`index.html` is the GitHub Pages dashboard. It reads the same `Picks` tab through Google Sheets gviz CSV and renders:

- Picks
- Stats placeholder
- Game Entry
- Method notes

`WCGrader_v1.py` grades completed picks using API-Football fixture/player stats and writes only the `Result` and `Actual` columns.

`WCFormPull.py` pulls recent international form from API-Football, writes a `Player_Form` tab, and maintains a local `player_id_cache.json` so Odds API/Gemini player names can be matched to API-Football player IDs repeatably. The resolver is nationality-gated so a player is skipped rather than matched to the wrong human. The engine reads that form tab before calling Gemini and appends per-pick form fields for dashboard badges.

## Sheet

Workbook:

`1ZijOHruRgILnyR4H_jJh3pQrU3A9PJepWMLtRf3Ie9g`

Tabs:

`Picks`

`Player_Form`

Columns:

`Player, Team, Opponent, Prop, Line, Pick, Tier, Confidence, Reasoning, Game_Time, Book, UD_FP, Result, Actual, Timestamp, Intl_Sample, Avg_Shots, Avg_SOT, Avg_Tackles, Goal_Scorer_Rate, Last_5_Shots`

`UD_FP` is intentionally blank in v1. A soccer fantasy formula can be defined after the tournament data proves what matters.

`Player_Form` stores the reusable form summary: player name, API-Football ID, nationality, international sample size, minutes, shots, SOT, tackles, goals, goal-rate proxy, and update notes.

## Run

Dry-run with sample data:

```bash
python3 WCEnginev1.py --once --dry-run --sample
```

Dry-run against live APIs:

```bash
ODDS_API_KEY=... GEMINI_API_KEY=... python3 WCEnginev1.py --once --dry-run
```

Write live picks to Sheets:

```bash
ODDS_API_KEY=... GEMINI_API_KEY=... GOOGLE_SERVICE_ACCOUNT_JSON=... python3 WCEnginev1.py --once
```

Validate grader aliases without Sheets or API-Football:

```bash
python3 WCGrader_v1.py --alias-check
```

Pull form data for current picks only:

```bash
API_FOOTBALL_KEY=... GOOGLE_SERVICE_ACCOUNT_JSON=... python3 WCFormPull.py --picks-only
```

Build the full World Cup squad form tab and committed player-ID cache:

```bash
API_FOOTBALL_KEY=... GOOGLE_SERVICE_ACCOUNT_JSON=... python3 WCFormPull.py --all
```

Verify international competition IDs exposed by API-Football:

```bash
API_FOOTBALL_KEY=... python3 WCFormPull.py --verify-leagues --dry-run
```

Clear polluted form data without pulling new API data:

```bash
GOOGLE_SERVICE_ACCOUNT_JSON=... python3 WCFormPull.py --clear-form
```

## Secrets

No API keys are hardcoded. The engine reads:

- `ODDS_API_KEY`
- `GEMINI_API_KEY`
- `GOOGLE_SERVICE_ACCOUNT_JSON`
- `API_FOOTBALL_KEY`

Colab userdata is also supported for manual runs.

## Notes

- The dashboard file must stay named `index.html` at the repo root for GitHub Pages.
- Tier names are exactly `SMASH`, `STRONG`, and `LEAN` for cross-sport parity.
- Multi-book line shopping is out of scope for v1.
- Soft-line / asymmetric-info detection is out of scope for v1.
- Form badges use `Player_Form` where available and gracefully show limited-history badges for old rows or unmatched players.
- This is a personal research tool, not betting advice.
