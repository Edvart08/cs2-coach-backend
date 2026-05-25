from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import os
import re
import json

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

class Stats(BaseModel):
    kd: str
    winrate: str
    hltv: str
    hs: str
    adr: str
    clutch1v1: str
    entrySuccess: str
    rank: str
    matches: str

@app.post("/analyze")
async def analyze(stats: Stats):
    prompt = f"""Ты тренер CS2. Проанализируй статистику игрока и верни ТОЛЬКО валидный JSON без markdown, без пояснений.

Статы: K/D={stats.kd} WR={stats.winrate}% HLTV={stats.hltv} HS={stats.hs}% ADR={stats.adr} 1v1={stats.clutch1v1}% Entry={stats.entrySuccess}% Rank={stats.rank} Matches={stats.matches}

Верни строго этот JSON:
{{"level":"Новичок","overall":"краткий вывод об игре","mainProblem":"главная проблема","weaknesses":[{{"stat":"название","problem":"описание проблемы","fix":"совет"}},{{"stat":"название","problem":"описание проблемы","fix":"совет"}}],"strengths":[{{"stat":"название","comment":"комментарий"}},{{"stat":"название","comment":"комментарий"}}],"plan":["день 1: задание","день 2: задание","день 3: задание"],"goal":"цель через месяц"}}

level = одно из: Новичок, Средний, Хороший, Про"""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_KEY}"

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            url,
            headers={"Content-Type": "application/json"},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.7}
            }
        )

    data = response.json()
    print("=== GEMINI RESPONSE ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        return {"error": f"Gemini error: {json.dumps(data)}", "result": ""}

    print("=== RAW TEXT ===")
    print(repr(text))

    # Убираем markdown
    text = re.sub(r"```(?:json)?", "", text).strip().replace("```", "").strip()

    try:
        parsed = json.loads(text)
        return {"result": json.dumps(parsed, ensure_ascii=False)}
    except:
        m = re.search(r'\{[\s\S]*\}', text)
        if m:
            try:
                parsed = json.loads(m.group(0))
                return {"result": json.dumps(parsed, ensure_ascii=False)}
            except:
                pass
        return {"result": text, "error": "parse_error"}

@app.get("/")
def root():
    return {"status": "ok"}
