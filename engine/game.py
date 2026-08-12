"""Duel Masters game engine.

The whole game runs as one generator (`Game._script`). Whenever a player must
decide something, the generator yields a `Decision` whose `options` fully
enumerate the legal choices. `Game.submit(option_id)` resumes the game.
This uniform decision/option model drives both the web UI and the LLM player.
"""
from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field

from .cards import CardDef, load_defs


class GameOver(Exception):
    def __init__(self, winner: int, reason: str):
        self.winner = winner
        self.reason = reason


@dataclass
class Option:
    id: int
    text: str
    kind: str
    data: dict = field(default_factory=dict)


@dataclass
class Decision:
    player: int
    kind: str  # main | charge | block | shield_trigger | choose | yes_no
    prompt: str
    options: list[Option]


class CardInstance:
    def __init__(self, iid: int, definition: CardDef, owner: int):
        self.iid = iid
        self.d = definition
        self.owner = owner
        self.tapped = False
        self.sick = False
        # cleared when leaving the battle zone or at end of turn:
        self.temp_power_attacker = 0
        self.temp_double_breaker = False
        self.temp_slayer = False
        self.temp_unblockable = False
        self.chaos_struck = False  # may be attacked while untapped this turn

    def reset_flags(self):
        self.tapped = False
        self.sick = False
        self.temp_power_attacker = 0
        self.temp_double_breaker = False
        self.temp_slayer = False
        self.temp_unblockable = False
        self.chaos_struck = False

    def clear_temps(self):
        self.temp_power_attacker = 0
        self.temp_double_breaker = False
        self.temp_slayer = False
        self.temp_unblockable = False
        self.chaos_struck = False


class Player:
    def __init__(self, idx: int, name: str):
        self.idx = idx
        self.name = name
        self.deck: list[CardInstance] = []
        self.hand: list[CardInstance] = []
        self.shields: list[CardInstance] = []
        self.mana: list[CardInstance] = []
        self.battle: list[CardInstance] = []
        self.grave: list[CardInstance] = []
        self.creeping_plague = False  # this turn, blocked creatures get slayer


class Game:
    def __init__(self, deck_lists: list[list[str]], names=("You", "AI"), seed=None):
        defs = load_defs()
        self.rng = random.Random(seed)
        self.players = [Player(0, names[0]), Player(1, names[1])]
        self._iid = 0
        for p, deck in zip(self.players, deck_lists):
            for card_name in deck:
                p.deck.append(self._make(defs[card_name], p.idx))
            self.rng.shuffle(p.deck)
            p.shields = [p.deck.pop() for _ in range(5)]
            p.hand = [p.deck.pop() for _ in range(5)]
        self.turn = 1
        self.active = 0
        self.phase = "start"
        self.over = False
        self.winner: int | None = None
        self.log: list[str] = []
        self.pending: Decision | None = None
        self._gen = self._run()
        self._advance(None, first=True)

    # ------------------------------------------------------------- plumbing

    def _make(self, d: CardDef, owner: int) -> CardInstance:
        self._iid += 1
        return CardInstance(self._iid, d, owner)

    def _advance(self, answer, first=False):
        try:
            self.pending = self._gen.__next__() if first else self._gen.send(answer)
        except StopIteration:
            self.pending = None

    def submit(self, player: int, option_id: int):
        """Answer the pending decision with one of its option ids."""
        if self.over or not self.pending:
            raise ValueError("no pending decision")
        if player != self.pending.player:
            raise ValueError(f"not player {player}'s decision")
        match = [o for o in self.pending.options if o.id == option_id]
        if not match:
            raise ValueError(f"invalid option {option_id}")
        self._advance(match[0])

    def _run(self):
        try:
            while True:
                yield from self._take_turn()
                self.turn += 1
                self.active ^= 1
        except GameOver as e:
            self.winner = e.winner
            self.over = True
            self.say(f"GAME OVER — {self.players[e.winner].name} wins! ({e.reason})")

    def say(self, msg: str):
        self.log.append(msg)

    # ------------------------------------------------------------ utilities

    def opponent_of(self, idx: int) -> Player:
        return self.players[idx ^ 1]

    def find(self, iid: int) -> CardInstance | None:
        for p in self.players:
            for zone in (p.battle, p.hand, p.mana, p.grave, p.shields, p.deck):
                for c in zone:
                    if c.iid == iid:
                        return c
        return None

    def zone_of(self, inst: CardInstance):
        p = self.players[inst.owner]
        for name, zone in (("battle", p.battle), ("hand", p.hand), ("mana", p.mana),
                           ("grave", p.grave), ("shields", p.shields), ("deck", p.deck)):
            if inst in zone:
                return name, zone
        return None, None

    def power(self, inst: CardInstance, attacking=False) -> int:
        from .dm01 import CARD_SCRIPTS
        p = inst.d.power or 0
        script = CARD_SCRIPTS.get(inst.d.name, {})
        static = script.get("static_power")
        if static:
            p += static(self, inst, attacking)
        if attacking:
            p += inst.d.power_attacker + inst.temp_power_attacker
        return p

    def has_slayer(self, inst: CardInstance) -> bool:
        return inst.d.slayer or inst.temp_slayer

    def desc(self, inst: CardInstance) -> str:
        d = inst.d
        if d.type == "Creature":
            state = ""
            _, zone = self.zone_of(inst)
            if zone is self.players[inst.owner].battle:
                state = ", tapped" if inst.tapped else ", untapped"
            return f"{d.name} ({d.civilization} {self.power(inst)}{state})"
        return f"{d.name} ({d.civilization} spell)"

    # -------------------------------------------------------- zone movement

    def draw(self, idx: int, n: int = 1):
        p = self.players[idx]
        for _ in range(n):
            if not p.deck:
                raise GameOver(idx ^ 1, f"{p.name} cannot draw from an empty deck")
            p.hand.append(p.deck.pop())
            self.say(f"{p.name} draws a card.")

    def destroy(self, inst: CardInstance):
        """Destroy from battle zone, honoring would-be-destroyed replacements."""
        p = self.players[inst.owner]
        if inst not in p.battle:
            return
        p.battle.remove(inst)
        inst.clear_temps()
        inst.tapped = False
        if inst.d.destroy_replacement == "hand":
            p.hand.append(inst)
            self.say(f"{inst.d.name} would be destroyed — returns to {p.name}'s hand instead.")
        elif inst.d.destroy_replacement == "mana":
            p.mana.append(inst)
            self.say(f"{inst.d.name} would be destroyed — goes to {p.name}'s mana zone instead.")
        else:
            p.grave.append(inst)
            self.say(f"{inst.d.name} is destroyed.")

    def bounce(self, inst: CardInstance):
        p = self.players[inst.owner]
        if inst not in p.battle:
            return
        p.battle.remove(inst)
        inst.reset_flags()
        p.hand.append(inst)
        self.say(f"{inst.d.name} returns to {p.name}'s hand.")

    def to_mana_from_battle(self, inst: CardInstance):
        p = self.players[inst.owner]
        if inst not in p.battle:
            return
        p.battle.remove(inst)
        inst.reset_flags()
        p.mana.append(inst)
        self.say(f"{inst.d.name} is put into {p.name}'s mana zone.")

    # ------------------------------------------------------------ turn flow

    def _take_turn(self):
        p = self.players[self.active]
        self.phase = "start"
        self.say(f"— Turn {self.turn}: {p.name} —")
        for c in p.mana:
            c.tapped = False
        for c in p.battle:
            c.tapped = False
            c.sick = False
        if not (self.turn == 1):
            self.draw(p.idx)
        yield from self._main_phase(p)
        yield from self._end_phase(p)

    def _can_pay(self, p: Player, d: CardDef) -> bool:
        untapped = [m for m in p.mana if not m.tapped]
        return len(untapped) >= d.cost and any(m.d.civilization == d.civilization for m in untapped)

    def _pay(self, p: Player, d: CardDef):
        untapped = [m for m in p.mana if not m.tapped]
        pay = [next(m for m in untapped if m.d.civilization == d.civilization)]
        untapped.remove(pay[0])
        while len(pay) < d.cost:
            counts = Counter(m.d.civilization for m in untapped)
            # spend from the most abundant civ; on ties prefer this card's own civ
            best = max(untapped, key=lambda m: (counts[m.d.civilization],
                                                m.d.civilization == d.civilization))
            untapped.remove(best)
            pay.append(best)
        for m in pay:
            m.tapped = True

    def _attack_targets(self, attacker: CardInstance) -> list:
        """Legal targets: 'player' or opposing creatures."""
        opp = self.opponent_of(attacker.owner)
        targets = []
        if not attacker.d.cant_attack_players:
            targets.append("player")
        for c in opp.battle:
            if c.tapped or c.chaos_struck or attacker.d.can_attack_untapped:
                targets.append(c)
        return targets

    def _can_attack(self, c: CardInstance) -> bool:
        return (not c.tapped and not c.sick and not c.d.cant_attack
                and c.d.type == "Creature" and bool(self._attack_targets(c)))

    def _main_phase(self, p: Player):
        self.phase = "main"
        attacked = False
        charged = False
        while True:
            opts = []
            oid = 0
            if not charged:
                for c in p.hand:
                    opts.append(Option(oid := oid + 1,
                                       f"Charge {c.d.name} ({c.d.civilization}) as mana",
                                       "charge", {"iid": c.iid}))
            if not attacked:
                for c in p.hand:
                    if self._can_pay(p, c.d):
                        verb = "Summon" if c.d.type == "Creature" else "Cast"
                        stats = f", {c.d.power} power" if c.d.power else ""
                        opts.append(Option(oid := oid + 1,
                                           f"{verb} {c.d.name} ({c.d.civilization}, cost {c.d.cost}{stats})",
                                           "play", {"iid": c.iid}))
            must_attackers_ready = False
            for c in p.battle:
                if not self._can_attack(c):
                    continue
                if c.d.must_attack:
                    must_attackers_ready = True
                for t in self._attack_targets(c):
                    if t == "player":
                        opp = self.opponent_of(p.idx)
                        label = (f"Attack {opp.name}'s shields ({len(opp.shields)} left)"
                                 if opp.shields else f"Attack {opp.name} directly — FOR THE WIN")
                        opts.append(Option(oid := oid + 1,
                                           f"{c.d.name} ({self.power(c, attacking=True)}): {label}",
                                           "attack", {"iid": c.iid, "target": "player"}))
                    else:
                        opts.append(Option(oid := oid + 1,
                                           f"{c.d.name} ({self.power(c, attacking=True)}): Attack {self.desc(t)}",
                                           "attack", {"iid": c.iid, "target": t.iid}))
            if not must_attackers_ready:
                opts.append(Option(oid + 1, "End turn", "end_turn"))
            prompt = "Your move." if not must_attackers_ready else \
                "Your move. (A creature that must attack is ready, so you can't end the turn yet.)"
            ans = yield Decision(p.idx, "main", prompt, opts)
            if ans.kind == "end_turn":
                return
            if ans.kind == "charge":
                charged = True
                c = next(x for x in p.hand if x.iid == ans.data["iid"])
                p.hand.remove(c)
                c.reset_flags()
                p.mana.append(c)
                self.say(f"{p.name} charges {c.d.name} into mana. ({len(p.mana)} mana)")
            elif ans.kind == "play":
                c = next(x for x in p.hand if x.iid == ans.data["iid"])
                yield from self._play_card(p, c)
            elif ans.kind == "attack":
                attacked = True
                self.phase = "attack"
                attacker = next((x for x in p.battle if x.iid == ans.data["iid"]), None)
                if attacker is None:
                    continue
                if ans.data["target"] == "player":
                    yield from self._attack(attacker, None)
                else:
                    target = self.find(ans.data["target"])
                    yield from self._attack(attacker, target)

    def _play_card(self, p: Player, c: CardInstance, free=False):
        from .dm01 import CARD_SCRIPTS
        if not free:
            self._pay(p, c.d)
        if c in p.hand:
            p.hand.remove(c)
        script = CARD_SCRIPTS.get(c.d.name, {})
        if c.d.type == "Creature":
            c.reset_flags()
            c.sick = True
            p.battle.append(c)
            self.say(f"{p.name} summons {c.d.name} ({c.d.civilization} {c.d.power}).")
            on_play = script.get("on_play")
            if on_play:
                yield from (on_play(self, c) or ())
        else:
            self.say(f"{p.name} casts {c.d.name}.")
            spell = script.get("spell")
            if spell:
                yield from (spell(self, p.idx, c) or ())
            p.grave.append(c)

    # -------------------------------------------------------------- combat

    def _attack(self, attacker: CardInstance, target: CardInstance | None):
        p = self.players[attacker.owner]
        opp = self.opponent_of(attacker.owner)
        attacker.tapped = True
        tdesc = f"{opp.name}" if target is None else self.desc(target)
        self.say(f"{attacker.d.name} attacks {tdesc}!")
        blocker = yield from self._blocker_step(attacker, opp)
        if blocker is not None:
            blocker.tapped = True
            self.say(f"{opp.name} blocks with {blocker.d.name}!")
            if p.creeping_plague:
                attacker.temp_slayer = True
                self.say(f"{attacker.d.name} gets slayer (Creeping Plague).")
            self._battle(attacker, blocker)
            return
        if target is None:
            if not opp.shields:
                raise GameOver(p.idx, f"{attacker.d.name} lands the final blow on {opp.name}")
            n = 2 if (attacker.d.double_breaker or attacker.temp_double_breaker) else 1
            yield from self._break_shields(opp, n)
        else:
            if target in opp.battle:
                self._battle(attacker, target)

    def _blocker_step(self, attacker: CardInstance, opp: Player):
        if attacker.d.unblockable or attacker.temp_unblockable:
            return None
        others = sum(1 for c in self.players[attacker.owner].battle if c is not attacker)
        if attacker.d.unblockable_with_2_others and others >= 2:
            return None
        eligible = [b for b in opp.battle if b.d.blocker and not b.tapped]
        if attacker.d.unblockable_by_power_lte is not None:
            eligible = [b for b in eligible if self.power(b) > attacker.d.unblockable_by_power_lte]
        if not eligible:
            return None
        opts = [Option(0, "Don't block", "no_block")]
        for i, b in enumerate(eligible):
            opts.append(Option(i + 1, f"Block with {b.d.name} ({self.power(b)})",
                               "block", {"iid": b.iid}))
        ans = yield Decision(opp.idx, "block",
                             f"{attacker.d.name} ({self.power(attacker, attacking=True)}) is attacking. Block?",
                             opts)
        if ans.kind == "block":
            return next(b for b in eligible if b.iid == ans.data["iid"])
        return None

    def _battle(self, a: CardInstance, b: CardInstance):
        pa, pb = self.power(a, attacking=True), self.power(b)
        self.say(f"Battle: {a.d.name} ({pa}) vs {b.d.name} ({pb}).")
        a_dies = pa <= pb or self.has_slayer(b)
        b_dies = pb <= pa or self.has_slayer(a)
        a_won, b_won = pa > pb, pb > pa
        if b_dies:
            self.destroy(b)
        if a_dies:
            self.destroy(a)
        if a_won and not a_dies and a.d.dies_after_winning:
            self.destroy(a)
        if b_won and not b_dies and b.d.dies_after_winning:
            self.destroy(b)

    def _break_shields(self, defender: Player, n: int):
        n = min(n, len(defender.shields))
        broken = [defender.shields.pop() for _ in range(n)]
        self.say(f"{defender.name} loses {n} shield{'s' if n > 1 else ''} "
                 f"({len(defender.shields)} left)!")
        triggers = []
        for c in broken:
            defender.hand.append(c)
            if c.d.shield_trigger:
                triggers.append(c)
        for c in triggers:
            opts = [Option(0, f"Use shield trigger: {c.d.name} (free)", "use_trigger"),
                    Option(1, f"Keep {c.d.name} in hand", "skip_trigger")]
            ans = yield Decision(defender.idx, "shield_trigger",
                                 f"Shield trigger revealed: {c.d.name} — use it for free?", opts)
            if ans.kind == "use_trigger":
                self.say(f"{defender.name} uses shield trigger {c.d.name}!")
                yield from self._play_card(defender, c, free=True)

    # ------------------------------------------------------------ end phase

    def _end_phase(self, p: Player):
        from .dm01 import CARD_SCRIPTS
        self.phase = "end"
        for c in list(p.battle):
            eot = CARD_SCRIPTS.get(c.d.name, {}).get("end_of_turn")
            if eot and c in p.battle:
                yield from (eot(self, c) or ())
        for pl in self.players:
            pl.creeping_plague = False
            for c in pl.battle:
                c.clear_temps()

    # ---------------------------------------------------------- state views

    def card_json(self, c: CardInstance, in_battle=False):
        j = {"iid": c.iid, "name": c.d.name, "civ": c.d.civilization, "type": c.d.type,
             "cost": c.d.cost, "race": c.d.race, "power": c.d.power, "image": c.d.image,
             "text": c.d.rules_text}
        if in_battle:
            j.update(tapped=c.tapped, sick=c.sick,
                     current_power=self.power(c),
                     attack_power=self.power(c, attacking=True))
        return j

    def view(self, viewer: int) -> dict:
        out = {"turn": self.turn, "active": self.active, "phase": self.phase,
               "over": self.over, "winner": self.winner, "log": self.log[-60:],
               "players": []}
        for p in self.players:
            out["players"].append({
                "name": p.name, "idx": p.idx,
                "deck_count": len(p.deck), "hand_count": len(p.hand),
                "shield_count": len(p.shields),
                "battle": [self.card_json(c, in_battle=True) for c in p.battle],
                "mana": [{**self.card_json(c), "tapped": c.tapped} for c in p.mana],
                "mana_untapped": sum(1 for c in p.mana if not c.tapped),
                "grave": [self.card_json(c) for c in p.grave],
            })
        out["hand"] = [self.card_json(c) for c in self.players[viewer].hand]
        if self.pending:
            d = self.pending
            out["pending"] = {"player": d.player, "kind": d.kind, "prompt": d.prompt}
            if d.player == viewer:
                out["pending"]["options"] = [
                    {"id": o.id, "text": o.text, "kind": o.kind, **o.data} for o in d.options]
        return out
