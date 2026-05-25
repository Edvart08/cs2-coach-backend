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

GROQ_KEY = os.environ.get("GROQ_API_KEY")

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
    prompt = f"""Проанализируй статистику игрока CS2 и верни ТОЛЬКО валидный JSON без markdown и пояснений.

Статы: K/D={stats.kd} WR={stats.winrate}% HLTV={stats.hltv} HS={stats.hs}% ADR={stats.adr} 1v1={stats.clutch1v1}% Entry={stats.entrySuccess}% Rank={stats.rank} Matches={stats.matches}

Верни строго этот JSON:
{{"level":"Новичок","overall":"краткий вывод об игре","mainProblem":"главная проблема","weaknesses":[{{"stat":"название","problem":"описание проблемы","fix":"совет"}},{{"stat":"название","problem":"описание проблемы","fix":"совет"}}],"strengths":[{{"stat":"название","comment":"комментарий"}},{{"stat":"название","comment":"комментарий"}}],"plan":["день 1: задание","день 2: задание","день 3: задание"],"goal":"цель через месяц"}}

level = одно из: Новичок, Средний, Хороший, Про"""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": "Ты тренер CS2. Отвечай ТОЛЬКО валидным JSON без markdown и пояснений."},
                    {"role": "user", "content": prompt}
                ],
                "temperature": 0.7
            }
        )

    data = response.json()
    print("=== GROQ RESPONSE ===")
    print(json.dumps(data, ensure_ascii=False, indent=2))

    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        return {"error": f"Groq error: {json.dumps(data)}", "result": ""}

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
