"""DM-01 card effect scripts.

Registry entries per card name:
  on_play(game, inst)        - creature enter-battle-zone trigger (generator)
  spell(game, player, card)  - spell resolution (generator)
  end_of_turn(game, inst)    - end-of-turn trigger (generator)
  static_power(game, inst, attacking) -> int  - continuous power bonus
"""
from __future__ import annotations

from .game import Decision, Option


# ------------------------------------------------------------------ helpers

def pick(game, chooser, cands, n, prompt, verb, optional=False):
    """Iteratively pick up to n cards from cands.
    optional=True -> 'choose up to N' (may stop early / pick none);
    optional=False -> mandatory: must pick n (or all remaining if fewer)."""
    chosen = []
    cands = list(cands)
    while cands and len(chosen) < n:
        opts = []
        for i, c in enumerate(cands):
            opts.append(Option(i, f"{verb} {game.desc(c)}", "target", {"iid": c.iid}))
        if optional:
            opts.append(Option(len(cands), "Done / skip", "done"))
        ans = yield Decision(chooser, "choose", prompt, opts)
        if ans.kind == "done":
            break
        c = next(x for x in cands if x.iid == ans.data["iid"])
        cands.remove(c)
        chosen.append(c)
    return chosen


def instant(fn):
    """Wrap a plain (non-decision) effect function as a generator script.
    (The engine also tolerates hooks that return None, so this is optional.)"""
    def wrapper(*args):
        fn(*args)
        return
        yield  # pragma: no cover - makes wrapper a generator
    return wrapper


def yes_no(game, player, prompt, yes_text, no_text="No"):
    ans = yield Decision(player, "yes_no", prompt,
                         [Option(0, yes_text, "yes"), Option(1, no_text, "no")])
    return ans.kind == "yes"


def draw_up_to(game, player, n):
    p = game.players[player]
    n = min(n, len(p.deck))
    if n <= 0:
        return
    opts = [Option(i, f"Draw {i} card{'s' if i != 1 else ''}", "count", {"n": i})
            for i in range(n + 1)]
    ans = yield Decision(player, "choose", f"Draw up to {n} cards.", opts)
    if ans.data["n"]:
        game.draw(player, ans.data["n"])


def search_deck(game, player, prompt, filt=lambda c: True, reveal=False):
    """Search own deck, optionally take one matching card to hand, shuffle."""
    p = game.players[player]
    cands = sorted([c for c in p.deck if filt(c)], key=lambda c: (c.d.cost, c.d.name))
    opts = [Option(i, f"Take {c.d.name} ({c.d.civilization}, cost {c.d.cost})",
                   "target", {"iid": c.iid}) for i, c in enumerate(cands)]
    opts.append(Option(len(opts), "Take nothing", "done"))
    ans = yield Decision(player, "choose", prompt, opts)
    if ans.kind != "done":
        c = next(x for x in cands if x.iid == ans.data["iid"])
        p.deck.remove(c)
        p.hand.append(c)
        if reveal:
            game.say(f"{p.name} takes {c.d.name} from the deck (revealed).")
        else:
            game.say(f"{p.name} takes a card from the deck.")
    else:
        game.say(f"{p.name} searches the deck and takes nothing.")
    game.rng.shuffle(p.deck)


def discard_random(game, victim):
    p = game.players[victim]
    if not p.hand:
        return
    c = game.rng.choice(p.hand)
    p.hand.remove(c)
    p.grave.append(c)
    game.say(f"{p.name} discards {c.d.name} at random.")


def mana_to_grave(game, player, n):
    p = game.players[player]
    picked = yield from pick(game, player, p.mana, min(n, len(p.mana)),
                             f"Put {n} card(s) from your mana zone into your graveyard.",
                             "Send to graveyard")
    for c in picked:
        p.mana.remove(c)
        c.tapped = False
        p.grave.append(c)
        game.say(f"{p.name} puts {c.d.name} from mana zone into graveyard.")


def top_deck_to_mana(game, player, n):
    p = game.players[player]
    for _ in range(min(n, len(p.deck))):
        c = p.deck.pop()
        p.mana.append(c)
        game.say(f"{p.name} puts {c.d.name} from the top of the deck into mana. "
                 f"({len(p.mana)} mana)")


def battle_creatures(game, owner=None, opponent_of=None, filt=lambda g, c: True):
    out = []
    for p in game.players:
        if owner is not None and p.idx != owner:
            continue
        if opponent_of is not None and p.idx == opponent_of:
            continue
        out.extend(c for c in p.battle if filt(game, c))
    return out


def has_race(game, player, race):
    return any(c.d.race == race for c in game.players[player].battle)


# --------------------------------------------------------- effect factories

def fx_destroy_enemy(max_power=None, untapped_only=False):
    def spell(game, player, card):
        cands = [c for c in game.opponent_of(player).battle
                 if (max_power is None or game.power(c) <= max_power)
                 and (not untapped_only or not c.tapped)]
        picked = yield from pick(game, player, cands, 1, f"{card.d.name}: choose a creature to destroy.",
                                 "Destroy")
        for c in picked:
            game.destroy(c)
    return spell


def fx_tap_enemy(n, optional=False):
    def spell(game, player, card):
        cands = [c for c in game.opponent_of(player).battle if not c.tapped]
        picked = yield from pick(game, player, cands, n,
                                 f"{card.d.name}: choose creature(s) to tap.", "Tap",
                                 optional=optional)
        for c in picked:
            c.tapped = True
            game.say(f"{c.d.name} is tapped.")
    return spell


def fx_bounce_any(n, optional=False):
    def spell(game, player, card):
        cands = battle_creatures(game)
        picked = yield from pick(game, player, cands, n,
                                 f"{card.d.name}: choose creature(s) to return to hand.",
                                 "Return", optional=optional)
        for c in picked:
            game.bounce(c)
    return spell


def fx_pump(n_targets, pa_bonus, double_breaker=False, all_own=False):
    def spell(game, player, card):
        own = game.players[player].battle
        if all_own:
            targets = list(own)
        else:
            targets = yield from pick(game, player, own, n_targets,
                                      f"{card.d.name}: choose your creature to power up.",
                                      "Power up")
        for c in targets:
            c.temp_power_attacker += pa_bonus
            if double_breaker:
                c.temp_double_breaker = True
            game.say(f"{c.d.name} gets \"power attacker +{pa_bonus}\""
                     + (" and \"double breaker\"" if double_breaker else "") + " this turn.")
    return spell


def fx_unblockable(n, optional=False):
    def spell(game, player, card):
        own = [c for c in game.players[player].battle]
        picked = yield from pick(game, player, own, n,
                                 f"{card.d.name}: choose your creature(s) to make unblockable this turn.",
                                 "Make unblockable", optional=optional)
        for c in picked:
            c.temp_unblockable = True
            game.say(f"{c.d.name} can't be blocked this turn.")
    return spell


def fx_self_sac(n=1):
    def on_play(game, inst):
        p = game.players[inst.owner]
        picked = yield from pick(game, inst.owner, p.battle, n,
                                 f"{inst.d.name}: destroy {n} of your creatures.", "Destroy")
        for c in picked:
            game.destroy(c)
    return on_play


def fx_opp_chooses_sac(to_mana=False):
    def on_play(game, inst):
        opp = game.opponent_of(inst.owner)
        verb = "Put into mana zone" if to_mana else "Destroy"
        picked = yield from pick(game, opp.idx, opp.battle, 1,
                                 f"{inst.d.name}: choose one of your creatures to "
                                 f"{'put into your mana zone' if to_mana else 'destroy'}.", verb)
        for c in picked:
            (game.to_mana_from_battle if to_mana else game.destroy)(c)
    return on_play


def fx_eot_untap_self(game, inst):
    if inst.tapped:
        if (yield from yes_no(game, inst.owner, f"Untap {inst.d.name}?", f"Untap {inst.d.name}")):
            inst.tapped = False
            game.say(f"{inst.d.name} untaps.")


def fx_race_pump(race, bonus, attacks_only):
    def static_power(game, inst, attacking):
        if attacks_only and not attacking:
            return 0
        return bonus if has_race(game, inst.owner, race) else 0
    return static_power


# ----------------------------------------------------------------- registry

def _aqua_sniper(game, inst):
    picked = yield from pick(game, inst.owner, battle_creatures(game), 2,
                             "Aqua Sniper: choose up to 2 creatures to return to hands.",
                             "Return", optional=True)
    for c in picked:
        game.bounce(c)


def _gigaberos(game, inst):
    p = game.players[inst.owner]
    others = [c for c in p.battle]
    can_sac_two = len(others) >= 2
    opts = [Option(0, "Destroy this creature (Gigaberos)", "self")]
    if can_sac_two:
        opts.insert(0, Option(1, "Destroy 2 of your creatures", "sac"))
    ans = yield Decision(inst.owner, "choose", "Gigaberos: choose one.", opts)
    if ans.kind == "sac":
        picked = yield from pick(game, inst.owner, p.battle, 2,
                                 "Destroy 2 of your creatures.", "Destroy")
        for c in picked:
            game.destroy(c)
    else:
        game.destroy(inst)


def _rothus(game, inst):
    p = game.players[inst.owner]
    picked = yield from pick(game, inst.owner, p.battle, 1,
                             "Rothus: destroy one of your creatures.", "Destroy")
    for c in picked:
        game.destroy(c)
    opp = game.opponent_of(inst.owner)
    picked = yield from pick(game, opp.idx, opp.battle, 1,
                             "Rothus: choose one of your creatures to destroy.", "Destroy")
    for c in picked:
        game.destroy(c)


def _illusionary_merfolk(game, inst):
    if has_race(game, inst.owner, "Cyber Lord"):
        yield from draw_up_to(game, inst.owner, 3)


def _saucer_head_shark(game, inst):
    for c in battle_creatures(game, filt=lambda g, x: g.power(x) <= 2000):
        game.bounce(c)


def _gigargon(game, inst):
    p = game.players[inst.owner]
    cands = [c for c in p.grave if c.d.type == "Creature"]
    picked = yield from pick(game, inst.owner, cands, 2,
                             "Gigargon: return up to 2 creatures from your graveyard to your hand.",
                             "Return", optional=True)
    for c in picked:
        p.grave.remove(c)
        p.hand.append(c)
        game.say(f"{p.name} returns {c.d.name} from graveyard to hand.")


def _unicorn_fish(game, inst):
    picked = yield from pick(game, inst.owner, battle_creatures(game), 1,
                             "Unicorn Fish: you may return a creature to its owner's hand.",
                             "Return", optional=True)
    for c in picked:
        game.bounce(c)


def _miele(game, inst):
    cands = [c for c in game.opponent_of(inst.owner).battle if not c.tapped]
    picked = yield from pick(game, inst.owner, cands, 1,
                             "Miele: you may tap one of your opponent's creatures.",
                             "Tap", optional=True)
    for c in picked:
        c.tapped = True
        game.say(f"{c.d.name} is tapped.")


def _aqua_hulcus(game, inst):
    if game.players[inst.owner].deck and (
            yield from yes_no(game, inst.owner, "Aqua Hulcus: draw a card?", "Draw a card")):
        game.draw(inst.owner)


def _poisonous_mushroom(game, inst):
    p = game.players[inst.owner]
    picked = yield from pick(game, inst.owner, p.hand, 1,
                             "Poisonous Mushroom: you may put a card from your hand into your mana zone.",
                             "Put into mana", optional=True)
    for c in picked:
        p.hand.remove(c)
        c.reset_flags()
        p.mana.append(c)
        game.say(f"{p.name} puts {c.d.name} into mana. ({len(p.mana)} mana)")


def _thorny_mandra(game, inst):
    p = game.players[inst.owner]
    cands = [c for c in p.grave if c.d.type == "Creature"]
    picked = yield from pick(game, inst.owner, cands, 1,
                             "Thorny Mandra: you may put a creature from your graveyard into your mana zone.",
                             "Put into mana", optional=True)
    for c in picked:
        p.grave.remove(c)
        p.mana.append(c)
        game.say(f"{p.name} puts {c.d.name} from graveyard into mana.")


def _dark_reversal(game, player, card):
    p = game.players[player]
    cands = [c for c in p.grave if c.d.type == "Creature"]
    picked = yield from pick(game, player, cands, 1,
                             "Dark Reversal: return a creature from your graveyard to your hand.",
                             "Return")
    for c in picked:
        p.grave.remove(c)
        p.hand.append(c)
        game.say(f"{p.name} returns {c.d.name} from graveyard to hand.")


def _natural_snare(game, player, card):
    cands = list(game.opponent_of(player).battle)
    picked = yield from pick(game, player, cands, 1,
                             "Natural Snare: choose an opponent's creature to put into their mana zone.",
                             "Send to mana")
    for c in picked:
        game.to_mana_from_battle(c)


def _pangaeas_song(game, player, card):
    p = game.players[player]
    picked = yield from pick(game, player, p.battle, 1,
                             "Pangaea's Song: put one of your creatures into your mana zone.",
                             "Send to mana")
    for c in picked:
        game.to_mana_from_battle(c)


def _meteosaur(game, inst):
    cands = [c for c in game.opponent_of(inst.owner).battle if game.power(c) <= 2000]
    picked = yield from pick(game, inst.owner, cands, 1,
                             "Meteosaur: destroy an enemy creature with power 2000 or less.",
                             "Destroy")
    for c in picked:
        game.destroy(c)


def _holy_awe(game, player, card):
    for c in game.opponent_of(player).battle:
        c.tapped = True
    game.say("Holy Awe taps all opposing creatures!")
    yield from ()


def _chaos_strike(game, player, card):
    cands = [c for c in game.opponent_of(player).battle if not c.tapped]
    picked = yield from pick(game, player, cands, 1,
                             "Chaos Strike: choose an untapped enemy creature — it can be attacked this turn.",
                             "Mark")
    for c in picked:
        c.chaos_struck = True
        game.say(f"{c.d.name} can be attacked this turn as though it were tapped.")


def _creeping_plague(game, player, card):
    game.players[player].creeping_plague = True
    game.say("This turn, your creatures get slayer when blocked.")
    yield from ()


def _toel_eot(game, inst):
    p = game.players[inst.owner]
    if any(c.tapped for c in p.battle):
        if (yield from yes_no(game, inst.owner, "Toel: untap all your creatures?",
                              "Untap all")):
            for c in p.battle:
                c.tapped = False
            game.say(f"{p.name}'s creatures untap (Toel).")


def _bolshack_power(game, inst, attacking):
    if not attacking:
        return 0
    return 1000 * sum(1 for c in game.players[inst.owner].grave if c.d.civilization == "Fire")


def _iocant_power(game, inst, attacking):
    return 2000 if has_race(game, inst.owner, "Angel Command") else 0


CARD_SCRIPTS: dict[str, dict] = {
    # ---- Light
    "Urth, Purifying Elemental": {"end_of_turn": fx_eot_untap_self},
    "Frei, Vizier of Air": {"end_of_turn": fx_eot_untap_self},
    "Toel, Vizier of Hope": {"end_of_turn": _toel_eot},
    "Ruby Grass": {"end_of_turn": fx_eot_untap_self},
    "Iocant, the Oracle": {"static_power": _iocant_power},
    "Miele, Vizier of Lightning": {"on_play": _miele},
    "Rayla, Truth Enforcer": {"on_play": lambda g, i: search_deck(
        g, i.owner, "Rayla: search your deck for a spell.",
        lambda c: c.d.type == "Spell", reveal=True)},
    "Holy Awe": {"spell": _holy_awe},
    "Solar Ray": {"spell": fx_tap_enemy(1)},
    "Moonlight Flash": {"spell": fx_tap_enemy(2, optional=True)},
    "Sonic Wing": {"spell": fx_unblockable(1)},
    "Laser Wing": {"spell": fx_unblockable(2, optional=True)},
    # ---- Water
    "Aqua Sniper": {"on_play": _aqua_sniper},
    "Aqua Hulcus": {"on_play": _aqua_hulcus},
    "King Ripped-Hide": {"on_play": lambda g, i: draw_up_to(g, i.owner, 2)},
    "Illusionary Merfolk": {"on_play": _illusionary_merfolk},
    "Saucer-Head Shark": {"on_play": _saucer_head_shark},
    "Unicorn Fish": {"on_play": _unicorn_fish},
    "Virtual Tripwire": {"spell": fx_tap_enemy(1)},
    "Brain Serum": {"spell": lambda g, p, c: draw_up_to(g, p, 2)},
    "Crystal Memory": {"spell": lambda g, p, c: search_deck(
        g, p, "Crystal Memory: search your deck for any card.")},
    "Spiral Gate": {"spell": fx_bounce_any(1)},
    "Teleportation": {"spell": fx_bounce_any(2, optional=True)},
    # ---- Darkness
    "Black Feather, Shadow of Rage": {"on_play": fx_self_sac(1)},
    "Stinger Worm": {"on_play": fx_self_sac(1)},
    "Gigaberos": {"on_play": _gigaberos},
    "Swamp Worm": {"on_play": fx_opp_chooses_sac()},
    "Masked Horror, Shadow of Scorn": {"on_play": instant(
        lambda g, i: discard_random(g, i.owner ^ 1))},
    "Vampire Silphy": {"on_play": instant(lambda g, i: [
        g.destroy(c) for c in battle_creatures(g, filt=lambda gg, x: gg.power(x) <= 3000)])},
    "Gigargon": {"on_play": _gigargon},
    "Terror Pit": {"spell": fx_destroy_enemy()},
    "Death Smoke": {"spell": fx_destroy_enemy(untapped_only=True)},
    "Ghost Touch": {"spell": instant(lambda g, p, c: discard_random(g, p ^ 1))},
    "Dark Reversal": {"spell": _dark_reversal},
    "Creeping Plague": {"spell": _creeping_plague},
    "Chaos Strike": {"spell": _chaos_strike},
    # ---- Fire
    "Scarlet Skyterror": {"on_play": instant(lambda g, i: [
        g.destroy(c) for c in battle_creatures(g, filt=lambda gg, x: x.d.blocker)])},
    "Rothus, the Traveler": {"on_play": _rothus},
    "Meteosaur": {"on_play": _meteosaur},
    "Artisan Picora": {"on_play": lambda g, i: mana_to_grave(g, i.owner, 1)},
    "Onslaughter Triceps": {"on_play": lambda g, i: mana_to_grave(g, i.owner, 1)},
    "Explosive Fighter Ucarn": {"on_play": lambda g, i: mana_to_grave(g, i.owner, 2)},
    "Bolshack Dragon": {"static_power": _bolshack_power},
    "Fatal Attacker Horvath": {"static_power": fx_race_pump("Armorloid", 2000, True)},
    "Armored Walker Urherion": {"static_power": fx_race_pump("Human", 2000, True)},
    "Crimson Hammer": {"spell": fx_destroy_enemy(max_power=2000)},
    "Tornado Flame": {"spell": fx_destroy_enemy(max_power=4000)},
    "Burning Power": {"spell": fx_pump(1, 2000)},
    "Aura Blast": {"spell": fx_pump(0, 2000, all_own=True)},
    "Magma Gazer": {"spell": fx_pump(1, 4000, double_breaker=True)},
    # ---- Nature
    "Bronze-Arm Tribe": {"on_play": instant(lambda g, i: top_deck_to_mana(g, i.owner, 1))},
    "Poisonous Mushroom": {"on_play": _poisonous_mushroom},
    "Thorny Mandra": {"on_play": _thorny_mandra},
    "Storm Shell": {"on_play": fx_opp_chooses_sac(to_mana=True)},
    "Natural Snare": {"spell": _natural_snare},
    "Pangaea's Song": {"spell": _pangaeas_song},
    "Ultimate Force": {"spell": instant(lambda g, p, c: top_deck_to_mana(g, p, 2))},
    "Dimension Gate": {"spell": lambda g, p, c: search_deck(
        g, p, "Dimension Gate: search your deck for a creature.",
        lambda x: x.d.type == "Creature", reveal=True)},
}
