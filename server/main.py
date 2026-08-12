"""FastAPI server: game sessions, human moves, background AI turns, static UI.

Run:  uvicorn server.main:app --reload
Then open http://127.0.0.1:8000
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.decks import RANDOM, deck_list, load_decks
from engine.game import Game
from server.llm import LLMConfig, choose_option

app = FastAPI(title="Duel Masters")
ROOT = Path(__file__).resolve().parent.parent

GAMES: dict[str, dict] = {}  # gid -> {game, cfg, lock, ai_task}

HUMAN, AI = 0, 1


class NewGame(BaseModel):
    player_deck: str = RANDOM
    ai_deck: str = RANDOM
    ai_name: str = "AI"
    provider: str = "auto"   # "auto" | "anthropic" | "openai" | "none" (hotseat)
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    seed: int | None = None


def _resolve_provider(provider: str) -> str:
    if provider != "auto":
        return provider
    if os.environ.get("OPENAI_API_KEY") or os.environ.get("MODEL_ACCESS_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "none"  # no key anywhere -> hotseat


class Act(BaseModel):
    option_id: int
    player: int = HUMAN


@app.get("/api/decks")
def decks():
    out = {RANDOM: {"description":
                    "A fresh random two-civilization deck, built new every game.",
                    "cards": {}}}
    out.update({name: {"description": d["description"], "cards": d["cards"]}
                for name, d in load_decks().items()})
    return out


@app.post("/api/games")
async def new_game(req: NewGame = NewGame()):
    provider = _resolve_provider(req.provider)
    ai_name = req.ai_name if provider != "none" else "Player 2"
    try:
        g = Game([deck_list(req.player_deck), deck_list(req.ai_deck)],
                 names=("Player 1", ai_name), seed=req.seed)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    gid = uuid.uuid4().hex[:12]
    cfg = None
    if provider != "none":
        cfg = LLMConfig(provider, req.model, req.api_key, req.base_url)
        if not cfg.api_key:
            raise HTTPException(400, f"no API key: pass one or set the env var for {provider}")
    GAMES[gid] = {"game": g, "cfg": cfg, "lock": asyncio.Lock(), "ai_task": None,
                  "ai_thinking": False}
    _maybe_start_ai(gid)
    return {"game_id": gid, "provider": provider,
            "model": cfg.model if cfg else None}


def _maybe_start_ai(gid: str):
    s = GAMES[gid]
    g = s["game"]
    if (s["cfg"] and not g.over and g.pending and g.pending.player == AI
            and (s["ai_task"] is None or s["ai_task"].done())):
        s["ai_task"] = asyncio.create_task(_ai_loop(gid))


async def _ai_loop(gid: str):
    s = GAMES[gid]
    g = s["game"]
    async with s["lock"]:
        s["ai_thinking"] = True
        try:
            while not g.over and g.pending and g.pending.player == AI:
                opt_id, reason = await choose_option(g, AI, s["cfg"])
                opt = next(o for o in g.pending.options if o.id == opt_id)
                if reason:
                    g.say(f"🤖 {g.players[AI].name}: {reason}")
                g.say(f"🤖 chooses: {opt.text}")
                g.submit(AI, opt_id)
        finally:
            s["ai_thinking"] = False


@app.get("/api/games/{gid}/state")
def state(gid: str, player: int = HUMAN):
    s = GAMES.get(gid)
    if not s:
        raise HTTPException(404, "no such game")
    v = s["game"].view(player)
    v["ai_thinking"] = s["ai_thinking"]
    v["ai_model"] = s["cfg"].model if s["cfg"] else None
    return v


@app.post("/api/games/{gid}/act")
async def act(gid: str, req: Act):
    s = GAMES.get(gid)
    if not s:
        raise HTTPException(404, "no such game")
    g = s["game"]
    if s["ai_thinking"]:
        raise HTTPException(409, "AI is thinking")
    if s["cfg"] and req.player != HUMAN:
        raise HTTPException(403, "that seat is played by the AI")
    try:
        g.submit(req.player, req.option_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    _maybe_start_ai(gid)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(ROOT / "web" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
