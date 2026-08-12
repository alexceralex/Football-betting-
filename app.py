
import streamlit as st
import requests, math, time
import pandas as pd
from datetime import datetime, timezone, timedelta

st.set_page_config(page_title="Football Value Scanner V3.1", page_icon="⚽", layout="wide")

API_BASE = "https://api.odds-api.io/v3"
BOOKMAKER = "Betano"
MAX_SHORTLIST = 10

# -----------------------------
# State
# -----------------------------
for key, default in {
    "blocked_until": None,
    "last_good_events": [],
    "last_good_events_at": None,
    "last_good_odds": [],
    "last_good_odds_at": None,
    "last_good_valuebets": [],
    "last_good_valuebets_at": None,
    "request_counter": 0,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# -----------------------------
# Helpers
# -----------------------------
def api_key():
    try:
        return st.secrets["ODDS_API_KEY"]
    except Exception:
        return ""

def now_utc():
    return datetime.now(timezone.utc)

def blocked():
    b = st.session_state.blocked_until
    return bool(b and now_utc() < b)

def parse_backoff(headers):
    retry = headers.get("Retry-After")
    if retry:
        try:
            return now_utc() + timedelta(seconds=max(10, int(float(retry))))
        except:
            pass

    reset = headers.get("x-ratelimit-reset")
    if reset:
        try:
            v = float(reset)
            # unix timestamp
            if v > 1_000_000_000:
                return datetime.fromtimestamp(v, tz=timezone.utc)
            # seconds from now
            if v > 0:
                return now_utc() + timedelta(seconds=v)
        except:
            pass

    return now_utc() + timedelta(seconds=90)

def api_get(path, params):
    if blocked():
        return None, {"status": 429, "blocked": True}, "BACKOFF"

    p = dict(params)
    p["apiKey"] = api_key()
    st.session_state.request_counter += 1

    try:
        r = requests.get(f"{API_BASE}{path}", params=p, timeout=20)
    except Exception as ex:
        return None, {"status": "NETWORK", "error": str(ex)}, "NETWORK"

    meta = {
        "status": r.status_code,
        "remaining": r.headers.get("x-ratelimit-remaining"),
        "reset": r.headers.get("x-ratelimit-reset"),
        "limit": r.headers.get("x-ratelimit-limit"),
        "retry_after": r.headers.get("Retry-After"),
    }

    if r.status_code == 429:
        st.session_state.blocked_until = parse_backoff(r.headers)
        return None, meta, "RATE_LIMIT"

    try:
        r.raise_for_status()
    except Exception as ex:
        return None, {**meta, "error": str(ex)}, "HTTP"

    try:
        return r.json(), meta, None
    except Exception as ex:
        return None, {**meta, "error": f"JSON: {ex}"}, "PARSE"

def as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data", "events", "results", "odds", "valueBets"):
            v = payload.get(k)
            if isinstance(v, list):
                return v
    return []

def g(obj, *paths, default=None):
    for path in paths:
        cur = obj
        ok = True
        for key in path.split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return default

def eid(e):
    return str(g(e, "id", "eventId", "event_id", default=""))

def team_name(e, side):
    if side == "home":
        x = g(e, "home", "homeTeam.name", "home_team")
    else:
        x = g(e, "away", "awayTeam.name", "away_team")
    if isinstance(x, dict):
        x = x.get("name")
    return str(x or "")

def match_name(e):
    h, a = team_name(e, "home"), team_name(e, "away")
    if h or a:
        return f"{h} – {a}"
    return str(g(e, "name", "eventName", default="Meci"))

def league_name(e):
    x = g(e, "league.name", "competition.name", "league", "competition", default="")
    if isinstance(x, dict):
        return str(x.get("name", ""))
    return str(x)

def score_parts(e):
    hs = g(e, "scores.home", "score.home", "homeScore", default=None)
    aw = g(e, "scores.away", "score.away", "awayScore", default=None)
    try: hs = int(hs)
    except: hs = None
    try: aw = int(aw)
    except: aw = None
    return hs, aw

def score_text(e):
    hs, aw = score_parts(e)
    return f"{hs}-{aw}" if hs is not None and aw is not None else ""

def minute(e):
    m = g(e, "minute", "live.minute", "clock.minute", "time.minute", default=None)
    try: return int(float(m))
    except: return None

def status_text(e):
    return str(g(e, "status", "state", default=""))

def clamp(x, a, b):
    return max(a, min(b, x))

def fair_odds(p):
    return round(1/p, 2) if p and p > 0 else None

def implied_prob(o):
    return 1/o if o and o > 0 else None

def league_reliability(lg):
    s = lg.lower()
    if any(x in s for x in ["friendly", "u19", "u20", "u21", "reserve", "youth"]):
        return 0.78
    if any(x in s for x in [
        "champions league","europa league","conference league","premier league",
        "serie a","la liga","bundesliga","ligue 1","superliga","eredivisie","primeira"
    ]):
        return 1.00
    return 0.90

def simple_live_signal(e):
    """
    Conservative signal using only fields Odds-API live events reliably exposes.
    It deliberately DOES NOT fake xG/SOT if they are not present.
    """
    m = minute(e)
    hs, aw = score_parts(e)
    if m is None:
        m = 45
    if hs is None or aw is None:
        hs = aw = 0

    total = hs + aw
    remaining = max(0, 95 - m)

    # Baseline remaining-goals process; game-state adjustments only.
    lam = 2.65 / 90 * remaining
    diff = hs - aw

    if diff != 0 and m < 75:
        lam *= 1.10  # trailing side usually pushes
    if abs(diff) >= 2 and m >= 60:
        lam *= 0.86  # control mode
    if m >= 75 and diff == 0:
        lam *= 1.05

    lam *= league_reliability(league_name(e))
    p_goal = 1 - math.exp(-lam)

    # confidence cannot be HIGH without richer live stats
    confidence = "MEDIUM" if m is not None and score_text(e) else "LOW"

    # ranking only; not a betting probability
    rank = p_goal
    if 25 <= m <= 72:
        rank += 0.05
    if total <= 2:
        rank += 0.02
    if confidence == "MEDIUM":
        rank += 0.02

    return {
        "signal": round(rank, 3),
        "p_any_goal": clamp(p_goal, 0.02, 0.98),
        "lambda_remaining": round(lam, 3),
        "confidence": confidence,
    }

def model_candidates(e):
    s = simple_live_signal(e)
    m = minute(e)
    hs, aw = score_parts(e)
    if m is None or hs is None or aw is None:
        return []

    p_any = s["p_any_goal"]
    lam = s["lambda_remaining"]
    total = hs + aw
    candidates = []

    # Any additional goal
    candidates.append({
        "Market": "Goals",
        "Selection": "Over 0.5 remaining",
        "P": p_any,
        "Fair": fair_odds(p_any),
        "Confidence": s["confidence"],
        "Reason": "baseline live-goal model from time remaining + game state",
    })

    # Under 3.5
    max_new = 3 - total
    if max_new >= 0:
        cdf = 0.0
        for k in range(max_new + 1):
            cdf += math.exp(-lam) * (lam ** k) / math.factorial(k)
        p_u35 = clamp(cdf, 0.02, 0.98)
        candidates.append({
            "Market": "Totals",
            "Selection": "Under 3.5",
            "P": p_u35,
            "Fair": fair_odds(p_u35),
            "Confidence": s["confidence"],
            "Reason": "Poisson remaining-goals model",
        })

    # Double chance if a team is already leading
    diff = hs - aw
    if diff > 0:
        p = clamp(0.68 + 0.0045*m + min(diff,2)*0.07, 0.45, 0.97)
        candidates.append({
            "Market": "Double Chance",
            "Selection": "Home or Draw",
            "P": p,
            "Fair": fair_odds(p),
            "Confidence": s["confidence"],
            "Reason": "current lead + time remaining",
        })
    elif diff < 0:
        p = clamp(0.68 + 0.0045*m + min(-diff,2)*0.07, 0.45, 0.97)
        candidates.append({
            "Market": "Double Chance",
            "Selection": "Away or Draw",
            "P": p,
            "Fair": fair_odds(p),
            "Confidence": s["confidence"],
            "Reason": "current lead + time remaining",
        })

    return candidates

def normalize_text(x):
    return str(x).lower().replace(" ", "").replace("_", "").replace("-", "")

def flatten_odds(payload, lookup):
    rows = []
    items = as_list(payload)
    if isinstance(payload, dict) and not items:
        items = [payload]

    for item in items:
        i = str(g(item, "eventId", "event_id", "id", default=""))
        ev = lookup.get(i, {})

        books = g(item, "bookmakers", "sportsbooks", default=None)
        if isinstance(books, dict):
            books = [{"name": k, **(v if isinstance(v, dict) else {"markets": v})}
                     for k, v in books.items()]
        if not isinstance(books, list):
            books = [item]

        for b in books:
            bn = str(g(b, "name", "bookmaker", "sportsbook", default=BOOKMAKER))
            if BOOKMAKER.lower() not in bn.lower():
                continue

            markets = g(b, "markets", "odds", default=[])
            if isinstance(markets, dict):
                markets = [{"name": k, "outcomes": v} for k, v in markets.items()]
            if not isinstance(markets, list):
                continue

            for mk in markets:
                mn = str(g(mk, "name", "key", "market", default=""))
                outs = g(mk, "outcomes", "selections", "prices", default=[])
                if isinstance(outs, dict):
                    outs = [{"name": k, "price": v} for k, v in outs.items()]
                if not isinstance(outs, list):
                    continue

                for o in outs:
                    try:
                        price = float(g(o, "price", "odds", "decimal", "value"))
                    except:
                        continue
                    rows.append({
                        "EventID": i,
                        "Match": match_name(ev),
                        "MarketRaw": mn,
                        "SelectionRaw": str(g(o, "name", "label", "selection", default="")),
                        "Line": g(o, "point", "line", "handicap", default=""),
                        "Odds": price,
                    })
    return rows

def odds_matches(model, odd):
    m = normalize_text(model["Market"] + model["Selection"])
    o = normalize_text(odd["MarketRaw"] + odd["SelectionRaw"] + str(odd["Line"]))

    mapping = {
        "over0.5remaining": ["over0.5","over05"],
        "under3.5": ["under3.5","under35"],
        "homeordraw": ["1x","homeordraw","homedraw"],
        "awayordraw": ["x2","awayordraw","awaydraw"],
    }
    for k, pats in mapping.items():
        if k in m:
            return any(p in o for p in pats)
    return False

def dynamic_edge_min(odds):
    if odds < 1.50: return 0.075
    if odds < 1.80: return 0.060
    if odds < 2.20: return 0.050
    return 0.045

# -----------------------------
# API fetches: ONE live call + ONE odds batch
# -----------------------------
@st.cache_data(ttl=120, show_spinner=False)
def fetch_live_once():
    # Dedicated live endpoint = one request
    data, meta, err = api_get("/events/live", {"sport": "football"})
    if not err and data:
        evs = as_list(data)
        return evs, meta, None
    return [], meta, err

@st.cache_data(ttl=60, show_spinner=False)
def fetch_odds_once(ids):
    ids = list(ids)[:MAX_SHORTLIST]
    if not ids:
        return [], None, None
    data, meta, err = api_get(
        "/odds/multi",
        {"eventIds": ",".join(ids), "bookmakers": BOOKMAKER}
    )
    vals = as_list(data)
    if not vals and isinstance(data, dict):
        vals = [data]
    return vals, meta, err

@st.cache_data(ttl=90, show_spinner=False)
def fetch_value_bets_once():
    data, meta, err = api_get(
        "/value-bets",
        {
            "bookmaker": BOOKMAKER,
            "sport": "football",
            "includeEventDetails": "true"
        }
    )
    return as_list(data), meta, err

# -----------------------------
# UI
# -----------------------------
st.title("⚽ Football Value Scanner V3.1")
st.caption("Rate-limit optimized: 1 live request + 1 odds request on max 10 shortlisted games.")

with st.sidebar:
    min_odds = st.number_input("Cotă minimă", 1.01, 10.0, 1.40, 0.05)
    min_prob = st.slider("Probabilitate minimă (%)", 50, 90, 70, 1) / 100
    shortlist_n = st.slider("Shortlist maxim", 3, MAX_SHORTLIST, 8, 1)
    exclude_friendlies = st.checkbox("Exclude amicale", True)
    use_valuebets = st.checkbox("Încarcă și API Value Bets", False)

    if st.button("🔄 Refresh controlat", use_container_width=True):
        # Clear caches only if not currently in backoff
        if blocked():
            st.warning("API încă în backoff. Nu forțez request nou.")
        else:
            st.cache_data.clear()
            st.rerun()

if not api_key():
    st.error("Lipsește ODDS_API_KEY din Streamlit Secrets.")
    st.stop()

if blocked():
    remain = max(0, int((st.session_state.blocked_until - now_utc()).total_seconds()))
    st.warning(f"API în backoff încă ~{remain}s. Folosesc ultimul snapshot bun dacă există.")

events, event_meta, event_err = fetch_live_once()

if events:
    st.session_state.last_good_events = events
    st.session_state.last_good_events_at = now_utc()
elif st.session_state.last_good_events:
    events = st.session_state.last_good_events
    event_err = "CACHE_FALLBACK"
else:
    st.error("Nu am evenimente live și nici snapshot anterior. Așteaptă resetarea rate-limitului.")
    st.stop()

# universe
universe = []
for e in events:
    lg = league_name(e)
    if exclude_friendlies and "friendly" in lg.lower():
        continue
    m = minute(e)
    if m is not None and m > 88:
        continue
    universe.append(e)

signal_rows = []
for e in universe:
    sig = simple_live_signal(e)
    signal_rows.append({
        "EventID": eid(e),
        "Competition": league_name(e),
        "Match": match_name(e),
        "Score": score_text(e),
        "Minute": minute(e),
        "Status": status_text(e),
        "Signal": sig["signal"],
        "p_any_goal": round(sig["p_any_goal"]*100,1),
        "xGR proxy": sig["lambda_remaining"],
        "Confidence": sig["confidence"],
    })

sigdf = pd.DataFrame(signal_rows)
if not sigdf.empty:
    sigdf = sigdf.sort_values("Signal", ascending=False)

short_ids = sigdf.head(shortlist_n)["EventID"].tolist() if not sigdf.empty else []
lookup = {eid(e): e for e in universe}

odds_payload, odds_meta, odds_err = fetch_odds_once(tuple(short_ids))

if odds_payload:
    st.session_state.last_good_odds = odds_payload
    st.session_state.last_good_odds_at = now_utc()
elif st.session_state.last_good_odds:
    odds_payload = st.session_state.last_good_odds
    odds_err = "CACHE_FALLBACK"

odds_rows = flatten_odds(odds_payload, lookup)

# value engine
value_rows = []
for i in short_ids:
    e = lookup.get(i)
    if not e:
        continue
    for model in model_candidates(e):
        for odd in [o for o in odds_rows if o["EventID"] == i and odds_matches(model, o)]:
            odds = odd["Odds"]
            p = model["P"]
            imp = implied_prob(odds)
            edge = (p - imp) if imp else 0
            min_edge = dynamic_edge_min(odds)

            # Conservative: MEDIUM confidence means READY at best, not BET.
            if (
                odds >= min_odds
                and p >= min_prob
                and edge >= min_edge
                and model["Confidence"] == "HIGH"
                and event_err is None
                and odds_err is None
            ):
                level = "BET"
            elif odds >= min_odds and p >= min_prob and edge >= max(0.04, min_edge-0.02):
                level = "READY"
            elif odds >= min_odds and p >= 0.62 and edge >= 0.02:
                level = "WATCH"
            else:
                level = "PASS"

            value_rows.append({
                "Level": level,
                "Competition": league_name(e),
                "Match": match_name(e),
                "Score": score_text(e),
                "Minute": minute(e),
                "Market": model["Market"],
                "Selection": model["Selection"],
                "Model %": round(p*100,1),
                "Fair": model["Fair"],
                "Betano": round(odds,2),
                "Edge pp": round(edge*100,1),
                "Confidence": model["Confidence"],
                "Reason": model["Reason"],
                "Source state": "LIVE" if event_err is None and odds_err is None else "CACHED",
            })

vdf = pd.DataFrame(value_rows)
if not vdf.empty:
    order = {"BET":0,"READY":1,"WATCH":2,"PASS":3}
    vdf["rank"] = vdf["Level"].map(order)
    vdf = vdf.sort_values(["rank","Edge pp"], ascending=[True,False]).drop(columns=["rank"])

tabs = st.tabs(["🌍 LIVE","🎯 SHORTLIST","💰 VALUE PICKS","🧠 CONSENSUS","🧪 API HEALTH"])

with tabs[0]:
    c1,c2,c3 = st.columns(3)
    c1.metric("Live universe", len(universe))
    c2.metric("Shortlist", len(short_ids))
    c3.metric("Requests this session", st.session_state.request_counter)
    st.dataframe(sigdf, use_container_width=True, hide_index=True)

with tabs[1]:
    st.caption("Shortlistul se face înainte de odds; nu consumăm cote pentru tot universul.")
    st.dataframe(sigdf.head(shortlist_n), use_container_width=True, hide_index=True)

with tabs[2]:
    if vdf.empty:
        st.info("Nu am putut mapa încă piețe Betano la modelele curente.")
    else:
        display = vdf[(vdf["Betano"] >= min_odds) & (vdf["Model %"] >= min_prob*100)]
        if display.empty:
            st.info("0 selecții trec pragurile actuale.")
        else:
            st.dataframe(display, use_container_width=True, hide_index=True,
                         column_config={
                             "Betano": st.column_config.NumberColumn(format="%.2f"),
                             "Fair": st.column_config.NumberColumn(format="%.2f")
                         })
            if event_err == "CACHE_FALLBACK" or odds_err == "CACHE_FALLBACK":
                st.warning("Date cached: afișez shortlistul, dar nu ridic nimic la BET.")
            else:
                bets = display[display["Level"]=="BET"]
                if bets.empty:
                    st.write("Niciun BET valid acum.")
                else:
                    for _, r in bets.head(5).iterrows():
                        st.success(
                            f"{r['Match']} | {r['Score']} {r['Minute']}' | "
                            f"{r['Selection']} @ {r['Betano']} | Model {r['Model %']}%"
                        )

with tabs[3]:
    st.write("Cross-check opțional: Odds-API `/value-bets` folosește consensul pieței, nu modelul nostru.")
    if use_valuebets:
        vb, vb_meta, vb_err = fetch_value_bets_once()
        if vb:
            st.session_state.last_good_valuebets = vb
            st.session_state.last_good_valuebets_at = now_utc()
        elif st.session_state.last_good_valuebets:
            vb = st.session_state.last_good_valuebets
            vb_err = "CACHE_FALLBACK"

        rows = []
        for x in vb:
            ev = g(x, "event", default={}) or {}
            odds_obj = g(x, "bookmakerOdds", default={}) or {}
            side = str(g(x, "betSide", default=""))
            price = g(odds_obj, side, default=None)
            try: price = float(price)
            except: price = None
            evv = g(x, "expectedValue", default=None)
            try: evv = float(evv)
            except: evv = None
            rows.append({
                "Match": f"{g(ev,'home',default='')} – {g(ev,'away',default='')}",
                "League": g(ev,"league",default=""),
                "Market": g(x,"market.name",default=""),
                "Side": side,
                "Betano": price,
                "Expected Value": evv,
                "Updated": g(x,"expectedValueUpdatedAt",default=""),
            })
        vbdf = pd.DataFrame(rows)
        if not vbdf.empty:
            st.dataframe(vbdf, use_container_width=True, hide_index=True)
        else:
            st.info("Niciun value bet primit.")
    else:
        st.info("Bifează opțiunea din sidebar doar când vrei cross-check; evităm un request în plus la fiecare rulare.")

with tabs[4]:
    st.write("Event endpoint meta:", event_meta)
    st.write("Odds endpoint meta:", odds_meta)
    st.write("Blocked until:", st.session_state.blocked_until)
    st.write("Last good events:", st.session_state.last_good_events_at)
    st.write("Last good odds:", st.session_state.last_good_odds_at)
    st.write("Requests this session:", st.session_state.request_counter)
    st.info("V3.1 nu face retry agresiv. La 429 intră în backoff și nu mai consumă request-uri până la expirare.")

st.caption(
    "V3.1 este optimizat pentru limita API. Important: Odds-API live events oferă în mod sigur scor/status; "
    "fără xG/SOT/box touches, modelul nu pretinde confidence HIGH. Pentru probabilități serioase, următorul pas "
    "este integrarea unei surse dedicate de statistici live."
)
