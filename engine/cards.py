"""Card definitions: load scraped JSON and parse keyword abilities from rules text."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data" / "cards.json"

CIVS = ("Light", "Water", "Darkness", "Fire", "Nature")


@dataclass
class CardDef:
    id: str
    name: str
    civilization: str
    type: str  # "Creature" | "Spell"
    cost: int
    race: str | None
    power: int | None
    image: str
    rules_text: list[str]

    # parsed keywords
    blocker: bool = False
    double_breaker: bool = False
    shield_trigger: bool = False
    slayer: bool = False
    power_attacker: int = 0
    cant_attack: bool = False
    cant_attack_players: bool = False
    unblockable: bool = False
    unblockable_by_power_lte: int | None = None
    unblockable_with_2_others: bool = False
    can_attack_untapped: bool = False
    must_attack: bool = False
    dies_after_winning: bool = False
    destroy_replacement: str | None = None  # "hand" | "mana"

    # lines the keyword parser didn't consume -> must be covered by a script
    scripted_lines: list[str] = field(default_factory=list)


def _strip_reminder(line: str) -> str:
    return re.sub(r"\s*\([^)]*\)", "", line).strip()


def _parse_keywords(d: CardDef) -> None:
    for raw in d.rules_text:
        line = _strip_reminder(raw)
        low = line.lower().rstrip(".")
        m = re.fullmatch(r"power attacker \+(\d+)", low)
        if low == "blocker":
            d.blocker = True
        elif low == "double breaker":
            d.double_breaker = True
        elif low == "shield trigger":
            d.shield_trigger = True
        elif low == "slayer":
            d.slayer = True
        elif m:
            d.power_attacker = int(m.group(1))
        elif low == "this creature can't attack players":
            d.cant_attack_players = True
        elif low == "this creature can't attack":
            d.cant_attack = True
        elif low == "this creature can't be blocked":
            d.unblockable = True
        elif (m := re.fullmatch(
                r"this creature can't be blocked by any creature that has power (\d+) or less", low)):
            d.unblockable_by_power_lte = int(m.group(1))
        elif low == ("this creature can't be blocked while you have at least 2 "
                     "other creatures in the battle zone"):
            d.unblockable_with_2_others = True
        elif low == "this creature can attack untapped creatures":
            d.can_attack_untapped = True
        elif low == "this creature attacks each turn if able":
            d.must_attack = True
        elif low == "when this creature wins a battle, destroy it":
            d.dies_after_winning = True
        elif low in (
            "when this creature would be destroyed, put it into your hand instead",
            "when this creature would be destroyed, return it to your hand instead",
        ):
            d.destroy_replacement = "hand"
        elif low == "when this creature would be destroyed, put it into your mana zone instead":
            d.destroy_replacement = "mana"
        else:
            d.scripted_lines.append(line)


_defs: dict[str, CardDef] | None = None


def load_defs() -> dict[str, CardDef]:
    """Card defs keyed by name."""
    global _defs
    if _defs is None:
        raw = json.loads(DATA.read_text())
        _defs = {}
        for c in raw:
            d = CardDef(
                id=c["id"], name=c["name"], civilization=c["civilization"],
                type=c["type"], cost=c["cost"], race=c.get("race"),
                power=c.get("power"), image=c["image"], rules_text=c["rules_text"],
            )
            _parse_keywords(d)
            _defs[d.name] = d
    return _defs
