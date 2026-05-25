from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import httpx
import os

app = FastAPI()

# Разрешаем запросы с любого сайта
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
    prompt = f"""Ты тренер CS2. ТОЛЬКО валидный JSON без markdown. Поля макс 6 слов. Ровно 2 weaknesses, 2 strengths, 3 пункта плана.
Статы: K/D={stats.kd} WR={stats.winrate}% HLTV={stats.hltv} HS={stats.hs}% ADR={stats.adr} 1v1={stats.clutch1v1}% Entry={stats.entrySuccess}% Rank={stats.rank}
Шаблон: {{"level":"Новичок","overall":"","mainProblem":"","weaknesses":[{{"stat":"","problem":"","fix":""}},{{"stat":"","problem":"","fix":""}}],"strengths":[{{"stat":"","comment":""}},{{"stat":"","comment":""}}],"plan":["","",""],"goal":""}}"""

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
                "messages": [{"role": "user", "content": prompt}]
            }
        )
    
    data = response.json()
    text = "".join(b.get("text", "") for b in data.get("content", []))
    return {"result": text}

@app.get("/")
def root():
    return {"status": "ok"}
