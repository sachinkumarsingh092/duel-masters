"""Engine tests: targeted rules checks + random-playout fuzzing."""
import itertools
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.cards import load_defs
from engine.decks import deck_list, load_decks
from engine.dm01 import CARD_SCRIPTS
from engine.game import Game


def test_coverage():
    """Every non-keyword rules line must have a script."""
    for d in load_defs().values():
        assert not (d.scripted_lines and d.name not in CARD_SCRIPTS), \
            f"{d.name} missing script for {d.scripted_lines}"


def test_decks_valid():
    for name in load_decks():
        assert len(deck_list(name)) == 40


def test_keywords_parsed():
    defs = load_defs()
    assert defs["Gran Gure, Space Guardian"].blocker
    assert defs["Gran Gure, Space Guardian"].cant_attack_players
    assert defs["Hanusa, Radiance Elemental"].double_breaker
    assert defs["Terror Pit"].shield_trigger
    assert defs["Bone Assassin, the Ripper"].slayer
    assert defs["Brawler Zyler"].power_attacker == 2000
    assert defs["Candy Drop"].unblockable
    assert defs["Tower Shell"].unblockable_by_power_lte == 4000
    assert defs["Tropico"].unblockable_with_2_others
    assert defs["Gatling Skyterror"].can_attack_untapped
    assert defs["Deadly Fighter Braid Claw"].must_attack
    assert defs["Bone Spider"].dies_after_winning
    assert defs["Chilias, the Oracle"].destroy_replacement == "hand"
    assert defs["Mighty Shouter"].destroy_replacement == "mana"
    assert defs["Marine Flower"].cant_attack


def test_first_player_skips_draw():
    g = Game([deck_list("Blazing Speed"), deck_list("Shadow Legion")], seed=1)
    assert len(g.players[0].hand) == 5  # no draw on turn 1
    assert len(g.players[0].deck) == 30
    assert len(g.players[0].shields) == 5


def _random_playout(seed: int, max_steps=3000) -> Game:
    rng = random.Random(seed)
    names = list(load_decks())
    g = Game([deck_list(rng.choice(names)), deck_list(rng.choice(names))],
             seed=seed)
    for _ in range(max_steps):
        if g.over or not g.pending:
            break
        opt = rng.choice(g.pending.options)
        g.submit(g.pending.player, opt.id)
    return g


def _greedy_playout(seed: int, max_steps=4000) -> Game:
    """Prefers playing cards and attacking, so card scripts actually resolve.
    Random playouts rarely cast anything, which hid a crash in an on-play
    script that returned None instead of a generator."""
    rng = random.Random(seed)
    names = list(load_decks())
    g = Game([deck_list(rng.choice(names)), deck_list(rng.choice(names))], seed=seed)
    rank = {"play": 0, "attack": 1, "use_trigger": 0, "target": 1, "block": 2,
            "charge": 3, "yes": 2, "count": 2, "done": 4, "end_turn": 5}
    for _ in range(max_steps):
        if g.over or not g.pending:
            break
        opts = g.pending.options
        best = min(rank.get(o.kind, 3) for o in opts)
        choice = rng.choice([o for o in opts if rank.get(o.kind, 3) == best])
        g.submit(g.pending.player, choice.id)
    return g


def test_greedy_playouts():
    """Every card script must resolve without blowing up."""
    finished = 0
    for seed in range(80):
        g = _greedy_playout(seed)
        if g.over:
            finished += 1
            assert g.winner in (0, 1)
    print(f"\n{finished}/80 greedy games finished")
    assert finished >= 70


def test_random_playouts():
    """Fuzz: many full random games must terminate cleanly."""
    finished = 0
    for seed in range(120):
        g = _random_playout(seed)
        assert g.over or g.pending is not None, f"seed {seed}: game wedged"
        if g.over:
            finished += 1
            assert g.winner in (0, 1)
            # zone conservation: every card accounted for
            for p in g.players:
                total = sum(len(z) for z in
                            (p.deck, p.hand, p.shields, p.mana, p.battle, p.grave))
                other = g.players[p.idx ^ 1]
                grand = total + sum(len(z) for z in
                                    (other.deck, other.hand, other.shields,
                                     other.mana, other.battle, other.grave))
                assert grand == 80, f"seed {seed}: {grand} cards total"
    print(f"\n{finished}/120 random games finished (rest hit step cap)")
    assert finished >= 100


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                fails += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if fails else 0)
