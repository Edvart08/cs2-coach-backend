from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import os
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

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
    prompt = f"""Статы игрока CS2:
K/D={stats.kd} WR={stats.winrate}% HLTV={stats.hltv} HS={stats.hs}% ADR={stats.adr} 1v1={stats.clutch1v1}% Entry={stats.entrySuccess}% Rank={stats.rank} Matches={stats.matches}

Верни ТОЛЬКО этот JSON (без markdown, без пояснений, только фигурные скобки):
{{"level":"Новичок","overall":"краткий вывод об игре","mainProblem":"главная проблема","weaknesses":[{{"stat":"название","problem":"описание проблемы","fix":"совет"}},{{"stat":"название","problem":"описание проблемы","fix":"совет"}}],"strengths":[{{"stat":"название","comment":"комментарий"}},{{"stat":"название","comment":"комментарий"}}],"plan":["день 1: задание","день 2: задание","день 3: задание"],"goal":"цель через месяц"}}

level должен быть одним из: Новичок, Средний, Хороший, Про"""

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 2000,
                "system": "Ты тренер CS2. Отвечай ТОЛЬКО валидным JSON без каких-либо пояснений, markdown или дополнительного текста.",
                "messages": [{"role": "user", "content": prompt}]
            }
        )

    data = response.json()
    text = "".join(b.get("text", "") for b in data.get("content", []))

    # Убираем markdown-обёртку если Claude всё равно добавил
    text = re.sub(r"```(?:json)?", "", text).strip()

    return {"result": text}

@app.get("/")
def root():
    return {"status": "ok"}
