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

YOU ARE PLAYING TO WIN. Be aggressive. The most common losing mistake is \
passing the turn with unspent mana or with attacks available.

STRATEGY PLAYBOOK
1. Charge exactly one mana per turn. Charge a card you're least likely to cast.
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
    if plays and brief["your_untapped_mana"]:
        brief["WARNING"] = (f"You have {brief['your_untapped_mana']} untapped mana and "
                            f"{len(plays)} affordable card(s). Ending the turn wastes them.")
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


def _priority(game, idx: int, o) -> int:
    """Aggressive options first: models are biased toward the top of a list."""
    opp = game.opponent_of(idx)
    if o.kind == "attack":
        if o.data.get("target") == "player":
            return 0 if not opp.shields else 2
        a, t = game.find(o.data["iid"]), game.find(o.data.get("target"))
        if a and t and game.power(a, attacking=True) > game.power(t):
            return 1                      # free removal
        return 4
    return {"use_trigger": 0, "block": 1, "target": 1, "play": 3,
            "charge": 5, "done": 6, "skip_trigger": 7, "end_turn": 9}.get(o.kind, 4)


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
            "Legal options (strongest first):\n"
            + "\n".join(f"  {o.id}: {o.text}" for o in ordered)
            + '\n\nReply with JSON only: {"reason": "...", "option": <id>}')
    messages = [{"role": "user", "content": user}]
    by_id = {o.id: o for o in options}
    can_develop = bool(brief.get("affordable_plays") or brief.get("attacks_available"))
    last_err = None
    nudged = False

    for attempt in range(4):
        try:
            text = await _call(cfg, messages)
            opt_id, reason = _parse(text, set(by_id))
            if (not nudged and can_develop and by_id[opt_id].kind == "end_turn"):
                nudged = True   # one shove against passing the turn away
                messages = [{"role": "user", "content": user +
                             "\n\n(You chose to end the turn while you still have mana to "
                             "spend or attacks available — that wastes the whole turn. "
                             "Re-read tactical_brief and pick a developing or attacking "
                             "move unless every single one is clearly bad. JSON only.)"}]
                continue
            return opt_id, reason
        except Exception as e:  # noqa: BLE001 - any failure -> retry then fallback
            last_err = e
            log.warning("LLM opponent error (attempt %d, model=%s, url=%s): %s",
                        attempt + 1, cfg.model, cfg.base_url, e)
            messages = [{"role": "user", "content": user + f"\n\n(Your previous reply "
                         f"was invalid: {e}. Reply with valid JSON and a legal option id.)"}]
    fallback = max(options, key=lambda o: -_priority(game, player_idx, o))
    return fallback.id, f"(LLM unreachable — heuristic move. Error: {last_err})"


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
