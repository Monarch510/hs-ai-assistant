"""局面数据结构：单位、一方状态、两方棋盘。

依赖 .model_effects（Effect / parse_effect）和 .cards；顶层不 import 门面 model（会成环）。
评估用的 _DB 缓存留在门面 model 上，这里需要时在函数内部回查。
"""

from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass, field

from .cards import Card
from .model_effects import Effect, parse_effect

@dataclass
class Unit:
    card_id: str
    name: str
    atk: int
    hp: int
    max_hp: int = 0
    taunt: bool = False
    divine: bool = False
    charge: bool = False
    rush: bool = False
    rush_only: bool = False
    reborn: bool = False          # 已经复生过（避免无限复活）
    dormant: bool = False         # 休眠：不能攻击，也不能被攻击      # 本回合只能攻击随从（突袭刚入场）
    windfury: bool = False
    lifesteal: bool = False
    poisonous: bool = False
    spell_damage: int = 0
    attacks_left: int = 1
    frozen: bool = False
    can_attack_now: bool = True
    alive: bool = True

    def __post_init__(self) -> None:
        if self.max_hp <= 0:
            self.max_hp = max(self.hp, 1)

    @property
    def value(self) -> float:
        if not self.alive:
            return 0.0
        v = self.atk * 1.0 + self.hp * 1.05
        if self.taunt:
            v += 0.6
        if self.divine:
            v += 1.4 + self.atk * 0.2
        if self.windfury:
            v += self.atk * 0.5
        if self.poisonous:
            v += 1.5
        if self.lifesteal:
            v += 0.6
        if self.spell_damage:
            v += self.spell_damage * 1.2
        return v

    def take_damage(self, amount: int) -> int:
        """返回实际造成的伤害（圣盾会吃掉一次）。"""
        if amount <= 0 or not self.alive:
            return 0
        if self.divine:
            self.divine = False
            return 0
        self.hp -= amount
        if self.hp <= 0:
            self.alive = False
        return amount

    def heal(self, amount: int) -> None:
        if self.alive:
            self.hp = min(self.max_hp, self.hp + amount)

    def clone(self) -> "Unit":
        return copy.copy(self)

@dataclass
class SideState:
    hero_hp: int = 30
    armor: int = 0
    mana: int = 0
    max_mana: int = 0
    deck: list[Card] = field(default_factory=list)
    hand: list[Card] = field(default_factory=list)
    board: list[Unit] = field(default_factory=list)
    fatigue: int = 0
    weapon: Unit | None = None
    hero_class: str = "NEUTRAL"
    hero_power_used: bool = False
    hero_power: "Card | None" = None
    spells_cast: int = 0
    weapon_attacks_left: int = 0     # 英雄本回合还能用武器攻击几次
    hero_atk: int = 0                # 英雄自身攻击力（没有武器也可能是 3，如被加攻）
    hero_attacks_left: int = 0       # 英雄本回合还能攻击几次
    minions_played: int = 0          # 本局已打出的随从数（被动技能计数器用）
    passive_rule: dict = field(default_factory=dict)   # 被动技能的计数规则
    cthun_uses_left: int = 0         # 这回合帝国之眼还能用几次技能
    cthun_used: set = field(default_factory=set)       # 这个克苏恩已经用过的技能

    def clone(self) -> "SideState":
        # 用 copy.copy 而不是逐字段重建：以前是手写字段列表，后来加的
        # hero_atk / hero_attacks_left / _cthun_by_unit 全漏了，模拟里英雄攻击、
        # 克苏恩「同一只不能重复选」都会凭空消失，而且没有任何报错。
        c = copy.copy(self)
        c.deck = list(self.deck)
        c.hand = list(self.hand)
        c.board = [u.clone() for u in self.board]
        c.weapon = self.weapon.clone() if self.weapon else None
        c.passive_rule = dict(self.passive_rule)
        c.cthun_used = set(self.cthun_used)
        c._cthun_by_unit = {k: set(v) for k, v in getattr(self, "_cthun_by_unit", {}).items()}
        return c

    @property
    def board_attack(self) -> int:
        return sum(u.atk * (2 if u.windfury else 1) for u in self.board if u.alive and u.atk > 0)

class Board:
    """两方 + 回合信息。side 0 = 我方，side 1 = 对方。"""

    def __init__(self, sides: list[SideState], turn: int = 1, active: int = 0,
                 cards: "CardDB | None" = None) -> None:
        self.sides = sides
        self.turn = turn
        self.active = active
        self.log: list[str] = []
        self.cards = cards

    def clone(self) -> "Board":
        b = Board([s.clone() for s in self.sides], self.turn, self.active, self.cards)
        b.log = list(self.log)
        return b

    def note(self, text: str) -> None:
        self.log.append(text)

    def damage_unit(self, board_side: SideState, unit: Unit, amount: int) -> int:
        dealt = unit.take_damage(amount)
        if not unit.alive and unit in board_side.board:
            board_side.board.remove(unit)
            self._on_death(board_side, unit)
        return dealt

    def _on_death(self, side: SideState, unit: Unit) -> None:
        card_kw = self.cards.get(unit.card_id) if (self.cards and unit.card_id) else None
        kw_text = ((card_kw.keyword_cn if card_kw else "") or "") + ((card_kw.text or "") if card_kw else "")
        if "复生" in kw_text and not unit.reborn:
            side.board.append(Unit(
                card_id=unit.card_id, name=unit.name,
                atk=unit.atk, hp=1, max_hp=1,
                taunt=unit.taunt, divine=False,
                rush=unit.rush, windfury=unit.windfury,
                lifesteal=unit.lifesteal, poisonous=unit.poisonous,
                attacks_left=0, reborn=True,
            ))
            self.note(f"{unit.name} 复生（1 血）")
        """亡语：随从死亡时按卡面结算（目前只处理「召唤」类）。

        不处理的话，斩杀计算会忽略「打死嘲讽后亡语又招出一个嘲讽」，
        把打不穿的场面算成能穿。
        """
        card = self.cards.get(unit.card_id) if (self.cards and unit.card_id) else None
        text = (card.text or "") if card is not None else ""
        if "亡语" not in text:
            return
        eff = parse_effect(card)
        if eff.heal:
            side.hero_hp += eff.heal
            self.note(f"{unit.name} 的亡语回复 {eff.heal} 点")
        if eff.armor:
            side.armor += eff.armor
        # 亡语抽牌：不往手牌塞占位（会让 legal_actions 崩），直接忽略
        if eff.damage:
            # 亡语伤害打的是「那一方的敌人」——也就是我们
            idx = 0 if side is self.sides[0] else 1
            other = self.sides[1 - idx]
            hits_hero = any(k in text for k in ("所有敌人", "随机分配", "敌方英雄"))
            minions_only = "所有随从" in text and not hits_hero
            if eff.aoe or eff.scope in ("enemy_minions", "all", "enemy"):
                for u in list(other.board):
                    self.damage_unit(other, u, eff.damage)
                if hits_hero or not minions_only:
                    self.damage_hero(other, eff.damage)
                self.note(f"{unit.name} 的亡语造成 {eff.damage} 点群体伤害")
            else:
                self.damage_hero(other, eff.damage)
                self.note(f"{unit.name} 的亡语对敌方英雄造成 {eff.damage} 点")
        if (eff.summon_atk or eff.summon_hp) and eff.summon_self and not eff.summon_opp:
            for _ in range(max(1, eff.summon_count)):
                side.board.append(Unit(
                    card_id="", name="亡语召唤物",
                    atk=max(0, eff.summon_atk), hp=max(1, eff.summon_hp),
                    taunt="嘲讽" in text, attacks_left=0,
                ))
            self.note(f"{unit.name} 的亡语召唤了随从")

    def damage_hero(self, side: SideState, amount: int) -> int:
        absorbed = min(side.armor, amount)
        side.armor -= absorbed
        rest = amount - absorbed
        side.hero_hp = max(0, side.hero_hp - rest)
        return amount

    def apply_effect(self, eff: Effect, caster: SideState, target: Unit | None) -> None:
        """把解释出来的效果作用到场上。caster 是施法方。"""
        foe_side = next(s for s in self.sides if s is not caster)
        my_side = caster

        if eff.buff_hand and eff.buff_atk and caster.hand:
            for k, hc in enumerate(caster.hand):
                if hc is not None and getattr(hc, "type", "") == "MINION":
                    try:
                        caster.hand[k] = dataclasses.replace(
                            hc, atk=(hc.atk or 0) + eff.buff_atk,
                            health=(hc.health or 0) + eff.buff_hp)
                    except Exception:
                        pass
                    break

        if eff.set_hp:
            for side_ in (my_side, foe_side):
                for u in side_.board:
                    if u.alive and u.hp > eff.set_hp:
                        u.hp = eff.set_hp
            self.note(f"所有随从生命值变为 {eff.set_hp}")

        # 召唤：summon_self / summon_opp 决定给谁（「为每个玩家召唤」两个都开）
        summon_sides = []
        if eff.summon_self:
            summon_sides.append(my_side)
        if eff.summon_opp:
            summon_sides.append(foe_side)
        if summon_sides and (eff.summon_atk or eff.summon_hp or eff.summon_count > 1):
            for target_side in summon_sides:
                for _ in range(max(1, eff.summon_count)):
                    if len([u for u in target_side.board if u.alive]) >= 7:
                        break
                    target_side.board.append(Unit(
                        card_id="", name="召唤物",
                        atk=max(0, eff.summon_atk), hp=max(1, eff.summon_hp),
                        attacks_left=0))
            if eff.buff_atk or eff.buff_hp:
                # 「将你的随从变形」：按卡面顺序，含刚召唤的
                for u in my_side.board:
                    if u.alive:
                        u.atk += eff.buff_atk
                        u.hp += eff.buff_hp
                self.note(f"{len([u for u in my_side.board if u.alive])} 个随从变形 +{eff.buff_atk}/+{eff.buff_hp}")

        if eff.opp_hand_delta:
            keep = max(0, len(foe_side.hand) + eff.opp_hand_delta)
            foe_side.hand = foe_side.hand[:keep]
            # 偷到手里的牌：只记张数（占位 None，下游已做防护）
            if eff.draw:
                my_side.hand.extend([None] * int(eff.draw))
            self.note(f"对手手牌 -{abs(eff.opp_hand_delta)} 张")

        if eff.steal:
            for u in list(foe_side.board):
                if u.alive:
                    foe_side.board.remove(u)
                    u.attacks_left = 0
                    my_side.board.append(u)
                    self.note(f"夺取了 {u.name} 的控制权")
                    break

        if eff.destroy:
            if eff.scope == "all_minions":
                for s in (my_side, foe_side):
                    for u in list(s.board):
                        if eff.min_atk and u.atk < eff.min_atk:
                            continue
                        self.damage_unit(s, u, 9999)
            elif target is not None:
                owner = foe_side if target in foe_side.board else my_side
                self.damage_unit(owner, target, 9999)

        if eff.damage:
            dmg = eff.damage
            bonus = sum(u.spell_damage for u in caster.board if u.alive)
            if eff.scope in ("target",) and target is not None:
                owner = foe_side if target in foe_side.board else my_side
                if target.card_id == "HERO":
                    self.damage_hero(owner, dmg + bonus)
                else:
                    self.damage_unit(owner, target, dmg + bonus)
            elif eff.scope == "enemy_minions":
                for u in list(foe_side.board):
                    self.damage_unit(foe_side, u, dmg + bonus)
            elif eff.scope == "all_minions":
                for s in (my_side, foe_side):
                    for u in list(s.board):
                        self.damage_unit(s, u, dmg + bonus)
            elif eff.scope == "enemy_hero":
                self.damage_hero(foe_side, dmg + bonus)
            elif eff.scope == "random_enemy":
                pool = [u for u in foe_side.board if u.alive]
                if pool:
                    self.damage_unit(foe_side, pool[0], dmg + bonus)
                else:
                    self.damage_hero(foe_side, dmg + bonus)

        if eff.heal:
            if target is not None and target in my_side.board:
                target.heal(eff.heal)
            elif target is not None and target.card_id == "HERO":
                my_side.hero_hp = min(30, my_side.hero_hp + eff.heal)
            else:
                my_side.hero_hp = min(30, my_side.hero_hp + eff.heal)

        # armor 以前只在亡语里结算，普通法术/技能拿到护甲会被静默丢掉
        if eff.armor:
            caster.armor += eff.armor

        if eff.buff_atk or eff.buff_hp:
            if target is not None and target in my_side.board:
                target.atk += eff.buff_atk
                target.max_hp += eff.buff_hp
                target.hp += eff.buff_hp

        if eff.freeze and target is not None and target.alive:
            target.frozen = True
            target.can_attack_now = False

        for _ in range(eff.draw):
            self.draw(caster)

    def draw(self, side: SideState) -> Card | None:
        if side.deck:
            card = side.deck.pop(0)
            if len(side.hand) < 10:
                side.hand.append(card)
            return card
        side.fatigue += 1
        self.damage_hero(side, side.fatigue)
        self.note(f"疲劳伤害 {side.fatigue}")
        return None
