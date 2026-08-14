"""LLM opponent: turns the pending Decision into a prompt, parses the reply.

Supports the Anthropic Messages API and any OpenAI-compatible
chat-completions endpoint (OpenAI, DigitalOcean Gradient AI, Ollama, vLLM…).
"""
from __future__ import annotations

import json
import logging
import os
import re

import httpx

log = logging.getLogger("uvicorn.error")

SYSTEM = """You are a competitive Duel Masters player. You receive the game \
state and a numbered list of legal options for one decision. Pick the best one.

RULES SUMMARY
- Cards cost mana. You may charge ONE card from hand into mana per turn (it's \
gone from hand forever). Playing a card taps mana equal to its cost and needs \
one mana of its civilization.
- Creatures can't attack the turn they enter (summoning sickness). Attacking \
taps the creature; tapped creatures can be attacked and can't block.
- Attacking the player breaks a shield, which goes to THEIR hand — and shield \
triggers there are played FREE against you.
- Blockers may intercept any attack. In battle, higher power wins (ties kill \
both). "Power attacker" bonuses only count while attacking. Slayers destroy \
whatever they battle, win or lose.
- A player with no shields loses when the next attack connects. Drawing from \
an empty deck loses.

YOU ARE PLAYING TO WIN. Be aggressive. The two losing mistakes are passing \
the turn with mana unspent, and attacking before you have finished summoning.

TURN SEQUENCE — DO IT IN THIS ORDER, EVERY TURN:
  (1) charge one card into mana  (2) summon/cast everything you can afford  \
(3) then attack with everything that profitably can.
ATTACKING ENDS YOUR MAIN STEP. The moment you declare your first attack you \
may not play any more cards this turn, so a creature left in hand is wasted. \
The ONLY exception: if an attack wins the game right now, attack immediately.

STRATEGY PLAYBOOK
1. Charge exactly one card per turn — that mana is usable immediately, so \
charge BEFORE summoning. Charge a card you're least likely to cast, and \
prefer a civilization missing from your mana zone.
2. NEVER end your turn with mana unspent if you can summon a creature. Board \
presence wins this game. Play on curve EVERY turn.
3. Attack every turn you profitably can. A creature that never attacks does \
nothing. Untapped enemy creatures can't be attacked (unless stated), so hit \
the player or a tapped creature.
4. Attack enemy TAPPED creatures you outpower — free removal, no shield-trigger risk.
5. Kill blockers first; then your attackers get through to shields.
6. Count lethal every turn: their shields, plus one more connecting attack \
wins. Double breakers break 2. If you have lethal, TAKE IT.
7. Breaking shields hands them the card and may trigger a free shield trigger, \
so prefer breaking when you can close the game soon or you're ahead on board.
8. Blocking: block when your blocker kills the attacker and lives, or when a \
shield loss would put you in lethal range. Don't chump-block a big attacker \
early with a useful blocker.
9. Removal goes on their biggest threat or their blocker — not the first target listed.
10. Always use a shield trigger when offered — it is free value.

A "tactical_brief" is provided with the exact arithmetic already done for you \
(who kills whom, what wins the game, what mana you're wasting). TRUST IT and \
act on it — do not re-derive it and do not ignore it.

Reply with ONLY a JSON object, reasoning first:
{"reason": "<one short sentence>", "option": <id>}
"""


class LLMConfig:
    def __init__(self, provider="openai", model=None, api_key=None, base_url=None):
        self.provider = provider
        model = model or os.environ.get("MODEL_NAME")
        base_url = base_url or os.environ.get("MODEL_BASE_URL")
        if provider == "anthropic":
            self.model = model or "claude-sonnet-5"
            self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            self.base_url = (base_url or "https://api.anthropic.com").rstrip("/")
        else:
            openai_key = api_key or os.environ.get("OPENAI_API_KEY")
            do_key = os.environ.get("MODEL_ACCESS_KEY")  # DigitalOcean Gradient AI
            self.api_key = openai_key or do_key or ""
            if not base_url and not openai_key and do_key:
                # key came from MODEL_ACCESS_KEY -> talk to DO's inference endpoint
                base_url = "https://inference.do-ai.run"
                model = model or "llama3.3-70b-instruct"
            self.model = model or "gpt-4o"
            base = (base_url or "https://api.openai.com").rstrip("/")
            if base.endswith("/v1"):  # accept ".../v1" or bare host
                base = base[:-3].rstrip("/")
            self.base_url = base


def _clean_text(rules: list[str]) -> str:
    """Ability text without reminder parentheticals; short."""
    out = "; ".join(re.sub(r"\s*\([^)]*\)", "", r).strip() for r in rules)
    return out[:220]


def _card_brief(game, c, in_battle=False) -> dict:
    d = {"name": c.d.name, "civ": c.d.civilization, "cost": c.d.cost,
         "type": c.d.type}
    if c.d.power is not None:
        d["power"] = c.d.power
    text = _clean_text(c.d.rules_text)
    if text:
        d["text"] = text
    if in_battle:
        d["power"] = game.power(c)
        d["attacks_for"] = game.power(c, attacking=True)
        d["tapped"] = c.tapped
        if c.sick:
            d["cant_attack_yet"] = True
    return d


def compact_state(game, player_idx: int) -> dict:
    """Small, model-friendly view of the game from `player_idx`'s seat."""
    me = game.players[player_idx]
    opp = game.opponent_of(player_idx)

    def mana_summary(p):
        by_civ = {}
        for m in p.mana:
            if not m.tapped:
                by_civ[m.d.civilization] = by_civ.get(m.d.civilization, 0) + 1
        return {"untapped": sum(by_civ.values()), "total": len(p.mana),
                "untapped_by_civ": by_civ}

    return {
        "turn": game.turn,
        "you": {
            "shields": len(me.shields),
            "hand": [_card_brief(game, c) for c in me.hand],
            "battle_zone": [_card_brief(game, c, in_battle=True) for c in me.battle],
            "mana": mana_summary(me),
            "deck_cards_left": len(me.deck),
        },
        "opponent": {
            "shields": len(opp.shields),
            "hand_count": len(opp.hand),
            "battle_zone": [_card_brief(game, c, in_battle=True) for c in opp.battle],
            "mana": mana_summary(opp),
            "deck_cards_left": len(opp.deck),
        },
        "recent_events": game.log[-10:],
    }


def _breaks(c) -> int:
    return 2 if (c.d.double_breaker or c.temp_double_breaker) else 1


def tactical_brief(game, idx: int) -> dict:
    """Do the combat arithmetic for the model: what kills what, what wins."""
    me, opp = game.players[idx], game.opponent_of(idx)
    options = game.pending.options
    free_blockers = [b for b in opp.battle if b.d.blocker and not b.tapped]
    brief = {
        "your_untapped_mana": sum(1 for m in me.mana if not m.tapped),
        "your_shields": len(me.shields),
        "opponent_shields": len(opp.shields),
        "opponent_untapped_blockers": [f"{b.d.name} ({game.power(b)})" for b in free_blockers],
    }

    if game.pending.kind == "block":
        m = re.search(r"\((\d+)\) is attacking", game.pending.prompt or "")
        apow = int(m.group(1)) if m else None
        rows = []
        for o in options:
            if o.kind != "block":
                continue
            b = game.find(o.data["iid"])
            if not b:
                continue
            bp = game.power(b)
            rows.append({"option": o.id, "blocker": f"{b.d.name} ({bp})",
                         "kills_attacker": apow is not None and (bp > apow or game.has_slayer(b)),
                         "your_blocker_dies": apow is not None and bp <= apow})
        brief["attacker_power"] = apow
        brief["block_choices"] = rows
        brief["shields_lost_if_you_dont_block"] = "1 or 2 (double breaker)"
        return brief

    plays, attacks = [], []
    for o in options:
        if o.kind == "play":
            c = game.find(o.data["iid"])
            if c:
                plays.append({"option": o.id, "card": c.d.name, "cost": c.d.cost,
                              "type": c.d.type,
                              **({"power": c.d.power} if c.d.power else {})})
        elif o.kind == "attack":
            a = game.find(o.data["iid"])
            if not a:
                continue
            ap = game.power(a, attacking=True)
            row = {"option": o.id, "attacker": f"{a.d.name} ({ap})"}
            if o.data.get("target") == "player":
                unblockable = a.d.unblockable or a.temp_unblockable
                killers = [f"{b.d.name} ({game.power(b)})" for b in free_blockers
                           if game.power(b) >= ap]
                row["target"] = "player"
                if not opp.shields:
                    row["result"] = ("WINS THE GAME NOW" if (unblockable or not free_blockers)
                                     else "wins the game unless blocked")
                else:
                    row["result"] = f"breaks {_breaks(a)} of {len(opp.shields)} shields"
                if killers and not unblockable:
                    row["your_creature_dies_if_blocked_by"] = killers
            else:
                t = game.find(o.data["target"])
                if not t:
                    continue
                tp = game.power(t)
                row["target"] = f"{t.d.name} ({tp})"
                row["result"] = ("you kill it and survive" if ap > tp
                                 else "both die" if ap == tp else "YOUR creature dies")
                if t.d.blocker:
                    row["target_is_a_blocker"] = True
            attacks.append(row)

    brief["affordable_plays"] = plays
    brief["attacks_available"] = attacks
    if any(o.kind == "charge" for o in options):
        brief["you_have_NOT_charged_mana_this_turn"] = True
    if plays:
        cards = ", ".join(p["card"] for p in plays)
        brief["ORDER_OF_PLAY"] = (
            f"You can still summon/cast: {cards}. Attacking ENDS your main step, so "
            f"do all of that FIRST, then attack. Ending the turn now wastes "
            f"{brief['your_untapped_mana']} untapped mana.")
    elif attacks:
        brief["ORDER_OF_PLAY"] = ("Nothing left to play — attack with everything that "
                                  "profitably can, then end the turn.")
    return brief


def _forced_choice(game, idx: int, options):
    """Take guaranteed wins and no-brainers without spending an LLM call."""
    if len(options) == 1:
        return options[0].id, "only legal move"
    opp = game.opponent_of(idx)
    if opp.shields:
        return None
    free_blockers = [b for b in opp.battle if b.d.blocker and not b.tapped]
    for o in options:
        if o.kind != "attack" or o.data.get("target") != "player":
            continue
        a = game.find(o.data["iid"])
        if a and (not free_blockers or a.d.unblockable or a.temp_unblockable):
            return o.id, "no shields left — swinging for the win"
    return None


HOSTILE_VERBS = ("destroy", "tap ", "return", "send to graveyard",
                 "put into mana", "mark", "discard")


def _spell_is_dead(game, idx: int, c) -> bool:
    """Would this spell do nothing? Casting it just throws the card away."""
    if c.d.type != "Spell":
        return False
    text = " ".join(c.d.rules_text).lower()
    mine, theirs = game.players[idx].battle, game.opponent_of(idx).battle
    needs_mine = "your creature" in text or "your creatures" in text
    needs_theirs = "opponent's creature" in text or "opponent's creatures" in text
    if needs_mine and not needs_theirs and not mine:
        return True
    if needs_theirs and not needs_mine and not theirs:
        return True
    return False


def _target_is_good(game, idx: int, o) -> bool:
    """Never destroy your own creature or buff theirs when picking a target."""
    c = game.find(o.data.get("iid")) if o.data.get("iid") is not None else None
    if not c:
        return True
    hostile = any(o.text.lower().startswith(v.strip()) for v in HOSTILE_VERBS)
    return hostile != (c.owner == idx)


def _block_is_good(game, idx: int, o) -> bool:
    """Block when the blocker kills the attacker or lives through it."""
    b = game.find(o.data.get("iid"))
    if not b:
        return False
    m = re.search(r"\((\d+)\) is attacking", game.pending.prompt or "")
    if not m:
        return True
    apow, bpow = int(m.group(1)), game.power(b)
    return bpow > apow or game.has_slayer(b) or bpow >= apow


def _priority(game, idx: int, o) -> int:
    """Recommended order. Charged mana is usable the same turn, and attacking
    ENDS the main step (the engine stops offering plays), so every summon has
    to come before the first attack. Only a game-winning swing jumps the queue."""
    opp = game.opponent_of(idx)
    if o.kind == "attack":
        if o.data.get("target") == "player":
            if not opp.shields:
                return 0                  # closing the game beats developing
            return 7                      # break shields once done summoning
        a, t = game.find(o.data["iid"]), game.find(o.data.get("target"))
        if a and t and game.power(a, attacking=True) > game.power(t):
            return 6                      # free removal, still after summoning
        return 8                          # unfavorable trade
    if o.kind == "play":
        c = game.find(o.data["iid"])
        if c is None:
            return 5
        if _spell_is_dead(game, idx, c):
            return 11                     # worse than doing nothing
        return 4 if c.d.type == "Creature" else 5
    if o.kind == "target":
        return 2 if _target_is_good(game, idx, o) else 11
    if o.kind == "block":
        return 2 if _block_is_good(game, idx, o) else 11
    return {"use_trigger": 1, "yes": 2, "count": 2, "charge": 3,
            "no_block": 9, "done": 9, "skip_trigger": 9,
            "end_turn": 10}.get(o.kind, 5)


def _charge_score(game, idx: int, o):
    """Which card to turn into mana: cover a missing civilization first, then
    the card you're least likely to cast (can't afford it soon, costs most)."""
    me = game.players[idx]
    c = game.find(o.data["iid"])
    if not c:
        return (9, 9, 0)
    have_civs = {m.d.civilization for m in me.mana}
    castable_soon = (c.d.cost or 0) <= len(me.mana) + 1
    return (0 if c.d.civilization not in have_civs else 1,
            1 if castable_soon else 0,
            -(c.d.cost or 0))


def heuristic_choice(game, idx: int) -> int:
    """Plays a competent turn with no model at all: forced wins, then
    charge -> summon -> attack."""
    options = game.pending.options
    forced = _forced_choice(game, idx, options)
    if forced:
        return forced[0]

    def key(o):
        p = _priority(game, idx, o)
        if o.kind == "charge":
            return (p,) + _charge_score(game, idx, o) + (o.id,)
        if o.kind == "play":
            c = game.find(o.data["iid"])
            return (p, -(c.d.cost or 0) if c else 0, 0, 0, o.id)
        return (p, 0, 0, 0, o.id)
    return min(options, key=key).id


def _pushback(game, idx: int, chosen, brief) -> str | None:
    """One shove when the model is about to waste its turn."""
    plays = brief.get("affordable_plays") or []
    if chosen.kind == "end_turn" and brief.get("you_have_NOT_charged_mana_this_turn"):
        return ("You are ending the turn without charging a card into your mana zone. "
                "That is one mana you can never get back, and it is free — charge "
                "something first (you may still act afterwards).")
    if chosen.kind == "end_turn" and (plays or brief.get("attacks_available")):
        return ("You ended the turn while you still have mana to spend or attacks "
                "available — that wastes the whole turn. Re-read tactical_brief and "
                "develop or attack instead unless every option is clearly bad.")
    if chosen.kind == "attack" and plays and game.opponent_of(idx).shields:
        names = ", ".join(str(p.get("card")) for p in plays)
        return (f"Attacking ENDS your main step, so you would forfeit summoning "
                f"{names} this turn. Summon everything you can afford FIRST, then "
                f"attack with everything.")
    return None


async def choose_option(game, player_idx: int, cfg: LLMConfig) -> tuple[int, str]:
    """Returns (option_id, reason). Falls back to a random legal option."""
    options = game.pending.options
    forced = _forced_choice(game, player_idx, options)
    if forced:
        return forced

    brief = tactical_brief(game, player_idx)
    ordered = sorted(options, key=lambda o: _priority(game, player_idx, o))
    payload = {"state": compact_state(game, player_idx), "tactical_brief": brief}
    user = (f"You are '{game.players[player_idx].name}'.\n{json.dumps(payload)}\n\n"
            f"DECISION: {game.pending.prompt}\n"
            "Legal options (recommended order, best first):\n"
            + "\n".join(f"  {o.id}: {o.text}" for o in ordered)
            + '\n\nReply with JSON only: {"reason": "...", "option": <id>}')
    messages = [{"role": "user", "content": user}]
    by_id = {o.id: o for o in options}
    last_err = None
    shoves = 0

    for attempt in range(5):
        try:
            text = await _call(cfg, messages)
            opt_id, reason = _parse(text, set(by_id))
            note = _pushback(game, player_idx, by_id[opt_id], brief)
            if note and shoves < 2:
                shoves += 1
                messages = [{"role": "user", "content": f"{user}\n\n({note} JSON only.)"}]
                continue
            return opt_id, reason
        except Exception as e:  # noqa: BLE001 - any failure -> retry then fallback
            last_err = e
            log.warning("LLM opponent error (attempt %d, model=%s, url=%s): %s",
                        attempt + 1, cfg.model, cfg.base_url, e)
            messages = [{"role": "user", "content": user + f"\n\n(Your previous reply "
                         f"was invalid: {e}. Reply with valid JSON and a legal option id.)"}]
    return (heuristic_choice(game, player_idx),
            f"(LLM unreachable — heuristic move. Error: {last_err})")


def _parse(text: str, legal: set[int]) -> tuple[int, str]:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("no JSON object in reply")
    obj = json.loads(m.group(0))
    opt = int(obj["option"])
    if opt not in legal:
        raise ValueError(f"option {opt} not legal")
    return opt, str(obj.get("reason", ""))[:300]


async def _call(cfg: LLMConfig, messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=90) as client:
        if cfg.provider == "anthropic":
            r = await client.post(
                f"{cfg.base_url}/v1/messages",
                headers={"x-api-key": cfg.api_key,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": cfg.model, "max_tokens": 500,
                      "system": SYSTEM, "messages": messages})
            r.raise_for_status()
            data = r.json()
            return "".join(b.get("text", "") for b in data["content"])
        r = await client.post(
            f"{cfg.base_url}/v1/chat/completions",
            headers={"Authorization": f"Bearer {cfg.api_key}",
                     "content-type": "application/json"},
            json={"model": cfg.model, "temperature": 0.3, "max_tokens": 500,
                  "messages": [{"role": "system", "content": SYSTEM}] + messages})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
