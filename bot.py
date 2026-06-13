"""
CS2 Coach Telegram Bot
Запускается как asyncio background task из main.py при старте FastAPI.

Функции:
  /start — привязка аккаунта через deep link (?start=STEAMID)
  /stats — краткая статистика
  /match — разбор последнего матча FACEIT
  /remind — включить/выключить ежедневное напоминание
  /unlink — отвязать Telegram

Уведомления (шлются из main.py через notify_match):
  — после каждого нового матча FACEIT: результат + краткий AI-разбор
  — ежедневное напоминание потренироваться (если включено)
"""

import asyncio, httpx, json, os, time, logging

logger = logging.getLogger("tg_bot")

TG_BASE = "https://api.telegram.org/bot"
FACEIT_BASE = "https://open.faceit.com/data/v4"


# ── Helpers ───────────────────────────────────────────────────────────────────

async def tg_api(token: str, method: str, **kwargs) -> dict:
    url = f"{TG_BASE}{token}/{method}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(url, json=kwargs)
            return r.json()
    except Exception as e:
        logger.warning(f"[tg_api] {method} error: {e}")
        return {}


async def send_msg(token: str, chat_id, text: str, markup=None, parse_mode="HTML"):
    body = {"chat_id": str(chat_id), "text": text, "parse_mode": parse_mode}
    if markup:
        body["reply_markup"] = markup
    await tg_api(token, "sendMessage", **body)


async def answer_callback(token: str, callback_id: str):
    await tg_api(token, "answerCallbackQuery", callback_query_id=callback_id)


# ── Main polling loop ─────────────────────────────────────────────────────────

async def run_bot(token: str, get_state, set_state, on_link, on_unlink,
                  get_faceit, get_groq_summary, groq_key: str, faceit_key: str):
    """
    token          — TG_BOT_TOKEN
    get_state()    — возвращает dict tg_users: {steamid: {chat_id, remind, last_match_id}}
    set_state(d)   — сохраняет tg_users
    on_link(steamid, chat_id) — callback при привязке
    on_unlink(chat_id)        — callback при отвязке
    get_faceit(steamid)       — возвращает faceit-данные игрока (из кеша main.py)
    get_groq_summary(prompt)  — делает AI-запрос, возвращает строку
    """
    if not token:
        logger.info("[bot] TG_BOT_TOKEN не задан — бот отключён")
        return

    offset = 0
    logger.info("[bot] Polling started")

    while True:
        try:
            data = await tg_api(token, "getUpdates",
                                timeout=30, offset=offset, allowed_updates=["message", "callback_query"])
            updates = data.get("result", [])

            for upd in updates:
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    await handle_callback(token, upd["callback_query"], get_state, set_state,
                                          on_unlink, get_faceit, get_groq_summary, groq_key, faceit_key)
                elif "message" in upd:
                    await handle_message(token, upd["message"], get_state, set_state,
                                         on_link, on_unlink, get_faceit, get_groq_summary, groq_key, faceit_key)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"[bot] polling error: {e}")
            await asyncio.sleep(5)

        await asyncio.sleep(1)


async def handle_message(token, msg, get_state, set_state,
                         on_link, on_unlink, get_faceit, get_groq_summary, groq_key, faceit_key):
    chat_id = str(msg["chat"]["id"])
    text = msg.get("text", "").strip()
    tg_users = get_state()

    # Найдём steamid по chat_id
    steamid = next((sid for sid, u in tg_users.items() if str(u.get("chat_id")) == chat_id), None)

    # /start — может содержать deep link (?start=STEAMID)
    if text.startswith("/start"):
        parts = text.split(" ", 1)
        deep_steamid = parts[1].strip() if len(parts) > 1 else ""

        if deep_steamid and deep_steamid.isdigit():
            # Привязываем
            tg_users[deep_steamid] = tg_users.get(deep_steamid, {})
            tg_users[deep_steamid]["chat_id"] = chat_id
            tg_users[deep_steamid].setdefault("remind", True)
            tg_users[deep_steamid].setdefault("last_match_id", "")
            set_state(tg_users)
            on_link(deep_steamid, chat_id)
            await send_msg(token, chat_id,
                "✅ <b>Telegram привязан!</b>\n\n"
                "Теперь ты будешь получать:\n"
                "• Уведомление после каждого матча FACEIT\n"
                "• Ежедневное напоминание потренироваться\n\n"
                "Команды:\n"
                "/stats — твоя статистика\n"
                "/match — разбор последнего матча\n"
                "/remind — вкл/выкл напоминания\n"
                "/unlink — отвязать Telegram"
            )
        else:
            if steamid:
                await send_msg(token, chat_id,
                    f"👋 Привет! Твой аккаунт уже привязан.\n\n"
                    "/stats — статистика\n"
                    "/match — последний матч\n"
                    "/remind — напоминания\n"
                    "/unlink — отвязать"
                )
            else:
                await send_msg(token, chat_id,
                    "👋 Привет! Я CS2 Coach Bot.\n\n"
                    "Чтобы привязать аккаунт — зайди на "
                    "<a href=\"https://cs-coach.ru\">cs-coach.ru</a> → Настройки → Telegram."
                )
        return

    if not steamid:
        await send_msg(token, chat_id,
            "⚠️ Аккаунт не привязан.\n"
            "Зайди на <a href=\"https://cs-coach.ru\">cs-coach.ru</a> → Настройки → Telegram."
        )
        return

    if text == "/stats":
        await cmd_stats(token, chat_id, steamid, get_faceit)

    elif text == "/match":
        await cmd_match(token, chat_id, steamid, get_faceit, get_groq_summary, groq_key, faceit_key)

    elif text == "/remind":
        user = tg_users[steamid]
        new_val = not user.get("remind", True)
        user["remind"] = new_val
        set_state(tg_users)
        status = "включены ✅" if new_val else "выключены ❌"
        await send_msg(token, chat_id, f"🔔 Ежедневные напоминания {status}")

    elif text == "/unlink":
        await send_msg(token, chat_id,
            "Уверен, что хочешь отвязать Telegram?",
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
            "/remind — вкл/выкл напоминания\n"
            "/unlink — отвязать Telegram"
        )


async def handle_callback(token, cb, get_state, set_state,
                          on_unlink, get_faceit, get_groq_summary, groq_key, faceit_key):
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
        await send_msg(token, chat_id, "✅ Telegram отвязан. Уведомления отключены.")

    elif data == "cancel":
        await send_msg(token, chat_id, "Отменено.")

    elif data.startswith("match_ai:"):
        steamid = data[9:]
        await send_msg(token, chat_id, "⏳ Делаю AI-разбор...")
        await cmd_match(token, chat_id, steamid, get_faceit, get_groq_summary, groq_key, faceit_key)


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_stats(token, chat_id, steamid, get_faceit):
    fc = get_faceit(steamid)
    if not fc:
        await send_msg(token, chat_id, "⚠️ Нет данных FACEIT. Убедись, что аккаунт подключён на cs-coach.ru")
        return
    lt = fc.get("lifetime", {})
    elo = fc.get("elo", "—")
    lvl = fc.get("level", "—")
    kd  = lt.get("kd", "—")
    wr  = lt.get("winrate", "—")
    hs  = lt.get("hs", "—")
    matches = lt.get("matches", "—")
    streak  = lt.get("current_streak", "—")
    await send_msg(token, chat_id,
        f"📊 <b>Твоя статистика FACEIT</b>\n\n"
        f"⚡ <b>ELO:</b> {elo} · LVL {lvl}\n"
        f"🎯 <b>K/D:</b> {kd}\n"
        f"💥 <b>HS:</b> {hs}%\n"
        f"🏆 <b>WR:</b> {wr}%\n"
        f"🎮 <b>Матчей:</b> {matches}\n"
        f"🔥 <b>Текущая серия:</b> {streak}\n\n"
        f"<a href=\"https://cs-coach.ru\">Полный анализ на cs-coach.ru →</a>"
    )


async def cmd_match(token, chat_id, steamid, get_faceit, get_groq_summary, groq_key, faceit_key):
    fc = get_faceit(steamid)
    if not fc:
        await send_msg(token, chat_id, "⚠️ Нет данных FACEIT.")
        return
    matches = fc.get("matches", [])
    if not matches:
        await send_msg(token, chat_id, "⚠️ Нет матчей FACEIT.")
        return
    m = matches[0]  # последний матч
    result   = "✅ ПОБЕДА" if str(m.get("result")) == "1" else "❌ ПОРАЖЕНИЕ"
    score    = m.get("score", "?")
    kills    = m.get("kills", "?")
    deaths   = m.get("deaths", "?")
    assists  = m.get("assists", "?")
    hs_pct   = m.get("hs_pct", "?")
    kd_ratio = m.get("kd", "?")
    elo_ch   = int(m.get("elo_change", 0))
    elo_str  = (f"+{elo_ch}" if elo_ch > 0 else str(elo_ch)) if elo_ch else "—"
    map_name = m.get("map", "?")

    text = (
        f"🎮 <b>Последний матч</b>\n\n"
        f"🗺 Карта: <b>{map_name}</b>\n"
        f"{'✅' if elo_ch > 0 else '❌'} {result} · {score}\n"
        f"⚡ ELO: <b>{elo_str}</b>\n\n"
        f"K: <b>{kills}</b> · D: <b>{deaths}</b> · A: <b>{assists}</b>\n"
        f"K/D: <b>{kd_ratio}</b> · HS: <b>{hs_pct}%</b>"
    )

    # Пробуем сгенерировать AI-резюме
    if groq_key:
        lt = fc.get("lifetime", {})
        prompt = (
            f"Игрок FACEIT LVL {fc.get('level','?')} ELO {fc.get('elo','?')}.\n"
            f"Общая стата: K/D={lt.get('kd','?')} WR={lt.get('winrate','?')}% HS={lt.get('hs','?')}%.\n"
            f"Последний матч: карта={map_name}, результат={result}, счёт={score}, "
            f"убийства={kills}, смерти={deaths}, ассисты={assists}, K/D={kd_ratio}, HS={hs_pct}%, ELO={elo_str}.\n\n"
            f"Дай одно конкретное наблюдение и один конкретный совет на следующий матч. "
            f"2-3 предложения, без воды, на 'ты'."
        )
        summary = await get_groq_summary(prompt)
        if summary:
            text += f"\n\n🤖 <i>{summary}</i>"

    await send_msg(token, chat_id, text,
        markup={"inline_keyboard": [[
            {"text": "📈 Полный анализ на сайте", "url": "https://cs-coach.ru"},
        ]]}
    )


# ── Match notifier (вызывается из main.py после появления нового матча) ────────

async def notify_new_match(token: str, chat_id, match: dict, faceit: dict, groq_key: str):
    """Шлёт уведомление о новом матче с кратким AI-резюме."""
    result   = "✅ ПОБЕДА" if str(match.get("result")) == "1" else "❌ ПОРАЖЕНИЕ"
    elo_ch   = int(match.get("elo_change", 0))
    elo_str  = (f"+{elo_ch}" if elo_ch > 0 else str(elo_ch)) if elo_ch else "—"
    map_name = match.get("map", "?")
    kills    = match.get("kills", "?")
    deaths   = match.get("deaths", "?")
    kd_ratio = match.get("kd", "?")
    hs_pct   = match.get("hs_pct", "?")
    score    = match.get("score", "?")

    text = (
        f"🎮 <b>Новый матч!</b>\n\n"
        f"🗺 {map_name} · {result}\n"
        f"Счёт: {score}\n"
        f"⚡ ELO: <b>{elo_str}</b> → {faceit.get('elo','?')}\n\n"
        f"K: {kills} · D: {deaths} · K/D: {kd_ratio} · HS: {hs_pct}%"
    )

    if groq_key:
        lt = faceit.get("lifetime", {})
        prompt = (
            f"Игрок FACEIT LVL {faceit.get('level','?')} ELO {faceit.get('elo','?')}.\n"
            f"Сыграл матч: карта={map_name}, {result}, счёт={score}, "
            f"убийства={kills}, смерти={deaths}, K/D={kd_ratio}, HS={hs_pct}%, ELO изменение={elo_str}.\n\n"
            f"Одно конкретное наблюдение по матчу и один совет на следующий. "
            f"2 предложения максимум, без воды, на 'ты'."
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
                text += f"\n\n🤖 <i>{summary}</i>"
        except Exception:
            pass

    await send_msg(token, str(chat_id), text,
        markup={"inline_keyboard": [[
            {"text": "📈 Открыть на сайте", "url": "https://cs-coach.ru"},
        ]]}
    )


# ── Daily reminder loop ───────────────────────────────────────────────────────

REMINDER_TEXTS = [
    "🎯 Сегодня тренировался? Зайди на cs-coach.ru — тренер уже ждёт.",
    "💪 Прогресс не делается сам. 1 матч сегодня — уже лучше, чем ничего.",
    "🔥 Серия не прервётся сама. Открой cs-coach.ru и сыграй хотя бы один матч.",
    "📈 Лучшие игроки тренируются каждый день. Ты сегодня?",
    "⚡ Тренер ждёт разбора. Зайди на cs-coach.ru после игры.",
]

async def daily_reminder_loop(token: str, get_state):
    """Каждый день в 19:00 МСК (UTC+3 = 16:00 UTC) шлёт напоминание."""
    import random
    if not token:
        return
    while True:
        now = time.gmtime()
        # Ждём 19:00 по МСК = 16:00 UTC
        target_hour = 16
        secs_until = ((target_hour - now.tm_hour) % 24) * 3600 - now.tm_min * 60 - now.tm_sec
        if secs_until <= 0:
            secs_until += 86400
        await asyncio.sleep(secs_until)

        tg_users = get_state()
        text = random.choice(REMINDER_TEXTS)
        for steamid, user in tg_users.items():
            if user.get("remind", True) and user.get("chat_id"):
                try:
                    await send_msg(token, user["chat_id"], text,
                        markup={"inline_keyboard": [[
                            {"text": "🎮 Открыть cs-coach.ru", "url": "https://cs-coach.ru"},
                        ]]}
                    )
                except Exception:
                    pass
        await asyncio.sleep(60)  # не шлём дважды в ту же минуту


# ── Match poll loop ───────────────────────────────────────────────────────────

async def match_poll_loop(token: str, get_state, set_state,
                          fetch_faceit_fn, groq_key: str, interval: int = 300):
    """Каждые 5 минут проверяет новые матчи у привязанных пользователей."""
    if not token:
        return
    await asyncio.sleep(30)  # даём время на старт
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
                latest_id = latest.get("match_id", "")
                last_known = user.get("last_match_id", "")
                if latest_id and latest_id != last_known:
                    tg_users[steamid]["last_match_id"] = latest_id
                    changed = True
                    await notify_new_match(token, chat_id, latest, faceit, groq_key)
                    await asyncio.sleep(2)  # небольшая пауза между пользователями
            except Exception as e:
                logger.warning(f"[match_poll] {steamid}: {e}")
        if changed:
            set_state(tg_users)
        await asyncio.sleep(interval)
