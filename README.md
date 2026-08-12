# Duel Masters vs AI

A personal Duel Masters TCG implementation — play the classic DM-01 base set in
your browser against any LLM (Anthropic API or any OpenAI-compatible endpoint),
or hotseat against a friend.

Card data and images from [db.duelmasters.us](https://db.duelmasters.us/).

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export MODEL_ACCESS_KEY=...    # DigitalOcean Gradient AI (OpenAI-compatible)
export MODEL_NAME=llama3.3-70b-instruct   # any model slug your key can use
.venv/bin/uvicorn server.main:app
```

Open <http://127.0.0.1:8000> — a game against the AI starts immediately with
random decks. There is no setup screen; everything comes from env vars:

| Env var | Meaning |
|---|---|
| `MODEL_ACCESS_KEY` | OpenAI-compatible key; implies DigitalOcean's `https://inference.do-ai.run` endpoint |
| `MODEL_NAME` | model slug for the AI opponent |
| `MODEL_BASE_URL` | override the OpenAI-compatible endpoint (OpenAI, Ollama, vLLM, OpenRouter…) |
| `OPENAI_API_KEY` | alternative key; defaults to `api.openai.com` |
| `ANTHROPIC_API_KEY` | used if no OpenAI-compatible key is set (Anthropic Messages API) |

No key at all → the game starts in hotseat mode. Force hotseat with
`/?hotseat=1`; the second player opens the `?gid=…&seat=1` link printed in the
browser console. Play by drag & drop (hand→mana charges, hand→battle
summons, creature→enemy or shields attacks); click any card to read it
full-size; 📜 toggles the log.

## Layout

| Path | What |
|---|---|
| `data/cards.json` | Scraped DM-01 card database (120 cards) |
| `engine/cards.py` | Card defs + keyword parsing (blocker, double breaker, …) |
| `engine/game.py` | Rules engine: one generator, pauses at every `Decision` |
| `engine/dm01.py` | Per-card effect scripts (all 120 cards covered) |
| `engine/decks.py` | Deck loading/validation (40 cards, max 4 copies) |
| `decks/decks.json` | Three prebuilt decks |
| `server/main.py` | FastAPI: sessions, moves, background AI turns |
| `server/llm.py` | LLM adapter: state → prompt → validated option |
| `web/` | Browser UI (vanilla JS, real card images) |
| `scripts/scrape_cards.py` | Re-scrape / add more sets (`python3 scripts/scrape_cards.py dm-02`) |
| `tests/test_engine.py` | Rules checks + 120-game random fuzz |

## How the engine works

The entire game is a Python generator. It runs until a player must choose
something, then yields a `Decision` with fully enumerated legal `options`
(play X, attack Y with Z, block/don't, use shield trigger, pick targets…).
`game.submit(player, option_id)` resumes it. The LLM opponent is just a loop
that reads the state JSON + options and answers with an option id and a
one-line reason (shown in the log as table talk 🤖).

## Known simplifications

- Mana is auto-tapped with a civ-preserving heuristic (in DM-01 there is no
  play outside your own main phase, so this is almost always optimal).
- When several shield triggers break at once they resolve in break order
  (officially the owner picks the order).
- Double-breaker shield picks are automatic (shields are face-down anyway).

## Adding more sets

Scrape a set (`scripts/scrape_cards.py dm-02`), then implement any new
keywords in `engine/cards.py` and card scripts in a new `engine/dm02.py`
(evolution creatures need engine support first). `tests/test_engine.py`'s
coverage check will list every unscripted ability line.
