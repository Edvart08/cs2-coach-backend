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
YOO_SHOP_ID   = os.environ.get("YOOKASSA_SHOP_ID", "1376791")  # ShopID из ЮКассы
YOO_SECRET    = os.environ.get("YOOKASSA_SECRET", "")         # Секретный ключ из ЮКассы → Интеграция → API ключи
FRONTEND_URL   = os.environ.get("FRONTEND_URL", "https://cs2-coach-frontend.vercel.app")
TG_BOT_TOKEN   = os.environ.get("TG_BOT_TOKEN", "")
TG_ADMIN_ID    = os.environ.get("TG_ADMIN_ID", "")
STEAM_OPENID  = "https://steamcommunity.com/openid/login"
FACEIT_BASE   = "https://open.faceit.com/data/v4"

leaderboard       = []
analysis_history  = {}
pending_payments  = {}   # order_id -> {steamid, plan}
support_sessions  = {}   # steamid -> {username, msgs:[{from,text,ts}]}
admin_active      = {}   # tg_user_id -> steamid (текущий пользователь в диалоге)
lb_rate_limit     = {}   # steamid -> last_add timestamp (rate limit для лидерборда)
analyze_rate_limit= {}   # steamid -> last_analyze timestamp
promo_codes       = {}   # code -> {discount, uses_left, used_by:[steamid]}
admin_logs        = []   # [{ts, action, detail}] последние 200 событий
user_visits       = {}   # steamid -> {username, avatar, first_seen, last_seen, visit_count, stats}
banned_users      = {}   # steamid -> {reason, banned_at, banned_by}

def log_admin(action: str, detail: str = ""):
    admin_logs.insert(0, {"ts": int(time.time()), "action": action, "detail": detail})
    if len(admin_logs) > 200:
        admin_logs.pop()

# ── Persistence — файловое хранилище ──────────────────────────────────────────
DATA_DIR = "/tmp/cs2coach_data"
os.makedirs(DATA_DIR, exist_ok=True)

def _load(name: str, default):
    try:
        path = f"{DATA_DIR}/{name}.json"
        if os.path.exists(path):
            with open(path, "r") as f:
                return json.load(f)
    except: pass
    return default

def _save(name: str, data):
    try:
        with open(f"{DATA_DIR}/{name}.json", "w") as f:
            json.dump(data, f, ensure_ascii=False)
    except: pass

# Загружаем данные при старте
leaderboard      = _load("leaderboard", [])
pro_users        = _load("pro_users", {})
pro_keys         = _load("pro_keys", {})
ai_usage         = _load("ai_usage", {})
banned_users     = _load("banned_users", {})

# Если pro_users пустой (Render рестартнул) — пробуем восстановить из GitHub backup
async def restore_from_github():
    global pro_users, pro_keys, leaderboard, ai_usage, banned_users
    GITHUB_BACKUP_URL = os.environ.get("GITHUB_BACKUP_URL", "")
    if not GITHUB_BACKUP_URL or pro_users:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(GITHUB_BACKUP_URL)
            if r.status_code == 200:
                data = r.json()
                if data.get("pro_users"):   pro_users  = data["pro_users"];  _save("pro_users", pro_users)
                if data.get("pro_keys"):    pro_keys   = data["pro_keys"];   _save("pro_keys", pro_keys)
                if data.get("leaderboard"): leaderboard = data["leaderboard"]; _save("leaderboard", leaderboard)
                if data.get("banned_users"): banned_users = data["banned_users"]; _save("banned_users", banned_users)
                print(f"[RESTORE] PRO: {len(pro_users)}, banned: {len(banned_users)}")
    except Exception as e:
        print(f"[RESTORE] Ошибка: {e}")

@app.on_event("startup")
async def startup():
    asyncio.create_task(restore_from_github())

FREE_LIMIT = 1   # 1 бесплатный AI разбор в неделю

def is_pro(steamid: str) -> bool:
    return steamid in pro_users

def check_usage(steamid: str) -> dict:
    week = time.strftime("%Y-W%W")
    u = ai_usage.get(steamid, {"week": week, "count": 0})
    if u.get("week") != week:
        u = {"week": week, "count": 0}
    ai_usage[steamid] = u
    pro = is_pro(steamid)
    remaining = 999 if pro else max(0, FREE_LIMIT - u["count"])
    return {"pro": pro, "count": u["count"], "remaining": remaining, "limit": FREE_LIMIT}

def consume_usage(steamid: str):
    if not steamid or is_pro(steamid):
        return
    week = time.strftime("%Y-W%W")
    u = ai_usage.get(steamid, {"week": week, "count": 0})
    if u.get("week") != week:
        u = {"week": week, "count": 0}
    u["count"] += 1
    ai_usage[steamid] = u

def gen_key() -> str:
    chars = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(chars) for _ in range(4)) for _ in range(4)]
    return "CS2PRO-" + "-".join(parts)

def activate_pro(steamid: str, plan: str, order_id: str):
    key = gen_key()
    while key in pro_keys:
        key = gen_key()
    pro_keys[key] = {"used": True, "steamid": steamid}
    pro_users[steamid] = {"key": key, "activated_at": int(time.time()), "plan": plan, "order": order_id}
    _save("pro_users", pro_users)
    log_admin("PRO активирован", f"steamid={steamid} plan={plan} order={order_id}")


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
    import asyncio
    tasks = [
        client.get("https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v0002/",
            params={"key":STEAM_API_KEY,"steamids":steam_id}),
        client.get("https://api.steampowered.com/IPlayerService/GetSteamLevel/v1/",
            params={"key":STEAM_API_KEY,"steamid":steam_id}),
        client.get("https://api.steampowered.com/ISteamUser/GetFriendList/v0001/",
            params={"key":STEAM_API_KEY,"steamid":steam_id,"relationship":"friend"}),
        client.get("https://api.steampowered.com/IPlayerService/GetBadges/v1/",
            params={"key":STEAM_API_KEY,"steamid":steam_id}),
        client.get("https://api.steampowered.com/ISteamUser/GetPlayerBans/v1/",
            params={"key":STEAM_API_KEY,"steamids":steam_id}),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    sr, lr, fr, br, banr = results
    players = sr.json().get("response",{}).get("players",[]) if not isinstance(sr, Exception) else []
    if not players:
        return {"username":"Unknown","avatar":"","created":None,"steam_level":None}
    p = players[0]
    steam_level = lr.json().get("response",{}).get("player_level") if not isinstance(lr, Exception) else None
    friend_count = 0
    if not isinstance(fr, Exception):
        try: friend_count = len(fr.json().get("friendslist",{}).get("friends",[]))
        except: pass
    badge_count, player_xp = 0, 0
    if not isinstance(br, Exception):
        try:
            bd = br.json().get("response",{})
            badge_count = len(bd.get("badges",[])); player_xp = bd.get("player_xp",0)
        except: pass
    vac_banned = game_banned = False
    if not isinstance(banr, Exception):
        try:
            bans = banr.json().get("players",[{}])[0]
            vac_banned = bans.get("VACBanned",False); game_banned = bans.get("NumberOfGameBans",0)>0
        except: pass
    return {
        "username": p.get("personaname","Unknown"),
        "avatar": p.get("avatarfull",""),
        "created": p.get("timecreated"),
        "steam_level": steam_level,
        "profile_url": p.get("profileurl",""),
        "country": p.get("loccountrycode",""),
        "real_name": p.get("realname",""),
        "vac_banned": vac_banned,
        "game_banned": game_banned,
        "friend_count": friend_count,
        "badge_count": badge_count,
        "player_xp": player_xp,
    }

async def steam_cs2(steam_id, client):
    if not STEAM_API_KEY:
        return {"private": True}
    gr = await client.get("https://api.steampowered.com/ISteamUserStats/GetUserStatsForGame/v0002/",
        params={"key":STEAM_API_KEY,"steamid":steam_id,"appid":730})
    raw = {s["name"]:s["value"] for s in gr.json().get("playerstats",{}).get("stats",[])}
    if not raw:
        return {"private": True}
    k=raw.get("total_kills",0); d=raw.get("total_deaths",1)
    w=raw.get("total_wins",0); m=raw.get("total_matches_played",0)
    hk=raw.get("total_kills_headshot",0)
    shots_fired=raw.get("total_shots_fired",0); shots_hit=raw.get("total_shots_hit",0)
    kd = round(k/max(d,1),2)
    if m>=10:
        raw_wr=round(w/max(m,1)*100)
        if raw_wr>100: raw_wr=round(w/max(m*16,1)*100)
        if raw_wr>80 and m>=100: raw_wr=80
        wr=min(79,max(0,raw_wr))
    else: wr=0
    hs=min(100,round(hk/max(k,1)*100)) if k>0 else 0
    accuracy=round(shots_hit/max(shots_fired,1)*100) if shots_fired>0 else 0
    playtime_min=0
    try:
        pg=await client.get("https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/",
            params={"key":STEAM_API_KEY,"steamid":steam_id,"appids_filter[0]":730,
                    "include_appinfo":"false","include_played_free_games":"true"})
        for g in pg.json().get("response",{}).get("games",[]):
            if g.get("appid")==730: playtime_min=g.get("playtime_forever",0); break
    except: pass
    return {
        "private":False,
        "kd":f"{kd:.2f}","winrate":str(wr),"hs":str(hs),"matches":str(m),
        "kills":str(k),"deaths":str(d),"wins":str(w),
        "mvps":str(raw.get("total_mvps",0)),"playtime":str(playtime_min),
        "accuracy":str(accuracy),
        "knife_kills":str(raw.get("total_kills_knife",0)),
        "pistol_kills":str(raw.get("total_kills_pistols",0)),
        "sniper_kills":str(raw.get("total_kills_snipers",0)),
        "grenade_kills":str(raw.get("total_kills_grenade",0)),
        "blind_kills":str(raw.get("total_kills_enemy_blinded",0)),
        "dominated":str(raw.get("total_dominations",0)),
        "revenges":str(raw.get("total_revenges",0)),
        "bombs_planted":str(raw.get("total_planted_bombs",0)),
        "bombs_defused":str(raw.get("total_defused_bombs",0)),
        "rounds_pistol_won":str(raw.get("total_wins_pistolround",0)),
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
        elo_changes = {}
        if hr.status_code == 200:
            for m in hr.json().get("items",[]):
                mid = m.get("match_id")
                if mid:
                    match_ids.append(mid)
                    elo_changes[mid] = m.get("elo_change", 0)

        # Per-match stats (parallel)
        matches = []
        if match_ids:
            results = await asyncio.gather(*[faceit_match_stats(client,mid,fid) for mid in match_ids])
            for mid, m in zip(match_ids, results):
                if m:
                    m["elo_change"] = elo_changes.get(mid, 0)
                    matches.append(m)

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
    # Проверяем бан
    if steamid in banned_users:
        b = banned_users[steamid]
        raise HTTPException(status_code=403, detail=f"Аккаунт заблокирован. Причина: {b.get('reason','Нарушение правил')}")
    
    async with httpx.AsyncClient(timeout=12) as client:
        profile = await steam_profile(steamid, client)
        cs2     = await steam_cs2(steamid, client)
    faceit = await faceit_full(steam_id=steamid)
    
    # Трекинг визита пользователя
    now = int(time.time())
    if steamid not in user_visits:
        user_visits[steamid] = {
            "username": profile.get("username",""),
            "avatar": profile.get("avatar",""),
            "first_seen": now,
            "last_seen": now,
            "visit_count": 1,
            "country": profile.get("country",""),
            "steam_level": profile.get("steam_level"),
        }
    else:
        user_visits[steamid]["last_seen"] = now
        user_visits[steamid]["visit_count"] = user_visits[steamid].get("visit_count",0) + 1
        user_visits[steamid]["username"] = profile.get("username", user_visits[steamid].get("username",""))
        user_visits[steamid]["avatar"] = profile.get("avatar", user_visits[steamid].get("avatar",""))
    
    return {"steamid":steamid, **profile,
            "faceit": faceit if "error" not in faceit else None,
            "cs2": cs2, "history": analysis_history.get(steamid,[])[:5],
            "is_pro": is_pro(steamid)}

# ── Analyze ───────────────────────────────────────────────────────────────────
@app.post("/analyze")
async def analyze(stats: Stats):
    if stats.steamid:
        # Проверяем бан
        if stats.steamid in banned_users:
            b = banned_users[stats.steamid]
            return {"error": "banned", "result": f"Аккаунт заблокирован. Причина: {b.get('reason','Нарушение правил')}"}
        usage = check_usage(stats.steamid)
        if usage["remaining"] == 0:
            return {"error": "limit_reached", "result": ""}
        # Rate limit: не чаще 1 анализа в 10 секунд с одного steamid (защита от скрипт-спама)
        now = time.time()
        last_a = analyze_rate_limit.get(stats.steamid, 0)
        if now - last_a < 10:
            return {"error": "too_fast", "result": ""}
        analyze_rate_limit[stats.steamid] = now
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
    def sort_key(x):
        try: return int(x.get("overall", 0) or 0)
        except: 
            try: return float(x.get("stats",{}).get("kd",0) or 0)
            except: return 0
    s = sorted(leaderboard, key=sort_key, reverse=True)
    # Добавляем is_pro флаг к каждому игроку
    result = []
    for entry in s[:100]:
        e = dict(entry)
        e["is_pro"] = is_pro(e.get("steamid",""))
        result.append(e)
    return {"leaderboard": result, "total": len(leaderboard)}

@app.post("/leaderboard/add")
async def add_lb(entry: LBEntry):
    global leaderboard, lb_rate_limit
    now = time.time()
    
    # Трекинг пользователя при каждом leaderboard/add
    steamid = entry.steamid
    if steamid:
        if steamid not in user_visits:
            user_visits[steamid] = {
                "username": entry.username or "",
                "avatar": entry.avatar or "",
                "first_seen": int(now),
                "last_seen": int(now),
                "visit_count": 1,
                "country": "",
                "steam_level": None,
            }
        else:
            user_visits[steamid]["last_seen"] = int(now)
            user_visits[steamid]["visit_count"] = user_visits[steamid].get("visit_count",0) + 1
            if entry.username: user_visits[steamid]["username"] = entry.username
            if entry.avatar:   user_visits[steamid]["avatar"] = entry.avatar

    # Rate limit: один апдейт на steamid не чаще раза в 30 минут
    last = lb_rate_limit.get(entry.steamid, 0)
    if now - last < 1800:
        def sort_key_r(x):
            try: return int(x.get("overall", 0) or 0)
            except:
                try: return float(x.get("stats",{}).get("kd",0) or 0)
                except: return 0
        sorted_lb = sorted(leaderboard, key=sort_key_r, reverse=True)
        rank = next((i+1 for i,e in enumerate(sorted_lb) if e.get("steamid")==entry.steamid), None)
        return {"ok": True, "total": len(leaderboard), "rank": rank, "cached": True}
    lb_rate_limit[entry.steamid] = now
    leaderboard = [e for e in leaderboard if e.get("steamid")!=entry.steamid]
    leaderboard.append(entry.dict())
    _save("leaderboard", leaderboard)
    def sort_key(x):
        try: return int(x.get("overall", 0) or 0)
        except:
            try: return float(x.get("stats",{}).get("kd",0) or 0)
            except: return 0
    sorted_lb = sorted(leaderboard, key=sort_key, reverse=True)
    rank = next((i+1 for i,e in enumerate(sorted_lb) if e.get("steamid")==entry.steamid), None)
    return {"ok": True, "total": len(leaderboard), "rank": rank}

@app.get("/history/{steamid}")
def get_history(steamid: str):
    return {"history":analysis_history.get(steamid,[])}

# ── AI Summary ────────────────────────────────────────────────────────────────
class WeeklyReportReq(BaseModel):
    kd_start: str; kd_end: str
    hs_start: str; hs_end: str
    wr_start: str; wr_end: str
    matches_played: str
    wins: str
    best_map: Optional[str] = ""
    worst_map: Optional[str] = ""
    achievements_unlocked: Optional[list] = []
    faceit_level: Optional[str] = ""
    elo_change: Optional[str] = "0"

@app.post("/weekly-report")
async def weekly_report(req: WeeklyReportReq):
    kd_diff  = round(float(req.kd_end or 0) - float(req.kd_start or 0), 2)
    hs_diff  = round(float(req.hs_end or 0) - float(req.hs_start or 0), 1)
    wr_diff  = round(float(req.wr_end or 0) - float(req.wr_start or 0), 1)

    prompt = f"""Ты CS2 тренер. Напиши еженедельный отчёт для игрока.
Данные за неделю:
- K/D: {req.kd_start} → {req.kd_end} ({'+' if kd_diff>=0 else ''}{kd_diff})
- HS%: {req.hs_start}% → {req.hs_end}% ({'+' if hs_diff>=0 else ''}{hs_diff}%)
- WR%: {req.wr_start}% → {req.wr_end}% ({'+' if wr_diff>=0 else ''}{wr_diff}%)
- Сыграно матчей: {req.matches_played}, Побед: {req.wins}
- Лучшая карта: {req.best_map or 'нет данных'}
- Худшая карта: {req.worst_map or 'нет данных'}
- FACEIT уровень: {req.faceit_level or 'нет'}
- Изменение ELO: {req.elo_change}
- Разблокированные достижения: {', '.join(req.achievements_unlocked) if req.achievements_unlocked else 'нет'}

Верни ТОЛЬКО JSON без markdown:
{{"summary":"2-3 предложения о прогрессе за неделю — конкретно с цифрами",
"highlight":"самое лучшее что произошло за неделю",
"concern":"главная проблема которую надо решить на следующей неделе",
"next_week_goal":"конкретная цель на следующую неделю с цифрой",
"verdict":"РОСТ / СТАГНАЦИЯ / ПАДЕНИЕ — одно слово"
}}"""

    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile",
                "messages":[{"role":"system","content":"Ты тренер CS2. Только JSON без markdown."},
                             {"role":"user","content":prompt}],
                "temperature":0.7,
                "response_format":{"type":"json_object"}})
    try:
        return {"result": json.loads(r.json()["choices"][0]["message"]["content"])}
    except:
        return {"error":"parse_error"}
    kd: str; winrate: str; hs: str; matches: str; rank: str
    faceit_level: Optional[str] = ""; faceit_elo: Optional[str] = ""
    maps: Optional[list] = []
    recent_matches: Optional[list] = []
    best_map: Optional[str] = ""; worst_map: Optional[str] = ""

@app.post("/ai-summary")
async def ai_summary(req: SummaryReq):
    # Карты
    maps_text = ""
    if req.maps:
        sorted_maps = sorted(req.maps, key=lambda m: float(m.get("winrate",0)), reverse=True)
        rows = [f"{m.get('map')}: {m.get('winrate')}% WR, {m.get('kd')} K/D, {m.get('matches')} матчей" for m in sorted_maps[:6]]
        maps_text = "\nСтатистика по картам:\n" + "\n".join(rows)
        if sorted_maps:
            maps_text += f"\nЛУЧШАЯ карта: {sorted_maps[0].get('map')} ({sorted_maps[0].get('winrate')}% WR)"
            maps_text += f"\nХУДШАЯ карта: {sorted_maps[-1].get('map')} ({sorted_maps[-1].get('winrate')}% WR)"

    # Последние матчи
    recent_text = ""
    if req.recent_matches:
        recent_rows = []
        for m in req.recent_matches[:5]:
            res = "Победа" if m.get("result") == "1" else "Поражение"
            recent_rows.append(f"{m.get('map','?')}: {res}, K/D {m.get('kd','?')}, HS {m.get('hs','?')}%, ADR {m.get('adr','?')}")
        recent_text = "\nПоследние матчи:\n" + "\n".join(recent_rows)

    prompt = f"""Ты личный CS2 тренер. Игрок только что открыл свой профиль.
Напиши им КОНКРЕТНЫЙ разбор — упомяни реальные карты, реальные цифры из их статистики.
Не пиши общие советы — только то что видишь в данных.

Данные: K/D={req.kd}, WR={req.winrate}%, HS%={req.hs}, Матчей={req.matches}, FACEIT lvl={req.faceit_level or "нет"}, ELO={req.faceit_elo or "нет"}{maps_text}{recent_text}

Верни ТОЛЬКО JSON без markdown:
{{"verdict":"2-3 предложения — конкретный вывод с упоминанием реальных карт и цифр из данных выше",
"strengths":["конкретная сильная сторона с цифрой","конкретная сильная сторона с цифрой"],
"problems":["конкретная проблема с цифрой или картой","конкретная проблема","конкретная проблема"],
"priority":"одно конкретное действие прямо сейчас — с привязкой к слабой карте или стате",
"role":"ENTRY / SUPPORT / RIFLER / LURKER / AWP — угадай по статам",
"roast":"короткая честная фраза суровый тренер, без оскорблений но прямо — упомяни конкретную слабость",
"best_map":{{"name":"название лучшей карты","wr":"WR% цифра","tip":"одна фраза — что делать на этой карте"}},
"worst_map":{{"name":"название худшей карты","wr":"WR% цифра","tip":"убрать из пула или конкретный совет"}}
}}"""

    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.post("https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization":f"Bearer {GROQ_KEY}","Content-Type":"application/json"},
            json={"model":"llama-3.3-70b-versatile",
                "messages":[{"role":"system","content":"Ты тренер по CS2. Отвечай ТОЛЬКО валидным JSON без markdown. Всегда упоминай конкретные цифры и карты из данных."},
                             {"role":"user","content":prompt}],
                "temperature":0.75,
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
    system = f"Ты личный CS2 тренер-аналитик. {context} Если игрок просит полный структурированный разбор — скажи что его можно получить во вкладке ТРЕНЕР на сайте. В чате давай краткие конкретные советы."
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



# ── YooMoney платежи ──────────────────────────────────────────────────────────
class PaymentReq(BaseModel):
    steamid: str
    plan: str = "month"
    promo: Optional[str] = ""   # промокод

@app.post("/payment/create")
async def payment_create(req: PaymentReq):
    if not YOO_SECRET:
        raise HTTPException(status_code=503, detail="Платежи временно недоступны")
    base = 299 if req.plan == "month" else 1990
    final = base
    promo_applied = None

    # Применяем промокод если есть
    if req.promo:
        code = req.promo.upper().strip()
        if code in promo_codes:
            p = promo_codes[code]
            if p["uses_left"] > 0 and req.steamid not in p.get("used_by",[]):
                discount = p.get("discount", 0)
                final = max(1, round(base * (1 - discount / 100)))
                promo_applied = {"code": code, "discount": discount}
                # Если 100% — активируем бесплатно без платежа
                if discount >= 100:
                    p["uses_left"] -= 1
                    p["used_by"].append(req.steamid)
                    activate_pro(req.steamid, req.plan, f"promo_{code}")
                    log_admin("PRO через промокод 100%", f"code={code} steamid={req.steamid}")
                    return {"ok": True, "free": True, "message": "PRO активирован бесплатно!"}

    amount = f"{final}.00"
    order_id = f"{req.steamid}_{int(time.time())}"
    pending_payments[order_id] = {
        "steamid": req.steamid, "plan": req.plan,
        "amount": amount, "promo": promo_applied
    }

    import base64
    creds = base64.b64encode(f"{YOO_SHOP_ID}:{YOO_SECRET}".encode()).decode()
    desc = f"CS2 AI Тренер PRO — {req.plan}"
    if promo_applied:
        desc += f" (скидка {promo_applied['discount']}%)"
    payload = {
        "amount": {"value": amount, "currency": "RUB"},
        "confirmation": {
            "type": "redirect",
            "return_url": f"{FRONTEND_URL}?payment=success&order={order_id}"
        },
        "capture": True,
        "description": desc,
        "metadata": {"steamid": req.steamid, "plan": req.plan, "order_id": order_id},
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://api.yookassa.ru/v3/payments",
            headers={
                "Authorization": f"Basic {creds}",
                "Content-Type": "application/json",
                "Idempotence-Key": order_id,
            },
            json=payload
        )
    data = r.json()
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=data.get("description","Ошибка ЮКассы"))
    pay_url = data.get("confirmation", {}).get("confirmation_url", "")
    payment_id = data.get("id", "")
    pending_payments[order_id]["payment_id"] = payment_id
    pending_payments[payment_id] = pending_payments[order_id]

    # Помечаем промокод как использованный после создания платежа
    if promo_applied:
        code = promo_applied["code"]
        promo_codes[code]["uses_left"] -= 1
        promo_codes[code]["used_by"].append(req.steamid)

    return {"url": pay_url, "order_id": order_id, "final_price": final, "promo": promo_applied}

@app.post("/payment/webhook")
async def payment_webhook(request: Request):
    """ЮКасса webhook уведомление"""
    try:
        data = await request.json()
    except:
        return HTMLResponse("BAD REQUEST", status_code=400)

    event = data.get("event", "")
    obj   = data.get("object", {})

    # Нас интересует только успешный платёж
    if event != "payment.succeeded":
        return HTMLResponse("OK")

    payment_id = obj.get("id", "")
    metadata   = obj.get("metadata", {})
    steamid    = metadata.get("steamid", "")
    plan       = metadata.get("plan", "month")
    order_id   = metadata.get("order_id", "")

    # Проверяем что платёж реально succeeded
    if obj.get("status") != "succeeded":
        return HTMLResponse("OK")

    if steamid and order_id:
        activate_pro(steamid, plan, order_id)
        # Чистим pending
        pending_payments.pop(order_id, None)
        pending_payments.pop(payment_id, None)
        amount = obj.get("amount", {}).get("value", "?")
        await _tg_send_bg(f"💰 Оплата получена!\nSteam: {steamid}\nПлан: {plan}\nСумма: {amount} руб\nOrder: {order_id}")

    return HTMLResponse("OK")

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
    _save("pro_users", pro_users)
    return {"ok": True, "message": "Pro активирован!"}

@app.get("/pro/{steamid}")
async def pro_status(steamid: str):
    usage = check_usage(steamid)
    result = {"pro": usage["pro"], "remaining": usage["remaining"], "limit": FREE_LIMIT}
    if usage["pro"] and steamid in pro_users:
        result["data"] = pro_users[steamid]
    return result

@app.post("/pro/restore")
async def pro_restore(request: Request):
    """Восстанавливает PRO из кеша фронтенда если бэкенд потерял данные после рестарта"""
    try:
        data = await request.json()
        steamid = data.get("steamid","")
        pro_data = data.get("pro_data", {})
        if not steamid or not pro_data:
            return {"ok": False, "reason": "no data"}
        # Если уже есть PRO — ничего не делаем
        if steamid in pro_users:
            return {"ok": True, "already": True}
        # Проверяем что данные выглядят валидно
        activated_at = pro_data.get("activated_at", 0)
        plan = pro_data.get("plan", "")
        order = pro_data.get("order", "")
        if not activated_at or not plan:
            return {"ok": False, "reason": "invalid data"}
        # Проверяем что подписка не истекла
        days = 365 if plan=="year" else 30 if plan=="month" else 0
        if days > 0:
            expires = activated_at * 1000 + days * 86400 * 1000
            import time as _t
            if _t.time() * 1000 > expires:
                return {"ok": False, "reason": "expired"}
        # Восстанавливаем
        pro_users[steamid] = {
            "key": pro_data.get("key", f"RESTORED-{steamid[:8]}"),
            "activated_at": activated_at,
            "plan": plan,
            "order": order or f"restored_{int(time.time())}"
        }
        _save("pro_users", pro_users)
        log_admin("PRO восстановлен из кеша", f"steamid={steamid} plan={plan} order={order}")
        return {"ok": True, "restored": True}
    except Exception as e:
        return {"ok": False, "reason": str(e)}

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
    _save("pro_keys", pro_keys)
    log_admin("Ключи сгенерированы", f"количество={n}")
    return {"keys": keys}

@app.get("/admin/keys/list")
async def list_keys(request: Request):
    token = request.headers.get("x-admin-token","")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"keys": pro_keys, "pro_users": pro_users}

@app.post("/admin/grant-pro")
async def grant_pro(request: Request):
    token = request.headers.get("x-admin-token","")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    steamid = data.get("steamid","")
    plan = data.get("plan","manual")
    if not steamid:
        raise HTTPException(status_code=400, detail="steamid required")
    activate_pro(steamid, plan, f"manual_{int(time.time())}")
    return {"ok": True, "message": f"PRO выдан для {steamid}"}

@app.post("/admin/revoke-pro")
async def revoke_pro(request: Request):
    token = request.headers.get("x-admin-token","")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    steamid = data.get("steamid","")
    if steamid in pro_users:
        del pro_users[steamid]
        _save("pro_users", pro_users)
        log_admin("PRO отозван", f"steamid={steamid}")
    return {"ok": True}

@app.get("/admin/stats")
async def admin_stats(token: str = ""):
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    now = int(time.time())
    day = 86400
    all_users = set()
    all_users.update(user_visits.keys())
    all_users.update(analysis_history.keys())
    all_users.update(e.get("steamid","") for e in leaderboard if e.get("steamid"))
    all_users.update(pro_users.keys())
    all_users.discard("")
    analyses_today = sum(1 for sid, hist in analysis_history.items() for h in hist if now - h.get("timestamp",0) < day)
    new_users_today = sum(1 for sid, v in user_visits.items() if now - v.get("first_seen",0) < day)
    return {
        "total_users": len(all_users),
        "pro_users_count": len(pro_users),
        "leaderboard_count": len(leaderboard),
        "analyses_today": analyses_today,
        "new_users_today": new_users_today,
        "total_keys": len(pro_keys),
        "unused_keys": sum(1 for k,v in pro_keys.items() if not v.get("used")),
        "promo_codes_count": len(promo_codes),
        "pending_payments": len(pending_payments),
        "banned_count": len(banned_users),
    }

@app.get("/admin/logs")
async def admin_get_logs(token: str = ""):
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"logs": admin_logs[:100]}

@app.get("/admin/users")
async def admin_users(token: str = "", limit: int = 100):
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # Собираем всех пользователей из всех источников
    all_steamids = set()
    all_steamids.update(user_visits.keys())
    all_steamids.update(analysis_history.keys())
    all_steamids.update(e.get("steamid","") for e in leaderboard if e.get("steamid"))
    all_steamids.update(pro_users.keys())
    all_steamids.discard("")
    
    users = []
    for steamid in all_steamids:
        visit = user_visits.get(steamid, {})
        hist  = analysis_history.get(steamid, [])
        lb_entry = next((e for e in leaderboard if e.get("steamid")==steamid), {})
        last_ts = max(
            visit.get("last_seen", 0),
            hist[0].get("timestamp", 0) if hist else 0
        )
        users.append({
            "steamid": steamid,
            "username": visit.get("username") or lb_entry.get("username",""),
            "avatar": visit.get("avatar") or lb_entry.get("avatar",""),
            "country": visit.get("country",""),
            "steam_level": visit.get("steam_level"),
            "first_seen": visit.get("first_seen", 0),
            "last_seen": last_ts,
            "visit_count": visit.get("visit_count", 0),
            "analyses": len(hist),
            "is_pro": is_pro(steamid),
            "is_banned": steamid in banned_users,
            "ban_reason": banned_users.get(steamid,{}).get("reason",""),
            "kd": lb_entry.get("stats",{}).get("kd",""),
            "matches": lb_entry.get("stats",{}).get("matches",""),
            "faceit_level": lb_entry.get("level",""),
        })
    
    users.sort(key=lambda x: x["last_seen"], reverse=True)
    return {"users": users[:limit], "total": len(users)}

@app.post("/admin/ban-user")
async def ban_user(request: Request):
    token = request.headers.get("x-admin-token","")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    steamid = data.get("steamid","")
    reason  = data.get("reason","Нарушение правил")
    if not steamid:
        raise HTTPException(status_code=400, detail="steamid required")
    banned_users[steamid] = {"reason": reason, "banned_at": int(time.time())}
    _save("banned_users", banned_users)
    log_admin("Пользователь забанен", f"steamid={steamid} reason={reason}")
    return {"ok": True}

@app.post("/admin/unban-user")
async def unban_user(request: Request):
    token = request.headers.get("x-admin-token","")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    steamid = data.get("steamid","")
    if steamid in banned_users:
        del banned_users[steamid]
        _save("banned_users", banned_users)
    log_admin("Пользователь разбанен", f"steamid={steamid}")
    return {"ok": True}

@app.get("/check-ban/{steamid}")
async def check_ban(steamid: str):
    if steamid in banned_users:
        b = banned_users[steamid]
        raise HTTPException(status_code=403, detail=f"Аккаунт заблокирован. Причина: {b.get('reason','')}")
    return {"ok": True}



class PromoReq(BaseModel):
    code: str
    steamid: str

@app.post("/admin/promo/create")
async def create_promo(request: Request):
    token = request.headers.get("x-admin-token","")
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    data = await request.json()
    code = data.get("code", "").upper().strip()
    uses = int(data.get("uses", 1))
    plan = data.get("plan", "month")
    discount = min(100, max(0, int(data.get("discount", 0))))  # скидка в процентах 0-100
    if not code:
        code = "PROMO-" + "".join(secrets.choice(string.ascii_uppercase+string.digits) for _ in range(6))
    promo_codes[code] = {
        "uses_left": uses, "plan": plan, "discount": discount,
        "used_by": [], "created_at": int(time.time())
    }
    log_admin("Промокод создан", f"code={code} uses={uses} plan={plan} discount={discount}%")
    return {"ok": True, "code": code}

@app.get("/promo/check")
async def check_promo(code: str, plan: str = "month"):
    """Проверить промокод и вернуть итоговую сумму"""
    c = code.upper().strip()
    if c not in promo_codes:
        raise HTTPException(status_code=404, detail="Промокод не найден")
    p = promo_codes[c]
    if p["uses_left"] <= 0:
        raise HTTPException(status_code=400, detail="Промокод уже использован")
    base = 299 if plan == "month" else 1990
    discount = p.get("discount", 0)
    final = round(base * (1 - discount / 100))
    final = max(1, final)  # минимум 1 рубль
    return {
        "ok": True,
        "code": c,
        "discount": discount,
        "plan": plan,
        "base_price": base,
        "final_price": final,
        "is_free": discount >= 100,
    }

@app.post("/promo/activate")
async def activate_promo(req: PromoReq):
    code = req.code.upper().strip()
    if code not in promo_codes:
        raise HTTPException(status_code=404, detail="Промокод не найден")
    p = promo_codes[code]
    if p["uses_left"] <= 0:
        raise HTTPException(status_code=400, detail="Промокод уже использован")
    if req.steamid in p["used_by"]:
        raise HTTPException(status_code=400, detail="Вы уже использовали этот промокод")
    discount = p.get("discount", 0)
    if discount < 100:
        raise HTTPException(status_code=400, detail="Этот промокод не даёт бесплатный доступ — используй его при оплате")
    # 100% скидка — активируем PRO бесплатно
    p["uses_left"] -= 1
    p["used_by"].append(req.steamid)
    activate_pro(req.steamid, p["plan"], f"promo_{code}")
    log_admin("Промокод активирован (бесплатно)", f"code={code} steamid={req.steamid}")
    return {"ok": True, "message": "PRO активирован через промокод!"}

@app.get("/admin/promos")
async def list_promos(token: str = ""):
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"promos": promo_codes}

class SupportReq(BaseModel):
    message: str
    steamid: Optional[str] = ""
    username: Optional[str] = "Аноним"

async def tg_send(text: str, chat_id: str = None, markup=None):
    if not TG_BOT_TOKEN: return
    cid = chat_id or TG_ADMIN_ID
    if not cid: return
    body = {"chat_id": cid, "text": text, "parse_mode": "HTML"}
    if markup: body["reply_markup"] = markup
    try:
        async with httpx.AsyncClient(timeout=12) as c:
            await c.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", json=body)
    except Exception:
        pass

async def _tg_send_bg(text: str, chat_id: str = None, markup=None):
    asyncio.create_task(tg_send(text, chat_id=chat_id, markup=markup))

@app.post("/support")
async def support_msg(req: SupportReq):
    sid = req.steamid or "anon"
    ts  = int(time.time())
    if sid not in support_sessions:
        support_sessions[sid] = {"username": req.username, "msgs": []}

    # Дедупликация
    recent = support_sessions[sid]["msgs"]
    if recent:
        last = recent[-1]
        if last["from"] == "user" and last["text"] == req.message and (ts - last["ts"]) < 10:
            return {"ok": True}

    support_sessions[sid]["msgs"].append({"from":"user","text":req.message,"ts":ts})

    # Собираем полную инфу об игроке
    pro_info = pro_users.get(sid)
    visit_info = user_visits.get(sid, {})

    pro_status = "❌ Нет PRO"
    if pro_info:
        activated = pro_info.get("activated_at", 0)
        plan = pro_info.get("plan","?")
        order = pro_info.get("order","?")
        days_ago = max(0, (ts - activated) // 86400) if activated else "?"
        plan_days = 365 if plan == "year" else 30 if plan == "month" else 0
        days_left = max(0, plan_days - days_ago) if plan_days else "∞"
        activated_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(activated)) if activated else "?"
        pro_status = (
            f"✅ PRO активен\n"
            f"   📋 План: {plan}\n"
            f"   📅 Активирован: {activated_str}\n"
            f"   ⏳ Осталось: {days_left} дн.\n"
            f"   🧾 Заказ: {order}"
        )

    visit_str = ""
    if visit_info:
        last_seen = visit_info.get("last_seen", 0)
        first_seen = visit_info.get("first_seen", 0)
        visit_count = visit_info.get("visit_count", 0)
        last_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(last_seen)) if last_seen else "?"
        first_str = time.strftime("%d.%m.%Y", time.localtime(first_seen)) if first_seen else "?"
        visit_str = f"\n👁 Визитов: {visit_count} · Первый: {first_str} · Последний: {last_str}"

    # История оплат из analysis_history
    hist = analysis_history.get(sid, [])
    hist_str = f"\n🎮 Анализов запрошено: {len(hist)}" if hist else ""

    users_count = len(support_sessions)
    text = (
        f"💬 <b>Поддержка · {req.username}</b>\n"
        f"Steam: <code>{sid}</code>\n"
        f"{'🔗 https://steamcommunity.com/profiles/'+sid if sid!='anon' else ''}\n\n"
        f"📩 <b>Сообщение:</b>\n{req.message}\n\n"
        f"─────────────────\n"
        f"<b>Статус игрока:</b>\n{pro_status}"
        f"{visit_str}"
        f"{hist_str}\n\n"
        f"<i>Активных диалогов: {users_count}</i>"
    )
    markup = {"inline_keyboard":[[
        {"text":f"✏️ Ответить {req.username}","callback_data":f"reply:{sid}"},
        {"text":"👥 Все диалоги","callback_data":"list_users"},
    ],[
        {"text":"📊 Профиль игрока","callback_data":f"profile:{sid}"},
        {"text":"⚡ Выдать PRO","callback_data":f"grant:{sid}"},
    ]]}
    await _tg_send_bg(text, markup=markup)
    return {"ok": True}

@app.get("/support/poll/{steamid}")
async def support_poll(steamid: str, since: int = 0):
    sess = support_sessions.get(steamid, {"msgs":[]})
    new_msgs = [m for m in sess["msgs"] if m["ts"] > since and m["from"]=="admin"]
    return {"messages": new_msgs}

@app.get("/support/history/{steamid}")
async def support_history(steamid: str):
    """Возвращает историю чата поддержки для синхронизации между устройствами"""
    sess = support_sessions.get(steamid, {"msgs":[]})
    # Возвращаем только сообщения от обеих сторон (не системные)
    msgs = [m for m in sess.get("msgs", []) if m.get("text")]
    return {"messages": msgs, "username": sess.get("username","")}

@app.post("/telegram/webhook")
async def tg_webhook(request: Request):
    try: data = await request.json()
    except: return {"ok":True}

    # Callback кнопки
    if "callback_query" in data:
        cb   = data["callback_query"]
        cid  = str(cb["from"]["id"])
        cbd  = cb.get("data","")
        msg_id = cb["message"]["message_id"]

        # Подтверждаем нажатие
        async with httpx.AsyncClient(timeout=5) as c:
            await c.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery",
                json={"callback_query_id": cb["id"]})

        if cbd.startswith("reply:"):
            steamid = cbd[6:]
            admin_active[cid] = steamid
            sess = support_sessions.get(steamid, {})
            uname = sess.get("username","?")
            # Показываем историю переписки
            msgs = sess.get("msgs", [])[-10:]
            history = ""
            for m in msgs:
                who = "👤 Игрок" if m["from"]=="user" else "👨‍💼 Ты"
                history += f"\n{who}: {m['text']}"
            msg_text = (
                f"\u270f\ufe0f Отвечаешь <b>{uname}</b>\n"
                f"Steam: <code>{steamid}</code>\n"
                f"{'─'*25}\n"
                f"<b>История ({len(msgs)} сообщ.):</b>"
                f"{history}\n"
                f"{'─'*25}\n"
                "Напиши ответ — придёт на сайт мгновенно.\n"
                "/stop — выйти | /users — все диалоги"
            )
            await tg_send(msg_text, chat_id=cid)

        elif cbd == "list_users":
            if not support_sessions:
                await tg_send("Нет активных диалогов", chat_id=cid)
            else:
                rows = []
                for sid, sess in list(support_sessions.items())[-10:]:
                    last = sess["msgs"][-1]["text"][:40] if sess["msgs"] else "—"
                    pro_mark = "⚡" if sid in pro_users else ""
                    rows.append([{"text":f"{pro_mark}👤 {sess['username']}: {last}","callback_data":f"reply:{sid}"}])
                await tg_send("👥 <b>Активные диалоги:</b>", chat_id=cid,
                    markup={"inline_keyboard":rows})

        elif cbd.startswith("grant:"):
            target_sid = cbd[6:]
            if target_sid in pro_users:
                uname = user_visits.get(target_sid, {}).get("username", target_sid)
                await tg_send(f"ℹ️ У {uname} уже есть PRO\n<code>{target_sid}</code>", chat_id=cid)
            else:
                activate_pro(target_sid, "manual", f"tg_grant_{int(time.time())}")
                uname = user_visits.get(target_sid, {}).get("username", target_sid)
                log_admin("PRO выдан через TG", f"steamid={target_sid} by_admin={cid}")
                await tg_send(
                    f"✅ PRO выдан игроку <b>{uname}</b>\n<code>{target_sid}</code>\n"
                    f"Plan: manual · Выдан вручную из поддержки", chat_id=cid)

        elif cbd.startswith("profile:"):
            target_sid = cbd[8:]
            pro_info = pro_users.get(target_sid)
            visit_info = user_visits.get(target_sid, {})
            uname = visit_info.get("username", target_sid)
            avatar = visit_info.get("avatar","")
            visits = visit_info.get("visit_count", 0)
            first = time.strftime("%d.%m.%Y", time.localtime(visit_info.get("first_seen",0))) if visit_info.get("first_seen") else "?"
            last  = time.strftime("%d.%m.%Y %H:%M", time.localtime(visit_info.get("last_seen",0))) if visit_info.get("last_seen") else "?"
            analyses = len(analysis_history.get(target_sid, []))
            pro_str = "❌ Нет PRO"
            if pro_info:
                plan = pro_info.get("plan","?")
                activated = pro_info.get("activated_at",0)
                order = pro_info.get("order","?")
                activated_str = time.strftime("%d.%m.%Y", time.localtime(activated)) if activated else "?"
                pro_str = f"✅ PRO · {plan} · с {activated_str} · заказ {order}"
            profile_text = (
                f"👤 <b>{uname}</b>\n"
                f"<code>{target_sid}</code>\n"
                f"🔗 steamcommunity.com/profiles/{target_sid}\n\n"
                f"📊 PRO: {pro_str}\n"
                f"👁 Визитов: {visits} · Первый: {first} · Последний: {last}\n"
                f"🤖 AI анализов: {analyses}\n"
                f"🚫 Забанен: {'да' if target_sid in banned_users else 'нет'}"
            )
            markup = {"inline_keyboard":[[
                {"text":"⚡ Выдать PRO","callback_data":f"grant:{target_sid}"},
                {"text":"🚫 Забанить","callback_data":f"ban:{target_sid}"},
            ]]}
            await tg_send(profile_text, chat_id=cid, markup=markup)

        elif cbd.startswith("ban:"):
            target_sid = cbd[4:]
            if target_sid not in banned_users:
                banned_users[target_sid] = {"reason":"Support ban", "banned_at":int(time.time())}
                _save("banned_users", banned_users)
                uname = user_visits.get(target_sid, {}).get("username", target_sid)
                log_admin("Бан через TG", f"steamid={target_sid}")
                await tg_send(f"🚫 Игрок <b>{uname}</b> забанен\n<code>{target_sid}</code>", chat_id=cid)
            else:
                await tg_send(f"ℹ️ Уже забанен", chat_id=cid)

        return {"ok":True}

    # Текстовые сообщения от админа
    if "message" in data:
        msg  = data["message"]
        cid  = str(msg.get("from",{}).get("id",""))
        text = msg.get("text","")

        if text == "/users":
            if not support_sessions:
                await tg_send("Нет диалогов", chat_id=cid)
            else:
                rows = [[{"text":f"👤 {s['username']}","callback_data":f"reply:{sid}"}]
                        for sid,s in list(support_sessions.items())[-10:]]
                await tg_send("👥 Выбери пользователя:", chat_id=cid, markup={"inline_keyboard":rows})
            return {"ok":True}

        if text == "/stop":
            admin_active.pop(cid, None)
            await tg_send("❌ Вышел из режима ответа", chat_id=cid)
            return {"ok":True}

        # Отправляем ответ пользователю
        steamid = admin_active.get(cid)
        if steamid and steamid in support_sessions:
            ts = int(time.time())
            support_sessions[steamid]["msgs"].append({"from":"admin","text":text,"ts":ts})
            uname = support_sessions[steamid]["username"]
            await tg_send(f"✅ Ответ отправлен <b>{uname}</b>", chat_id=cid)
        elif cid == TG_ADMIN_ID:
            await tg_send("Выбери пользователя через /users или кнопку 'Ответить'", chat_id=cid)

    return {"ok":True}


from fastapi.responses import Response as FastResponse

MAP_NAMES = {
    "mirage":"de_mirage","inferno":"de_inferno","dust2":"de_dust2",
    "nuke":"de_nuke","ancient":"de_ancient","anubis":"de_anubis",
    "vertigo":"de_vertigo","overpass":"de_overpass",
}

@app.get("/map-radar/{mapname}")
async def map_radar(mapname: str):
    key = mapname.lower()
    de_name = MAP_NAMES.get(key, f"de_{key}")
    url = f"https://cdn.cloudflare.steamstatic.com/apps/csgo/maps/{de_name}_radar.png"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers={"User-Agent":"Mozilla/5.0"})
    if r.status_code == 200:
        return FastResponse(content=r.content, media_type="image/png",
            headers={"Cache-Control":"public, max-age=86400"})
    return FastResponse(status_code=404)

@app.get("/health")
def health():
    return {"status": "ok", "ts": int(time.time())}

# ── Steam Match History via Auth Code ────────────────────────────────────────
# Хранилище auth кодов (steamid → {auth_code, last_match_code})
steam_auth_codes: dict = {}

class SteamAuthReq(BaseModel):
    steamid: str
    auth_code: str       # код вида XXXX-XXXXX-XXXX из Steam Support
    match_code: str      # код последнего матча вида CSGO-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX

@app.post("/steam/auth-code")
async def save_steam_auth(req: SteamAuthReq):
    """Сохраняем auth код пользователя и сразу загружаем матчи"""
    if not STEAM_API_KEY:
        raise HTTPException(status_code=503, detail="Steam API недоступен")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            "https://api.steampowered.com/ICSGOPlayers_730/GetNextMatchSharingCode/v1",
            params={"key": STEAM_API_KEY, "steamid": req.steamid,
                    "steamidkey": req.auth_code, "knowncode": req.match_code}
        )
        if r.status_code == 403:
            raise HTTPException(status_code=400, detail="Неверный код аутентификации или код матча.")
        if r.status_code != 200:
            raise HTTPException(status_code=400, detail=f"Ошибка Steam API: {r.status_code}")
        next_code = r.json().get("result", {}).get("nextcode", "")

    steam_auth_codes[req.steamid] = {
        "auth_code": req.auth_code, "last_code": req.match_code,
        "next_code": next_code, "saved_at": int(time.time())
    }
    return {"ok": True, "has_more": bool(next_code and next_code != "n/a")}

@app.post("/steam/auto-connect")
async def auto_connect_steam(request: Request):
    """Подключение только по auth_code — последний матч ищем автоматически через MM Stats"""
    if not STEAM_API_KEY:
        raise HTTPException(status_code=503, detail="Steam API недоступен")
    data = await request.json()
    steamid = data.get("steamid","")
    auth_code = data.get("auth_code","").strip()
    if not steamid or not auth_code:
        raise HTTPException(status_code=400, detail="Нужен steamid и auth_code")

    # Получаем последние матчи через GetUserMatchHistory (без match code)
    # Используем GetRecentlyPlayedGames + ICSGOPlayers для начального кода
    async with httpx.AsyncClient(timeout=15) as client:
        # Пробуем получить хоть какой-то sharing code из последних матчей
        # Для этого запрашиваем GetNextMatchSharingCode с начальным кодом "0"
        # Это недокументированная фича — иногда работает
        test_codes = ["CSGO-AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"]  # заглушка

        # Реальная валидация: проверяем что auth_code корректный через GetAccountPublicInfo
        r_check = await client.get(
            "https://api.steampowered.com/ICSGOPlayers_730/GetNextMatchSharingCode/v1",
            params={"key": STEAM_API_KEY, "steamid": steamid,
                    "steamidkey": auth_code, "knowncode": "CSGO-AAAAA-AAAAA-AAAAA-AAAAA-AAAAA"}
        )
        # 403 = неверный auth_code, 412 = неверный match code (но auth валидный!)
        if r_check.status_code == 403:
            raise HTTPException(status_code=400, detail="Неверный код аутентификации. Проверь код на странице Steam Support.")

        # Если 412 — auth_code верный, просто match_code неверный
        # Это нормально — сохраняем без начального матча, будем обновлять
        next_code = ""
        if r_check.status_code == 200:
            next_code = r_check.json().get("result", {}).get("nextcode", "")

    # Сохраняем — без начального match_code, будем грузить матчи начиная с доступных
    steam_auth_codes[steamid] = {
        "auth_code": auth_code,
        "last_code": next_code or "CSGO-AAAAA-AAAAA-AAAAA-AAAAA-AAAAA",
        "saved_at": int(time.time()),
        "auto_connected": True
    }
    return {"ok": True, "match_code": next_code or "auto"}


@app.get("/steam/matches/{steamid}")
async def get_steam_matches(steamid: str, limit: int = 10):
    """Получаем историю матчей через sharing codes — быстро, без скачивания демок"""
    if steamid not in steam_auth_codes:
        raise HTTPException(status_code=404, detail="Auth код не найден.")
    if not STEAM_API_KEY:
        raise HTTPException(status_code=503, detail="Steam API недоступен")

    info = steam_auth_codes[steamid]
    auth_code = info["auth_code"]
    matches = []
    current_code = info["last_code"]

    async with httpx.AsyncClient(timeout=10) as client:
        for _ in range(limit):
            if not current_code or current_code == "n/a":
                break

            decoded = decode_sharing_code(current_code)
            match_id = decoded.get("matchid", "")

            matches.append({
                "code": current_code,
                "match_id": match_id,
                "map": "",   # карту без демки получить нельзя
            })

            # Следующий код
            r = await client.get(
                "https://api.steampowered.com/ICSGOPlayers_730/GetNextMatchSharingCode/v1",
                params={"key": STEAM_API_KEY, "steamid": steamid,
                        "steamidkey": auth_code, "knowncode": current_code}
            )
            if r.status_code != 200:
                break
            next_code = r.json().get("result", {}).get("nextcode", "")
            if not next_code or next_code == "n/a" or next_code == current_code:
                break
            current_code = next_code

    if current_code and current_code != info["last_code"]:
        steam_auth_codes[steamid]["last_code"] = current_code

    return {"matches": matches, "total": len(matches), "has_auth": True}

@app.get("/steam/has-auth/{steamid}")
async def check_steam_auth(steamid: str):
    """Проверить есть ли сохранённый auth код"""
    has = steamid in steam_auth_codes
    return {"has_auth": has, "saved_at": steam_auth_codes.get(steamid, {}).get("saved_at")}

def decode_sharing_code(code: str) -> dict:
    """Декодирует CS2 match sharing code"""
    try:
        chars = "ABCDEFGHJKLMNOPQRSTUVWXYZabcdefhjkmnopqrstuvwxyz23456789"
        clean = code.replace("CSGO-", "").replace("-", "")
        n = 0
        for c in reversed(clean):
            if c not in chars:
                continue
            n = n * len(chars) + chars.index(c)
        # Структура: 64 бита matchId + 64 бита reservationId + 16 бит tvPort
        # Упакованы в little-endian как 144-битное число
        # Корректное декодирование по спеке akiver/csgo-sharecode
        tv_port  = n & 0xFFFF
        n >>= 16
        res_id   = n & 0xFFFFFFFFFFFFFFFF
        n >>= 64
        match_id = n & 0xFFFFFFFFFFFFFFFF
        return {
            "matchid": str(match_id),
            "reservationid": str(res_id),
            "tvport": tv_port,
        }
    except Exception as e:
        return {"matchid": "", "reservationid": "", "tvport": 0, "error": str(e)}


@app.get("/admin/backup")
def admin_backup(token: str = ""):
    """Отдаёт все данные одним JSON — для GitHub Action backup"""
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")
    return {
        "ts": int(time.time()),
        "leaderboard": leaderboard,
        "pro_users": pro_users,
        "pro_keys": pro_keys,
        "ai_usage": ai_usage,
        "banned_users": banned_users,
    }

@app.post("/admin/restore")
async def admin_restore(request: Request, token: str = ""):
    """Восстанавливает данные из backup"""
    if token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")
    global leaderboard, pro_users, pro_keys, ai_usage, banned_users
    data = await request.json()
    if "leaderboard" in data:
        leaderboard = data["leaderboard"]; _save("leaderboard", leaderboard)
    if "pro_users" in data:
        pro_users = data["pro_users"]; _save("pro_users", pro_users)
    if "pro_keys" in data:
        pro_keys = data["pro_keys"]; _save("pro_keys", pro_keys)
    if "ai_usage" in data:
        ai_usage = data["ai_usage"]; _save("ai_usage", ai_usage)
    if "banned_users" in data:
        banned_users = data["banned_users"]; _save("banned_users", banned_users)
    return {"ok": True, "restored": list(data.keys())}

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

    # Считаем рейтинг
    avg_by_level = [
        {"kd":0.75,"hs":28,"wr":43},{"kd":0.82,"hs":30,"wr":44},{"kd":0.92,"hs":33,"wr":46},
        {"kd":1.00,"hs":36,"wr":48},{"kd":1.06,"hs":38,"wr":49},{"kd":1.12,"hs":40,"wr":50},
        {"kd":1.20,"hs":42,"wr":51},{"kd":1.28,"hs":44,"wr":52},{"kd":1.38,"hs":46,"wr":53},
        {"kd":1.52,"hs":48,"wr":54},{"kd":1.72,"hs":52,"wr":56},
    ]
    import math
    avg = avg_by_level[min(int(lvl or 0), 10)]
    def sig(v, a):
        try: return min(99, max(1, round(100/(1+math.exp(-4*(float(v)/float(a)-1))))))
        except: return 50
    kd_f  = float(kd)  if kd  and kd  != "—" else 0
    hs_f  = float(hs)  if hs  and hs  != "—" else 0
    wr_f  = float(wr)  if wr  and wr  != "—" else 0
    rating = min(99, round(sig(kd_f,avg["kd"])*0.45 + sig(hs_f,avg["hs"])*0.25 + sig(wr_f,avg["wr"])*0.30))
    r_color = "#55ee55" if rating>=70 else "#f5c518" if rating>=45 else "#ff8844"
    r_label = "ТОП ИГРОК" if rating>=80 else "ВЫШЕ СРЕДНЕГО" if rating>=60 else "СРЕДНИЙ УРОВЕНЬ" if rating>=40 else "ЕСТЬ КУДА РАСТИ"

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{username} · CS2 AI Тренер</title>
  <meta property="og:title" content="{username} — CS2 Coach Rating {rating}/100">
  <meta property="og:description" content="Рейтинг {rating}/100 · {r_label} · K/D {kd} · WR {wr}% · HS {hs}% · {'FACEIT LVL '+str(lvl)+' · '+str(elo)+' ELO' if elo else 'Steam игрок'}">
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
    .avatar{{width:72px;height:72px;border-radius:4px;border:2px solid {lc};object-fit:cover;}}
    .avatar-ph{{width:72px;height:72px;border-radius:4px;border:2px solid {lc};background:#1a1a10;
      display:flex;align-items:center;justify-content:center;font-size:28px;}}
    .name{{font-size:20px;color:#f5eed8;font-weight:700;margin-bottom:4px;}}
    .meta{{font-size:12px;color:#9a9270;}}
    .rating-block{{display:flex;align-items:center;gap:16px;margin:20px 0;
      background:#0d0d09;border:1px solid #2e2e1e;padding:16px 20px;}}
    .rating-num{{font-size:52px;color:{r_color};font-weight:900;line-height:1;}}
    .rating-label{{font-size:14px;color:{r_color};font-weight:700;margin-bottom:4px;}}
    .rating-sub{{font-size:12px;color:#6a6450;}}
    .stats{{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px;}}
    .stat{{padding:12px 8px;text-align:center;background:#0d0d09;border:1px solid #2e2e1e;}}
    .sl{{font-size:10px;color:#9a9270;letter-spacing:1px;margin-bottom:4px;}}
    .sv{{font-size:20px;color:#f5c518;font-weight:700;}}
    .verdict{{background:#1a1a0e;border-left:3px solid #f5c518;padding:12px 16px;margin:0 0 16px;
      font-size:13px;color:#c8bc98;line-height:1.6;font-style:italic;}}
    .cta{{display:block;text-align:center;padding:14px 24px;background:#f5c518;
      color:#080807;text-decoration:none;font-weight:700;font-size:14px;letter-spacing:2px;}}
    .badge{{display:inline-flex;align-items:center;gap:8px;background:{lc}18;
      border:1px solid {lc}44;padding:5px 12px;margin:8px 0 16px;}}
  </style>
</head>
<body>
  <div class="card">
    <div class="glow"></div>
    <div style="display:flex;gap:16px;align-items:center;margin-bottom:16px;">
      {"<img class='avatar' src='"+avatar+"' alt=''/>" if avatar else "<div class='avatar-ph'>👤</div>"}
      <div style="flex:1;">
        <div class="name">{username}</div>
        <div class="meta">{"Steam · " + str(profile.get("steam_level","")) + " lvl" if profile.get("steam_level") else "Steam"}</div>
        {f'<div class="badge"><span style="font-size:11px;color:#9a9270;letter-spacing:2px;">FACEIT LVL {lvl}</span><span style="font-size:16px;color:{lc};font-weight:700;margin-left:4px;">{elo} ELO</span></div>' if elo else ''}
      </div>
    </div>
    <div class="rating-block">
      <div class="rating-num">{rating}</div>
      <div>
        <div class="rating-label">{r_label}</div>
        <div class="rating-sub">Лучше чем {rating}% игроков{" FACEIT "+str(lvl) if lvl else ""}</div>
        <div class="rating-sub" style="margin-top:3px;">CS2 Coach Rating</div>
      </div>
    </div>
    {f'<div class="verdict">"{verdict[:120]}{"..." if len(verdict)>120 else ""}"</div>' if verdict else ''}
    <div class="stats">
      <div class="stat"><div class="sl">K/D</div><div class="sv">{kd}</div></div>
      <div class="stat"><div class="sl">HS%</div><div class="sv">{hs}%</div></div>
      <div class="stat"><div class="sl">WIN%</div><div class="sv">{wr}%</div></div>
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
