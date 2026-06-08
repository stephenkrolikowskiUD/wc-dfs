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

## Sheet

Workbook:

`1ZijOHruRgILnyR4H_jJh3pQrU3A9PJepWMLtRf3Ie9g`

Tab:

`Picks`

Columns:

`Player, Team, Opponent, Prop, Line, Pick, Tier, Confidence, Reasoning, Game_Time, Book, UD_FP, Result, Actual, Timestamp`

`UD_FP` is intentionally blank in v1. A soccer fantasy formula can be defined after the tournament data proves what matters.

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
- This is a personal research tool, not betting advice.
