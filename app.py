
import streamlit as st
import requests
import pandas as pd
import math, time, json
from datetime import datetime, timezone
from collections import defaultdict

st.set_page_config(page_title="Football Value Scanner V3", page_icon="⚽", layout="wide")

API_BASE = "https://api.odds-api.io/v3"
MAX_BATCH = 10

# -----------------------------
# Helpers / API
# -----------------------------
def api_key():
    try:
        return st.secrets["ODDS_API_KEY"]
    except Exception:
        return ""

def api_get(path, params):
    p = dict(params)
    p["apiKey"] = api_key()
    r = requests.get(f"{API_BASE}{path}", params=p, timeout=20)
    meta = {
        "status": r.status_code,
        "remaining": r.headers.get("x-ratelimit-remaining"),
        "reset": r.headers.get("x-ratelimit-reset"),
        "limit": r.headers.get("x-ratelimit-limit"),
    }
    if r.status_code == 429:
        return None, meta, "RATE_LIMIT"
    r.raise_for_status()
    return r.json(), meta, None

def as_list(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for k in ("data","events","results","odds"):
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

def eid(e):
    return str(g(e, "id","eventId","event_id", default=""))

def name(e):
    h = g(e,"home","homeTeam.name","home_team","participants.home.name")
    a = g(e,"away","awayTeam.name","away_team","participants.away.name")
    if isinstance(h,dict): h = h.get("name")
    if isinstance(a,dict): a = a.get("name")
    if h and a: return f"{h} – {a}"
    return str(g(e,"name","eventName",default="Meci"))

def league(e):
    x = g(e,"league.name","competition.name","league","competition",default="")
    if isinstance(x,dict): return str(x.get("name",""))
    return str(x)

def score_parts(e):
    hs = g(e,"score.home","homeScore","scores.home","live.home",default=None)
    aw = g(e,"score.away","awayScore","scores.away","live.away",default=None)
    try: hs = int(hs)
    except: hs = None
    try: aw = int(aw)
    except: aw = None
    return hs,aw

def score_text(e):
    hs,aw = score_parts(e)
    return f"{hs}-{aw}" if hs is not None and aw is not None else ""

def minute(e):
    m = g(e,"minute","live.minute","clock.minute","time.minute",default=None)
    try: return int(float(m))
    except: return None

def status(e):
    return str(g(e,"status","state","live.status",default=""))

def ts_value(e):
    x = g(e,"updatedAt","updated_at","lastUpdated","timestamp","live.updatedAt",default=None)
    if not x: return None
    try:
        if isinstance(x,(int,float)):
            return datetime.fromtimestamp(float(x)/1000 if float(x)>1e12 else float(x), tz=timezone.utc)
        s = str(x).replace("Z","+00:00")
        return datetime.fromisoformat(s)
    except:
        return None

def stat(e, *names):
    for n in names:
        v = g(e,
              f"stats.{n}", f"statistics.{n}", f"liveStats.{n}",
              f"stats.home.{n}", f"statistics.home.{n}", default=None)
        if v is not None:
            try: return float(v)
            except: pass
    return None

def pair_stat(e, key_aliases):
    # flexible pair parsing
    for key in key_aliases:
        for base in ("stats","statistics","liveStats"):
            obj = g(e,f"{base}.{key}",default=None)
            if isinstance(obj,dict):
                hv = obj.get("home")
                av = obj.get("away")
                try: hv=float(hv)
                except: hv=None
                try: av=float(av)
                except: av=None
                if hv is not None or av is not None:
                    return hv,av
            hv = g(e,f"{base}.home.{key}",default=None)
            av = g(e,f"{base}.away.{key}",default=None)
            if hv is not None or av is not None:
                try: hv=float(hv)
                except: hv=None
                try: av=float(av)
                except: av=None
                return hv,av
    return None,None

def clamp(x,a,b): return max(a,min(b,x))
def fair_odds(p): return round(1/p,2) if p and p>0 else None
def implied_prob(o): return 1/o if o and o>0 else None

def poisson_p_ge1(lmbda):
    return 1 - math.exp(-max(0,lmbda))

def poisson_p_total_at_least(current_goals, target_total, lambda_remaining):
    need = max(0, target_total-current_goals)
    if need <= 0: return 1.0
    # P(X >= need)
    cdf=0.0
    for k in range(need):
        cdf += math.exp(-lambda_remaining)*(lambda_remaining**k)/math.factorial(k)
    return clamp(1-cdf,0,1)

def poisson_p_total_under(current_goals, line_total, lambda_remaining):
    # under N.5 => total <= N
    max_total = math.floor(line_total)
    max_new = max_total-current_goals
    if max_new < 0: return 0.0
    cdf=0.0
    for k in range(max_new+1):
        cdf += math.exp(-lambda_remaining)*(lambda_remaining**k)/math.factorial(k)
    return clamp(cdf,0,1)

# -----------------------------
# Data quality / regimes
# -----------------------------
def freshness_seconds(e):
    t = ts_value(e)
    if not t: return None
    now = datetime.now(timezone.utc)
    try: return max(0,(now-t).total_seconds())
    except: return None

def freshness_label(sec):
    if sec is None: return "UNKNOWN"
    if sec <= 90: return "FRESH"
    if sec <= 240: return "AGING"
    return "STALE"

def league_reliability(lg):
    s = lg.lower()
    high = ["premier","champions league","europa league","conference league","serie a","la liga","bundesliga","ligue 1","superliga","eredivisie","primeira"]
    low = ["u19","u20","u21","reserve","friendly","regional","youth"]
    if any(x in s for x in high): return 1.00
    if any(x in s for x in low): return 0.78
    return 0.90

def regime(e):
    m = minute(e) or 0
    hs,aw = score_parts(e)
    rc_h,rc_a = pair_stat(e,["redCards","reds","red_cards"])
    if (rc_h or 0)+(rc_a or 0)>0: return "RED_CARD"
    if hs is not None and aw is not None:
        if m>=70 and hs==aw: return "LATE_LEVEL"
        if m>=60 and abs(hs-aw)>=2: return "CONTROL"
        if m<=65 and hs!=aw: return "TRAILING_TEAM_PUSH"
    return "NORMAL"

def evidence(e):
    xgh,xga = pair_stat(e,["xg","expectedGoals","expected_goals"])
    sh,sa = pair_stat(e,["shots","totalShots","total_shots"])
    soh,soa = pair_stat(e,["shotsOnTarget","shots_on_target","sot"])
    bch,bca = pair_stat(e,["bigChances","big_chances"])
    boxh,boxa = pair_stat(e,["boxTouches","touchesInBox","touches_in_box"])
    rh,ra = pair_stat(e,["redCards","reds","red_cards"])
    return {
        "xg_h":xgh,"xg_a":xga,"shots_h":sh,"shots_a":sa,"sot_h":soh,"sot_a":soa,
        "bc_h":bch,"bc_a":bca,"box_h":boxh,"box_a":boxa,"red_h":rh,"red_a":ra
    }

def data_confidence(e):
    ev = evidence(e)
    present = sum(v is not None for v in ev.values())
    m = minute(e)
    hs,aw = score_parts(e)
    score_ok = hs is not None and aw is not None and m is not None
    fresh = freshness_label(freshness_seconds(e))
    rel = league_reliability(league(e))
    score = (2 if score_ok else 0) + present/4 + (1 if fresh=="FRESH" else 0) + rel
    if score >= 6: return "HIGH"
    if score >= 4: return "MEDIUM"
    return "LOW"

# -----------------------------
# Live model
# -----------------------------
def expected_goals_remaining(e):
    m = minute(e)
    hs,aw = score_parts(e)
    if m is None or hs is None or aw is None: return None
    remaining = max(0,95-m)
    if remaining <= 0: return 0.0

    ev = evidence(e)
    xg_total = None
    if ev["xg_h"] is not None or ev["xg_a"] is not None:
        xg_total = (ev["xg_h"] or 0)+(ev["xg_a"] or 0)

    # baseline ~2.65 goals/90, blended with observed xG pace
    baseline_rate = 2.65/90
    if xg_total is not None and m >= 12:
        observed_rate = xg_total/max(m,12)
        rate = 0.55*baseline_rate + 0.45*observed_rate
    else:
        # use shot pace if xG absent
        sh = (ev["shots_h"] or 0)+(ev["shots_a"] or 0) if ev["shots_h"] is not None or ev["shots_a"] is not None else None
        if sh is not None and m>=15:
            shot_goal_rate = (sh/max(m,15))*0.10
            rate = 0.7*baseline_rate + 0.3*shot_goal_rate
        else:
            rate = baseline_rate

    reg = regime(e)
    if reg=="TRAILING_TEAM_PUSH": rate *= 1.12
    if reg=="LATE_LEVEL": rate *= 1.10
    if reg=="CONTROL": rate *= 0.88
    if reg=="RED_CARD": rate *= 1.08

    rel = league_reliability(league(e))
    return clamp(rate*remaining*rel,0.05,4.5)

def team_share(e, home=True):
    ev=evidence(e)
    keys=[("xg_h","xg_a",3.0),("sot_h","sot_a",2.0),("shots_h","shots_a",1.0),("box_h","box_a",1.0),("bc_h","bc_a",2.5)]
    num=den=0.0
    for hkey,akey,w in keys:
        hv,av=ev[hkey],ev[akey]
        if hv is not None or av is not None:
            hv=hv or 0; av=av or 0
            total=hv+av
            if total>0:
                share=hv/total if home else av/total
                num += share*w
                den += w
    if den==0: return 0.5
    return clamp(num/den,0.15,0.85)

def model_markets(e):
    m=minute(e); hs,aw=score_parts(e)
    if m is None or hs is None or aw is None: return []
    lrem=expected_goals_remaining(e)
    if lrem is None: return []
    total=hs+aw
    home_share=team_share(e,True)
    away_share=1-home_share
    conf=data_confidence(e)
    rel=league_reliability(league(e))
    uncertainty = {"HIGH":0.035,"MEDIUM":0.065,"LOW":0.11}[conf]

    out=[]

    def add(market, selection, p, thesis, timing_ok=True):
        p=clamp(p,0.02,0.98)
        lo=clamp(p-uncertainty,0,1); hi=clamp(p+uncertainty,0,1)
        out.append({
            "Market":market,"Selection":selection,"ModelP":p,"P_low":lo,"P_high":hi,
            "Fair":fair_odds(p),"Thesis":thesis,"TimingOK":timing_ok
        })

    # Goal markets
    p_any = poisson_p_ge1(lrem)
    add("Goals","Over 0.5 remaining",p_any,"Expected goals remaining + live pace",m<=82)

    p_home = poisson_p_ge1(lrem*home_share)
    p_away = poisson_p_ge1(lrem*away_share)
    add("Team to Score","Home team to score",p_home,"Team share of live attacking evidence",m<=78)
    add("Team to Score","Away team to score",p_away,"Team share of live attacking evidence",m<=78)

    p_o15 = poisson_p_total_at_least(total,2,lrem)
    p_o25 = poisson_p_total_at_least(total,3,lrem)
    p_u35 = poisson_p_total_under(total,3.5,lrem)
    add("Totals","Over 1.5",p_o15,"Poisson from xGR",m<=75)
    add("Totals","Over 2.5",p_o25,"Poisson from xGR",m<=68)
    add("Totals","Under 3.5",p_u35,"Poisson from xGR + game state",m>=20)

    # crude non-loss probabilities from current lead + remaining goals
    diff=hs-aw
    if diff>=1:
        p_home_nonloss=clamp(0.72 + 0.005*m + 0.08*min(diff,2) - 0.12*away_share,0.45,0.97)
        add("Double Chance","Home or Draw",p_home_nonloss,"Lead + time remaining + opponent pressure",True)
    elif diff<=-1:
        p_away_nonloss=clamp(0.72 + 0.005*m + 0.08*min(-diff,2) - 0.12*home_share,0.45,0.97)
        add("Double Chance","Away or Draw",p_away_nonloss,"Lead + time remaining + opponent pressure",True)

    return out

# -----------------------------
# Odds parsing / value engine
# -----------------------------
def chunks(xs,n):
    xs=list(xs)
    for i in range(0,len(xs),n):
        yield xs[i:i+n]

def flatten_odds(payload, event_lookup):
    rows=[]
    items=as_list(payload)
    if isinstance(payload,dict) and not items: items=[payload]
    for item in items:
        i=str(g(item,"eventId","event_id","id",default=""))
        ev=event_lookup.get(i,{})
        books=g(item,"bookmakers","sportsbooks",default=None)
        if isinstance(books,dict):
            books=[{"name":k,**(v if isinstance(v,dict) else {"markets":v})} for k,v in books.items()]
        if not isinstance(books,list): books=[item]
        for b in books:
            bn=str(g(b,"name","bookmaker","sportsbook",default=""))
            if "betano" not in bn.lower(): continue
            markets=g(b,"markets","odds",default=[])
            if isinstance(markets,dict): markets=[{"name":k,"outcomes":v} for k,v in markets.items()]
            if not isinstance(markets,list): continue
            for mk in markets:
                mn=str(g(mk,"name","key","market",default=""))
                outs=g(mk,"outcomes","selections","prices",default=[])
                if isinstance(outs,dict): outs=[{"name":k,"price":v} for k,v in outs.items()]
                if not isinstance(outs,list): continue
                for o in outs:
                    try: price=float(g(o,"price","odds","decimal","value"))
                    except: continue
                    rows.append({
                        "EventID":i,"Competition":league(ev),"Match":name(ev),
                        "Score":score_text(ev),"Minute":minute(ev),
                        "Bookmaker":bn,"MarketRaw":mn,
                        "SelectionRaw":str(g(o,"name","label","selection",default="")),
                        "Line":g(o,"point","line","handicap",default=""),"Odds":price
                    })
    return rows

def normalize_text(x): return str(x).lower().replace(" ","").replace("_","")

def match_model_to_odds(model_row, odd_row):
    m=normalize_text(model_row["Market"]+" "+model_row["Selection"])
    o=normalize_text(odd_row["MarketRaw"]+" "+odd_row["SelectionRaw"]+" "+str(odd_row["Line"]))
    # flexible matching
    patterns = [
        ("over0.5remaining", ["over0.5","over05"]),
        ("hometeamtoscore", ["hometeam","homeover0.5","homeover05","hometoscore"]),
        ("awayteamtoscore", ["awayteam","awayover0.5","awayover05","awaytoscore"]),
        ("over1.5", ["over1.5","over15"]),
        ("over2.5", ["over2.5","over25"]),
        ("under3.5", ["under3.5","under35"]),
        ("homeordraw", ["1x","homeordraw","homedraw"]),
        ("awayordraw", ["x2","awayordraw","awaydraw"]),
    ]
    for key, pats in patterns:
        if key in m:
            return any(p in o for p in pats)
    return False

def dynamic_edge_min(odds):
    if odds < 1.50: return 0.075
    if odds < 1.80: return 0.060
    if odds < 2.20: return 0.050
    return 0.045

def sanity_check(model_p, odds):
    imp=implied_prob(odds)
    if imp is None: return "NO_PRICE"
    gap=model_p-imp
    if gap > 0.25: return "VERIFY"
    return "OK"

def status_level(p_low, odds, edge, conf, timing_ok, sanity, fresh):
    if fresh=="STALE": return "NO DATA"
    if sanity=="VERIFY": return "VERIFY"
    if conf=="LOW": return "WATCH"
    if not timing_ok: return "WATCH"
    if odds < 1.40: return "WATCH"
    if p_low >= 0.70 and edge >= dynamic_edge_min(odds) and conf=="HIGH": return "BET"
    if p_low >= 0.67 and edge >= 0.04 and conf in ("HIGH","MEDIUM"): return "READY"
    if p_low >= 0.62 and edge >= 0.02: return "WATCH"
    return "PASS"

# -----------------------------
# Streamlit state
# -----------------------------
if "snapshots" not in st.session_state:
    st.session_state.snapshots=[]
if "last_event_state" not in st.session_state:
    st.session_state.last_event_state={}
if "price_history" not in st.session_state:
    st.session_state.price_history=defaultdict(list)

st.title("⚽ Football Value Scanner V3")
st.caption("Universe → shortlist → Betano odds → model probability → fair odds → edge → WATCH / READY / BET")

with st.sidebar:
    st.header("Reguli")
    min_odds=st.number_input("Cotă minimă",1.01,10.0,1.40,0.05)
    min_prob=st.slider("Probabilitate minimă (%)",50,90,70,1)/100
    shortlist_n=st.slider("Shortlist maxim",3,20,8,1)
    max_events=st.slider("Max. evenimente odds",10,100,60,10)
    exclude_friendlies=st.checkbox("Exclude amicale",True)
    require_medium=st.checkbox("Exclude confidence LOW din recomandări",True)
    if st.button("🔄 Refresh acum",use_container_width=True):
        st.cache_data.clear()
        st.rerun()

if not api_key():
    st.error("Lipsește ODDS_API_KEY din Streamlit Secrets.")
    st.stop()

@st.cache_data(ttl=60,show_spinner=False)
def load_events():
    attempts=[
        {"sport":"football","status":"live","bookmaker":"Betano"},
        {"sport":"soccer","status":"live","bookmaker":"Betano"},
        {"sport":"football","status":"live"},
        {"sport":"soccer","status":"live"},
    ]
    last=None
    for p in attempts:
        data,meta,err=api_get("/events",p)
        last=meta
        if err=="RATE_LIMIT": return [],last,"RATE_LIMIT"
        evs=as_list(data)
        if evs: return evs,last,None
    return [],last,"NO_EVENTS"

events,events_meta,events_err=load_events()
if events_err=="RATE_LIMIT":
    st.error("KILL SWITCH: API rate-limited. Nu emit recomandări pe date incomplete.")
    st.stop()
if not events:
    st.warning("Nu am primit meciuri live.")
    st.stop()

# Filter universe
universe=[]
for e in events:
    lg=league(e)
    if exclude_friendlies and "friendly" in lg.lower():
        continue
    m=minute(e)
    if m is not None and m>88:
        continue
    universe.append(e)

# Shock detection
shocks=set()
for e in universe:
    i=eid(e)
    state=(score_text(e), evidence(e).get("red_h"), evidence(e).get("red_a"))
    prev=st.session_state.last_event_state.get(i)
    if prev is not None and prev!=state:
        shocks.add(i)
    st.session_state.last_event_state[i]=state

# Build signal shortlist
signal_rows=[]
models_by_event={}
for e in universe:
    models=model_markets(e)
    models_by_event[eid(e)]=models
    conf=data_confidence(e)
    fresh=freshness_label(freshness_seconds(e))
    ev=evidence(e)
    xgr=expected_goals_remaining(e)
    best=max([m["ModelP"] for m in models],default=0)
    signal=best
    if conf=="HIGH": signal+=0.04
    elif conf=="MEDIUM": signal+=0.01
    if fresh=="FRESH": signal+=0.02
    if eid(e) in shocks: signal-=0.08
    signal*=league_reliability(league(e))
    signal_rows.append({
        "EventID":eid(e),"Competition":league(e),"Match":name(e),"Score":score_text(e),
        "Minute":minute(e),"Confidence":conf,"Freshness":fresh,"Regime":regime(e),
        "xGR":round(xgr,2) if xgr is not None else None,"Signal":round(signal,3),
        "Shock": "YES" if eid(e) in shocks else ""
    })

sigdf=pd.DataFrame(signal_rows).sort_values("Signal",ascending=False)
short_ids=sigdf.head(shortlist_n)["EventID"].tolist()

@st.cache_data(ttl=45,show_spinner=False)
def load_odds(ids):
    payloads=[]; metas=[]
    ids=list(ids)[:max_events]
    for batch in chunks(ids,MAX_BATCH):
        got=False
        for params in (
            {"eventIds":",".join(batch),"bookmakers":"Betano"},
            {"eventIds":",".join(batch),"bookmaker":"Betano"},
        ):
            data,meta,err=api_get("/odds/multi",params)
            metas.append(meta)
            if err=="RATE_LIMIT":
                return payloads,metas,"RATE_LIMIT"
            if data:
                vals=as_list(data)
                payloads.extend(vals if vals else ([data] if isinstance(data,dict) else []))
                got=True
                break
        if got: time.sleep(0.15)
    return payloads,metas,None

with st.spinner("Citesc cotele Betano doar pentru shortlist..."):
    odd_payloads,odd_metas,odd_err=load_odds(tuple(short_ids))

lookup={eid(e):e for e in universe}
odds_rows=flatten_odds(odd_payloads,lookup)

# Build value picks
value=[]
for i in short_ids:
    e=lookup.get(i)
    if not e: continue
    conf=data_confidence(e)
    fresh=freshness_label(freshness_seconds(e))
    if require_medium and conf=="LOW": continue
    for mr in models_by_event.get(i,[]):
        matches=[o for o in odds_rows if o["EventID"]==i and match_model_to_odds(mr,o)]
        for o in matches:
            odds=o["Odds"]
            p=mr["ModelP"]
            imp=implied_prob(odds)
            edge=p-imp if imp else None
            sanity=sanity_check(p,odds)
            level=status_level(mr["P_low"],odds,edge or -1,conf,mr["TimingOK"],sanity,fresh)
            if i in shocks and level in ("BET","READY"):
                level="RECALCULATING"
            key=(i,mr["Market"],mr["Selection"])
            st.session_state.price_history[key].append((datetime.now(timezone.utc).isoformat(),odds))
            hist=st.session_state.price_history[key][-5:]
            move=None
            if len(hist)>=2:
                move=round(hist[-1][1]-hist[0][1],2)

            threshold=max(min_odds, round(1/max(mr["P_low"]-dynamic_edge_min(max(odds,1.01)),0.01),2))
            value.append({
                "Level":level,"Competition":league(e),"Match":name(e),"Score":score_text(e),"Minute":minute(e),
                "Market":mr["Market"],"Selection":mr["Selection"],
                "Model %":round(p*100,1),"Range":f"{round(mr['P_low']*100)}–{round(mr['P_high']*100)}%",
                "Fair":mr["Fair"],"Betano":round(odds,2),"Edge pp":round((edge or 0)*100,1),
                "Confidence":conf,"Freshness":fresh,"Regime":regime(e),
                "Price Δ":move,"Min acceptable":threshold,
                "Thesis":mr["Thesis"],"Risk":("Recent shock / recalc" if i in shocks else "Model/data uncertainty")
            })

vdf=pd.DataFrame(value)
if not vdf.empty:
    rank={"BET":0,"READY":1,"WATCH":2,"RECALCULATING":3,"VERIFY":4,"NO DATA":5,"PASS":6}
    vdf["rank"]=vdf["Level"].map(rank).fillna(9)
    vdf=vdf.sort_values(["rank","Edge pp"],ascending=[True,False]).drop(columns=["rank"])

# snapshot logging
now_iso=datetime.now(timezone.utc).isoformat()
for row in value:
    if row["Level"] in ("BET","READY","WATCH"):
        snap=dict(row); snap["Timestamp"]=now_iso
        st.session_state.snapshots.append(snap)
st.session_state.snapshots=st.session_state.snapshots[-500:]

# -----------------------------
# UI Tabs
# -----------------------------
tabs=st.tabs(["🌍 LIVE UNIVERSE","🎯 SHORTLIST","💰 VALUE PICKS","📈 MODEL HEALTH","🧪 DIAGNOSTIC"])

with tabs[0]:
    st.metric("Live universe",len(universe))
    st.dataframe(sigdf,use_container_width=True,hide_index=True)

with tabs[1]:
    short=sigdf.head(shortlist_n).copy()
    st.write(f"Top {len(short)} după semnal statistic înainte de cote.")
    st.dataframe(short,use_container_width=True,hide_index=True)
    st.caption("Signal = probabilități model + confidence + freshness + reliability; nu folosește cota pentru a decide candidatul.")

with tabs[2]:
    if odd_err=="RATE_LIMIT":
        st.warning("KILL SWITCH: rate limit în timpul odds. Rezultatele pot fi parțiale; nu trata BET ca valid până la refresh complet.")
    if vdf.empty:
        st.info("Nicio piață Betano nu a putut fi mapată la modelele curente.")
    else:
        # Hard user filters
        display=vdf[(vdf["Betano"]>=min_odds) & (vdf["Model %"]>=min_prob*100)]
        if display.empty:
            st.info("0 selecții trec pragurile tale actuale.")
        else:
            st.dataframe(display,use_container_width=True,hide_index=True,
                         column_config={"Betano":st.column_config.NumberColumn(format="%.2f"),
                                        "Fair":st.column_config.NumberColumn(format="%.2f")})
            bets=display[display["Level"]=="BET"]
            st.subheader("BET")
            if bets.empty:
                st.write("Niciun BET valid acum.")
            else:
                for _,r in bets.head(5).iterrows():
                    st.success(f"{r['Match']} | {r['Score']} {r['Minute']}' | {r['Selection']} @ {r['Betano']} | Model {r['Model %']}% | Edge {r['Edge pp']} pp | {r['Confidence']}")

with tabs[3]:
    snaps=pd.DataFrame(st.session_state.snapshots)
    c1,c2,c3,c4=st.columns(4)
    c1.metric("Snapshots",len(snaps))
    c2.metric("BET",0 if snaps.empty else int((snaps["Level"]=="BET").sum()))
    c3.metric("READY",0 if snaps.empty else int((snaps["Level"]=="READY").sum()))
    c4.metric("WATCH",0 if snaps.empty else int((snaps["Level"]=="WATCH").sum()))
    st.write("V3 loghează snapshot-uri în sesiune. Backtest/calibrare reală devin valide doar după ce avem rezultate istorice.")
    if not snaps.empty:
        st.dataframe(snaps.tail(100),use_container_width=True,hide_index=True)
        st.download_button("Descarcă snapshots CSV",snaps.to_csv(index=False).encode(),"v3_snapshots.csv","text/csv")
    st.info("CLV tracking și calibrarea sunt active ca infrastructură, dar nu sunt declarate 'validate' până nu acumulăm suficiente observații și rezultate.")

with tabs[4]:
    st.write("Event API:",events_meta)
    st.write("Odds API last:",odd_metas[-1] if odd_metas else {})
    st.write("Shortlist IDs:",short_ids)
    st.write("Odds rows parsed:",len(odds_rows))
    st.write("Shock events:",list(shocks))
    if st.checkbox("Arată primul eveniment brut"):
        st.json(universe[0] if universe else {})
    if st.checkbox("Arată primul payload odds brut"):
        st.json(odd_payloads[0] if odd_payloads else {})

st.caption("V3: market-specific models, xGR, pace normalization, confidence ranges, freshness, league reliability, regime detection, shock detection, dynamic edge, sanity check, WATCH/READY/BET, price tracking, cooldown-ready history, snapshots & model-health infrastructure.")
