import streamlit as st
import requests, math, re, time
import pandas as pd
from datetime import datetime, timezone
from difflib import SequenceMatcher

st.set_page_config(page_title="Football Value Scanner V3.2", page_icon="⚽", layout="wide")

SPORTSCORE_MATCHES = "https://sportscore.com/api/widget/matches/"
SPORTSCORE_MATCH = "https://sportscore.com/api/widget/match/"
ODDS_BASE = "https://api.odds-api.io/v3"
BOOKMAKER = "Betano"

# -----------------------------
# Session state
# -----------------------------
for k, v in {
    "last_good_live": [],
    "last_good_live_at": None,
    "odds_access_state": "UNKNOWN",
    "odds_last_error": "",
    "request_count_sportscore": 0,
    "request_count_odds": 0,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# -----------------------------
# Generic helpers
# -----------------------------
def odds_key():
    try:
        return st.secrets["ODDS_API_KEY"]
    except Exception:
        return ""

def now():
    return datetime.now(timezone.utc)

def as_list(payload, keys=("matches","data","events","results","odds")):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in keys:
            if isinstance(payload.get(k), list):
                return payload[k]
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

def norm(s):
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s

def similarity(a,b):
    return SequenceMatcher(None, norm(a), norm(b)).ratio()

def clamp(x,a,b):
    return max(a,min(b,x))

def fair_odds(p):
    return round(1/p,2) if p and p>0 else None

def implied(o):
    return 1/o if o and o>0 else None

# -----------------------------
# SportScore parser
# -----------------------------
def ss_home(m):
    x = g(m,"home.name","homeTeam.name","home","team1.name","teams.home.name",default="")
    if isinstance(x,dict): x=x.get("name","")
    return str(x or "")

def ss_away(m):
    x = g(m,"away.name","awayTeam.name","away","team2.name","teams.away.name",default="")
    if isinstance(x,dict): x=x.get("name","")
    return str(x or "")

def ss_match_name(m):
    h,a=ss_home(m),ss_away(m)
    return f"{h} – {a}" if h or a else str(g(m,"name","title",default="Meci"))

def ss_league(m):
    x=g(m,"competition.name","league.name","competition","league","tournament.name",default="")
    if isinstance(x,dict): x=x.get("name","")
    return str(x or "")

def ss_score_parts(m):
    hs=g(m,"score.home","homeScore","scores.home","score1","home_score",default=None)
    aw=g(m,"score.away","awayScore","scores.away","score2","away_score",default=None)
    try: hs=int(hs)
    except: hs=None
    try: aw=int(aw)
    except: aw=None
    return hs,aw

def ss_score(m):
    h,a=ss_score_parts(m)
    return f"{h}-{a}" if h is not None and a is not None else ""

def ss_minute(m):
    x=g(m,"minute","clock.minute","live.minute","time.minute","elapsed",default=None)
    if x is None:
        txt=str(g(m,"status","state","time",default=""))
        mt=re.search(r"(\d{1,3})",txt)
        if mt:
            x=mt.group(1)
    try: return int(float(x))
    except: return None

def ss_status(m):
    return str(g(m,"status","state","phase","time.status",default=""))

def ss_slug(m):
    return str(g(m,"slug","matchSlug","urlSlug",default=""))

def ss_id(m):
    return str(g(m,"id","matchId","match_id",default=ss_slug(m)))

def is_live_status(m):
    s=ss_status(m).lower()
    minute=ss_minute(m)
    if minute is not None and 0 <= minute <= 130:
        return True
    return any(x in s for x in ["live","1st","2nd","half","ht","extra","pen"])

# -----------------------------
# API calls
# -----------------------------
@st.cache_data(ttl=60, show_spinner=False)
def fetch_sportscore_live():
    st.session_state.request_count_sportscore += 1
    r=requests.get(
        SPORTSCORE_MATCHES,
        params={"sport":"football","limit":50,"src":"football-value-scanner"},
        timeout=20
    )
    r.raise_for_status()
    data=r.json()
    matches=as_list(data,("matches","data","results"))
    live=[m for m in matches if is_live_status(m)]
    return live

@st.cache_data(ttl=120, show_spinner=False)
def fetch_match_detail(slug):
    if not slug:
        return {}
    st.session_state.request_count_sportscore += 1
    try:
        r=requests.get(
            SPORTSCORE_MATCH,
            params={"sport":"football","slug":slug,"src":"football-value-scanner"},
            timeout=15
        )
        if r.status_code != 200:
            return {}
        return r.json()
    except:
        return {}

def odds_get(path, params):
    if not odds_key():
        return None, "NO_KEY", None

    st.session_state.request_count_odds += 1
    p=dict(params)
    p["apiKey"]=odds_key()

    try:
        r=requests.get(f"{ODDS_BASE}{path}",params=p,timeout=20)
    except Exception as ex:
        return None,"NETWORK",str(ex)

    if r.status_code in (401,403):
        return None,"AUTH_OR_PLAN",r.text[:300]

    if r.status_code == 429:
        return None,"BLOCKED_429",r.text[:300]

    if r.status_code >= 400:
        return None,f"HTTP_{r.status_code}",r.text[:300]

    try:
        return r.json(),"OK",None
    except Exception as ex:
        return None,"PARSE",str(ex)

# -----------------------------
# Live model: intentionally conservative
# -----------------------------
def league_reliability(lg):
    s=lg.lower()
    if any(x in s for x in ["friendly","u19","u20","u21","reserve","youth"]):
        return 0.78
    if any(x in s for x in [
        "champions league","europa league","conference league",
        "premier league","serie a","la liga","bundesliga","ligue 1",
        "superliga","eredivisie","primeira"
    ]):
        return 1.00
    return 0.90

def timeline_features(detail):
    events=as_list(detail,("timeline","events","incidents","data"))
    cards=goals=subs=0
    recent_events=0
    for ev in events:
        txt=" ".join([
            str(g(ev,"type",default="")),
            str(g(ev,"event",default="")),
            str(g(ev,"name",default=""))
        ]).lower()
        if "goal" in txt: goals+=1
        if "card" in txt or "yellow" in txt or "red" in txt: cards+=1
        if "sub" in txt: subs+=1
        em=g(ev,"minute","time.minute","elapsed",default=None)
        try:
            if int(float(em)) >= 70:
                recent_events+=1
        except:
            pass
    return {"goals":goals,"cards":cards,"subs":subs,"recent_events":recent_events}

def live_model(m, detail=None):
    minute=ss_minute(m)
    hs,aw=ss_score_parts(m)
    if minute is None or hs is None or aw is None:
        return []

    total=hs+aw
    remaining=max(0,95-minute)
    lam=(2.65/90)*remaining*league_reliability(ss_league(m))

    diff=hs-aw
    if diff != 0 and minute < 75:
        lam*=1.10
    if abs(diff)>=2 and minute>=60:
        lam*=0.86
    if minute>=75 and diff==0:
        lam*=1.05

    feat=timeline_features(detail or {})
    if feat["recent_events"]>=3:
        lam*=1.05

    p_goal=clamp(1-math.exp(-lam),0.02,0.98)
    confidence="MEDIUM" if detail else "LOW"

    out=[{
        "Market":"Goals",
        "Selection":"Over 0.5 remaining",
        "P":p_goal,
        "Fair":fair_odds(p_goal),
        "Confidence":confidence,
        "Reason":"time remaining + score state + competition baseline"
    }]

    max_new=3-total
    if max_new>=0:
        cdf=0.0
        for k in range(max_new+1):
            cdf += math.exp(-lam)*(lam**k)/math.factorial(k)
        p_u35=clamp(cdf,0.02,0.98)
        out.append({
            "Market":"Totals",
            "Selection":"Under 3.5",
            "P":p_u35,
            "Fair":fair_odds(p_u35),
            "Confidence":confidence,
            "Reason":"remaining-goals Poisson"
        })

    if diff>0:
        p=clamp(0.68+0.0045*minute+min(diff,2)*0.07,0.45,0.97)
        out.append({
            "Market":"Double Chance","Selection":"Home or Draw",
            "P":p,"Fair":fair_odds(p),"Confidence":confidence,
            "Reason":"current lead + time remaining"
        })
    elif diff<0:
        p=clamp(0.68+0.0045*minute+min(-diff,2)*0.07,0.45,0.97)
        out.append({
            "Market":"Double Chance","Selection":"Away or Draw",
            "P":p,"Fair":fair_odds(p),"Confidence":confidence,
            "Reason":"current lead + time remaining"
        })

    return out

def signal_score(m):
    minute=ss_minute(m) or 45
    hs,aw=ss_score_parts(m)
    if hs is None or aw is None:
        hs=aw=0
    total=hs+aw
    rem=max(0,95-minute)
    p_goal=1-math.exp(-(2.65/90)*rem*league_reliability(ss_league(m)))
    score=p_goal
    if 25<=minute<=72: score+=0.05
    if total<=2: score+=0.02
    return round(score,3)

# -----------------------------
# Odds event mapping
# -----------------------------
def odds_event_home(e):
    x=g(e,"home","homeTeam.name","home_team",default="")
    if isinstance(x,dict): x=x.get("name","")
    return str(x or "")

def odds_event_away(e):
    x=g(e,"away","awayTeam.name","away_team",default="")
    if isinstance(x,dict): x=x.get("name","")
    return str(x or "")

def map_event(ssm, odds_events):
    h,a=ss_home(ssm),ss_away(ssm)
    best=None
    bestscore=0
    for oe in odds_events:
        oh,oa=odds_event_home(oe),odds_event_away(oe)
        sc=(similarity(h,oh)+similarity(a,oa))/2
        if sc>bestscore:
            bestscore=sc
            best=oe
    return best,bestscore

def flatten_betano(payload):
    rows=[]
    items=as_list(payload,("data","events","results","odds"))
    if isinstance(payload,dict) and not items:
        items=[payload]
    for item in items:
        event_id=str(g(item,"eventId","event_id","id",default=""))
        books=g(item,"bookmakers","sportsbooks",default=None)
        if isinstance(books,dict):
            books=[{"name":k,**(v if isinstance(v,dict) else {"markets":v})} for k,v in books.items()]
        if not isinstance(books,list): books=[item]
        for b in books:
            bn=str(g(b,"name","bookmaker","sportsbook",default=""))
            if "betano" not in bn.lower(): continue
            markets=g(b,"markets","odds",default=[])
            if isinstance(markets,dict):
                markets=[{"name":k,"outcomes":v} for k,v in markets.items()]
            if not isinstance(markets,list): continue
            for mk in markets:
                mn=str(g(mk,"name","key","market",default=""))
                outs=g(mk,"outcomes","selections","prices",default=[])
                if isinstance(outs,dict):
                    outs=[{"name":k,"price":v} for k,v in outs.items()]
                if not isinstance(outs,list): continue
                for o in outs:
                    try: price=float(g(o,"price","odds","decimal","value"))
                    except: continue
                    rows.append({
                        "EventID":event_id,
                        "MarketRaw":mn,
                        "SelectionRaw":str(g(o,"name","label","selection",default="")),
                        "Line":g(o,"point","line","handicap",default=""),
                        "Odds":price
                    })
    return rows

def odds_matches(model, odd):
    m=norm(model["Market"]+model["Selection"])
    o=norm(odd["MarketRaw"]+odd["SelectionRaw"]+str(odd["Line"]))
    mapping={
        "over05remaining":["over05"],
        "under35":["under35"],
        "homeordraw":["1x","homeordraw","homedraw"],
        "awayordraw":["x2","awayordraw","awaydraw"],
    }
    for k,pats in mapping.items():
        if k in m:
            return any(x in o for x in pats)
    return False

# -----------------------------
# UI
# -----------------------------
st.title("⚽ Football Value Scanner V3.2")
st.caption("SportScore live universe + plan-aware Betano odds. Live scanner no longer dies if Odds-API blocks live odds.")

with st.sidebar:
    min_odds=st.number_input("Cotă minimă",1.01,10.0,1.40,0.05)
    min_prob=st.slider("Probabilitate minimă (%)",50,90,70,1)/100
    shortlist_n=st.slider("Shortlist",3,10,8,1)
    exclude_friendlies=st.checkbox("Exclude amicale",True)
    load_details=st.checkbox("Încarcă detalii SportScore pentru shortlist",True)
    try_live_odds=st.checkbox("Încearcă live odds Betano",False)
    if st.button("🔄 Refresh",use_container_width=True):
        st.cache_data.clear()
        st.rerun()

try:
    live=fetch_sportscore_live()
    if live:
        st.session_state.last_good_live=live
        st.session_state.last_good_live_at=now()
except Exception as ex:
    live=[]

if not live and st.session_state.last_good_live:
    live=st.session_state.last_good_live
    live_source="CACHED"
else:
    live_source="LIVE"

if not live:
    st.error("SportScore nu a returnat meciuri live în răspunsul curent.")
    st.stop()

universe=[]
for m in live:
    if exclude_friendlies and "friendly" in ss_league(m).lower():
        continue
    mi=ss_minute(m)
    if mi is not None and mi>88:
        continue
    universe.append(m)

rows=[]
for m in universe:
    rows.append({
        "ID":ss_id(m),
        "Competition":ss_league(m),
        "Match":ss_match_name(m),
        "Score":ss_score(m),
        "Minute":ss_minute(m),
        "Status":ss_status(m),
        "Signal":signal_score(m),
        "Slug":ss_slug(m)
    })

udf=pd.DataFrame(rows)
if not udf.empty:
    udf=udf.sort_values("Signal",ascending=False)

short=udf.head(shortlist_n) if not udf.empty else pd.DataFrame()
short_ids=short["ID"].tolist() if not short.empty else []

details={}
if load_details:
    for _,r in short.iterrows():
        slug=r["Slug"]
        if slug:
            details[r["ID"]]=fetch_match_detail(slug)

model_rows=[]
match_lookup={ss_id(m):m for m in universe}
for mid in short_ids:
    m=match_lookup.get(mid)
    if not m: continue
    detail=details.get(mid,{})
    for c in live_model(m,detail):
        model_rows.append({
            "ID":mid,
            "Competition":ss_league(m),
            "Match":ss_match_name(m),
            "Score":ss_score(m),
            "Minute":ss_minute(m),
            **c
        })

modeldf=pd.DataFrame(model_rows)

odds_state="NOT_REQUESTED"
odds_note="Live odds disabled to protect free plan."
betano_rows=[]

if try_live_odds:
    events_payload,state,err=odds_get(
        "/events",
        {"sport":"football","status":"live","bookmaker":BOOKMAKER}
    )
    odds_state=state

    if state=="OK":
        oe=as_list(events_payload,("data","events","results"))
        mapped=[]

        for mid in short_ids:
            sm=match_lookup.get(mid)
            match,sc=map_event(sm,oe) if sm else (None,0)
            if match is not None and sc>=0.72:
                mapped.append((mid,match,sc))

        event_ids=[
            str(g(x[1],"id","eventId","event_id",default=""))
            for x in mapped
        ]
        event_ids=[x for x in event_ids if x][:10]

        if event_ids:
            payload,state2,err2=odds_get(
                "/odds/multi",
                {
                    "eventIds":",".join(event_ids),
                    "bookmakers":BOOKMAKER
                }
            )
            odds_state=state2

            if state2=="OK":
                betano_rows=flatten_betano(payload)
                odds_note=f"Live odds request succeeded. Parsed {len(betano_rows)} Betano selections."
            else:
                odds_note=f"Live odds endpoint unavailable: {state2}. {err2 or ''}"
        else:
            odds_note="Odds event mapping found no confident matches."
    else:
        if state=="BLOCKED_429":
            odds_note="Odds-API blocked this live request on the current plan. This is NOT treated as hourly quota exhaustion."
        elif state=="AUTH_OR_PLAN":
            odds_note="Current API plan/authorization does not allow this live request."
        else:
            odds_note=f"Odds request failed: {state}. {err or ''}"

st.session_state.odds_access_state=odds_state
st.session_state.odds_last_error=odds_note

value=[]
if betano_rows and not modeldf.empty:
    pass

tabs=st.tabs([
    "🌍 LIVE",
    "🎯 SHORTLIST",
    "🧠 MODEL",
    "💰 BETANO ACCESS",
    "🧪 HEALTH"
])

with tabs[0]:
    c1,c2,c3=st.columns(3)
    c1.metric("Live matches",len(universe))
    c2.metric("Live source",live_source)
    c3.metric("SportScore requests",st.session_state.request_count_sportscore)

    st.dataframe(
        udf,
        use_container_width=True,
        hide_index=True
    )

    st.markdown(
        'Data from [SportScore](https://sportscore.com/)'
    )

with tabs[1]:
    st.caption("Shortlistul este calculat fără cote.")
    st.dataframe(
        short,
        use_container_width=True,
        hide_index=True
    )

with tabs[2]:
    st.warning(
        "Modelul V3.2 nu pretinde xG/SOT dacă sursa nu le oferă. "
        "Confidence rămâne LOW/MEDIUM."
    )

    if modeldf.empty:
        st.info("Nicio candidată model.")
    else:
        show=modeldf.copy()
        show["Model %"]=(show["P"]*100).round(1)
        show=show.drop(columns=["P"])

        st.dataframe(
            show,
            use_container_width=True,
            hide_index=True
        )

        qualified=show[
            show["Model %"]>=min_prob*100
        ]

        st.caption(
            f"{len(qualified)} evenimente model ≥ "
            f"{int(min_prob*100)}%, înainte de verificarea cotei."
        )

with tabs[3]:
    st.write("Status:",odds_state)
    st.info(odds_note)

    if not try_live_odds:
        st.write(
            "Bifează «Încearcă live odds Betano» numai când vrei să testezi accesul. "
            "Nu consumăm request-uri Odds-API în mod implicit."
        )

    if betano_rows:
        odf=pd.DataFrame(betano_rows)
        odf=odf[odf["Odds"]>=min_odds]

        st.dataframe(
            odf,
            use_container_width=True,
            hide_index=True
        )

    st.caption(
        "Pe planul free, dacă live odds sunt blocate, "
        "scannerul rămâne funcțional pe SportScore; "
        "nu afișează cote pre-match ca și cum ar fi live."
    )

with tabs[4]:
    st.write(
        "SportScore live snapshot:",
        st.session_state.last_good_live_at
    )

    st.write(
        "SportScore requests this session:",
        st.session_state.request_count_sportscore
    )

    st.write(
        "Odds-API requests this session:",
        st.session_state.request_count_odds
    )

    st.write(
        "Odds access state:",
        st.session_state.odds_access_state
    )

    st.write(
        "Odds note:",
        st.session_state.odds_last_error
    )

    st.success(
        "Kill-switch separat: lipsa live odds "
        "nu mai oprește universul live."
    )

    st.markdown(
        'SportScore free API requires visible attribution: '
        '[Powered by SportScore](https://sportscore.com/).'
    )

st.caption(
    "V3.2 architecture: SportScore = free live scores/universe; "
    "Odds-API = optional Betano layer. "
    "No live price is presented unless the live endpoint actually succeeds."
)
