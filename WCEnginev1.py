# World Cup DFS Pick Engine v1
#
# TODO: Add Multi-Book Line Shopping extension after v1 proves stable.
# TODO: Add asymmetric-info / soft-line detection after multi-book snapshots exist.

import argparse
import json
import math
import os
import re
import time
import unicodedata
from datetime import datetime, timezone

import gspread
import pytz
import requests
from google import genai
from google.genai import types
from google.auth import default
from google.oauth2.service_account import Credentials


SHEET_ID = "1ZijOHruRgILnyR4H_jJh3pQrU3A9PJepWMLtRf3Ie9g"
SHEET_NAME = "Picks"
ODDS_API_SPORT_KEY = "soccer_fifa_world_cup"
PROP_MARKETS_STANDARD = [
    "player_shots",
    "player_shots_on_target",
    "player_goal_scorer_anytime",
]
PROP_MARKETS_ALTERNATE = [
    "player_shots_alternate",
    "player_shots_on_target_alternate",
    "player_tackles_alternate",
    "player_goals_alternate",
]
PROP_MARKETS = PROP_MARKETS_STANDARD + PROP_MARKETS_ALTERNATE
GEMINI_MODEL = "gemini-2.5-flash-lite"

PROP_MARKET_LABELS = {
    "player_shots": "Shots",
    "player_shots_alternate": "Shots",
    "player_shots_on_target": "SOT",
    "player_shots_on_target_alternate": "SOT",
    "player_goal_scorer_anytime": "Goal Scorer",
    "player_tackles_alternate": "Tackles",
    "player_goals_alternate": "Goals",
}

PROP_LABEL_TO_MARKET = {v.upper(): k for k, v in PROP_MARKET_LABELS.items()}
PROP_LABEL_TO_MARKET["SHOTS ON TARGET"] = "player_shots_on_target"
PROP_LABEL_TO_MARKET["SHOTSONTARGET"] = "player_shots_on_target"
PROP_LABEL_TO_MARKET["GOAL SCORER"] = "player_goal_scorer_anytime"
PROP_LABEL_TO_MARKET["GOALSCORER"] = "player_goal_scorer_anytime"
PROP_LABEL_TO_MARKET["GOAL SCORER ANYTIME"] = "player_goal_scorer_anytime"
PROP_LABEL_TO_MARKET["GOALSCORERANYTIME"] = "player_goal_scorer_anytime"

PICKS_COLUMNS = [
    "Player",
    "Team",
    "Opponent",
    "Prop",
    "Line",
    "Pick",
    "Tier",
    "Confidence",
    "Reasoning",
    "Game_Time",
    "Book",
    "UD_FP",
    "Result",
    "Actual",
    "Timestamp",
]

TIER_NAMES = {"SMASH", "STRONG", "LEAN"}
PICK_NAMES = {"OVER", "UNDER"}
EASTERN = pytz.timezone("America/New_York")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
REQUEST_TIMEOUT = 20
MAX_API_RETRIES = 3


WC_PROMPT_V1 = """
You are the World Cup DFS pick engine. Build ranked player-prop picks from the exact fixture and prop data below.

Today is {today_est}. This is a tournament slate, so weigh match context heavily.

ANALYSIS FACTORS:
- Recent form from the provided notes when available, especially last 5 matches.
- Opponent defensive metrics and expected game script.
- Tournament stage and rotation risk.
- Expected possession share.
- Set-piece role for shots and shots on target.
- Tackles should favor players facing high-possession opponents or wide overloads.
- Goal Scorer Anytime is a binary scorer market. Treat it as "Yes to score" and emit lean OVER at line 0.5.
- Alternate markets are milestone lines such as X+ shots, X+ tackles, or X+ goals.

RULES:
- Only output picks for these five prop types: Shots, Shots on Target, Tackles, Goal Scorer, Goals.
- Ignore passes, assists, cards, fouls, offsides, saves, corners, fantasy points, and any market not listed above.
- For Goal Scorer picks, use prop_type "Goal Scorer", line 0.5, and lean "OVER" to mean yes.
- For alternate markets, keep the milestone line exactly as provided and frame the pick as X+ shots/tackles/goals.
- Tier names must be exactly SMASH, STRONG, or LEAN.
- Confidence must be an integer from 1 to 10.
- SMASH should be reserved for the top 3-4 picks only.
- STRONG should require multiple confirming signals: positive price/line context, strong role expectation, and supportive matchup or possession context. If only one signal is strong, use LEAN instead.
- Prefer players whose role is stable for the full match. Downgrade rotation-risk players.
- Include at least one pick from each available prop type when there are enough props.
- Do not invent players, teams, opponents, books, prop types, or lines.

AVAILABLE FIXTURES:
{fixtures_json}

AVAILABLE PLAYER PROPS:
{props_json}

OUTPUT FORMAT:
Return ONLY a JSON array. No markdown. No prose outside JSON.
Each object must have exactly these keys:
rank, player, team, opponent, prop_type, line, lean, tier, confidence, rationale, book, game_time

Example:
[
  {{"rank":1,"player":"Kylian Mbappe","team":"FRA","opponent":"USA","prop_type":"Shots","line":3.5,"lean":"OVER","tier":"SMASH","confidence":9,"rationale":"Primary shooter with set-piece equity and favorable possession share.","book":"draftkings","game_time":"2026-06-11T19:00:00Z"}}
]
"""


def now_est():
    return datetime.now(EASTERN)


def timestamp_est():
    return now_est().strftime("%Y-%m-%d %I:%M:%S %p EST")


def timestamp_utc_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_secret(name, prompt_text=None, allow_missing=False):
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


def clean_cell(val):
    if val is None:
        return ""
    if isinstance(val, float) and (math.isnan(val) or math.isinf(val)):
        return ""
    if hasattr(val, "item"):
        return clean_cell(val.item())
    return val


def normalize_player_name(name):
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[’'`\.]", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_prop(prop):
    raw = str(prop or "").strip()
    key = re.sub(r"\s+", "", raw.upper())
    if key in {"SOT", "SHOTSONTARGET", "SHOTS_ON_TARGET"}:
        return "SOT"
    if key == "SHOTS":
        return "Shots"
    if key == "TACKLES":
        return "Tackles"
    if key in {"GOALSCORER", "GOALSCORERANYTIME", "ANYTIMEGOALSCORER"}:
        return "Goal Scorer"
    if key == "GOALS":
        return "Goals"
    market = PROP_LABEL_TO_MARKET.get(raw.upper()) or PROP_LABEL_TO_MARKET.get(key)
    return PROP_MARKET_LABELS.get(market, raw)


def normalize_tier(tier):
    val = str(tier or "").strip().upper()
    return val if val in TIER_NAMES else "LEAN"


def normalize_pick(pick):
    val = str(pick or "").strip().upper()
    if val in {"O", "OVER"}:
        return "Over"
    if val in {"U", "UNDER", "FADE"}:
        return "Under"
    return "Over"


def parse_gemini_json_array(raw):
    cleaned = str(raw or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        last_complete = cleaned.rfind("}")
        if last_complete > 0:
            data = json.loads(cleaned[: last_complete + 1] + "]")
        else:
            raise
    if not isinstance(data, list):
        raise ValueError("Gemini output was not a JSON array")
    return data


def odds_api_get(path, params, max_retries=MAX_API_RETRIES):
    url = f"{ODDS_API_BASE}{path}"
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                wait = 10 * (attempt + 1)
                print(f"   ⏳ Odds API rate limit — waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = 5 * (attempt + 1)
                print(f"   ⏳ Odds API server error {resp.status_code} — waiting {wait}s")
                time.sleep(wait)
                continue
            if resp.status_code == 422:
                resp.raise_for_status()
            resp.raise_for_status()
            remaining = resp.headers.get("x-requests-remaining")
            used = resp.headers.get("x-requests-used")
            if remaining or used:
                print(f"   📊 Odds API quota remaining: {remaining or '?'} / used: {used or '?'}")
            return resp.json()
        except requests.RequestException as e:
            if isinstance(e, requests.HTTPError) and getattr(e.response, "status_code", None) == 422:
                raise
            if attempt == max_retries - 1:
                raise
            wait = 5 * (attempt + 1)
            print(f"   ⚠️ Odds API request failed: {e} — retrying in {wait}s")
            time.sleep(wait)
    return None


def fetch_fixtures(odds_api_key):
    print("Fetching World Cup fixtures...")
    events = odds_api_get(
        f"/sports/{ODDS_API_SPORT_KEY}/events",
        {"apiKey": odds_api_key},
    ) or []
    fixtures = []
    for event in events:
        home = event.get("home_team") or ""
        away = event.get("away_team") or ""
        fixtures.append(
            {
                "event_id": event.get("id", ""),
                "home_team": home,
                "away_team": away,
                "matchup": f"{away} @ {home}" if home and away else event.get("id", ""),
                "commence_time": event.get("commence_time", ""),
            }
        )
    fixtures = [f for f in fixtures if f["event_id"]]
    print(f"✅ Found {len(fixtures)} upcoming World Cup fixture(s)")
    return fixtures


def normalize_outcome(market_key: str, outcome: dict) -> dict | None:
    """Returns {player, line, side, price} or None if unparseable."""
    outcome_name = str(outcome.get("name") or "").strip()
    description = str(outcome.get("description") or "").strip()
    price = outcome.get("price", "")

    if market_key == "player_goal_scorer_anytime":
        player = description or outcome.get("participant") or outcome.get("player") or outcome_name
        if not player or price in (None, ""):
            return None
        return {"player": player, "line": 0.5, "side": "Over", "price": price}

    if market_key.endswith("_alternate"):
        desc_lower = description.lower()
        side = "Under" if re.search(r"\bunder\b", desc_lower) else "Over"
        line = outcome.get("point")
        if line is None:
            line_match = re.search(r"(?:over|under)?\s*([0-9]+(?:\.[0-9]+)?)", description, flags=re.I)
            if line_match:
                line = line_match.group(1)
        try:
            line = float(line)
        except (TypeError, ValueError):
            return None
        player = outcome_name
        if outcome_name.upper() in {"OVER", "UNDER"}:
            player = outcome.get("participant") or outcome.get("player") or ""
        player = player or outcome.get("participant") or outcome.get("player") or ""
        if not player or price in (None, ""):
            return None
        return {"player": player, "line": line, "side": side, "price": price}

    side = outcome_name.upper()
    if side not in {"OVER", "UNDER"}:
        return None
    player = description or outcome.get("participant") or outcome.get("player") or ""
    line = outcome.get("point")
    if not player or line is None or price in (None, ""):
        return None
    return {"player": player, "line": line, "side": side.title(), "price": price}


def parse_prop_outcomes(event, market_key, bookmaker_key, market):
    rows = []
    metric = PROP_MARKET_LABELS.get(market_key, market_key)
    for outcome in market.get("outcomes", []) or []:
        normalized = normalize_outcome(market_key, outcome)
        if not normalized:
            continue
        rows.append(
            {
                "event_id": event.get("id", ""),
                "matchup": f"{event.get('away_team', '')} @ {event.get('home_team', '')}",
                "home_team": event.get("home_team", ""),
                "away_team": event.get("away_team", ""),
                "commence_time": event.get("commence_time", ""),
                "player": normalized["player"],
                "prop_type": metric,
                "line": normalized["line"],
                "lean": normalized["side"],
                "odds": normalized["price"],
                "book": bookmaker_key,
            }
        )
    return rows


def dedupe_best_props(rows):
    best = {}
    for row in rows:
        key = (
            row.get("event_id", ""),
            normalize_player_name(row.get("player")),
            normalize_prop(row.get("prop_type")),
            str(row.get("line", "")),
            str(row.get("lean", "")).upper(),
        )
        try:
            odds = float(row.get("odds"))
        except (TypeError, ValueError):
            odds = -999999
        current = best.get(key)
        try:
            current_odds = float(current.get("odds")) if current else -999999
        except (TypeError, ValueError):
            current_odds = -999999
        if current is None or odds > current_odds:
            best[key] = row
    return list(best.values())


def fetch_props(odds_api_key, fixture):
    event_id = fixture["event_id"]
    print(f"   Fetching props for {fixture['matchup']}...")
    rows = []
    for market_name in PROP_MARKETS:
        try:
            data = odds_api_get(
                f"/sports/{ODDS_API_SPORT_KEY}/events/{event_id}/odds",
                {
                    "apiKey": odds_api_key,
                    "regions": "us",
                    "markets": market_name,
                    "oddsFormat": "american",
                },
            )
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 422:
                print(f"   ℹ️ Market {market_name} not available for event {event_id} — skipping")
                continue
            raise
        if not data:
            continue
        for bookmaker in data.get("bookmakers", []) or []:
            book_key = bookmaker.get("key", "")
            for market in bookmaker.get("markets", []) or []:
                market_key = market.get("key", "")
                if market_key != market_name:
                    continue
                rows.extend(parse_prop_outcomes(data, market_key, book_key, market))
    return dedupe_best_props(rows)


def collapse_props_for_prompt(props):
    grouped = {}
    for row in props:
        key = (
            row.get("event_id", ""),
            normalize_player_name(row.get("player")),
            normalize_prop(row.get("prop_type")),
            str(row.get("line", "")),
            row.get("book", ""),
        )
        entry = grouped.setdefault(
            key,
            {
                "event_id": row.get("event_id", ""),
                "matchup": row.get("matchup", ""),
                "game_time": row.get("commence_time", ""),
                "player": row.get("player", ""),
                "prop_type": normalize_prop(row.get("prop_type")),
                "line": row.get("line", ""),
                "book": row.get("book", ""),
                "over_odds": "",
                "under_odds": "",
            },
        )
        if row.get("lean") == "Over":
            entry["over_odds"] = row.get("odds", "")
        elif row.get("lean") == "Under":
            entry["under_odds"] = row.get("odds", "")
    return list(grouped.values())


def infer_teams_for_pick(pick, fixtures):
    team = str(pick.get("team") or pick.get("Team") or "").strip()
    opponent = str(pick.get("opponent") or pick.get("Opponent") or "").strip()
    game_time = str(pick.get("game_time") or pick.get("Game_Time") or "").strip()
    if team and opponent:
        return team, opponent, game_time

    raw_team = str(pick.get("country") or "").strip()
    if raw_team:
        team = raw_team
    for fixture in fixtures:
        if game_time and fixture.get("commence_time") != game_time:
            continue
        if team:
            if team.lower() == str(fixture.get("home_team", "")).lower():
                return team, fixture.get("away_team", ""), fixture.get("commence_time", game_time)
            if team.lower() == str(fixture.get("away_team", "")).lower():
                return team, fixture.get("home_team", ""), fixture.get("commence_time", game_time)
    return team, opponent, game_time


def build_gemini_context(fixtures, props):
    prompt_props = collapse_props_for_prompt(props)
    context = WC_PROMPT_V1.format(
        today_est=timestamp_est(),
        fixtures_json=json.dumps(fixtures, ensure_ascii=False, indent=2)[:12000],
        props_json=json.dumps(prompt_props[:500], ensure_ascii=False, indent=2)[:45000],
    )
    return context


def call_gemini(context, gemini_api_key):
    if not gemini_api_key:
        print("⚠️ No GEMINI_API_KEY — using no AI picks")
        return []
    client = genai.Client(api_key=gemini_api_key)
    print(f"🤖 Calling Gemini model {GEMINI_MODEL}...")
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=context,
        config=types.GenerateContentConfig(
            temperature=0.35,
            max_output_tokens=8192,
            response_mime_type="application/json",
        ),
    )
    raw = response.text or ""
    return parse_gemini_json_array(raw)


def build_prop_lookup(props):
    lookup = {}
    for row in props:
        key = (
            normalize_player_name(row.get("player")),
            normalize_prop(row.get("prop_type")),
            str(row.get("line", "")),
            row.get("lean", "").upper(),
        )
        lookup.setdefault(key, row)
    return lookup


def validate_and_format_picks(raw_picks, fixtures, props):
    # TODO: AI v1 extension
    prop_lookup = build_prop_lookup(props)
    rows = []
    for idx, pick in enumerate(raw_picks or [], start=1):
        player = str(pick.get("player") or pick.get("Player") or "").strip()
        prop = normalize_prop(pick.get("prop_type") or pick.get("prop") or pick.get("Prop"))
        lean = normalize_pick(pick.get("lean") or pick.get("pick") or pick.get("Pick"))
        line = pick.get("line") if pick.get("line") is not None else pick.get("Line")
        if not player or prop not in {"Shots", "SOT", "Tackles", "Goal Scorer", "Goals"} or line in (None, ""):
            continue
        key = (normalize_player_name(player), prop, str(line), lean.upper())
        prop_row = prop_lookup.get(key)
        if not prop_row:
            # If Gemini emits an int/float line that stringifies differently, fall back by player/prop/lean.
            fallback = [
                r
                for k, r in prop_lookup.items()
                if k[0] == normalize_player_name(player) and k[1] == prop and k[3] == lean.upper()
            ]
            prop_row = fallback[0] if fallback else {}
        team, opponent, game_time = infer_teams_for_pick(pick, fixtures)
        if prop_row:
            game_time = game_time or prop_row.get("commence_time", "")
            matchup = prop_row.get("matchup", "")
            if (not team or not opponent) and " @ " in matchup:
                away, home = matchup.split(" @ ", 1)
                team = team or ""
                opponent = opponent or ""
                if not team:
                    team = pick.get("team") or ""
                if not opponent:
                    opponent = home if team == away else away if team == home else ""
        confidence_raw = pick.get("confidence") or pick.get("Confidence") or 5
        try:
            confidence = int(float(confidence_raw))
        except (TypeError, ValueError):
            confidence = 5
        confidence = max(1, min(10, confidence))
        rows.append(
            {
                "Player": player,
                "Team": team,
                "Opponent": opponent,
                "Prop": prop,
                "Line": line,
                "Pick": lean,
                "Tier": normalize_tier(pick.get("tier") or pick.get("confidence_tier") or pick.get("confidence")),
                "Confidence": confidence,
                "Reasoning": str(pick.get("rationale") or pick.get("reasoning") or "")[:500],
                "Game_Time": game_time,
                "Book": pick.get("book") or prop_row.get("book", ""),
                "UD_FP": "",
                "Result": "",
                "Actual": "",
                "Timestamp": timestamp_utc_iso(),
            }
        )
    for rank, row in enumerate(rows, start=1):
        row["_rank"] = rank
    return rows


def validate_schema(rows):
    for idx, row in enumerate(rows, start=1):
        missing = [col for col in PICKS_COLUMNS if col not in row]
        if missing:
            raise RuntimeError(f"Pick row {idx} missing columns: {missing}")
        bad_extra = [col for col in row if col not in PICKS_COLUMNS and col != "_rank"]
        if bad_extra:
            raise RuntimeError(f"Pick row {idx} has unexpected columns: {bad_extra}")
    return True


def write_to_sheet(picks):
    if not picks:
        print("⏭️ No picks to write")
        return
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=SHEET_NAME, rows=100, cols=len(PICKS_COLUMNS))
        ws.update("A1:O1", [PICKS_COLUMNS])

    existing_headers = ws.row_values(1)
    if existing_headers != PICKS_COLUMNS:
        if not existing_headers:
            ws.update("A1:O1", [PICKS_COLUMNS])
        else:
            raise RuntimeError(f"{SHEET_NAME} header mismatch. Expected {PICKS_COLUMNS}, found {existing_headers}")

    values = [[clean_cell(row.get(col, "")) for col in PICKS_COLUMNS] for row in picks]
    ws.append_rows(values, value_input_option="USER_ENTERED")
    print(f"✅ Appended {len(values)} pick row(s) to {SHEET_NAME}")


def sample_fixtures_and_props():
    fixtures = [
        {
            "event_id": "sample-1",
            "home_team": "USA",
            "away_team": "FRA",
            "matchup": "FRA @ USA",
            "commence_time": "2026-06-11T19:00:00Z",
        }
    ]
    props = []
    samples = [
        ("Kylian Mbappe", "Shots", 3.5, "betrivers", -125, -105),
        ("Antoine Griezmann", "SOT", 0.5, "betrivers", -110, -120),
        ("Weston McKennie", "Tackles", 2.5, "draftkings", 105, ""),
        ("Christian Pulisic", "Goal Scorer", 0.5, "fanduel", 180, ""),
        ("Olivier Giroud", "Goals", 1.5, "draftkings", 240, ""),
    ]
    for player, prop, line, book, over, under in samples:
        for lean, odds in [("Over", over), ("Under", under)]:
            if odds == "":
                continue
            props.append(
                {
                    "event_id": "sample-1",
                    "matchup": "FRA @ USA",
                    "home_team": "USA",
                    "away_team": "FRA",
                    "commence_time": "2026-06-11T19:00:00Z",
                    "player": player,
                    "prop_type": prop,
                    "line": line,
                    "lean": lean,
                    "odds": odds,
                    "book": book,
                }
            )
    return fixtures, props


def sample_picks():
    return [
        {
            "rank": 1,
            "player": "Kylian Mbappe",
            "team": "FRA",
            "opponent": "USA",
            "prop_type": "Shots",
            "line": 3.5,
            "lean": "OVER",
            "tier": "SMASH",
            "confidence": 9,
            "rationale": "Primary shooter with set-piece equity and strong volume role.",
            "book": "draftkings",
            "game_time": "2026-06-11T19:00:00Z",
        },
        {
            "rank": 2,
            "player": "Antoine Griezmann",
            "team": "FRA",
            "opponent": "USA",
            "prop_type": "SOT",
            "line": 0.5,
            "lean": "OVER",
            "tier": "STRONG",
            "confidence": 8,
            "rationale": "Set-piece and attacking midfield role creates shot-on-target paths.",
            "book": "draftkings",
            "game_time": "2026-06-11T19:00:00Z",
        },
        {
            "rank": 3,
            "player": "Tyler Adams",
            "team": "USA",
            "opponent": "FRA",
            "prop_type": "Tackles",
            "line": 2.5,
            "lean": "OVER",
            "tier": "LEAN",
            "confidence": 6,
            "rationale": "France possession pressure creates defensive action volume.",
            "book": "draftkings",
            "game_time": "2026-06-11T19:00:00Z",
        },
        {
            "rank": 4,
            "player": "Weston McKennie",
            "team": "USA",
            "opponent": "FRA",
            "prop_type": "Tackles",
            "line": 2.5,
            "lean": "OVER",
            "tier": "STRONG",
            "confidence": 8,
            "rationale": "Projected to defend high-volume French midfield and wide overloads.",
            "book": "draftkings",
            "game_time": "2026-06-11T19:00:00Z",
        },
        {
            "rank": 5,
            "player": "Christian Pulisic",
            "team": "USA",
            "opponent": "FRA",
            "prop_type": "Goal Scorer",
            "line": 0.5,
            "lean": "OVER",
            "tier": "LEAN",
            "confidence": 6,
            "rationale": "Primary attacking role and penalty equity give a yes-to-score path.",
            "book": "fanduel",
            "game_time": "2026-06-11T19:00:00Z",
        },
        {
            "rank": 6,
            "player": "Olivier Giroud",
            "team": "FRA",
            "opponent": "USA",
            "prop_type": "Goals",
            "line": 1.5,
            "lean": "OVER",
            "tier": "LEAN",
            "confidence": 5,
            "rationale": "Alternate milestone only; viable if starting centrally with box-touch volume.",
            "book": "draftkings",
            "game_time": "2026-06-11T19:00:00Z",
        },
    ]


def run_engine(args):
    print("=" * 60)
    print("🌎 WORLD CUP DFS PICK ENGINE v1")
    print("=" * 60)
    print(f"🕐 Run time: {timestamp_est()}")
    print(f"🧪 Dry run: {'YES' if args.dry_run else 'NO'}")

    if args.sample:
        print("🧪 Sample mode enabled — no external API calls")
        fixtures, props = sample_fixtures_and_props()
        raw_picks = sample_picks()
    else:
        odds_api_key = load_secret("ODDS_API_KEY", "🔑 Paste your Odds API Key: ", allow_missing=args.dry_run)
        gemini_api_key = load_secret("GEMINI_API_KEY", "🔑 Paste your Gemini API Key: ", allow_missing=args.dry_run)
        if not odds_api_key:
            print("⚠️ No ODDS_API_KEY available — no fixtures or props fetched")
            fixtures, props, raw_picks = [], [], []
        else:
            fixtures = fetch_fixtures(odds_api_key)
            props = []
            for fixture in fixtures:
                props.extend(fetch_props(odds_api_key, fixture))
                time.sleep(0.5)
            print(f"✅ Parsed {len(props)} prop outcome row(s)")
            if fixtures and props and gemini_api_key:
                context = build_gemini_context(fixtures, props)
                raw_picks = call_gemini(context, gemini_api_key)
            else:
                raw_picks = []

    picks = validate_and_format_picks(raw_picks, fixtures, props)
    validate_schema(picks)

    print("\n📊 Pick summary")
    print(f"   Fixtures: {len(fixtures)}")
    print(f"   Prop outcome rows: {len(props)}")
    print(f"   Valid picks: {len(picks)}")
    if picks:
        by_prop = {}
        for row in picks:
            by_prop[row["Prop"]] = by_prop.get(row["Prop"], 0) + 1
        print(f"   Prop distribution: {by_prop}")
        print(f"   #1 Pick: {picks[0]['Player']} — {picks[0]['Prop']} {picks[0]['Pick']} {picks[0]['Line']} ({picks[0]['Tier']})")
        print("\nSample output row:")
        print(json.dumps({col: picks[0].get(col, "") for col in PICKS_COLUMNS}, indent=2))

    if args.dry_run:
        print("\n🧪 Dry run complete — skipped Google Sheets write")
    else:
        write_to_sheet(picks)

    print("\n" + "=" * 60)
    print("✅ WORLD CUP DFS PICK ENGINE COMPLETE")
    print("=" * 60)
    return picks


def parse_args():
    parser = argparse.ArgumentParser(description="World Cup DFS Pick Engine")
    parser.add_argument("--once", action="store_true", help="Run once and exit. Present for cron parity.")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing to Google Sheets.")
    parser.add_argument("--sample", action="store_true", help="Use sample fixtures, props, and picks for offline smoke testing.")
    return parser.parse_args()


if __name__ == "__main__":
    run_engine(parse_args())
