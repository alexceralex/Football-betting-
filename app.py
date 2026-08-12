import streamlit as st
import requests, math, re, time
import pandas as pd
from datetime import datetime, timezone
from difflib import SequenceMatcher

st.set_page_config(page_title="Football Value Scanner V3.4", page_icon="⚽", layout="wide")

SPORTSCORE_MATCHES = "https://sportscore.com/api/widget/matches/"
SPORTSCORE_MATCH = "https://sportscore.com/api/widget/match/"
SOFASCORE_LIVE = "https://www.sofascore.com/api/v1/sport/football/events/live"
SOFASCORE_EVENT = "https://www.sofascore.com/api/v1/event/{event_id}"
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
    "request_count_sofascore": 0,
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
    # SportScore may expose scores either as scalars or nested objects.
    hs=g(
        m,
        "score.home","scores.home","score1","home_score",
        "homeScore.current","homeScore.display","homeScore.normaltime","homeScore.period1",
        "homeScore",
        default=None
    )
    aw=g(
        m,
        "score.away","scores.away","score2","away_score",
        "awayScore.current","awayScore.display","awayScore.normaltime","awayScore.period1",
        "awayScore",
        default=None
    )
    if isinstance(hs,dict):
        hs=g(hs,"current","display","normaltime","period1",default=None)
    if isinstance(aw,dict):
        aw=g(aw,"current","display","normaltime","period1",default=None)
    try: hs=int(float(hs))
    except: hs=None
    try: aw=int(float(aw))
    except: aw=None
    return hs,aw

def ss_score(m):
    h,a=ss_score_parts(m)
    return f"{h}-{a}" if h is not None and a is not None else ""

def _status_text(m):
    vals=[]
    for path in (
        "status.type","status.description","status.name","status.code",
        "state.type","state.description","state.name",
        "phase","period","matchStatus","status"
    ):
        x=g(m,path,default=None)
        if x is not None and not isinstance(x,(dict,list)):
            vals.append(str(x))
    return " ".join(vals).strip()

def ss_minute(m):
    for path in (
        "minute","clock.minute","live.minute","time.minute","elapsed",
        "statusTime.minute","matchClock.minute","timer.minute","time.current"
    ):
        x=g(m,path,default=None)
        try:
            if x is not None:
                v=int(float(x))
                if 0 <= v <= 130:
                    return v
        except:
            pass

    # SofaScore commonly exposes the current period start timestamp rather than a minute.
    period_start=g(m,"time.currentPeriodStartTimestamp","currentPeriodStartTimestamp",default=None)
    try:
        if period_start is not None:
            ps=datetime.fromtimestamp(float(period_start), tz=timezone.utc)
            elapsed=max(0,int((now()-ps).total_seconds()//60))
            st=_status_text(m).lower()
            if any(x in st for x in ["2nd","second half","2nd half"]):
                return min(130,45+elapsed)
            if any(x in st for x in ["extra","overtime"]):
                return min(130,90+elapsed)
            return min(130,elapsed)
    except Exception:
        pass

    # Some feeds expose the clock as text such as 67:21 or 67'.
    for path in (
        "clock.display","clock.displayValue","statusTime.display",
        "time.display","status.description","state.description"
    ):
        txt=str(g(m,path,default="") or "")
        mt=re.search(r"(?<!\d)(\d{1,3})(?=[:'’\s]|$)",txt)
        if mt:
            try:
                v=int(mt.group(1))
                if 0 <= v <= 130:
                    return v
            except:
                pass
    return None

def ss_status(m):
    return _status_text(m)

def ss_slug(m):
    return str(g(m,"slug","matchSlug","urlSlug","event.slug",default=""))

def ss_id(m):
    return str(g(m,"id","matchId","match_id","eventId","event_id",default=ss_slug(m)))

def event_source(m):
    return str(m.get("_source", "UNKNOWN")) if isinstance(m, dict) else "UNKNOWN"

def mark_source(matches, source):
    out=[]
    for m in matches or []:
        if isinstance(m, dict):
            x=dict(m)
            x["_source"]=source
            out.append(x)
    return out

def _timestamp_utc(m):
    x=g(
        m,
        "startTimestamp","start_timestamp","startTimeTimestamp",
        "time.startTimestamp","kickoffTimestamp","timestamp",
        default=None
    )
    try:
        x=float(x)
        if x > 10_000_000_000:
            x=x/1000
        return datetime.fromtimestamp(x,tz=timezone.utc)
    except:
        return None

def is_live_status(m):
    # First trust explicit boolean flags when available.
    for path in ("live","isLive","inProgress","isInProgress"):
        x=g(m,path,default=None)
        if x is True or str(x).lower() == "true":
            return True

    minute=ss_minute(m)
    if minute is not None and 0 <= minute <= 130:
        return True

    s=_status_text(m).lower().replace("_"," ").replace("-"," ")
    compact=re.sub(r"\s+"," ",s).strip()

    terminal=(
        "finished","full time","ended","after penalties","after extra time",
        "cancelled","canceled","postponed","abandoned","walkover","awarded"
    )
    scheduled=(
        "not started","notstarted","scheduled","fixture","upcoming"
    )
    if any(x in compact for x in terminal):
        return False
    if any(x in compact for x in scheduled):
        return False

    live_tokens=(
        "live","inprogress","in progress","1st half","first half",
        "2nd half","second half","halftime","half time","ht",
        "extra time","penalties","penalty shootout","break"
    )
    if any(x in compact for x in live_tokens):
        return True

    # Last-resort classification for feeds that omit a readable status:
    # a scored event very close to kickoff is almost certainly active.
    kick=_timestamp_utc(m)
    hs,aw=ss_score_parts(m)
    if kick is not None and hs is not None and aw is not None:
        age=(now()-kick).total_seconds()/60
        if -10 <= age <= 225:
            return True

    return False

# -----------------------------
# API calls
# -----------------------------
@st.cache_data(ttl=30, show_spinner=False)
def fetch_sofascore_live():
    st.session_state.request_count_sofascore += 1
    r=requests.get(
        SOFASCORE_LIVE,
        headers={
            "Accept":"application/json, text/plain, */*",
            "User-Agent":"Mozilla/5.0 (compatible; FootballValueScanner/3.4)"
        },
        timeout=20
    )
    r.raise_for_status()
    data=r.json()
    events=as_list(data,("events","data","results"))
    return mark_source(events,"SOFASCORE")

@st.cache_data(ttl=60, show_spinner=False)
def fetch_sofascore_detail(event_id):
    if not event_id:
        return {}
    st.session_state.request_count_sofascore += 1
    headers={
        "Accept":"application/json, text/plain, */*",
        "User-Agent":"Mozilla/5.0 (compatible; FootballValueScanner/3.4)"
    }
    detail={}
    try:
        r=requests.get(f"{SOFASCORE_EVENT.format(event_id=event_id)}/incidents",headers=headers,timeout=12)
        if r.status_code==200:
            detail.update(r.json() if isinstance(r.json(),dict) else {})
    except Exception:
        pass
    try:
        r=requests.get(f"{SOFASCORE_EVENT.format(event_id=event_id)}/statistics",headers=headers,timeout=12)
        if r.status_code==200:
            detail["statistics_payload"]=r.json()
    except Exception:
        pass
    return detail

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
    return mark_source(matches,"SPORTSCORE")

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
        # Important: 429 can be plan-blocked, not just rate-limit.
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
    # Generic event extraction; only uses fields if present.
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
st.title("⚽ Football Value Scanner V3.4")
st.caption("SofaScore live universe + SportScore fallback/enrichment + plan-aware Betano odds.")

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

# Live universe: SofaScore primary, SportScore fallback
feed_errors=[]
raw_matches=[]
live=[]
live_source="NONE"

try:
    raw_matches=fetch_sofascore_live()
    live=[m for m in raw_matches if is_live_status(m)]
    # The endpoint is live-only in normal operation; keep returned events even if a status label changes.
    if raw_matches and not live:
        live=raw_matches
    if live:
        live_source="SOFASCORE"
except Exception as ex:
    feed_errors.append(f"SofaScore: {ex}")

if not live:
    try:
        raw_ss=fetch_sportscore_live()
        ss_live=[m for m in raw_ss if is_live_status(m)]
        if ss_live:
            raw_matches=raw_ss
            live=ss_live
            live_source="SPORTSCORE"
    except Exception as ex:
        feed_errors.append(f"SportScore: {ex}")

if live:
    st.session_state.last_good_live=live
    st.session_state.last_good_live_at=now()
elif st.session_state.last_good_live:
    live=st.session_state.last_good_live
    live_source="CACHED"

if not live:
    st.error("Nicio sursă nu a furnizat un univers LIVE utilizabil în acest refresh.")
    if feed_errors:
        st.code("\n".join(feed_errors))
    if raw_matches:
        diag=[]
        for m in raw_matches[:20]:
            diag.append({
                "Source":event_source(m),
                "Match":ss_match_name(m),
                "Status":ss_status(m),
                "Minute":ss_minute(m),
                "Score":ss_score(m),
                "Start":str(_timestamp_utc(m) or "")
            })
        st.dataframe(pd.DataFrame(diag),use_container_width=True,hide_index=True)
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
        "Source":event_source(m),
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

# Optional detail calls: free SportScore, only shortlist
details={}
if load_details:
    for _,r in short.iterrows():
        mid=r["ID"]
        m=match_lookup.get(mid) if "match_lookup" in locals() else None
        # match_lookup is built immediately below in older versions; resolve directly if needed.
        if m is None:
            m=next((x for x in universe if ss_id(x)==mid),None)
        if m is None:
            continue
        if event_source(m)=="SOFASCORE":
            details[mid]=fetch_sofascore_detail(mid)
        else:
            slug=ss_slug(m)
            if slug:
                details[mid]=fetch_match_detail(slug)

# Our model candidates
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

# Plan-aware odds
odds_state="NOT_REQUESTED"
odds_note="Live odds disabled to protect free plan."
betano_rows=[]
if try_live_odds:
    # First request event universe from Odds-API.
    events_payload,state,err=odds_get("/events",{"sport":"football","status":"live","bookmaker":BOOKMAKER})
    odds_state=state
    if state=="OK":
        oe=as_list(events_payload,("data","events","results"))
        mapped=[]
        for mid in short_ids:
            sm=match_lookup.get(mid)
            match,sc=map_event(sm,oe) if sm else (None,0)
            if match is not None and sc>=0.72:
                mapped.append((mid,match,sc))
        event_ids=[str(g(x[1],"id","eventId","event_id",default="")) for x in mapped]
        event_ids=[x for x in event_ids if x][:10]

        if event_ids:
            payload,state2,err2=odds_get(
                "/odds/multi",
                {"eventIds":",".join(event_ids),"bookmakers":BOOKMAKER}
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

# Join model to odds when available
value=[]
if betano_rows and not modeldf.empty:
    # Need mapped Odds event IDs; easiest generic matching by market only is not enough across event IDs.
    # Keep this conservative: values are shown only when event identity can be verified later.
    pass

tabs=st.tabs(["🌍 LIVE","🎯 SHORTLIST","🧠 MODEL","💰 BETANO ACCESS","🧪 HEALTH"])

with tabs[0]:
    c1,c2,c3=st.columns(3)
    c1.metric("Live matches",len(universe))
    c2.metric("Live source",live_source)
    c3.metric("SportScore requests",st.session_state.request_count_sportscore)
    st.dataframe(udf,use_container_width=True,hide_index=True)
    st.markdown('Data from [SportScore](https://sportscore.com/)')

with tabs[1]:
    st.caption("Shortlistul este calculat fără cote.")
    st.dataframe(short,use_container_width=True,hide_index=True)

with tabs[2]:
    st.warning("Modelul V3.2 nu pretinde xG/SOT dacă sursa nu le oferă. Confidence rămâne LOW/MEDIUM.")
    if modeldf.empty:
        st.info("Nicio candidată model.")
    else:
        show=modeldf.copy()
        show["Model %"]=(show["P"]*100).round(1)
        show=show.drop(columns=["P"])
        st.dataframe(show,use_container_width=True,hide_index=True)
        qualified=show[show["Model %"]>=min_prob*100]
        st.caption(f"{len(qualified)} evenimente model ≥ {int(min_prob*100)}%, înainte de verificarea cotei.")

with tabs[3]:
    st.write("Status:",odds_state)
    st.info(odds_note)
    if not try_live_odds:
        st.write("Bifează «Încearcă live odds Betano» numai când vrei să testezi accesul. Nu consumăm request-uri Odds-API în mod implicit.")
    if betano_rows:
        odf=pd.DataFrame(betano_rows)
        odf=odf[odf["Odds"]>=min_odds]
        st.dataframe(odf,use_container_width=True,hide_index=True)
    st.caption("Pe planul free, dacă live odds sunt blocate, scannerul rămâne funcțional pe SportScore; nu afișează cote pre-match ca și cum ar fi live.")

with tabs[4]:
    st.write("SportScore live snapshot:",st.session_state.last_good_live_at)
    st.write("SportScore requests this session:",st.session_state.request_count_sportscore)
    st.write("Odds-API requests this session:",st.session_state.request_count_odds)
    st.write("Odds access state:",st.session_state.odds_access_state)
    st.write("Odds note:",st.session_state.odds_last_error)
    st.success("Kill-switch separat: lipsa live odds nu mai oprește universul live.")
    st.markdown('SportScore free API requires visible attribution: [Powered by SportScore](https://sportscore.com/).')

st.caption(
    "V3.2 architecture: SportScore = free live scores/universe; Odds-API = optional Betano layer. "
    "No live price is presented unless the live endpoint actually succeeds."
)
