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
from datetime import datetime, timedelta, timezone

import gspread
import pytz
import requests
from google import genai
from google.genai import types
from google.auth import default
from google.oauth2.service_account import Credentials


SHEET_ID = "1ZijOHruRgILnyR4H_jJh3pQrU3A9PJepWMLtRf3Ie9g"
PICKS_HISTORY_SHEET_NAME = "Picks_History"
PICKS_CURRENT_SHEET_NAME = "Picks_Current"
INJURY_SHEET_NAME = "Player_Injuries"
ODDS_API_SPORT_KEY = "soccer_fifa_world_cup"
PROP_MARKETS_STANDARD = [
    "player_shots",
    "player_shots_on_target",
    "player_goal_scorer_anytime",
]
PROP_MARKETS_ALTERNATE = [
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
    "Intl_Sample",
    "Avg_Shots",
    "Avg_SOT",
    "Avg_Tackles",
    "Goal_Scorer_Rate",
    "Last_5_Shots",
    "Injury_Status",
    "Injury_Reason",
]

TIER_NAMES = {"SMASH", "STRONG", "LEAN"}
PICK_NAMES = {"OVER", "UNDER"}
EASTERN = pytz.timezone("America/New_York")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
REQUEST_TIMEOUT = 20
MAX_API_RETRIES = 3
SQUAD_CACHE_PATH = "squad_cache.json"
PROP_LOOKAHEAD_HOURS = 48
MARKET_422_CACHE_PATH = "market_422_cache.json"
MARKET_422_CACHE_TTL_SECONDS = 6 * 3600

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
    "denmark": "denmark",
    "ecuador": "ecuador",
    "egypt": "egypt",
    "england": "england",
    "france": "france",
    "germany": "germany",
    "ghana": "ghana",
    "iran": "iran",
    "ir iran": "iran",
    "italy": "italy",
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
    "u s a": "usa",
    "uruguay": "uruguay",
    "wales": "wales",
}


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
- Tackles and Goals may be alternate milestone markets such as X+ tackles or X+ goals.

RULES:
- Only output picks for these five prop types: Shots, Shots on Target, Tackles, Goal Scorer, Goals.
- Ignore passes, assists, cards, fouls, offsides, saves, corners, fantasy points, and any market not listed above.
- For Goal Scorer picks, use prop_type "Goal Scorer", line 0.5, and lean "OVER" to mean yes.
- Shots and SOT are standard over/under markets. Do not reinterpret a SOT line as total Shots.
- For Tackles and Goals alternate markets, keep the milestone line exactly as provided and frame the pick as X+ tackles/goals.
- Tier names must be exactly SMASH, STRONG, or LEAN.
- Confidence must be an integer from 1 to 10.
- SMASH should be reserved for the top 3-4 picks only.
- STRONG should require multiple confirming signals: positive price/line context, strong role expectation, and supportive matchup or possession context. If only one signal is strong, use LEAN instead.
- Prefer players whose role is stable for the full match. Downgrade rotation-risk players.
- Select the 3-7 best picks from the provided fixture data. If fewer than 3 picks are viable, return fewer.
- Include at least one pick from each available prop type when there are enough props.
- Do not invent players, teams, opponents, books, prop types, or lines.
- For each prop, you have the current sportsbook line and may have form_context with last 24 months of international form.
- Weight form_context heavily when present. When form_context is null, note "limited data" in reasoning and lower confidence accordingly.
- Injury status is provided when known. Downgrade or avoid players with injury_status below 1.0 unless the price and role still justify the risk. Mention injury uncertainty in the rationale.

AVAILABLE FIXTURES:
{fixtures_json}

AVAILABLE PLAYER PROPS:
{props_json}

IMPORTANT: You may only suggest picks for the exact (player, prop_type, line)
combinations listed in AVAILABLE PLAYER PROPS. Do not propose alternate lines
such as 1.36, 1.5, 1.6, or 1.75 unless that exact line appears in the data for
that exact player and prop_type. If a player has multiple lines available across
markets, choose from those exact listed lines only. The valid slate combinations
are listed above; pick from that set.

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


def canonical_team_name(name):
    norm = normalize_player_name(name)
    return TEAM_NAME_ALIASES.get(norm, norm)


def player_last_name(normalized_name):
    parts = normalized_name.split()
    return parts[-1] if parts else ""


def player_first_initial(normalized_name):
    parts = normalized_name.split()
    return parts[0][0] if parts and parts[0] else ""


def player_surnames(normalized_name):
    parts = normalized_name.split()
    if len(parts) <= 1:
        return set(parts)
    return set(parts[1:])


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


def line_key(value):
    val = safe_float(value)
    if val is None:
        return str(value or "").strip()
    return f"{val:.3f}".rstrip("0").rstrip(".")


def safe_float(value, default=None):
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


def parse_utc_datetime(value):
    try:
        return datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def fixtures_in_window(fixtures: list[dict], hours: int = PROP_LOOKAHEAD_HOURS) -> list[dict]:
    """Filter to fixtures whose prop markets are likely to be posted soon."""
    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(hours=hours)
    in_window = []
    deferred = 0
    invalid = 0
    for fixture in fixtures:
        kickoff = parse_utc_datetime(fixture.get("commence_time"))
        if not kickoff:
            invalid += 1
            continue
        if now <= kickoff <= cutoff:
            in_window.append(fixture)
        else:
            deferred += 1
    invalid_note = f", {invalid} invalid kickoff" if invalid else ""
    print(f"🔍 {hours}hr window filter: {len(in_window)} fixtures in window, {deferred} deferred{invalid_note}")
    return in_window


def load_422_cache():
    if not os.path.exists(MARKET_422_CACHE_PATH):
        return {}
    try:
        with open(MARKET_422_CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError) as e:
        print(f"⚠️ Could not read {MARKET_422_CACHE_PATH} — starting fresh: {e}")
        return {}


def save_422_cache(cache):
    now = time.time()
    pruned = {}
    for key, entry in (cache or {}).items():
        if not isinstance(entry, dict):
            continue
        ts = safe_float(entry.get("timestamp"))
        if ts is not None and now - ts <= MARKET_422_CACHE_TTL_SECONDS:
            pruned[key] = {"timestamp": ts}
    with open(MARKET_422_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(pruned, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"💾 Saved 422 market cache: {len(pruned)} active entr{'y' if len(pruned) == 1 else 'ies'}")


def market_422_key(fixture_id, market):
    return f"{fixture_id}:{market}"


def should_skip_422(cache, fixture_id, market):
    entry = (cache or {}).get(market_422_key(fixture_id, market))
    if not isinstance(entry, dict):
        return False
    ts = safe_float(entry.get("timestamp"))
    if ts is None:
        return False
    return time.time() - ts <= MARKET_422_CACHE_TTL_SECONDS


def mark_422(cache, fixture_id, market):
    if cache is not None:
        cache[market_422_key(fixture_id, market)] = {"timestamp": time.time()}


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
                "source_market": market_key,
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
            line_key(row.get("line", "")),
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


def fetch_props(odds_api_key, fixture, market_422_cache=None):
    event_id = fixture["event_id"]
    print(f"   Fetching props for {fixture['matchup']}...")
    rows = []
    for market_name in PROP_MARKETS:
        if market_422_cache is not None and should_skip_422(market_422_cache, event_id, market_name):
            print(f"      💾 422 cache hit: skipping {market_name}")
            continue
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
                mark_422(market_422_cache, event_id, market_name)
                print(f"      ℹ️ Market {market_name} not available for event {event_id} — cached 6h")
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
            line_key(row.get("line", "")),
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
                "source_market": row.get("source_market", ""),
                "line": row.get("line", ""),
                "book": row.get("book", ""),
                "over_odds": "",
                "under_odds": "",
                "form_context": row.get("form_context"),
                "api_football_id": row.get("api_football_id", ""),
                "injury_status": row.get("injury_status", 1.0),
                "injury_type": row.get("injury_type", ""),
                "injury_reason": row.get("injury_reason", ""),
            },
        )
        if row.get("lean") == "Over":
            entry["over_odds"] = row.get("odds", "")
        elif row.get("lean") == "Under":
            entry["under_odds"] = row.get("odds", "")
    return list(grouped.values())


def load_player_form_sheet():
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet("Player_Form")
        values = ws.get_all_values()
    except Exception as e:
        print(f"⚠️ Player_Form unavailable — form enrichment skipped: {e}")
        return {}
    if not values:
        return {}
    headers = values[0]
    form_data = {"by_name": {}, "by_id": {}}
    for vals in values[1:]:
        row = {headers[i]: vals[i] if i < len(vals) else "" for i in range(len(headers))}
        name = row.get("Player_Name", "")
        if not name:
            continue
        form_data["by_name"][normalize_player_name(name)] = row
        api_id = str(row.get("API_Football_ID", "")).strip()
        if api_id:
            form_data["by_id"][api_id] = row
    print(f"✅ Loaded Player_Form for {len(form_data['by_name'])} player(s)")
    return form_data


def load_injury_map():
    try:
        gc = get_gspread_client()
        sh = gc.open_by_key(SHEET_ID)
        ws = sh.worksheet(INJURY_SHEET_NAME)
        values = ws.get_all_values()
    except Exception as e:
        print(f"⚠️ {INJURY_SHEET_NAME} unavailable — injury enrichment skipped: {e}")
        return {}
    if not values:
        return {}
    headers = values[0]
    injuries = {}
    for vals in values[1:]:
        row = {headers[i]: vals[i] if i < len(vals) else "" for i in range(len(headers))}
        api_id = str(row.get("API_Football_ID", "")).strip()
        if not api_id:
            continue
        status = safe_float(row.get("Status_Score"), 1.0)
        current = injuries.get(api_id)
        if current is None or status < safe_float(current.get("Status_Score"), 1.0):
            injuries[api_id] = row
    print(f"✅ Loaded injury status for {len(injuries)} player(s)")
    return injuries


def load_squad_cache():
    try:
        with open(SQUAD_CACHE_PATH) as f:
            cache = json.load(f)
    except Exception as e:
        print(f"⚠️ {SQUAD_CACHE_PATH} unavailable — form join will use exact names only: {e}")
        return {}
    total_players = sum(len(team.get("players", [])) for team in cache.values())
    print(f"✅ Loaded {SQUAD_CACHE_PATH}: {len(cache)} team(s), {total_players} player(s)")
    return cache


def squad_player_to_resolved(player, team_name):
    return {
        "id": str(player.get("api_id", "")).strip(),
        "name": player.get("name", ""),
        "team": team_name,
    }


def resolve_player_from_team_squad(player_name, team_name, squad_cache):
    team_key = canonical_team_name(team_name)
    team = squad_cache.get(team_key)
    if not team:
        return None

    target = normalize_player_name(player_name)
    target_last = player_last_name(target)
    target_initial = player_first_initial(target)
    target_surnames = player_surnames(target)
    squad_players = team.get("players", []) or []

    exact = [p for p in squad_players if p.get("name_normalized") == target]
    if len(exact) == 1:
        return squad_player_to_resolved(exact[0], team.get("team_name", team_name))

    initial_last = []
    for p in squad_players:
        cand = p.get("name_normalized", "")
        cand_last = player_last_name(cand)
        cand_initial = player_first_initial(cand)
        if target_surnames and cand_last in target_surnames and target_initial and cand_initial == target_initial:
            initial_last.append(p)
    if len(initial_last) == 1:
        return squad_player_to_resolved(initial_last[0], team.get("team_name", team_name))

    last_matches = [p for p in squad_players if target_last and player_last_name(p.get("name_normalized", "")) == target_last]
    if len(last_matches) == 1:
        return squad_player_to_resolved(last_matches[0], team.get("team_name", team_name))

    substring_matches = [
        p
        for p in squad_players
        if target_last and target_last in p.get("name_normalized", "").split()
    ]
    if len(substring_matches) == 1:
        return squad_player_to_resolved(substring_matches[0], team.get("team_name", team_name))
    return None


def resolve_player_from_fixture_squads(row, squad_cache):
    player_name = row.get("player", "")
    teams = [row.get("home_team", ""), row.get("away_team", "")]
    matches = []
    for team_name in teams:
        resolved = resolve_player_from_team_squad(player_name, team_name, squad_cache)
        if resolved and resolved.get("id"):
            matches.append(resolved)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = ", ".join(f"{m.get('name')} ({m.get('team')})" for m in matches)
        print(f"   ⚠️ Ambiguous squad form match for {player_name}: {choices}")
    return None


def find_form_for_prop(row, form_data, squad_cache):
    player_key = normalize_player_name(row.get("player", ""))
    form = form_data.get("by_name", {}).get(player_key)
    if form:
        return form, "name"

    resolved = resolve_player_from_fixture_squads(row, squad_cache)
    if resolved:
        form = form_data.get("by_id", {}).get(resolved.get("id", ""))
        if form:
            return form, "squad_id"
        canonical_name = normalize_player_name(resolved.get("name", ""))
        form = form_data.get("by_name", {}).get(canonical_name)
        if form:
            return form, "squad_name"
    return None, None


def form_context_from_row(form):
    sample = safe_float(form.get("Intl_Matches_Last_24mo"), 0) or 0
    return {
        "intl_sample": int(sample),
        "avg_shots": safe_float(form.get("Avg_Shots")),
        "avg_sot": safe_float(form.get("Avg_SOT")),
        "avg_tackles": safe_float(form.get("Avg_Tackles")),
        "goal_scorer_rate": safe_float(form.get("Goal_Scorer_Rate")),
        "last_5_shots": form.get("Last_5_Shots", ""),
        "notes": form.get("Notes", ""),
    }


def enrich_with_form(prop_rows):
    """For each prop row, add player_form context if available."""
    form_data = load_player_form_sheet()
    squad_cache = load_squad_cache()
    injuries = load_injury_map()
    enriched = []
    matched = 0
    matched_by = {"name": 0, "squad_id": 0, "squad_name": 0}
    for row in prop_rows:
        out = dict(row)
        form, source = find_form_for_prop(row, form_data, squad_cache)
        if form:
            matched += 1
            matched_by[source] = matched_by.get(source, 0) + 1
        api_id = str((form or {}).get("API_Football_ID", "")).strip()
        if not api_id:
            resolved = resolve_player_from_fixture_squads(row, squad_cache)
            api_id = str((resolved or {}).get("id", "")).strip()
        injury = injuries.get(api_id, {}) if api_id else {}
        out["form_context"] = form_context_from_row(form) if form else None
        out["api_football_id"] = api_id
        out["injury_status"] = safe_float(injury.get("Status_Score"), 1.0) if injury else 1.0
        out["injury_type"] = injury.get("Injury_Type", "") if injury else ""
        out["injury_reason"] = injury.get("Reason", "") if injury else ""
        enriched.append(out)
    print(
        "✅ Form enrichment matched "
        f"{matched}/{len(prop_rows)} prop rows "
        f"(name={matched_by.get('name', 0)}, squad_id={matched_by.get('squad_id', 0)}, "
        f"squad_name={matched_by.get('squad_name', 0)})"
    )
    return enriched


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


def build_gemini_context(fixtures, props, already_enriched=False):
    enriched_props = props if already_enriched else enrich_with_form(props)
    prompt_props = collapse_props_for_prompt(enriched_props)
    context = WC_PROMPT_V1.format(
        today_est=timestamp_est(),
        fixtures_json=json.dumps(fixtures, ensure_ascii=False, indent=2)[:12000],
        props_json=json.dumps(prompt_props[:500], ensure_ascii=False, indent=2)[:45000],
    )
    return context, enriched_props


def call_gemini(context, gemini_api_key, label=""):
    if not gemini_api_key:
        print("⚠️ No GEMINI_API_KEY — using no AI picks")
        return []
    client = genai.Client(api_key=gemini_api_key)
    suffix = f" for {label}" if label else ""
    print(f"🤖 Calling Gemini model {GEMINI_MODEL}{suffix}...")
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


def props_for_fixture(props, fixture):
    event_id = str(fixture.get("event_id", "")).strip()
    return [row for row in props if str(row.get("event_id", "")).strip() == event_id]


def generate_picks_per_fixture(fixtures, props, gemini_api_key):
    """Call Gemini once per fixture to avoid global favorite bias across the slate."""
    enriched_props = enrich_with_form(props)
    raw_picks = []
    for fixture in fixtures:
        fixture_props = props_for_fixture(enriched_props, fixture)
        if not fixture_props:
            print(f"   ⚠️ No props available for {fixture.get('matchup', fixture.get('event_id', 'fixture'))}; skipping Gemini")
            continue
        label = fixture.get("matchup") or fixture.get("event_id", "")
        print(f"   🎯 Fixture prompt: {label} ({len(fixture_props)} prop row(s))")
        context, _ = build_gemini_context([fixture], fixture_props, already_enriched=True)
        fixture_picks = call_gemini(context, gemini_api_key, label=label)
        for pick in fixture_picks:
            pick["source_fixture_id"] = fixture.get("event_id", "")
            pick["source_matchup"] = label
        raw_picks.extend(fixture_picks)
        time.sleep(0.5)
    return raw_picks, enriched_props


def build_prop_lookup(props):
    lookup = {"exact": {}, "by_player_line_lean": {}, "by_player_prop_lean": {}}
    for row in props:
        key = (
            normalize_player_name(row.get("player")),
            normalize_prop(row.get("prop_type")),
            line_key(row.get("line", "")),
            row.get("lean", "").upper(),
        )
        lookup["exact"].setdefault(key, row)
        line_key_only = (key[0], key[2], key[3])
        lookup["by_player_line_lean"].setdefault(line_key_only, []).append(row)
        prop_key_only = (key[0], key[1], key[3])
        lookup["by_player_prop_lean"].setdefault(prop_key_only, []).append(row)
    return lookup


def find_matching_prop_row(prop_lookup, player, prop, line, lean):
    player_norm = normalize_player_name(player)
    prop_norm = normalize_prop(prop)
    lean_norm = lean.upper()
    exact_key = (player_norm, prop_norm, line_key(line), lean_norm)
    exact = prop_lookup["exact"].get(exact_key)
    if exact:
        return exact, "exact"

    # If Gemini used the wrong prop label but kept the real sportsbook line, correct to the
    # uniquely matching market rather than writing a misleading row.
    line_matches = prop_lookup["by_player_line_lean"].get((player_norm, line_key(line), lean_norm), [])
    unique_props = {normalize_prop(r.get("prop_type")) for r in line_matches}
    if len(line_matches) == 1 or len(unique_props) == 1:
        return line_matches[0], "line_market_correction"

    return None, "missing"


def validate_and_format_picks(raw_picks, fixtures, props):
    # TODO: AI v1 extension
    prop_lookup = build_prop_lookup(props)
    prop_lookup_by_event = {}
    rows = []
    for idx, pick in enumerate(raw_picks or [], start=1):
        player = str(pick.get("player") or pick.get("Player") or "").strip()
        prop = normalize_prop(pick.get("prop_type") or pick.get("prop") or pick.get("Prop"))
        lean = normalize_pick(pick.get("lean") or pick.get("pick") or pick.get("Pick"))
        line = pick.get("line") if pick.get("line") is not None else pick.get("Line")
        if not player or prop not in {"Shots", "SOT", "Tackles", "Goal Scorer", "Goals"} or line in (None, ""):
            continue
        source_fixture_id = str(pick.get("source_fixture_id") or "").strip()
        active_lookup = prop_lookup
        if source_fixture_id:
            if source_fixture_id not in prop_lookup_by_event:
                event_props = [row for row in props if str(row.get("event_id", "")).strip() == source_fixture_id]
                prop_lookup_by_event[source_fixture_id] = build_prop_lookup(event_props)
            active_lookup = prop_lookup_by_event[source_fixture_id]
        prop_row, match_kind = find_matching_prop_row(active_lookup, player, prop, line, lean)
        if not prop_row:
            print(f"   ⚠️ Skipping unanchored Gemini pick: {player} {prop} {lean} {line}")
            continue
        canonical_prop = normalize_prop(prop_row.get("prop_type"))
        canonical_line = prop_row.get("line", line)
        canonical_lean = normalize_pick(prop_row.get("lean", lean))
        if match_kind != "exact" or canonical_prop != prop or line_key(canonical_line) != line_key(line):
            print(
                "   ⚠️ Corrected Gemini pick to sportsbook market: "
                f"{player} {prop} {lean} {line} -> "
                f"{canonical_prop} {canonical_lean} {canonical_line} "
                f"({prop_row.get('source_market', 'unknown')})"
            )
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
        form_context = prop_row.get("form_context") or {}
        injury_status = safe_float(prop_row.get("injury_status"), 1.0)
        avg_shots = safe_float(form_context.get("avg_shots"))
        numeric_line = safe_float(canonical_line)
        if canonical_prop == "Shots" and numeric_line is not None and avg_shots is not None:
            if numeric_line < 1.5 and avg_shots > 1.5:
                print(
                    "   ⚠️ Suspect Shots line: "
                    f"{player} line={numeric_line:g}, avg_shots={avg_shots:g}, "
                    f"source_market={prop_row.get('source_market', 'unknown')}"
                )
        rows.append(
            {
                "Player": player,
                "Team": team,
                "Opponent": opponent,
                "Prop": canonical_prop,
                "Line": canonical_line,
                "Pick": canonical_lean,
                "Tier": normalize_tier(pick.get("tier") or pick.get("confidence_tier") or pick.get("confidence")),
                "Confidence": confidence,
                "Reasoning": str(pick.get("rationale") or pick.get("reasoning") or "")[:500],
                "Game_Time": game_time,
                "Book": prop_row.get("book", "") or pick.get("book", ""),
                "UD_FP": "",
                "Result": "",
                "Actual": "",
                "Timestamp": timestamp_utc_iso(),
                "Intl_Sample": form_context.get("intl_sample", ""),
                "Avg_Shots": form_context.get("avg_shots", ""),
                "Avg_SOT": form_context.get("avg_sot", ""),
                "Avg_Tackles": form_context.get("avg_tackles", ""),
                "Goal_Scorer_Rate": form_context.get("goal_scorer_rate", ""),
                "Last_5_Shots": form_context.get("last_5_shots", ""),
                "Injury_Status": injury_status if injury_status < 1.0 else "",
                "Injury_Reason": prop_row.get("injury_reason", ""),
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


def ensure_picks_worksheet(sh, sheet_name):
    try:
        ws = sh.worksheet(sheet_name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows=100, cols=len(PICKS_COLUMNS))
        ws.update("A1", [PICKS_COLUMNS])

    existing_headers = ws.row_values(1)
    if not existing_headers:
        existing_headers = PICKS_COLUMNS[:]
        ws.update("A1", [existing_headers])
    else:
        missing_cols = [col for col in PICKS_COLUMNS if col not in existing_headers]
        if missing_cols:
            existing_headers = existing_headers + missing_cols
            ws.update("A1", [existing_headers])
            print(f"✅ Added {sheet_name} columns: {', '.join(missing_cols)}")
    return ws, existing_headers


def write_to_sheet(picks, sheet_name=PICKS_HISTORY_SHEET_NAME, mode="append"):
    gc = get_gspread_client()
    sh = gc.open_by_key(SHEET_ID)
    ws, existing_headers = ensure_picks_worksheet(sh, sheet_name)

    if mode == "overwrite":
        ws.batch_clear(["A2:Z"])
        print(f"🧹 Cleared {sheet_name} current rows")

    if not picks:
        print(f"⏭️ No picks to write to {sheet_name}")
        return

    if mode not in {"append", "overwrite"}:
        raise ValueError(f"Unsupported write mode: {mode}")
    values = [[clean_cell(row.get(col, "")) for col in existing_headers] for row in picks]
    ws.append_rows(values, value_input_option="USER_ENTERED")
    action = "Wrote" if mode == "overwrite" else "Appended"
    print(f"✅ {action} {len(values)} pick row(s) to {sheet_name}")


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
            all_fixtures = fetch_fixtures(odds_api_key)
            fixtures = fixtures_in_window(all_fixtures)
            props = []
            market_422_cache = load_422_cache()
            for fixture in fixtures:
                props.extend(fetch_props(odds_api_key, fixture, market_422_cache))
                time.sleep(0.5)
            save_422_cache(market_422_cache)
            print(f"✅ Parsed {len(props)} prop outcome row(s)")
            if fixtures and props and gemini_api_key:
                raw_picks, props = generate_picks_per_fixture(fixtures, props, gemini_api_key)
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
        write_to_sheet(picks, sheet_name=PICKS_HISTORY_SHEET_NAME, mode="append")
        write_to_sheet(picks, sheet_name=PICKS_CURRENT_SHEET_NAME, mode="overwrite")

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
