"""
CS2 Coach Telegram Bot
Запускается как asyncio background task из main.py.
"""

import asyncio, httpx, json, os, time, logging, random

logger = logging.getLogger("tg_bot")
TG_BASE = "https://api.telegram.org/bot"
FACEIT_BASE = "https://open.faceit.com/data/v4"

# ── Helpers ───────────────────────────────────────────────────────────────────

async def tg_api(token: str, method: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{TG_BASE}{token}/{method}", json=kwargs)
            return r.json()
    except Exception as e:
        logger.warning(f"[tg_api] {method}: {e}")
        return {}

async def send_msg(token: str, chat_id, text: str, markup=None, parse_mode="HTML"):
    body = {"chat_id": str(chat_id), "text": text, "parse_mode": parse_mode}
    if markup:
        body["reply_markup"] = markup
    await tg_api(token, "sendMessage", **body)

async def answer_callback(token: str, callback_id: str):
    await tg_api(token, "answerCallbackQuery", callback_query_id=callback_id)

def elo_badge(elo):
    e = int(elo or 0)
    if e >= 2001: return "👑"
    if e >= 1751: return "💎"
    if e >= 1501: return "🟣"
    if e >= 1251: return "🔵"
    if e >= 1001: return "🟢"
    return "⚪"

def result_line(result, elo_ch):
    won = str(result) == "1"
    elo_str = (f"+{elo_ch}" if int(elo_ch) > 0 else str(elo_ch)) if elo_ch else "—"
    icon = "🏆 ПОБЕДА" if won else "💀 ПОРАЖЕНИЕ"
    elo_color = "📈" if int(elo_ch or 0) > 0 else "📉"
    return icon, elo_str, elo_color

# ── Main polling loop ─────────────────────────────────────────────────────────

async def run_bot(token, get_state, set_state, on_link, on_unlink,
                  get_faceit, get_groq_summary, groq_key, faceit_key):
    if not token:
        logger.info("[bot] TG_BOT_TOKEN_NOTIFY не задан — бот отключён")
        return
    offset = 0
    logger.info("[bot] Polling started")
    while True:
        try:
            data = await tg_api(token, "getUpdates",
                                timeout=30, offset=offset,
                                allowed_updates=["message", "callback_query"])
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    await handle_callback(token, upd["callback_query"], get_state, set_state,
                                          on_unlink, get_faceit, get_groq_summary)
                elif "message" in upd:
                    await handle_message(token, upd["message"], get_state, set_state,
                                         on_link, on_unlink, get_faceit, get_groq_summary)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[bot] polling error: {e}")
            await asyncio.sleep(5)
        await asyncio.sleep(1)

# ── Message handler ───────────────────────────────────────────────────────────

async def handle_message(token, msg, get_state, set_state,
                         on_link, on_unlink, get_faceit, get_groq_summary):
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()
    tg_users = get_state()
    steamid = next((s for s, u in tg_users.items() if str(u.get("chat_id")) == chat_id), None)

    if text.startswith("/start"):
        parts = text.split(" ", 1)
        deep_sid = parts[1].strip() if len(parts) > 1 else ""
        if deep_sid and deep_sid.isdigit():
            tg_users[deep_sid] = tg_users.get(deep_sid, {})
            tg_users[deep_sid].update({"chat_id": chat_id, "remind": True,
                                        "last_match_id": tg_users[deep_sid].get("last_match_id", "")})
            set_state(tg_users)
            on_link(deep_sid, chat_id)
            await send_msg(token, chat_id,
                "✅ <b>Готово! Telegram привязан к cs-coach.ru</b>\n\n"
                "Что я умею:\n"
                "🎮 Уведомление сразу после каждого матча FACEIT\n"
                "🤖 Краткий AI-разбор — что пошло не так\n"
                "⏰ Ежедневное напоминание потренироваться\n\n"
                "Команды:\n"
                "/stats — твоя статистика\n"
                "/match — разбор последнего матча\n"
                "/remind — вкл/выкл напоминания\n"
                "/unlink — отвязать\n\n"
                "Сыграй матч — я напишу сам 👊"
            )
        else:
            if steamid:
                fc = get_faceit(steamid)
                elo = fc.get("elo", "?") if fc else "?"
                lvl = fc.get("level", "?") if fc else "?"
                await send_msg(token, chat_id,
                    f"👋 Привет! Аккаунт уже привязан.\n"
                    f"⚡ FACEIT LVL {lvl} · {elo} ELO\n\n"
                    "/stats · /match · /remind · /unlink"
                )
            else:
                await send_msg(token, chat_id,
                    "👋 Привет! Я бот cs-coach.ru.\n\n"
                    "Чтобы привязать аккаунт — зайди на "
                    "<a href=\"https://cs-coach.ru\">cs-coach.ru</a> "
                    "→ Настройки → Подключения → Telegram."
                )
        return

    if not steamid:
        await send_msg(token, chat_id,
            "⚠️ Аккаунт не привязан.\n"
            "Зайди на <a href=\"https://cs-coach.ru\">cs-coach.ru</a> "
            "→ Настройки → Подключения → Telegram."
        )
        return

    if text == "/stats":
        await cmd_stats(token, chat_id, steamid, get_faceit)
    elif text == "/match":
        await send_msg(token, chat_id, "⏳ Загружаю последний матч...")
        await cmd_match(token, chat_id, steamid, get_faceit, get_groq_summary)
    elif text == "/remind":
        user = tg_users[steamid]
        new_val = not user.get("remind", True)
        user["remind"] = new_val
        set_state(tg_users)
        if new_val:
            await send_msg(token, chat_id, "🔔 Напоминания включены — буду писать каждый день в 19:00 МСК.")
        else:
            await send_msg(token, chat_id, "🔕 Напоминания выключены. Включить — /remind")
    elif text == "/unlink":
        await send_msg(token, chat_id,
            "Отвязать Telegram от cs-coach.ru?",
            markup={"inline_keyboard": [[
                {"text": "✅ Да, отвязать", "callback_data": f"unlink:{steamid}"},
                {"text": "❌ Отмена",        "callback_data": "cancel"},
            ]]}
        )
    else:
        await send_msg(token, chat_id,
            "Команды:\n"
            "/stats — статистика\n"
            "/match — последний матч\n"
            "/remind — напоминания\n"
            "/unlink — отвязать"
        )

# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_callback(token, cb, get_state, set_state,
                          on_unlink, get_faceit, get_groq_summary):
    await answer_callback(token, cb["id"])
    chat_id = str(cb["from"]["id"])
    data = cb.get("data", "")
    tg_users = get_state()

    if data.startswith("unlink:"):
        steamid = data[7:]
        if steamid in tg_users:
            del tg_users[steamid]
            set_state(tg_users)
            on_unlink(chat_id)
        await send_msg(token, chat_id, "✅ Telegram отвязан. Уведомления отключены.\nЗайди снова на cs-coach.ru если передумаешь.")
    elif data == "cancel":
        await send_msg(token, chat_id, "Отменено.")
    elif data.startswith("match_ai:"):
        steamid = data[9:]
        await send_msg(token, chat_id, "⏳ Делаю AI-разбор...")
        await cmd_match(token, chat_id, steamid, get_faceit, get_groq_summary)

# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_stats(token, chat_id, steamid, get_faceit):
    fc = get_faceit(steamid)
    if not fc:
        await send_msg(token, chat_id,
            "⚠️ <b>Нет данных FACEIT</b>\n\n"
            "Убедись, что FACEIT аккаунт подключён на "
            "<a href=\"https://cs-coach.ru\">cs-coach.ru</a>."
        )
        return
    lt  = fc.get("lifetime", {})
    elo = fc.get("elo", "?")
    lvl = fc.get("level", "?")
    kd  = lt.get("kd", "?")
    wr  = lt.get("winrate", "?")
    hs  = lt.get("hs", "?")
    matches = lt.get("matches", "?")
    streak  = lt.get("current_streak", "0")
    nick    = fc.get("nickname", "")

    badge = elo_badge(elo)
    streak_line = f"🔥 Серия: <b>{streak} побед подряд</b>" if int(streak or 0) > 1 else f"Текущая серия: {streak}"

    await send_msg(token, chat_id,
        f"{badge} <b>FACEIT · {nick}</b>\n"
        f"{'─'*22}\n"
        f"⚡ ELO: <b>{elo}</b>  ·  LVL <b>{lvl}</b>\n"
        f"🎯 K/D: <b>{kd}</b>  ·  HS: <b>{hs}%</b>\n"
        f"🏆 WR: <b>{wr}%</b>  ·  Матчей: <b>{matches}</b>\n"
        f"{streak_line}\n"
        f"{'─'*22}\n"
        f"<a href=\"https://cs-coach.ru\">📊 Полный анализ →</a>",
        markup={"inline_keyboard": [[
            {"text": "🎮 Разбор последнего матча", "callback_data": f"match_ai:{steamid}"},
        ]]}
    )


async def cmd_match(token, chat_id, steamid, get_faceit, get_groq_summary):
    fc = get_faceit(steamid)
    if not fc:
        await send_msg(token, chat_id, "⚠️ <b>Нет данных FACEIT.</b>")
        return
    matches = fc.get("matches", [])
    if not matches:
        await send_msg(token, chat_id, "⚠️ Матчи не найдены. Сыграй хотя бы один матч на FACEIT.")
        return

    m = matches[0]
    elo_ch   = int(m.get("elo_change", 0) or 0)
    result   = m.get("result", "0")
    won      = str(result) == "1"
    icon     = "🏆 ПОБЕДА" if won else "💀 ПОРАЖЕНИЕ"
    elo_str  = (f"+{elo_ch}" if elo_ch > 0 else str(elo_ch)) if elo_ch else "—"
    elo_icon = "📈" if elo_ch > 0 else "📉"
    map_name = m.get("map", "?")
    score    = m.get("score", "?")
    kills    = m.get("kills", "?")
    deaths   = m.get("deaths", "?")
    assists  = m.get("assists", "?")
    kd       = m.get("kd", "?")
    hs       = m.get("hs", "?")
    adr      = m.get("adr", "?")
    mvps     = m.get("mvps", "0")
    cur_elo  = fc.get("elo", "?")

    mvp_line = f"⭐ MVP: <b>{mvps}</b>  " if mvps and mvps != "0" else ""

    text = (
        f"🗺 <b>{map_name}</b> · {icon}\n"
        f"Счёт: <b>{score}</b>\n"
        f"{elo_icon} ELO: <b>{elo_str}</b>  →  {cur_elo}\n"
        f"{'─'*22}\n"
        f"🔫 K: <b>{kills}</b>  D: <b>{deaths}</b>  A: <b>{assists}</b>\n"
        f"📊 K/D: <b>{kd}</b>  ·  HS: <b>{hs}%</b>  ·  ADR: <b>{adr}</b>\n"
        f"{mvp_line}"
    )

    # AI-разбор
    if get_groq_summary:
        lt = fc.get("lifetime", {})
        prompt = (
            f"Игрок FACEIT LVL {fc.get('level','?')} ELO {cur_elo}. "
            f"Общая стата: K/D={lt.get('kd','?')} WR={lt.get('winrate','?')}% HS={lt.get('hs','?')}%.\n"
            f"Последний матч: карта={map_name}, {icon}, счёт={score}, "
            f"убийства={kills}, смерти={deaths}, ассисты={assists}, "
            f"K/D={kd}, HS={hs}%, ADR={adr}, ELO={elo_str}.\n\n"
            f"Одно конкретное наблюдение по матчу (что бросается в глаза) "
            f"и один конкретный совет на следующий матч. "
            f"2 предложения, без воды, на 'ты', без приветствий."
        )
        summary = await get_groq_summary(prompt)
        if summary:
            text += f"\n{'─'*22}\n🤖 <i>{summary}</i>"

    await send_msg(token, chat_id, text,
        markup={"inline_keyboard": [[
            {"text": "📈 Открыть на cs-coach.ru", "url": "https://cs-coach.ru"},
        ]]}
    )


# ── Match notifier ────────────────────────────────────────────────────────────

async def notify_new_match(token: str, chat_id, match: dict, faceit: dict, groq_key: str):
    elo_ch   = int(match.get("elo_change", 0) or 0)
    won      = str(match.get("result", "0")) == "1"
    icon     = "🏆 ПОБЕДА" if won else "💀 ПОРАЖЕНИЕ"
    elo_str  = (f"+{elo_ch}" if elo_ch > 0 else str(elo_ch)) if elo_ch else "—"
    elo_icon = "📈" if elo_ch > 0 else "📉"
    map_name = match.get("map", "?")
    score    = match.get("score", "?")
    kills    = match.get("kills", "?")
    deaths   = match.get("deaths", "?")
    assists  = match.get("assists", "?")
    kd       = match.get("kd", "?")
    hs       = match.get("hs", "?")
    adr      = match.get("adr", "?")
    mvps     = match.get("mvps", "0")
    cur_elo  = faceit.get("elo", "?")
    badge    = elo_badge(cur_elo)

    mvp_line = f"⭐ MVP: <b>{mvps}</b>  " if mvps and mvps != "0" else ""

    text = (
        f"🎮 <b>Новый матч завершён!</b>\n\n"
        f"🗺 <b>{map_name}</b> · {icon}\n"
        f"Счёт: <b>{score}</b>\n"
        f"{elo_icon} ELO: <b>{elo_str}</b>  →  {badge} {cur_elo}\n"
        f"{'─'*22}\n"
        f"🔫 K: <b>{kills}</b>  D: <b>{deaths}</b>  A: <b>{assists}</b>\n"
        f"📊 K/D: <b>{kd}</b>  ·  HS: <b>{hs}%</b>  ·  ADR: <b>{adr}</b>\n"
        f"{mvp_line}"
    )

    if groq_key:
        lt = faceit.get("lifetime", {})
        prompt = (
            f"Игрок FACEIT LVL {faceit.get('level','?')} ELO {cur_elo}. "
            f"Общая стата: K/D={lt.get('kd','?')} WR={lt.get('winrate','?')}% HS={lt.get('hs','?')}%.\n"
            f"Только что сыграл: карта={map_name}, {icon}, счёт={score}, "
            f"K={kills} D={deaths} A={assists}, K/D={kd}, HS={hs}%, ADR={adr}, ELO={elo_str}.\n\n"
            f"Одно конкретное наблюдение по матчу и один совет на следующий. "
            f"2 предложения максимум, на 'ты', без воды."
        )
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Authorization": f"Bearer {groq_key}", "Content-Type": "application/json"},
                    json={"model": "llama-3.3-70b-versatile",
                          "messages": [{"role": "user", "content": prompt}],
                          "max_tokens": 120}
                )
            summary = r.json()["choices"][0]["message"]["content"].strip()
            if summary:
                text += f"\n{'─'*22}\n🤖 <i>{summary}</i>"
        except Exception:
            pass

    await send_msg(token, str(chat_id), text,
        markup={"inline_keyboard": [[
            {"text": "📊 Полный анализ на сайте", "url": "https://cs-coach.ru"},
        ]]}
    )


# ── Daily reminder ────────────────────────────────────────────────────────────

REMINDERS = [
    "🎯 Сегодня не играл ещё? Самое время — зайди на <a href=\"https://cs-coach.ru\">cs-coach.ru</a> и разбери последний матч.",
    "💪 Прогресс не делается сам. 1 матч сегодня — уже лучше, чем ничего.",
    "🔥 Лучшие игроки разбирают матчи каждый день. Ты готов? <a href=\"https://cs-coach.ru\">cs-coach.ru</a>",
    "📈 Маленький шаг каждый день = большой скачок через месяц. Сыграй матч и посмотри разбор.",
    "⚡ Тренер ждёт разбора. Зайди на <a href=\"https://cs-coach.ru\">cs-coach.ru</a> после игры — увидишь что исправить.",
    "🎮 Сегодняшний матч — это данные для роста. Разбери его на cs-coach.ru.",
]

async def daily_reminder_loop(token: str, get_state):
    if not token:
        return
    while True:
        now = time.gmtime()
        target_hour = 16  # 19:00 МСК = 16:00 UTC
        secs = ((target_hour - now.tm_hour) % 24) * 3600 - now.tm_min * 60 - now.tm_sec
        if secs <= 0:
            secs += 86400
        await asyncio.sleep(secs)

        tg_users = get_state()
        text = random.choice(REMINDERS)
        for steamid, user in list(tg_users.items()):
            if user.get("remind", True) and user.get("chat_id"):
                try:
                    await send_msg(token, user["chat_id"], text,
                        markup={"inline_keyboard": [[
                            {"text": "🎮 Открыть cs-coach.ru", "url": "https://cs-coach.ru"},
                        ]]}
                    )
                    await asyncio.sleep(0.05)
                except Exception:
                    pass
        await asyncio.sleep(60)


# ── Match poll loop ───────────────────────────────────────────────────────────

async def match_poll_loop(token: str, get_state, set_state,
                          fetch_faceit_fn, groq_key: str, interval: int = 300):
    if not token:
        return
    await asyncio.sleep(30)
    while True:
        tg_users = get_state()
        changed = False
        for steamid, user in list(tg_users.items()):
            chat_id = user.get("chat_id")
            if not chat_id:
                continue
            try:
                faceit = await fetch_faceit_fn(steamid)
                if not faceit:
                    continue
                matches = faceit.get("matches", [])
                if not matches:
                    continue
                latest = matches[0]
                latest_id  = latest.get("match_id", "")
                last_known = user.get("last_match_id", "")
                if latest_id and latest_id != last_known:
                    tg_users[steamid]["last_match_id"] = latest_id
                    changed = True
                    await notify_new_match(token, chat_id, latest, faceit, groq_key)
                    await asyncio.sleep(2)
            except Exception as e:
                logger.warning(f"[match_poll] {steamid}: {e}")
        if changed:
            set_state(tg_users)
        await asyncio.sleep(interval)
