from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
import httpx, os, re, json, urllib.parse

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

GROQ_KEY      = os.environ.get("GROQ_API_KEY")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY")
FRONTEND_URL  = os.environ.get("FRONTEND_URL", "https://cs2-coach-frontend.vercel.app")
STEAM_OPENID  = "https://steamcommunity.com/openid/login"

leaderboard = []  # in-memory

# ── Models ──────────────────────────────────────────────────────────────────
class Stats(BaseModel):
    kd: str; winrate: str; hltv: str; hs: str; adr: str
    clutch1v1: str; entrySuccess: str; rank: str; matches: str

class LBEntry(BaseModel):
    steamid: str; username: str; avatar: str
    stats: dict; level: str; overall: str

# ── Steam Auth ───────────────────────────────────────────────────────────────
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
    verify_params = {**params, "openid.mode": "check_authentication"}

    async with httpx.AsyncClient(timeout=10) as client:
        vr = await client.post(STEAM_OPENID, data=verify_params)

    if "is_valid:true" not in vr.text:
        return HTMLResponse("<script>window.close();</script>")

    steam_id = params.get("openid.claimed_id", "").split("/")[-1]
    if not steam_id.isdigit():
        return HTMLResponse("<script>window.close();</script>")

    player = {"steamid": steam_id, "username": "Unknown", "avatar": ""}
    cs2 = {"kd":"0","winrate":"0","hltv":"0","hs":"0","adr":"0","clutch1v1":"0","entrySuccess":"0","rank":"0","matches":"0"}

    if STEAM_API_KEY:
        async with httpx.AsyncClient(timeout=10) as client:
            # Profile
            sr = await client.get(
                "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
                params={"key": STEAM_API_KEY, "steamids": steam_id}
            )
            players = sr.json().get("response", {}).get("players", [])
            if players:
                player["username"] = players[0].get("personaname", "Unknown")
                player["avatar"]   = players[0].get("avatarfull", "")

            # CS2 stats
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
            cs2 = {
                "kd":          f"{kills/max(deaths,1):.2f}",
                "winrate":     f"{wins/max(matches,1)*100:.0f}",
                "hltv":        "0.00",
                "hs":          f"{hs_k/max(kills,1)*100:.0f}",
                "adr":         "0",
                "clutch1v1":   "0",
                "entrySuccess":"0",
                "rank":        "0",
                "matches":     str(matches),
            }

    msg = json.dumps({"player": player, "stats": cs2})
    html = f"""<!DOCTYPE html><html>
<head><title>Steam</title></head>
<body style="background:#0a0a0a;color:#f5c518;font-family:monospace;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
  <div style="text-align:center">
    <div style="font-size:32px;margin-bottom:10px">✓</div>
    <div style="letter-spacing:4px;font-size:12px">АВТОРИЗАЦИЯ УСПЕШНА</div>
  </div>
  <script>
    if(window.opener){{
      window.opener.postMessage({msg},'*');
      setTimeout(()=>window.close(),900);
    }}
  </script>
</body></html>"""
    return HTMLResponse(html)

# ── Analyze ──────────────────────────────────────────────────────────────────
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
    try:
        return {"result": json.dumps(json.loads(text), ensure_ascii=False)}
    except:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                return {"result": json.dumps(json.loads(m.group(0)), ensure_ascii=False)}
            except: pass
        return {"result": text, "error": "parse_error"}

# ── Leaderboard ──────────────────────────────────────────────────────────────
@app.get("/leaderboard")
def get_leaderboard():
    s = sorted(leaderboard, key=lambda x: int(x.get("stats", {}).get("rank", 0) or 0), reverse=True)
    return {"leaderboard": s[:50]}

@app.post("/leaderboard/add")
async def add_to_leaderboard(entry: LBEntry):
    global leaderboard
    leaderboard = [e for e in leaderboard if e.get("steamid") != entry.steamid]
    leaderboard.append(entry.dict())
    return {"ok": True}

@app.get("/")
def root():
    return {"status": "ok"}
