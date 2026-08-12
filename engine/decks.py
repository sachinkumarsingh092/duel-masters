"""Deck loading, validation, and random deck generation."""
import json
import random
from pathlib import Path

from .cards import CIVS, load_defs

DECKS = Path(__file__).resolve().parent.parent / "decks" / "decks.json"

RANDOM = "Random deck"


def random_deck(rng: random.Random | None = None) -> list[str]:
    """A random but playable 40-card deck: two civilizations, capped top-end
    and spell counts so the curve isn't hopeless."""
    rng = rng or random.Random()
    defs = load_defs()
    civs = rng.sample(CIVS, 2)
    candidates = [d for d in defs.values() if d.civilization in civs for _ in range(4)]
    rng.shuffle(candidates)
    deck: list[str] = []
    counts: dict[str, int] = {}
    big = spells = 0
    for d in candidates:
        if len(deck) == 40:
            break
        if counts.get(d.name, 0) >= 4:
            continue
        if (d.cost or 0) >= 6 and big >= 8:
            continue
        if d.type == "Spell" and spells >= 12:
            continue
        deck.append(d.name)
        counts[d.name] = counts.get(d.name, 0) + 1
        big += (d.cost or 0) >= 6
        spells += d.type == "Spell"
    for d in candidates:  # top up if the caps left us short
        if len(deck) == 40:
            break
        if counts.get(d.name, 0) < 4:
            deck.append(d.name)
            counts[d.name] = counts.get(d.name, 0) + 1
    return deck


def load_decks() -> dict[str, dict]:
    return json.loads(DECKS.read_text())


def deck_list(name: str, rng: random.Random | None = None) -> list[str]:
    """Expand a deck to a flat 40-card list of card names."""
    if name == RANDOM:
        return random_deck(rng)
    deck = load_decks()[name]
    defs = load_defs()
    out = []
    for card, count in deck["cards"].items():
        if card not in defs:
            raise ValueError(f"unknown card in deck {name!r}: {card}")
        if not 1 <= count <= 4:
            raise ValueError(f"bad count for {card} in {name!r}: {count}")
        out.extend([card] * count)
    if len(out) != 40:
        raise ValueError(f"deck {name!r} has {len(out)} cards, needs 40")
    return out
