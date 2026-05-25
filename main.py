from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
import httpx, os, re, json, urllib.parse, time
from typing import Optional

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GROQ_KEY      = os.environ.get("GROQ_API_KEY")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY")
FACEIT_KEY    = os.environ.get("FACEIT_API_KEY")
STEAM_OPENID  = "https://steamcommunity.com/openid/login"

leaderboard      = []
analysis_history = {}  # steamid -> [entries]

# ── Models ───────────────────────────────────────────────────────────────────
class Stats(BaseModel):
    kd: str; winrate: str; hltv: str; hs: str; adr: str
    clutch1v1: str; entrySuccess: str; rank: str; matches: str
    steamid: Optional[str] = ""

class LBEntry(BaseModel):
    steamid: str; username: str; avatar: str
    stats: dict; level: str; overall: str

# ── Helpers ──────────────────────────────────────────────────────────────────
async def fetch_steam_profile(steam_id: str, client: httpx.AsyncClient):
    if not STEAM_API_KEY:
        return {"username": "Unknown", "avatar": "", "created": None, "level": None}
    sr = await client.get(
        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
        params={"key": STEAM_API_KEY, "steamids": steam_id}
    )
    players = sr.json().get("response", {}).get("players", [])
    if not players:
        return {"username": "Unknown", "avatar": "", "created": None, "level": None}
    p = players[0]

    # Steam level
    lvl_r = await client.get(
        "https://api.steampowered.com/IPlayerService/GetSteamLevel/v1/",
        params={"key": STEAM_API_KEY, "steamid": steam_id}
    )
    steam_level = lvl_r.json().get("response", {}).get("player_level", None)

    return {
        "username":    p.get("personaname", "Unknown"),
        "avatar":      p.get("avatarfull", ""),
        "created":     p.get("timecreated", None),
        "steam_level": steam_level,
        "profile_url": p.get("profileurl", ""),
        "country":     p.get("loccountrycode", ""),
    }

async def fetch_cs2_stats(steam_id: str, client: httpx.AsyncClient):
    if not STEAM_API_KEY:
        return {}
    gr = await client.get(
        "https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v0002/",
        params={"key": STEAM_API_KEY, "steamid": steam_id, "appid": 730}
    )
    raw = {s["name"]: s["value"] for s in gr.json().get("playerstats", {}).get("stats", [])}
    kills   = raw.get("total_kills", 0)
    deaths  = raw.get("total_deaths", 1)
    wins    = raw.get("total_wins", 0)
    matches = raw.get("total_matches_played", 1)
    hs_k    = raw.get("total_kills_headshot", 0)
    mvps    = raw.get("total_mvps", 0)
    return {
        "kd":       f"{kills/max(deaths,1):.2f}",
        "winrate":  f"{wins/max(matches,1)*100:.0f}",
        "hs":       f"{hs_k/max(kills,1)*100:.0f}",
        "matches":  str(matches),
        "kills":    str(kills),
        "deaths":   str(deaths),
        "wins":     str(wins),
        "mvps":     str(mvps),
        "hltv":     "0.00",
        "adr":      "0",
        "clutch1v1":"0",
        "entrySuccess":"0",
        "rank":     "0",
    }

async def fetch_faceit(steam_id: str, client: httpx.AsyncClient):
    if not FACEIT_KEY:
        return None
    try:
        r = await client.get(
            "https://open.faceit.com/data/v4/players",
            params={"game": "cs2", "game_player_id": steam_id},
            headers={"Authorization": f"Bearer {FACEIT_KEY}"}
        )
        if r.status_code != 200:
            return None
        d = r.json()
        game = d.get("games", {}).get("cs2", {})
        faceit_id = d.get("player_id", "")

        # Fetch lifetime stats
        stats_r = await client.get(
            f"https://open.faceit.com/data/v4/players/{faceit_id}/stats/cs2",
            headers={"Authorization": f"Bearer {FACEIT_KEY}"}
        )
        lifetime = {}
        if stats_r.status_code == 200:
            lifetime = stats_r.json().get("lifetime", {})

        return {
            "faceit_level": game.get("skill_level"),
            "faceit_elo":   game.get("faceit_elo"),
            "faceit_url":   d.get("faceit_url", "").replace("{lang}", "en"),
            "faceit_name":  d.get("nickname", ""),
            "hs_pct":       lifetime.get("Average Headshots %", ""),
            "kd_ratio":     lifetime.get("Average K/D Ratio", ""),
            "win_rate":     lifetime.get("Win Rate %", ""),
            "matches":      lifetime.get("Matches", ""),
        }
    except:
        return None

# ── Steam Auth ────────────────────────────────────────────────────────────────
@app.get("/auth/steam")
async def auth_steam(request: Request):
    base = str(request.base_url).rstrip("/")
    p = {
        "openid.ns":         "http://specs.openid.net/auth/2.0",
        "openid.mode":       "checkid_setup",
        "openid.return_to":  f"{base}/auth/steam/callback",
        "openid.realm":      base,
        "openid.identity":   "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    return RedirectResponse(f"{STEAM_OPENID}?{urllib.parse.urlencode(p)}")

@app.get("/auth/steam/callback")
async def auth_steam_callback(request: Request):
    params = dict(request.query_params)
    verify = {**params, "openid.mode": "check_authentication"}
    async with httpx.AsyncClient(timeout=10) as client:
        vr = await client.post(STEAM_OPENID, data=verify)
    if "is_valid:true" not in vr.text:
        return HTMLResponse("<script>window.close();</script>")

    steam_id = params.get("openid.claimed_id", "").split("/")[-1]
    if not steam_id.isdigit():
        return HTMLResponse("<script>window.close();</script>")

    async with httpx.AsyncClient(timeout=12) as client:
        profile = await fetch_steam_profile(steam_id, client)
        cs2     = await fetch_cs2_stats(steam_id, client)
        faceit  = await fetch_faceit(steam_id, client)

    player = {"steamid": steam_id, **profile, "faceit": faceit}
    msg = json.dumps({"player": player, "stats": cs2})

    html = f"""<!DOCTYPE html><html>
<head><title>Steam</title></head>
<body style="background:#080807;color:#f5c518;font-family:monospace;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
  <div style="text-align:center">
    <div style="font-size:36px;margin-bottom:12px">✓</div>
    <div style="letter-spacing:4px;font-size:12px">АВТОРИЗАЦИЯ УСПЕШНА</div>
  </div>
  <script>
    if(window.opener){{window.opener.postMessage({msg},'*');setTimeout(()=>window.close(),900);}}
  </script>
</body></html>"""
    return HTMLResponse(html)

# ── Profile ───────────────────────────────────────────────────────────────────
@app.get("/profile/{steamid}")
async def get_profile(steamid: str):
    async with httpx.AsyncClient(timeout=12) as client:
        profile = await fetch_steam_profile(steamid, client)
        cs2     = await fetch_cs2_stats(steamid, client)
        faceit  = await fetch_faceit(steamid, client)
    history = analysis_history.get(steamid, [])
    return {"steamid": steamid, **profile, "faceit": faceit, "cs2": cs2, "history": history[:5]}

# ── Analyze ───────────────────────────────────────────────────────────────────
@app.post("/analyze")
async def analyze(stats: Stats):
    prompt = f"""Проанализируй статистику игрока CS2 и верни ТОЛЬКО валидный JSON без markdown и пояснений.

Статы: K/D={stats.kd} WR={stats.winrate}% HLTV={stats.hltv} HS={stats.hs}% ADR={stats.adr} 1v1={stats.clutch1v1}% Entry={stats.entrySuccess}% Rank={stats.rank} Matches={stats.matches}

Верни строго этот JSON:
{{"level":"Новичок","overall":"краткий вывод об игре","mainProblem":"главная проблема","weaknesses":[{{"stat":"название","problem":"описание проблемы","fix":"совет"}},{{"stat":"название","problem":"описание проблемы","fix":"совет"}}],"strengths":[{{"stat":"название","comment":"комментарий"}},{{"stat":"название","comment":"комментарий"}}],"plan":["день 1: задание","день 2: задание","день 3: задание"],"goal":"цель через месяц"}}

level = одно из: Новичок, Средний, Хороший, Про"""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "Ты тренер CS2. Отвечай ТОЛЬКО валидным JSON без markdown."},
                    {"role": "user",   "content": prompt}
                ],
                "temperature": 0.7
            }
        )

    data = response.json()
    try:
        text = data["choices"][0]["message"]["content"]
    except:
        return {"error": f"Groq error: {json.dumps(data)}", "result": ""}

    text = re.sub(r"```(?:json)?", "", text).strip().replace("```", "").strip()

    parsed = None
    try:
        parsed = json.loads(text)
    except:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try: parsed = json.loads(m.group(0))
            except: pass

    if parsed and stats.steamid:
        if stats.steamid not in analysis_history:
            analysis_history[stats.steamid] = []
        analysis_history[stats.steamid].insert(0, {
            "timestamp": int(time.time()),
            "stats": stats.dict(),
            "result": parsed
        })
        analysis_history[stats.steamid] = analysis_history[stats.steamid][:20]

    if parsed:
        return {"result": json.dumps(parsed, ensure_ascii=False)}
    return {"result": text, "error": "parse_error"}

# ── Leaderboard ───────────────────────────────────────────────────────────────
@app.get("/leaderboard")
def get_leaderboard():
    s = sorted(leaderboard, key=lambda x: int(x.get("stats", {}).get("rank", 0) or 0), reverse=True)
    return {"leaderboard": s[:100]}

@app.post("/leaderboard/add")
async def add_to_leaderboard(entry: LBEntry):
    global leaderboard
    leaderboard = [e for e in leaderboard if e.get("steamid") != entry.steamid]
    leaderboard.append(entry.dict())
    return {"ok": True}

# ── History ───────────────────────────────────────────────────────────────────
@app.get("/history/{steamid}")
def get_history(steamid: str):
    return {"history": analysis_history.get(steamid, [])}

@app.get("/")
def root():
    return {"status": "ok"}
