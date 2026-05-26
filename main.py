from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
import httpx, os, re, json, urllib.parse, time, asyncio
from typing import Optional

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GROQ_KEY      = os.environ.get("GROQ_API_KEY")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY")
FACEIT_KEY    = os.environ.get("FACEIT_API_KEY")
STEAM_OPENID  = "https://steamcommunity.com/openid/login"
FACEIT_BASE   = "https://open.faceit.com/data/v4"

leaderboard      = []
analysis_history = {}

def fh():
    return {"Authorization": f"Bearer {FACEIT_KEY}"} if FACEIT_KEY else {}

# ── Models ───────────────────────────────────────────────────────────────────
class Stats(BaseModel):
    kd: str; winrate: str; hltv: str; hs: str; adr: str
    clutch1v1: str; entrySuccess: str; rank: str; matches: str
    steamid: Optional[str] = ""
    maps: Optional[list] = []

class LBEntry(BaseModel):
    steamid: str; username: str; avatar: str
    stats: dict; level: str; overall: str

# ── Steam helpers ─────────────────────────────────────────────────────────────
async def steam_profile(steam_id, client):
    if not STEAM_API_KEY:
        return {"username":"Unknown","avatar":"","created":None,"steam_level":None}
    sr = await client.get(f"https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
        params={"key":STEAM_API_KEY,"steamids":steam_id})
    players = sr.json().get("response",{}).get("players",[])
    if not players:
        return {"username":"Unknown","avatar":"","created":None,"steam_level":None}
    p = players[0]
    lr = await client.get("https://api.steampowered.com/IPlayerService/GetSteamLevel/v1/",
        params={"key":STEAM_API_KEY,"steamid":steam_id})
    return {
        "username": p.get("personaname","Unknown"),
        "avatar": p.get("avatarfull",""),
        "created": p.get("timecreated"),
        "steam_level": lr.json().get("response",{}).get("player_level"),
        "profile_url": p.get("profileurl",""),
        "country": p.get("loccountrycode",""),
    }

async def steam_cs2(steam_id, client):
    if not STEAM_API_KEY:
        return {}
    gr = await client.get("https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v0002/",
        params={"key":STEAM_API_KEY,"steamid":steam_id,"appid":730})
    raw = {s["name"]:s["value"] for s in gr.json().get("playerstats",{}).get("stats",[])}
    k,d = raw.get("total_kills",0), raw.get("total_deaths",1)
    w,m = raw.get("total_wins",0), raw.get("total_matches_played",1)
    hk  = raw.get("total_kills_headshot",0)
    return {
        "kd": f"{k/max(d,1):.2f}", "winrate": f"{w/max(m,1)*100:.0f}",
        "hs": f"{hk/max(k,1)*100:.0f}", "matches": str(m),
        "kills": str(k), "deaths": str(d), "wins": str(w),
        "mvps": str(raw.get("total_mvps",0)),
    }

# ── FACEIT helpers ────────────────────────────────────────────────────────────
async def faceit_player(client, steam_id=None, nickname=None):
    if not FACEIT_KEY:
        return None
    try:
        if steam_id:
            r = await client.get(f"{FACEIT_BASE}/players",
                params={"game":"cs2","game_player_id":steam_id}, headers=fh())
        else:
            r = await client.get(f"{FACEIT_BASE}/players",
                params={"nickname":nickname}, headers=fh())
        if r.status_code != 200:
            return None
        return r.json()
    except:
        return None

async def faceit_match_stats(client, match_id, faceit_id):
    try:
        r = await client.get(f"{FACEIT_BASE}/matches/{match_id}/stats", headers=fh())
        if r.status_code != 200:
            return None
        rounds = r.json().get("rounds",[])
        if not rounds:
            return None
        rd = rounds[0]
        rs = rd.get("round_stats",{})
        for team in rd.get("teams",[]):
            for pl in team.get("players",[]):
                if pl.get("player_id") == faceit_id:
                    ps = pl.get("player_stats",{})
                    return {
                        "map": rs.get("Map","").replace("de_","").title(),
                        "score": rs.get("Score",""),
                        "result": ps.get("Result","0"),
                        "kills": ps.get("Kills","0"),
                        "deaths": ps.get("Deaths","0"),
                        "assists": ps.get("Assists","0"),
                        "kd": ps.get("K/D Ratio","0"),
                        "kr": ps.get("K/R Ratio","0"),
                        "hs": ps.get("Headshots %","0"),
                        "adr": ps.get("ADR", ps.get("Average Damage per Round","0")),
                        "mvps": ps.get("MVPs","0"),
                    }
        return None
    except:
        return None

async def faceit_full(steam_id=None, nickname=None):
    if not FACEIT_KEY:
        return {"error":"no_faceit_key"}
    async with httpx.AsyncClient(timeout=20) as client:
        player = await faceit_player(client, steam_id, nickname)
        if not player:
            return {"error":"not_found"}
        fid  = player.get("player_id","")
        game = player.get("games",{}).get("cs2",{})

        # Lifetime + map segments
        sr = await client.get(f"{FACEIT_BASE}/players/{fid}/stats/cs2", headers=fh())
        lifetime, segments = {}, []
        if sr.status_code == 200:
            sd = sr.json()
            lifetime = sd.get("lifetime",{})
            for seg in sd.get("segments",[]):
                if seg.get("type")=="Map":
                    st = seg.get("stats",{})
                    segments.append({
                        "map": seg.get("label","").replace("de_","").title(),
                        "winrate": st.get("Win Rate %","0"),
                        "matches": st.get("Matches","0"),
                        "kd": st.get("Average K/D Ratio","0"),
                        "hs": st.get("Average Headshots %","0"),
                    })

        # Match history
        hr = await client.get(f"{FACEIT_BASE}/players/{fid}/history",
            params={"game":"cs2","limit":12}, headers=fh())
        match_ids = []
        if hr.status_code == 200:
            match_ids = [m.get("match_id") for m in hr.json().get("items",[])]

        # Per-match stats (parallel)
        matches = []
        if match_ids:
            results = await asyncio.gather(*[faceit_match_stats(client,mid,fid) for mid in match_ids])
            matches = [m for m in results if m]

        return {
            "faceit_id": fid,
            "nickname": player.get("nickname",""),
            "avatar": player.get("avatar",""),
            "country": player.get("country","").upper(),
            "level": game.get("skill_level"),
            "elo": game.get("faceit_elo"),
            "faceit_url": player.get("faceit_url","").replace("{lang}","en"),
            "lifetime": {
                "kd": lifetime.get("Average K/D Ratio",""),
                "hs": lifetime.get("Average Headshots %",""),
                "winrate": lifetime.get("Win Rate %",""),
                "matches": lifetime.get("Matches",""),
                "wins": lifetime.get("Wins",""),
                "kr": lifetime.get("Average K/R Ratio",""),
                "longest_streak": lifetime.get("Longest Win Streak",""),
                "current_streak": lifetime.get("Current Win Streak",""),
            },
            "maps": segments,
            "matches": matches,
        }

# ── Steam Auth ────────────────────────────────────────────────────────────────
@app.get("/auth/steam")
async def auth_steam(request: Request):
    base = str(request.base_url).rstrip("/")
    p = {
        "openid.ns":"http://specs.openid.net/auth/2.0",
        "openid.mode":"checkid_setup",
        "openid.return_to":f"{base}/auth/steam/callback",
        "openid.realm":base,
        "openid.identity":"http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id":"http://specs.openid.net/auth/2.0/identifier_select",
    }
    return RedirectResponse(f"{STEAM_OPENID}?{urllib.parse.urlencode(p)}")

@app.get("/auth/steam/callback")
async def auth_steam_callback(request: Request):
    params = dict(request.query_params)
    verify = {**params,"openid.mode":"check_authentication"}
    async with httpx.AsyncClient(timeout=10) as client:
        vr = await client.post(STEAM_OPENID, data=verify)
    if "is_valid:true" not in vr.text:
        return HTMLResponse("<script>window.close();</script>")
    steam_id = params.get("openid.claimed_id","").split("/")[-1]
    if not steam_id.isdigit():
        return HTMLResponse("<script>window.close();</script>")

    async with httpx.AsyncClient(timeout=12) as client:
        profile = await steam_profile(steam_id, client)
        cs2     = await steam_cs2(steam_id, client)
    faceit = await faceit_full(steam_id=steam_id)

    player = {"steamid":steam_id, **profile, "faceit": faceit if "error" not in faceit else None}
    msg = json.dumps({"player":player,"stats":cs2})
    return HTMLResponse(f"""<!DOCTYPE html><html><head><title>Steam</title></head>
<body style="background:#080807;color:#f5c518;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
<div style="text-align:center"><div style="font-size:36px;margin-bottom:12px">✓</div>
<div style="letter-spacing:4px;font-size:12px">АВТОРИЗАЦИЯ УСПЕШНА</div></div>
<script>if(window.opener){{window.opener.postMessage({msg},'*');setTimeout(()=>window.close(),900);}}</script>
</body></html>""")

# ── Search ────────────────────────────────────────────────────────────────────
@app.get("/search/{nickname}")
async def search(nickname: str):
    if not FACEIT_KEY:
        return {"results":[],"error":"no_faceit_key"}
    async with httpx.AsyncClient(timeout=12) as client:
        r = await client.get(f"{FACEIT_BASE}/search/players",
            params={"nickname":nickname,"game":"cs2","limit":8}, headers=fh())
        if r.status_code != 200:
            return {"results":[]}
        items = r.json().get("items",[])
        return {"results":[{
            "nickname": it.get("nickname",""),
            "avatar": it.get("avatar",""),
            "country": it.get("country","").upper(),
            "faceit_id": it.get("player_id",""),
            "verified": it.get("verified",False),
        } for it in items]}

@app.get("/faceit/by-nickname/{nickname}")
async def faceit_by_nickname(nickname: str):
    return await faceit_full(nickname=nickname)

@app.get("/faceit/{steamid}")
async def faceit_by_steam(steamid: str):
    return await faceit_full(steam_id=steamid)

# ── Profile ───────────────────────────────────────────────────────────────────
@app.get("/profile/{steamid}")
async def get_profile(steamid: str):
    async with httpx.AsyncClient(timeout=12) as client:
        profile = await steam_profile(steamid, client)
        cs2     = await steam_cs2(steamid, client)
    faceit = await faceit_full(steam_id=steamid)
    return {"steamid":steamid, **profile,
            "faceit": faceit if "error" not in faceit else None,
            "cs2": cs2, "history": analysis_history.get(steamid,[])[:5]}

# ── Analyze ───────────────────────────────────────────────────────────────────
@app.post("/analyze")
async def analyze(stats: Stats):
    maps_text = ""
    if stats.maps:
        rows = [f"{m.get('map')}: WR {m.get('winrate')}%, {m.get('matches')} матчей, K/D {m.get('kd')}" for m in stats.maps[:8]]
        maps_text = "\nСтатистика по картам:\n" + "\n".join(rows)

    prompt = f"""Проанализируй игрока CS2 и верни ТОЛЬКО валидный JSON без markdown.

Общие статы: K/D={stats.kd} WR={stats.winrate}% HLTV={stats.hltv} HS={stats.hs}% ADR={stats.adr} 1v1={stats.clutch1v1}% Entry={stats.entrySuccess}% Rank={stats.rank} Matches={stats.matches}{maps_text}

Если есть статистика карт — дай конкретные инсайты по картам (какая карта лучшая, какая худшая, что банить).

JSON строго такой:
{{"level":"Новичок","overall":"вывод","mainProblem":"проблема","weaknesses":[{{"stat":"","problem":"","fix":""}},{{"stat":"","problem":"","fix":""}},{{"stat":"","problem":"","fix":""}}],"strengths":[{{"stat":"","comment":""}},{{"stat":"","comment":""}},{{"stat":"","comment":""}}],"mapInsights":["инсайт про карту 1","инсайт про карту 2"],"plan":["день 1","день 2","день 3"],"goal":"цель"}}

level = одно из: Новичок, Средний, Хороший, Про"""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile",
                "messages":[
                    {"role":"system","content":"Ты тренер CS2. Отвечай ТОЛЬКО валидным JSON без markdown."},
                    {"role":"user","content":prompt}],
                "temperature":0.7})
    data = response.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except:
        return {"error":f"Groq error: {json.dumps(data)}","result":""}
    text = re.sub(r"```(?:json)?","",text).strip().replace("```","").strip()

    parsed = None
    try: parsed = json.loads(text)
    except:
        m = re.search(r'\{[\s\S]*\}',text)
        if m:
            try: parsed = json.loads(m.group(0))
            except: pass

    if parsed and stats.steamid:
        analysis_history.setdefault(stats.steamid,[]).insert(0,{
            "timestamp":int(time.time()),"stats":stats.dict(),"result":parsed})
        analysis_history[stats.steamid] = analysis_history[stats.steamid][:20]

    if parsed:
        return {"result":json.dumps(parsed,ensure_ascii=False)}
    return {"result":text,"error":"parse_error"}

# ── Leaderboard / History ─────────────────────────────────────────────────────
@app.get("/leaderboard")
def get_leaderboard():
    s = sorted(leaderboard, key=lambda x:int(x.get("stats",{}).get("rank",0) or 0), reverse=True)
    return {"leaderboard":s[:100]}

@app.post("/leaderboard/add")
async def add_lb(entry: LBEntry):
    global leaderboard
    leaderboard = [e for e in leaderboard if e.get("steamid")!=entry.steamid]
    leaderboard.append(entry.dict())
    return {"ok":True}

@app.get("/history/{steamid}")
def get_history(steamid: str):
    return {"history":analysis_history.get(steamid,[])}

@app.get("/")
def root():
    return {"status":"ok"}
