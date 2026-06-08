# World Cup DFS Dashboard Engine

A v1 World Cup player-prop pick engine built from the same mental model as the existing MLB/NBA/NHL/WNBA DFS dashboards: pull slate data, collect prop markets, ask Gemini for ranked picks, and write a clean pick sheet for grading and dashboard work.

## What It Builds

`WCEnginev1.py` pulls upcoming World Cup fixtures and player prop odds for:

- Shots
- Shots on Target
- Passes
- Tackles

It sends those fixtures and props to Gemini, validates the returned picks, and writes them to the `Picks` tab in Google Sheets.

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

## Secrets

No API keys are hardcoded. The engine reads:

- `ODDS_API_KEY`
- `GEMINI_API_KEY`
- `GOOGLE_SERVICE_ACCOUNT_JSON`

Colab userdata is also supported for manual runs.

## Notes

- Tier names are exactly `SMASH`, `STRONG`, and `LEAN` for cross-sport parity.
- Multi-book line shopping is out of scope for v1.
- Soft-line / asymmetric-info detection is out of scope for v1.
- This is a personal research tool, not betting advice.
