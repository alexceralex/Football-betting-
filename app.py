import streamlit as st
import requests
import pandas as pd
from datetime import datetime, timezone

st.set_page_config(page_title="Betano Live Scanner", page_icon="⚽", layout="wide")
API_BASE = "https://api.odds-api.io/v3"

def key():
    try:
        return st.secrets["ODDS_API_KEY"]
    except Exception:
        return ""

def api_get(path, params):
    p = dict(params)
    p["apiKey"] = key()
    r = requests.get(API_BASE + path, params=p, timeout=20)
    r.raise_for_status()
    return r.json()

def items(x):
    if isinstance(x, list): return x
    if isinstance(x, dict):
        for k in ("data","events","results","odds"):
            if isinstance(x.get(k), list): return x[k]
    return []

def pick(d, *keys, default=""):
    for path in keys:
        cur=d
        ok=True
        for k in path.split("."):
            if isinstance(cur,dict) and k in cur: cur=cur[k]
            else: ok=False; break
        if ok and cur is not None: return cur
    return default

def eid(e): return str(pick(e,"id","eventId","event_id"))
def match(e):
    h=pick(e,"home","homeTeam.name","home_team")
    a=pick(e,"away","awayTeam.name","away_team")
    if isinstance(h,dict): h=h.get("name","")
    if isinstance(a,dict): a=a.get("name","")
    return f"{h} – {a}" if h and a else str(pick(e,"name","eventName",default="Meci"))
def league(e): return str(pick(e,"league.name","competition.name","league","competition"))
def score(e):
    h=pick(e,"score.home","homeScore","scores.home",default=None)
    a=pick(e,"score.away","awayScore","scores.away",default=None)
    return f"{h}-{a}" if h is not None and a is not None else ""
def minute(e): return str(pick(e,"minute","live.minute","clock.minute"))

st.title("⚽ Betano Live Scanner")
st.caption("Fotbal LIVE → Betano odds → prag minim de cotă. Cheia API este citită numai din Streamlit Secrets.")

with st.sidebar:
    min_odds=st.number_input("Cotă minimă",1.01,10.0,1.40,0.05)
    bookmaker=st.text_input("Bookmaker","Betano")
    if st.button("🔄 Refresh",use_container_width=True):
        st.cache_data.clear(); st.rerun()

if not key():
    st.error('Adaugă în Streamlit Cloud → App settings → Secrets: ODDS_API_KEY = "cheia_ta"')
    st.stop()

@st.cache_data(ttl=45)
def live_events():
    errors=[]
    for sport in ("football","soccer"):
        try:
            x=api_get("/events",{"sport":sport,"status":"live"})
            y=items(x)
            if y: return y,""
        except Exception as ex: errors.append(str(ex))
    return []," | ".join(errors)

events,err=live_events()
if not events:
    st.warning("Nu am primit încă lista live în schema anticipată.")
    if err: st.code(err)
    st.stop()

lookup={eid(e):e for e in events if eid(e)}
c1,c2,c3=st.columns(3)
c1.metric("Meciuri live",len(events)); c2.metric("Prag",f"{min_odds:.2f}"); c3.metric("Bookmaker",bookmaker)

live_df=pd.DataFrame([{
    "Competiție":league(e),"Meci":match(e),"Scor":score(e),
    "Minut":minute(e),"Status":str(pick(e,"status","state")),"Event ID":eid(e)
} for e in events])
st.subheader("Toate meciurile LIVE găsite")
st.dataframe(live_df,use_container_width=True,hide_index=True)

@st.cache_data(ttl=30)
def get_odds(ids,bm):
    errors=[]
    ids=list(ids)
    for path,params in [
        ("/odds/multi",{"eventIds":",".join(ids),"bookmakers":bm}),
        ("/odds",{"eventIds":",".join(ids),"bookmakers":bm}),
    ]:
        try:
            x=api_get(path,params)
            if x: return x,""
        except Exception as ex: errors.append(f"{path}: {ex}")
    combined=[]
    for i in ids[:80]:
        for param_name in ("bookmakers","bookmaker"):
            try:
                x=api_get("/odds",{"eventId":i,param_name:bm})
                y=items(x)
                combined.extend(y if y else ([x] if isinstance(x,dict) else []))
                break
            except Exception as ex: errors.append(f"{i}: {ex}")
    return combined,(" | ".join(errors[-5:]) if not combined else "")

payload,odds_err=get_odds(tuple(lookup.keys()),bookmaker)

def flatten(payload):
    out=[]
    base=items(payload)
    if isinstance(payload,dict) and not base: base=[payload]
    for obj in base:
        i=str(pick(obj,"eventId","event_id","id"))
        ev=lookup.get(i,{})
        books=pick(obj,"bookmakers","sportsbooks",default=None)
        if isinstance(books,dict):
            books=[{"name":k,**(v if isinstance(v,dict) else {"markets":v})} for k,v in books.items()]
        if not isinstance(books,list): books=[obj]
        for b in books:
            bn=str(pick(b,"name","bookmaker","sportsbook",default=bookmaker))
            if bookmaker.lower() not in bn.lower(): continue
            markets=pick(b,"markets","odds",default=[])
            if isinstance(markets,dict): markets=[{"name":k,"outcomes":v} for k,v in markets.items()]
            if not isinstance(markets,list): continue
            for m in markets:
                mn=str(pick(m,"name","key","market"))
                outs=pick(m,"outcomes","selections","prices",default=[])
                if isinstance(outs,dict): outs=[{"name":k,"price":v} for k,v in outs.items()]
                if not isinstance(outs,list): continue
                for o in outs:
                    try: price=float(pick(o,"price","odds","decimal","value"))
                    except: continue
                    if price < min_odds: continue
                    out.append({"Competiție":league(ev),"Meci":match(ev),"Scor":score(ev),
                        "Minut":minute(ev),"Piață":mn,"Selecție":str(pick(o,"name","label","selection")),
                        "Linie":pick(o,"point","line","handicap"),"Cotă":price,"Bookmaker":bn})
    return out

rows=flatten(payload)
st.subheader(f"Cote {bookmaker} ≥ {min_odds:.2f}")
if rows:
    df=pd.DataFrame(rows).sort_values(["Cotă","Meci"])
    st.dataframe(df,use_container_width=True,hide_index=True,
                 column_config={"Cotă":st.column_config.NumberColumn(format="%.2f")})
    st.download_button("Descarcă CSV",df.to_csv(index=False).encode(),"betano_live_odds.csv","text/csv")
else:
    st.info("Nicio selecție găsită peste prag în răspunsul curent.")
    if odds_err: st.code(odds_err)

with st.expander("Diagnostic"):
    st.write("UTC:",datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    if st.checkbox("Arată primul eveniment brut"): st.json(events[0])
