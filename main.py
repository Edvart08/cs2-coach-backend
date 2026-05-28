from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
import httpx, os, re, json, urllib.parse, time, asyncio, secrets, string
from typing import Optional

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

import hashlib

GROQ_KEY      = os.environ.get("GROQ_API_KEY")
STEAM_API_KEY = os.environ.get("STEAM_API_KEY")
FACEIT_KEY    = os.environ.get("FACEIT_API_KEY")
ADMIN_TOKEN   = os.environ.get("ADMIN_TOKEN", "change_me_in_render")
FK_SHOP_ID    = os.environ.get("FREEKASSA_SHOP_ID", "")
FK_SECRET1    = os.environ.get("FREEKASSA_SECRET1", "")
FK_SECRET2    = os.environ.get("FREEKASSA_SECRET2", "")
FRONTEND_URL  = os.environ.get("FRONTEND_URL", "https://cs2-coach-frontend.vercel.app")
STEAM_OPENID  = "https://steamcommunity.com/openid/login"
FACEIT_BASE   = "https://open.faceit.com/data/v4"

leaderboard       = []
analysis_history  = {}
pending_payments  = {}   # order_id -> {steamid, plan}

# ── Pro система ───────────────────────────────────────────────────────────────
pro_users = {}
pro_keys  = {}
ai_usage  = {}
FREE_LIMIT = 5

def is_pro(steamid: str) -> bool:
    return steamid in pro_users

def check_usage(steamid: str) -> dict:
    today = time.strftime("%Y-%m-%d")
    u = ai_usage.get(steamid, {"date": today, "count": 0})
    if u["date"] != today:
        u = {"date": today, "count": 0}
    ai_usage[steamid] = u
    pro = is_pro(steamid)
    remaining = 999 if pro else max(0, FREE_LIMIT - u["count"])
    return {"pro": pro, "count": u["count"], "remaining": remaining, "limit": FREE_LIMIT}

def consume_usage(steamid: str):
    if not steamid or is_pro(steamid):
        return
    today = time.strftime("%Y-%m-%d")
    u = ai_usage.get(steamid, {"date": today, "count": 0})
    if u["date"] != today:
        u = {"date": today, "count": 0}
    u["count"] += 1
    ai_usage[steamid] = u

def gen_key() -> str:
    chars = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
    return "CS2PRO-" + "-".join(parts)

def fk_sign(amount, order_id, secret):
    return hashlib.md5(f"{FK_SHOP_ID}:{amount}:{secret}:RUB:{order_id}".encode()).hexdigest()

def activate_pro(steamid: str, plan: str, order_id: str):
    key = gen_key()
    while key in pro_keys:
        key = gen_key()
    pro_keys[key] = {"used": True, "steamid": steamid}
    pro_users[steamid] = {"key": key, "activated_at": int(time.time()), "plan": plan, "order": order_id}


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
        return {"private": True}
    gr = await client.get("https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v0002/",
        params={"key":STEAM_API_KEY,"steamid":steam_id,"appid":730})
    raw = {s["name"]:s["value"] for s in gr.json().get("playerstats",{}).get("stats",[])}
    if not raw:
        return {"private": True}
    k,d = raw.get("total_kills",0), raw.get("total_deaths",1)
    w,m = raw.get("total_wins",0), raw.get("total_matches_played",1)
    hk  = raw.get("total_kills_headshot",0)
    kd  = round(k/max(d,1), 2)
    wr  = min(100, round(w/max(m,1)*100))   # clamp 0-100
    hs  = min(100, round(hk/max(k,1)*100))  # clamp 0-100
    return {
        "private": False,
        "kd": f"{kd:.2f}", "winrate": str(wr),
        "hs": str(hs), "matches": str(m),
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
    if stats.steamid:
        usage = check_usage(stats.steamid)
        if usage["remaining"] == 0:
            return {"error": "limit_reached", "result": ""}
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
                    {"role":"system","content":"Ты тренер CS2. Отвечай ТОЛЬКО валидным JSON-объектом без markdown и пояснений."},
                    {"role":"user","content":prompt}],
                "temperature":0.6,
                "response_format":{"type":"json_object"}})
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
        if stats.steamid: consume_usage(stats.steamid)
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

# ── AI Summary ────────────────────────────────────────────────────────────────
class SummaryReq(BaseModel):
    kd: str; winrate: str; hs: str; matches: str; rank: str
    faceit_level: Optional[str] = ""; faceit_elo: Optional[str] = ""
    maps: Optional[list] = []

@app.post("/ai-summary")
async def ai_summary(req: SummaryReq):
    maps_text = ""
    if req.maps:
        rows = [f"{m.get('map')}: {m.get('winrate')}% WR / {m.get('matches')} матчей" for m in req.maps[:6]]
        maps_text = "\nКарты:\n" + "\n".join(rows)

    prompt = f"""Ты личный CS2 тренер. Игрок только что открыл свой профиль.
Напиши им честный и конкретный разбор В РАЗГОВОРНОМ ТОНЕ, как будто ты реально знаешь их игру.

Данные: K/D={req.kd}, WR={req.winrate}%, HS%={req.hs}, Матчей={req.matches}, FACEIT lvl={req.faceit_level or "нет"}, ELO={req.faceit_elo or "нет"}{maps_text}

Верни ТОЛЬКО JSON без markdown:
{{"verdict":"2-3 предложения — честный и конкретный вывод об игроке, как тренер, не как робот",
"problems":["конкретная проблема 1","конкретная проблема 2","конкретная проблема 3"],
"priority":"одна главная вещь которую надо исправить прямо сейчас",
"role":"ENTRY / SUPPORT / RIFLER / LURKER / AWP — угадай по статам",
"roast":"короткая честная фраза которую скажет суровый тренер, без оскорблений но прямо"
}}"""

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile",
                "messages":[{"role":"system","content":"Ты тренер по CS2. Отвечай ТОЛЬКО валидным JSON без markdown."},
                             {"role":"user","content":prompt}],
                "temperature":0.8,
                "response_format":{"type":"json_object"}})
    data = r.json()
    try:
        text = data["choices"][0]["message"]["content"]
        return {"result": json.loads(text)}
    except:
        return {"error":"parse_error"}

# ── AI Chat ────────────────────────────────────────────────────────────────────
class ChatMsg(BaseModel):
    role: str; content: str

class ChatReq(BaseModel):
    messages: list
    stats: Optional[dict] = {}

@app.post("/chat")
async def chat(req: ChatReq):
    s = req.stats or {}
    context = (
        f"Статы игрока: K/D={s.get('kd','?')}, WR={s.get('winrate','?')}%, "
        f"HS%={s.get('hs','?')}, Матчей={s.get('matches','?')}, "
        f"FACEIT lvl={s.get('faceit_level','нет')}, ELO={s.get('faceit_elo','нет')}. "
        f"Отвечай конкретно опираясь на эти данные. Отвечай по-русски. Будь прямым."
    )
    system = f"Ты личный CS2 тренер-аналитик. {context}"
    messages = [{"role":"system","content":system}] + [
        {"role":m["role"],"content":m["content"]} for m in req.messages[-10:]
    ]
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile","messages":messages,"temperature":0.75,"max_tokens":400})
    data = r.json()
    try:
        return {"reply": data["choices"][0]["message"]["content"]}
    except:
        return {"error": json.dumps(data)}


FRONTEND_URL = os.environ.get("FRONTEND_URL", "https://cs2-coach-frontend.vercel.app")


class MatchReq(BaseModel):
    map: str; result: str; kills: str; deaths: str
    assists: str; kd: str; hs: str; adr: str; mvps: str; score: str

@app.post("/analyze-match")
async def analyze_match(req: MatchReq):
    outcome = "ПОБЕДА" if req.result=="1" else "ПОРАЖЕНИЕ"
    prompt = f"""Ты CS2 тренер. Игрок сыграл матч и хочет знать что пошло не так.

Матч: {req.map} · {outcome} ({req.score})
Статы: K/D={req.kd}, Убийства={req.kills}, Смерти={req.deaths}, Ассисты={req.assists}, HS%={req.hs}%, ADR={req.adr}, MVP={req.mvps}

Дай конкретный разбор ЭТОГО матча. Не общие советы — именно что могло произойти исходя из этих цифр.

Верни ТОЛЬКО JSON:
{{"verdict":"1-2 конкретных предложения про этот матч",
"mistakes":["конкретная ошибка из этого матча 1","конкретная ошибка 2"],
"bright":"что было хорошо в этом матче",
"tip":"одно конкретное что улучшить на {req.map}"
}}"""

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile",
                "messages":[{"role":"system","content":"Тренер CS2. Отвечай ТОЛЬКО JSON без markdown."},
                             {"role":"user","content":prompt}],
                "temperature":0.75,
                "response_format":{"type":"json_object"}})
    data = r.json()
    try:
        return {"result": json.loads(data["choices"][0]["message"]["content"])}
    except:
        return {"error": "parse_error"}



# ── FreeKassa платежи ─────────────────────────────────────────────────────────
class PaymentReq(BaseModel):
    steamid: str
    plan: str = "month"   # month | year

@app.post("/payment/create")
async def payment_create(req: PaymentReq):
    if not FK_SHOP_ID:
        raise HTTPException(status_code=503, detail="Платежи временно недоступны")
    amount = 299 if req.plan == "month" else 1990
    order_id = f"{req.steamid}_{int(time.time())}"
    sign = fk_sign(amount, order_id, FK_SECRET1)
    pending_payments[order_id] = {"steamid": req.steamid, "plan": req.plan}
    pay_url = (
        f"https://pay.freekassa.com/"
        f"?m={FK_SHOP_ID}&oa={amount}&currency=RUB"
        f"&o={order_id}&s={sign}&lang=ru"
        f"&success_url={FRONTEND_URL}?payment=success"
        f"&failure_url={FRONTEND_URL}?payment=fail"
    )
    return {"url": pay_url, "order_id": order_id}

@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    try:
        form = await request.form()
        d = dict(form)
    except:
        d = {}
    merchant_order = d.get("MERCHANT_ORDER_ID", "")
    amount         = d.get("AMOUNT", "")
    sign_got       = d.get("SIGN", "")
    sign_exp       = hashlib.md5(f"{FK_SHOP_ID}:{amount}:{FK_SECRET2}:{merchant_order}".encode()).hexdigest()
    if sign_got != sign_exp:
        return HTMLResponse("NO", status_code=400)
    if merchant_order in pending_payments:
        p = pending_payments.pop(merchant_order)
        activate_pro(p["steamid"], p["plan"], merchant_order)
    return HTMLResponse("YES")

@app.get("/payment/status/{steamid}")
async def payment_status(steamid: str):
    return {"pro": is_pro(steamid), "data": pro_users.get(steamid)}

# ── Pro эндпоинты ─────────────────────────────────────────────────────────────
class KeyReq(BaseModel):
    steamid: str
    key: str

@app.post("/activate-key")
async def activate_key(req: KeyReq):
    k = req.key.upper().strip()
    if k not in pro_keys:
        raise HTTPException(status_code=404, detail="Ключ не найден")
    if pro_keys[k]["used"] and pro_keys[k]["steamid"] != req.steamid:
        raise HTTPException(status_code=400, detail="Ключ уже использован")
    pro_keys[k] = {"used": True, "steamid": req.steamid}
    pro_users[req.steamid] = {"key": k, "activated_at": int(time.time())}
    return {"ok": True, "message": "Pro активирован!"}

@app.get("/pro/{steamid}")
async def pro_status(steamid: str):
    usage = check_usage(steamid)
    return {"pro": usage["pro"], "remaining": usage["remaining"], "limit": FREE_LIMIT}

@app.post("/admin/keys/generate")
async def generate_keys(request: Request, n: int = 1):
    token = request.headers.get("x-admin-token","")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    n = min(n, 50)
    keys = []
    for _ in range(n):
        k = gen_key()
        while k in pro_keys:
            k = gen_key()
        pro_keys[k] = {"used": False, "steamid": None}
        keys.append(k)
    return {"keys": keys}

@app.get("/admin/keys/list")
async def list_keys(request: Request):
    token = request.headers.get("x-admin-token","")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"keys": pro_keys, "pro_users": pro_users}

@app.get("/health")
def health():
    return {"status": "ok", "ts": int(time.time())}

@app.get("/share/{steamid}", response_class=HTMLResponse)
async def share_profile(steamid: str):
    # Fetch data
    async with httpx.AsyncClient(timeout=14) as client:
        profile = await steam_profile(steamid, client)
        cs2     = await steam_cs2(steamid, client)
    faceit = await faceit_full(steam_id=steamid)
    fc = faceit if "error" not in faceit else {}

    username  = profile.get("username","Unknown")
    avatar    = profile.get("avatar","")
    country   = profile.get("country","")
    elo       = fc.get("elo","")
    lvl       = fc.get("level","")
    fc_nick   = fc.get("nickname","")
    kd        = fc.get("lifetime",{}).get("kd") or cs2.get("kd","—")
    wr        = fc.get("lifetime",{}).get("winrate") or cs2.get("winrate","—")
    hs        = fc.get("lifetime",{}).get("hs") or cs2.get("hs","—")
    matches   = fc.get("lifetime",{}).get("matches") or cs2.get("matches","—")

    lvl_colors = {"1":"#ccc","2":"#ccc","3":"#1CE400","4":"#1CE400","5":"#FFC800",
                  "6":"#FFC800","7":"#FF6309","8":"#FF6309","9":"#FE1F00","10":"#FE1F00"}
    lc = lvl_colors.get(str(lvl),"#f5c518") if lvl else "#f5c518"

    country_flag = ""
    if country and len(country)==2:
        try:
            country_flag = country.upper()
        except: pass

    history = analysis_history.get(steamid, [])
    verdict = history[0]["result"].get("overall","") if history else ""

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{username} · CS2 AI Тренер</title>
  <meta property="og:title" content="{username} — CS2 профиль">
  <meta property="og:description" content="K/D {kd} · WR {wr}% · HS {hs}% · {'FACEIT LVL '+str(lvl)+' · '+str(elo)+' ELO' if elo else 'Steam игрок'}{' · ' + verdict[:80] + '...' if verdict else ''}">
  <meta property="og:url" content="{FRONTEND_URL}">
  <meta name="twitter:card" content="summary">
  <style>
    *{{box-sizing:border-box;margin:0;padding:0;}}
    body{{background:#0a0a07;font-family:'Segoe UI',system-ui,sans-serif;color:#ddd6bc;
      display:flex;flex-direction:column;min-height:100vh;align-items:center;justify-content:center;padding:24px;}}
    .card{{background:#141409;border:1px solid #2e2e1e;border-top:3px solid #f5c518;
      max-width:480px;width:100%;padding:32px;position:relative;overflow:hidden;}}
    .glow{{position:absolute;top:-40px;right:-40px;width:180px;height:180px;
      background:radial-gradient(circle,{lc}20,transparent 70%);pointer-events:none;}}
    .avatar{{width:80px;height:80px;border-radius:4px;border:2px solid {lc};box-shadow:0 0 16px {lc}44;object-fit:cover;}}
    .avatar-ph{{width:80px;height:80px;border-radius:4px;border:2px solid {lc};background:#1a1a10;
      display:flex;align-items:center;justify-content:center;font-size:28px;}}
    .name{{font-size:22px;color:#f5eed8;font-weight:700;margin-bottom:4px;}}
    .meta{{font-size:13px;color:#9a9270;margin-bottom:12px;}}
    .lvl{{display:inline-block;background:{lc};color:#080807;font-size:11px;font-weight:700;padding:3px 10px;margin-bottom:16px;}}
    .stats{{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid #2e2e1e;margin-top:4px;}}
    .stat{{padding:14px 8px;text-align:center;border-right:1px solid #2e2e1e;}}
    .stat:last-child{{border-right:none;}}
    .sl{{font-size:11px;color:#9a9270;letter-spacing:1px;margin-bottom:4px;}}
    .sv{{font-size:20px;color:#f5c518;font-weight:700;font-family:'Consolas',monospace;}}
    .verdict{{background:#1a1a0e;border-left:3px solid #f5c518;padding:12px 16px;margin:16px 0;
      font-size:14px;color:#c8bc98;line-height:1.6;font-style:italic;}}
    .cta{{display:block;text-align:center;margin-top:20px;padding:12px 24px;
      background:#f5c518;color:#080807;text-decoration:none;font-weight:700;
      font-size:14px;letter-spacing:2px;}}
    .badge{{display:inline-flex;align-items:center;gap:8px;background:{lc}18;
      border:1px solid {lc}44;padding:6px 14px;margin-bottom:16px;}}
    .badge-elo{{font-size:22px;color:{lc};font-weight:700;}}
  </style>
</head>
<body>
  <div class="card">
    <div class="glow"></div>
    <div style="display:flex;gap:18px;align-items:flex-start;margin-bottom:4px;">
      {"<img class='avatar' src='"+avatar+"' alt=''/>" if avatar else "<div class='avatar-ph'>👤</div>"}
      <div style="flex:1;">
        <div class="name">{username}</div>
        <div class="meta">{"Steam · " + str(profile.get("steam_level","")) + " lvl" if profile.get("steam_level") else "Steam"}</div>
        {f'<div class="lvl">FACEIT LVL {lvl}</div>' if lvl else ''}
        {f'<div class="badge"><span style="font-size:13px;color:#9a9270;">ELO</span><span class="badge-elo">{elo}</span></div>' if elo else ''}
      </div>
    </div>
    {f'<div class="verdict">"{verdict[:120]}..."</div>' if verdict else ''}
    <div class="stats">
      <div class="stat"><div class="sl">K/D</div><div class="sv">{kd}</div></div>
      <div class="stat"><div class="sl">WIN%</div><div class="sv">{wr}%</div></div>
      <div class="stat"><div class="sl">HS%</div><div class="sv">{hs}%</div></div>
      <div class="stat"><div class="sl">МАТЧИ</div><div class="sv">{matches}</div></div>
    </div>
    <a class="cta" href="{FRONTEND_URL}">ПРОВЕРЬ СВОЙ ПРОФИЛЬ →</a>
    <div style="text-align:center;margin-top:12px;font-size:11px;color:#4a4830;letter-spacing:2px;">CS2 AI ТРЕНЕР</div>
  </div>
</body>
</html>"""
    return HTMLResponse(html)

@app.get("/")
def root():
    return {"status":"ok"}
