import streamlit as st
import requests
import math
import time
from datetime import datetime, timezone

import pandas as pd

st.set_page_config(
    page_title="Football Value Scanner V3.5",
    page_icon="⚽",
    layout="wide",
)

ODDS_BASE = "https://api.odds-api.io/v3"
BOOKMAKER = "Betano"

# --------------------------------------------------
# Session state
# --------------------------------------------------
DEFAULT_STATE = {
    "last_good_live": [],
    "last_good_live_at": None,
    "odds_circuit_open": False,
    "odds_circuit_reason": "",
    "request_count_odds": 0,
    "rate_limit_limit": None,
    "rate_limit_remaining": None,
    "rate_limit_reset": None,
}

for key, value in DEFAULT_STATE.items():
    if key not in st.session_state:
        st.session_state[key] = value


# --------------------------------------------------
# Helpers
# --------------------------------------------------
def now_utc():
    return datetime.now(timezone.utc)


def api_key():
    try:
        return str(st.secrets["ODDS_API_KEY"]).strip()
    except Exception:
        return ""


def clamp(value, low, high):
    return max(low, min(high, value))


def fair_odds(probability):
    if probability and probability > 0:
        return round(1.0 / probability, 2)
    return None


def parse_iso_datetime(value):
    if not value:
        return None

    try:
        value = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def event_id(event):
    return str(event.get("id", ""))


def home_name(event):
    value = event.get("home", "")
    if isinstance(value, dict):
        return str(value.get("name", ""))
    return str(value or "")


def away_name(event):
    value = event.get("away", "")
    if isinstance(value, dict):
        return str(value.get("name", ""))
    return str(value or "")


def league_name(event):
    value = event.get("league", "")
    if isinstance(value, dict):
        return str(value.get("name", ""))
    return str(value or "")


def event_status(event):
    return str(event.get("status", "") or "")


def score_parts(event):
    scores = event.get("scores", {})

    if not isinstance(scores, dict):
        return None, None

    home = scores.get("home")
    away = scores.get("away")

    try:
        home = int(home)
    except Exception:
        home = None

    try:
        away = int(away)
    except Exception:
        away = None

    return home, away


def score_text(event):
    home, away = score_parts(event)

    if home is None or away is None:
        return ""

    return f"{home}-{away}"


def approx_minute(event):
    """
    Odds-API live events expose kickoff time and status, but the documented
    live-events response does not guarantee a match-minute field.
    We therefore derive an APPROXIMATE minute from kickoff time.

    This is intentionally labelled approximate in the UI and model.
    """
    kickoff = parse_iso_datetime(event.get("date"))

    if kickoff is None:
        return None

    elapsed = int((now_utc() - kickoff).total_seconds() / 60)

    if elapsed < 0:
        return None

    return clamp(elapsed, 1, 120)


def league_reliability(league):
    text = str(league or "").lower()

    if any(x in text for x in [
        "friendly", "u19", "u20", "u21", "reserve", "youth"
    ]):
        return 0.78

    if any(x in text for x in [
        "champions league",
        "europa league",
        "conference league",
        "premier league",
        "serie a",
        "la liga",
        "bundesliga",
        "ligue 1",
        "superliga",
        "eredivisie",
        "primeira",
    ]):
        return 1.00

    return 0.90


def signal_score(event):
    minute = approx_minute(event)
    home, away = score_parts(event)

    if minute is None:
        minute = 45

    if home is None or away is None:
        home = away = 0

    total = home + away
    remaining = max(0, 95 - minute)

    lam = (
        (2.65 / 90)
        * remaining
        * league_reliability(league_name(event))
    )

    p_goal = 1 - math.exp(-lam)

    score = p_goal

    if 25 <= minute <= 72:
        score += 0.05

    if total <= 2:
        score += 0.02

    return round(score, 3)


# --------------------------------------------------
# Protected Odds-API request layer
# --------------------------------------------------
def record_rate_limit_headers(response):
    st.session_state.rate_limit_limit = response.headers.get(
        "x-ratelimit-limit"
    )
    st.session_state.rate_limit_remaining = response.headers.get(
        "x-ratelimit-remaining"
    )
    st.session_state.rate_limit_reset = response.headers.get(
        "x-ratelimit-reset"
    )


def protected_get(path, params=None, allow_when_circuit_open=False):
    """
    One guarded request.

    401 / 403 / 429 open the circuit breaker for the rest of the session,
    preventing accidental repeated requests on every Streamlit rerun.
    """
    if not api_key():
        return None, "NO_KEY", "ODDS_API_KEY lipsește din Streamlit Secrets."

    if st.session_state.odds_circuit_open and not allow_when_circuit_open:
        return (
            None,
            "CIRCUIT_OPEN",
            st.session_state.odds_circuit_reason,
        )

    query = dict(params or {})
    query["apiKey"] = api_key()

    try:
        st.session_state.request_count_odds += 1

        response = requests.get(
            f"{ODDS_BASE}{path}",
            params=query,
            headers={"Accept": "application/json"},
            timeout=20,
        )

        record_rate_limit_headers(response)

    except requests.RequestException as exc:
        return None, "NETWORK", str(exc)

    if response.status_code in (401, 403):
        reason = (
            f"Odds-API HTTP {response.status_code}. "
            "Circuit breaker activat; nu mai fac request-uri automat."
        )
        st.session_state.odds_circuit_open = True
        st.session_state.odds_circuit_reason = reason
        return None, "AUTH_OR_PLAN", reason

    if response.status_code == 429:
        reason = (
            "Odds-API HTTP 429. Circuit breaker activat; "
            "nu mai fac request-uri automat."
        )
        st.session_state.odds_circuit_open = True
        st.session_state.odds_circuit_reason = reason
        return None, "RATE_LIMIT_OR_PLAN", reason

    if response.status_code >= 400:
        return (
            None,
            f"HTTP_{response.status_code}",
            response.text[:300],
        )

    try:
        return response.json(), "OK", ""
    except Exception as exc:
        return None, "PARSE", str(exc)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_live_events():
    """
    Exactly one normal request for the live universe:
    GET /events?sport=football&status=live

    This endpoint/usage is documented by Odds-API.
    """
    payload, state, note = protected_get(
        "/events",
        {
            "sport": "football",
            "status": "live",
        },
    )

    if state != "OK":
        return [], state, note

    if isinstance(payload, list):
        events = payload
    elif isinstance(payload, dict):
        events = (
            payload.get("data")
            or payload.get("events")
            or payload.get("results")
            or []
        )
    else:
        events = []

    events = [
        e for e in events
        if isinstance(e, dict)
        and str(e.get("status", "")).lower() == "live"
    ]

    return events, "OK", ""


@st.cache_data(ttl=90, show_spinner=False)
def fetch_event_odds(event_id_value):
    """
    Called ONLY for shortlist events and ONLY if user explicitly enables
    Betano odds. One request per shortlisted event, cached for 90 seconds.
    """
    payload, state, note = protected_get(
        "/odds",
        {
            "eventId": event_id_value,
            "bookmakers": BOOKMAKER,
        },
    )

    return payload, state, note


# --------------------------------------------------
# Conservative model
# --------------------------------------------------
def live_model(event):
    minute = approx_minute(event)
    home, away = score_parts(event)

    if minute is None or home is None or away is None:
        return []

    total = home + away
    remaining = max(0, 95 - minute)

    lam = (
        (2.65 / 90)
        * remaining
        * league_reliability(league_name(event))
    )

    diff = home - away

    if diff != 0 and minute < 75:
        lam *= 1.10

    if abs(diff) >= 2 and minute >= 60:
        lam *= 0.86

    if minute >= 75 and diff == 0:
        lam *= 1.05

    p_goal = clamp(1 - math.exp(-lam), 0.02, 0.98)

    output = [{
        "Market": "Goals",
        "Selection": "Over 0.5 remaining",
        "P": p_goal,
        "Fair": fair_odds(p_goal),
        "Confidence": "LOW",
        "Reason": "approx minute + score + league baseline",
    }]

    max_new = 3 - total

    if max_new >= 0:
        cdf = 0.0

        for k in range(max_new + 1):
            cdf += (
                math.exp(-lam)
                * (lam ** k)
                / math.factorial(k)
            )

        p_under_35 = clamp(cdf, 0.02, 0.98)

        output.append({
            "Market": "Totals",
            "Selection": "Under 3.5",
            "P": p_under_35,
            "Fair": fair_odds(p_under_35),
            "Confidence": "LOW",
            "Reason": "remaining-goals Poisson; approximate minute",
        })

    if diff > 0:
        probability = clamp(
            0.68 + 0.0045 * minute + min(diff, 2) * 0.07,
            0.45,
            0.97,
        )

        output.append({
            "Market": "Double Chance",
            "Selection": "Home or Draw",
            "P": probability,
            "Fair": fair_odds(probability),
            "Confidence": "LOW",
            "Reason": "current lead + approximate minute",
        })

    elif diff < 0:
        probability = clamp(
            0.68 + 0.0045 * minute + min(-diff, 2) * 0.07,
            0.45,
            0.97,
        )

        output.append({
            "Market": "Double Chance",
            "Selection": "Away or Draw",
            "P": probability,
            "Fair": fair_odds(probability),
            "Confidence": "LOW",
            "Reason": "current lead + approximate minute",
        })

    return output


# --------------------------------------------------
# Generic odds parser for Betano response
# --------------------------------------------------
def flatten_betano(payload):
    rows = []

    if payload is None:
        return rows

    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        items = (
            payload.get("data")
            or payload.get("odds")
            or payload.get("bookmakers")
            or [payload]
        )

        if isinstance(items, dict):
            items = [items]
    else:
        return rows

    def walk(obj, market_name=""):
        if isinstance(obj, dict):
            bookmaker = str(
                obj.get("bookmaker")
                or obj.get("name")
                or ""
            )

            current_market = str(
                obj.get("market")
                or obj.get("key")
                or market_name
                or ""
            )

            price = (
                obj.get("price")
                or obj.get("odds")
                or obj.get("decimal")
            )

            selection = str(
                obj.get("selection")
                or obj.get("label")
                or obj.get("outcome")
                or obj.get("name")
                or ""
            )

            if price is not None:
                try:
                    price_float = float(price)

                    if (
                        "betano" in bookmaker.lower()
                        or bookmaker == ""
                    ):
                        rows.append({
                            "Market": current_market,
                            "Selection": selection,
                            "Odds": price_float,
                        })
                except Exception:
                    pass

            for key, value in obj.items():
                if isinstance(value, (dict, list)):
                    next_market = current_market

                    if key.lower() in (
                        "markets",
                        "market",
                        "outcomes",
                        "selections",
                    ):
                        next_market = current_market or key

                    walk(value, next_market)

        elif isinstance(obj, list):
            for item in obj:
                walk(item, market_name)

    walk(items)

    # Remove exact duplicates.
    unique = []
    seen = set()

    for row in rows:
        key = (
            row["Market"],
            row["Selection"],
            row["Odds"],
        )

        if key not in seen:
            seen.add(key)
            unique.append(row)

    return unique


# --------------------------------------------------
# UI
# --------------------------------------------------
st.title("⚽ Football Value Scanner V3.5")
st.caption(
    "Odds-API = live universe. Betano odds are opt-in. "
    "Circuit breaker prevents repeated blocked requests."
)

with st.sidebar:
    min_odds = st.number_input(
        "Cotă minimă",
        min_value=1.01,
        max_value=10.0,
        value=1.40,
        step=0.05,
    )

    min_prob = (
        st.slider(
            "Probabilitate minimă (%)",
            min_value=50,
            max_value=90,
            value=70,
            step=1,
        )
        / 100
    )

    shortlist_n = st.slider(
        "Shortlist",
        min_value=3,
        max_value=10,
        value=8,
        step=1,
    )

    exclude_friendlies = st.checkbox(
        "Exclude amicale",
        value=True,
    )

    try_betano = st.checkbox(
        "Încarcă Betano pentru shortlist",
        value=False,
        help=(
            "OFF implicit. Dacă îl activezi, aplicația face maximum "
            "un request per meci din shortlist și îl ține în cache 90 sec."
        ),
    )

    if st.button(
        "🔄 Refresh live",
        use_container_width=True,
    ):
        # Clear only cached data. Circuit breaker stays ON if API was blocked.
        st.cache_data.clear()
        st.rerun()

    if st.session_state.odds_circuit_open:
        st.error("API circuit breaker: ON")

        if st.button(
            "Reset circuit breaker",
            use_container_width=True,
        ):
            st.session_state.odds_circuit_open = False
            st.session_state.odds_circuit_reason = ""
            st.cache_data.clear()
            st.rerun()


# --------------------------------------------------
# Live universe
# --------------------------------------------------
live, live_state, live_note = fetch_live_events()

if live:
    st.session_state.last_good_live = live
    st.session_state.last_good_live_at = now_utc()
    live_source = "ODDS-API LIVE"

elif st.session_state.last_good_live:
    live = st.session_state.last_good_live
    live_source = "LAST GOOD SNAPSHOT"

else:
    live_source = "NO DATA"


if live_state != "OK":
    st.error(
        f"Live feed: {live_state}. {live_note}"
    )

    if not live:
        st.info(
            "Nu mai fac alte request-uri automat. "
            "Dacă API-ul a răspuns 401/403/429, circuit breaker-ul "
            "rămâne activ ca să nu ardă quota."
        )


# --------------------------------------------------
# Filters + table
# --------------------------------------------------
universe = []

for event in live:
    league = league_name(event)

    if exclude_friendlies and "friendly" in league.lower():
        continue

    universe.append(event)


universe = sorted(
    universe,
    key=signal_score,
    reverse=True,
)

rows = []

for event in universe:
    rows.append({
        "ID": event_id(event),
        "Competition": league_name(event),
        "Match": f"{home_name(event)} – {away_name(event)}",
        "Score": score_text(event),
        "Approx Min": approx_minute(event),
        "Status": event_status(event),
        "Signal": signal_score(event),
    })


live_df = pd.DataFrame(rows)

short_events = universe[:shortlist_n]

short_rows = []

for event in short_events:
    short_rows.append({
        "ID": event_id(event),
        "Competition": league_name(event),
        "Match": f"{home_name(event)} – {away_name(event)}",
        "Score": score_text(event),
        "Approx Min": approx_minute(event),
        "Signal": signal_score(event),
    })

short_df = pd.DataFrame(short_rows)


# --------------------------------------------------
# Model
# --------------------------------------------------
model_rows = []

for event in short_events:
    for candidate in live_model(event):
        model_rows.append({
            "ID": event_id(event),
            "Competition": league_name(event),
            "Match": f"{home_name(event)} – {away_name(event)}",
            "Score": score_text(event),
            "Approx Min": approx_minute(event),
            **candidate,
        })

model_df = pd.DataFrame(model_rows)


# --------------------------------------------------
# Optional Betano
# --------------------------------------------------
betano_frames = []
betano_state_rows = []

if try_betano and not st.session_state.odds_circuit_open:
    for event in short_events:
        eid = event_id(event)

        if not eid:
            continue

        payload, state, note = fetch_event_odds(eid)

        betano_state_rows.append({
            "Match": f"{home_name(event)} – {away_name(event)}",
            "State": state,
            "Note": note,
        })

        if state != "OK":
            # If circuit breaker opened on this request,
            # stop immediately. Do not keep burning requests.
            if st.session_state.odds_circuit_open:
                break
            continue

        parsed = flatten_betano(payload)

        if parsed:
            frame = pd.DataFrame(parsed)
            frame.insert(
                0,
                "Match",
                f"{home_name(event)} – {away_name(event)}",
            )
            betano_frames.append(frame)


if betano_frames:
    betano_df = pd.concat(
        betano_frames,
        ignore_index=True,
    )

    if "Odds" in betano_df.columns:
        betano_df = betano_df[
            betano_df["Odds"] >= min_odds
        ]
else:
    betano_df = pd.DataFrame()


# --------------------------------------------------
# Tabs
# --------------------------------------------------
tabs = st.tabs([
    "🌍 LIVE",
    "🎯 SHORTLIST",
    "🧠 MODEL",
    "💰 BETANO",
    "🧪 HEALTH",
])


with tabs[0]:
    col1, col2, col3 = st.columns(3)

    col1.metric(
        "Live matches",
        len(universe),
    )

    col2.metric(
        "Live source",
        live_source,
    )

    col3.metric(
        "Odds-API requests",
        st.session_state.request_count_odds,
    )

    if live_df.empty:
        st.warning(
            "Nu am meciuri live de afișat."
        )
    else:
        st.dataframe(
            live_df,
            use_container_width=True,
            hide_index=True,
        )

    st.caption(
        "Approx Min este estimat din kickoff deoarece răspunsul "
        "documentat pentru live events nu garantează minutul curent."
    )


with tabs[1]:
    if short_df.empty:
        st.info("Shortlist gol.")
    else:
        st.dataframe(
            short_df,
            use_container_width=True,
            hide_index=True,
        )


with tabs[2]:
    st.warning(
        "Modelul este deliberat LOW confidence cât timp minutul este estimat. "
        "Nu prezint probabilitatea ca certitudine."
    )

    if model_df.empty:
        st.info(
            "Nu am suficiente date pentru candidate."
        )
    else:
        view = model_df.copy()
        view["Model %"] = (
            view["P"] * 100
        ).round(1)

        qualified = view[
            view["P"] >= min_prob
        ].copy()

        view = view.drop(
            columns=["P"],
            errors="ignore",
        )

        st.dataframe(
            view,
            use_container_width=True,
            hide_index=True,
        )

        st.caption(
            f"{len(qualified)} candidate au model ≥ "
            f"{int(min_prob * 100)}% înainte de filtrarea pe cotă."
        )


with tabs[3]:
    if not try_betano:
        st.info(
            "Betano este OFF implicit. "
            "Bifează opțiunea din sidebar doar când vrei să consumi request-uri."
        )

    elif st.session_state.odds_circuit_open:
        st.error(
            "Circuit breaker activ. Nu mai cer cote Betano."
        )

    else:
        if not betano_df.empty:
            st.dataframe(
                betano_df,
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.warning(
                "Nu am pars-at cote Betano pentru shortlistul curent."
            )

        if betano_state_rows:
            st.dataframe(
                pd.DataFrame(betano_state_rows),
                use_container_width=True,
                hide_index=True,
            )


with tabs[4]:
    st.write(
        "Last good live snapshot:",
        st.session_state.last_good_live_at,
    )

    st.write(
        "Odds-API requests this session:",
        st.session_state.request_count_odds,
    )

    st.write(
        "Circuit breaker:",
        "ON" if st.session_state.odds_circuit_open else "OFF",
    )

    if st.session_state.odds_circuit_reason:
        st.write(
            "Circuit reason:",
            st.session_state.odds_circuit_reason,
        )

    st.write(
        "Rate limit:",
        st.session_state.rate_limit_limit,
    )

    st.write(
        "Rate remaining:",
        st.session_state.rate_limit_remaining,
    )

    st.write(
        "Rate reset:",
        st.session_state.rate_limit_reset,
    )

    st.success(
        "Normal live refresh = 1 request. "
        "Betano is opt-in and stops immediately on 401/403/429."
    )


st.caption(
    "V3.5: official Odds-API REST live universe, "
    "60s cache, last-good snapshot, circuit breaker, "
    "Betano opt-in."
)
