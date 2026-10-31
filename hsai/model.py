"""共享的局面模型。

四个地方都要「懂一点炉石规则」，为了不写四遍：
  - policy.py    建议引擎：在真实局面上枚举动作、给分
  - sim/engine.py 自对弈引擎：让 AI 真的把一局打完
  - learn.py     用模拟对局训权重
所以统一放在这里：随从/英雄/武器/手牌/牌库 + 一个够用的效果解释器。

明确它不是什么：这不是完整的炉石规则引擎。没有光环结算、没有复杂的目标链、
没有亡语链条（只做最常见的直伤/抽牌/BUFF/消灭）。无法建模的牌会被标记为
「效果未知」，建议引擎会因此降低推荐权重 —— 宁可不说，也不要胡说。
"""

from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .cards import Card, CardDB

# 中文数字（只到十，抽牌/召唤够用了）
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 关键词/文本效果的解释表（data/effects.json），改表就能改行为
_EFFECT_TABLE: dict[str, Any] | None = None
_CN_NUM_EXTRA = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}


def effect_table() -> dict[str, Any]:
    global _EFFECT_TABLE
    if _EFFECT_TABLE is not None:
        return _EFFECT_TABLE
    from .cards import DATA_DIR

    path = DATA_DIR / "effects.json"
    try:
        _EFFECT_TABLE = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _EFFECT_TABLE = {"keywords": {}, "patterns": []}
    return _EFFECT_TABLE


def keyword_info(mechanic: str) -> dict[str, Any]:
    return (effect_table().get("keywords") or {}).get(mechanic, {})


def _pattern_effect(text: str) -> "Effect | None":
    """按表里的顺序匹配；更具体的规则排在前面，命中的字段不会被后面的覆盖。"""
    table = effect_table().get("patterns") or []
    hit = None
    for pat in table:
        m = re.search(pat.get("regex", ""), text or "")
        if not m:
            continue
        eff = hit or Effect()
        for field_name, raw in (pat.get("effect") or {}).items():
            value: Any = raw
            if isinstance(raw, str) and raw.startswith("$"):
                key = raw[1:]
                if key.startswith("cn"):
                    token = m.group(int(key[2:]))
                    value = _CN_NUM_EXTRA.get(token, 1)
                else:
                    value = int(m.group(int(key)))
            if field_name in ("scope",):
                eff.scope = str(value)
            elif field_name == "aoe":
                eff.aoe = bool(value)
            elif getattr(eff, field_name, 0) in (0, False):
                setattr(eff, field_name, value)
        hit = eff
    return hit


_DMG_RE = re.compile(r"造成\s*\d+\s*点伤害|造成\s*(\d+)\s*点伤害")
_DMG_AMOUNT_RE = re.compile(r"造成(\d+)点伤害")
_HEAL_RE = re.compile(r"恢复(\d+)点生命值")
_BUFF_RE = re.compile(r"获得\+(\d+)/\+(\d+)")
_SUMMON_RE = re.compile(r"召唤(?:一个)?(\d+)/(\d+)")
_DRAW_RE = re.compile(r"抽(一张|两张|三张|四张|\d+张)牌")


def _num(token: str) -> int:
    token = token.rstrip("张")
    if token.isdigit():
        return int(token)
    return _CN_NUM.get(token, 1)


@dataclass
class Effect:
    """从卡牌文本里解释出来的效果（近似）。"""

    damage: int = 0
    scope: str = "target"  # target | enemy_minions | all_minions | enemy_hero | random_enemy | friendly
    draw: int = 0
    heal: int = 0
    buff_atk: int = 0
    buff_hp: int = 0
    destroy: bool = False
    freeze: bool = False
    summon_atk: int = 0
    summon_hp: int = 0
    summon_count: int = 1   # 一次召唤几个
    mana: int = 0            # 本回合获得的法力（例如幸运币）
    armor: int = 0           # 获得护甲（战士/德鲁伊英雄技能）
    discover: bool = False   # 发现：从三张里选一张
    aoe: bool = False
    known: bool = True
    note: str = ""

    def is_empty(self) -> bool:
        return not (self.damage or self.draw or self.heal or self.buff_atk or self.mana or self.armor or self.buff_hp
                    or self.destroy or self.freeze or self.summon_atk)


def parse_effect(card: Card) -> Effect:
    """把卡牌文本翻译成可执行效果。识别不出来就 known=False。"""
    # 数据里的文本带富文本标记：`造成$3点伤害`、`<b>战吼：</b>`，先去干净再匹配
    raw_text = card.text or ""
    text = re.sub(r"<[^>]+>", "", raw_text).replace("$", "").replace("#", "")
    eff = Effect()
    if not text:
        eff.known = card.type != "SPELL"
        return eff

    if m := _DMG_AMOUNT_RE.search(text):
        eff.damage = int(m.group(1))
        eff.aoe = "所有" in text
        if "所有随从" in text:
            eff.scope = "all_minions"
        elif "所有敌方随从" in text or "对所有敌方" in text:
            eff.scope = "enemy_minions"
        elif "所有敌人" in text:
            eff.scope = "enemy_minions"
        elif "随机" in text and "敌人" in text:
            eff.scope = "random_enemy"
        elif "敌方英雄" in text:
            eff.scope = "enemy_hero"
        else:
            eff.scope = "target"
    if m := _HEAL_RE.search(text):
        eff.heal = int(m.group(1))
    if not eff.damage and not eff.heal:
        if m := re.search(r"恢复(\d+)点", text):
            eff.heal = int(m.group(1))
    if m := _BUFF_RE.search(text):
        eff.buff_atk, eff.buff_hp = int(m.group(1)), int(m.group(2))
    if "消灭所有随从" in text:
        eff.destroy, eff.scope = True, "all_minions"
    elif "消灭一个" in text:
        eff.destroy, eff.scope = True, "target"
    if m := _DRAW_RE.search(text):
        eff.draw = _num(m.group(1))
    if m := _SUMMON_RE.search(text):
        eff.summon_atk, eff.summon_hp = int(m.group(1)), int(m.group(2))
    if "冻结" in text:
        eff.freeze = True
    # 随从的战吼伤害也算进去（例如「战吼：造成2点伤害」）
    if also := re.search(r"战吼[:：]?\s*造成(\d+)点伤害", text):
        eff.damage = max(eff.damage, int(also.group(1)))

    # 表驱动补充：data/effects.json 里的规则优先于内置正则覆盖不到的写法
    extra = _pattern_effect(text)
    if extra is not None:
        if extra.damage and not eff.damage:
            eff.damage, eff.scope, eff.aoe = extra.damage, extra.scope, extra.aoe
        if extra.heal and not eff.heal:
            eff.heal = extra.heal
        if extra.draw and not eff.draw:
            eff.draw = extra.draw
        if extra.buff_atk and not eff.buff_atk:
            eff.buff_atk, eff.buff_hp = extra.buff_atk, extra.buff_hp
        if extra.destroy and not eff.destroy:
            eff.destroy, eff.scope, eff.aoe = True, extra.scope, extra.aoe
        if extra.summon_atk and not eff.summon_atk:
            eff.summon_atk, eff.summon_hp = extra.summon_atk, extra.summon_hp
        if extra.freeze:
            eff.freeze = True

    if re.search(r"获得\s*(?:一|1)?\s*个?\s*(?:空的)?\s*法力水晶", text):
        eff.mana = 1
    if "发现" in text:
        eff.discover = True
    # 护甲的写法很杂：「获得2点护甲」「+2护甲」「本回合+2护甲」
    if m := re.search(r"(\d+)\s*点?护甲", text):
        eff.armor = int(m.group(1))

    # 召唤：中文数字 + 数量 + 攻/血（「召唤两个1/1的僵尸」「召唤五个2/1的」）
    _cn = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7}
    if m := re.search(r"召唤\s*(一|两|二|三|四|五|六|七|\d+)\s*个?\s*(\d+)\s*/\s*(\d+)", text):
        token = m.group(1)
        eff.summon_count = int(token) if token.isdigit() else _cn.get(token, 1)
        eff.summon_atk, eff.summon_hp = int(m.group(2)), int(m.group(3))
    elif m := re.search(r"复活为\s*(\d+)\s*/\s*(\d+)", text):
        # 「将最多5份残骸复活为2/2」——按最多份数估
        cap = re.search(r"最多\s*(\d+)\s*份", text)
        eff.summon_count = min(int(cap.group(1)), 7) if cap else 1
        eff.summon_atk, eff.summon_hp = int(m.group(1)), int(m.group(2))
    # 本回合加攻击力（盗贼/恶魔猎手英雄技能）
    if m := re.search(r"本回合\s*\+?\s*(?:\w\d+)?\s*(\d+)\s*攻击力", text):
        eff.buff_atk = int(m.group(1))

    # 「抽三张法力值消耗不同的牌」这类：抽 N 张后面还跟着限定语，
    # 原来的 抽N张牌 匹配不到，整张牌就被判成「效果未建模」
    if not eff.draw:
        if m := re.search(r"抽\s*(一|二|两|三|四|五|\d+)\s*张", text):
            token = m.group(1)
            eff.draw = int(token) if token.isdigit() else {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}[token]
    eff.known = bool(
        eff.damage or eff.heal or eff.draw or eff.buff_atk or eff.buff_hp
        or eff.destroy or eff.summon_atk or eff.freeze or eff.mana or eff.armor
        or eff.discover
    ) or card.type in ("MINION", "WEAPON", "HERO")
    if not eff.known:
        eff.note = "效果未能建模"
    return eff


# ---------------------------------------------------------------- 单位

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

    def clone(self) -> "SideState":
        return SideState(
            hero_hp=self.hero_hp,
            armor=self.armor,
            mana=self.mana,
            max_mana=self.max_mana,
            deck=list(self.deck),
            hand=list(self.hand),
            board=[u.clone() for u in self.board],
            fatigue=self.fatigue,
            weapon=self.weapon.clone() if self.weapon else None,
            hero_class=self.hero_class,
            hero_power_used=self.hero_power_used,
            hero_power=self.hero_power,
            spells_cast=self.spells_cast,
        )

    @property
    def board_attack(self) -> int:
        return sum(u.atk * (2 if u.windfury else 1) for u in self.board if u.alive and u.atk > 0)

    def total_hp(self) -> int:
        return self.hero_hp + self.armor


class Board:
    """两方 + 回合信息。side 0 = 我方，side 1 = 对方。"""

    def __init__(self, sides: list[SideState], turn: int = 1, active: int = 0,
                 cards: "CardDB | None" = None) -> None:
        self.sides = sides
        self.turn = turn
        self.active = active
        self.log: list[str] = []
        self.cards = cards

    def me(self) -> SideState:
        return self.sides[0]

    def them(self) -> SideState:
        return self.sides[1]

    def clone(self) -> "Board":
        b = Board([s.clone() for s in self.sides], self.turn, self.active, self.cards)
        b.log = list(self.log)
        return b

    def note(self, text: str) -> None:
        self.log.append(text)

    # ------------------------------------------------------------ 伤害
    def damage_unit(self, board_side: SideState, unit: Unit, amount: int) -> int:
        dealt = unit.take_damage(amount)
        if not unit.alive and unit in board_side.board:
            board_side.board.remove(unit)
        return dealt

    def damage_hero(self, side: SideState, amount: int) -> int:
        absorbed = min(side.armor, amount)
        side.armor -= absorbed
        rest = amount - absorbed
        side.hero_hp = max(0, side.hero_hp - rest)
        return amount

    def all_dead(self) -> int | None:
        if self.sides[0].hero_hp <= 0:
            return 1
        if self.sides[1].hero_hp <= 0:
            return 0
        return None

    # ------------------------------------------------------------ 效果执行
    def apply_effect(self, eff: Effect, caster: SideState, target: Unit | None) -> None:
        """把解释出来的效果作用到场上。caster 是施法方。"""
        foe_side = next(s for s in self.sides if s is not caster)
        my_side = caster

        if eff.destroy:
            if eff.scope == "all_minions":
                for s in (my_side, foe_side):
                    for u in list(s.board):
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

        if eff.buff_atk or eff.buff_hp:
            if target is not None and target in my_side.board:
                target.atk += eff.buff_atk
                target.max_hp += eff.buff_hp
                target.hp += eff.buff_hp

        if eff.freeze and target is not None and target.alive:
            target.frozen = True
            target.can_attack_now = False

        if eff.summon_atk or eff.summon_hp:
            count = max(1, min(int(eff.summon_count or 1), 7))
            for _ in range(count):
                my_side.board.append(Unit(card_id="", name="召唤物", atk=eff.summon_atk,
                                          hp=max(1, eff.summon_hp), max_hp=max(1, eff.summon_hp)))

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


# ---------------------------------------------------------------- 评估函数
DEFAULT_WEIGHTS = {
    "tempo": 1.0,        # 场面交换优势
    "face": 0.9,         # 打脸进度
    "hand": 0.7,         # 手牌差
    "health": 0.55,      # 血量差
    "taunt": 0.8,        # 嘲讽墙对进攻的阻滞
    "aoe_risk": 1.2,     # 铺场被清的风险
}


def reach_damage(side: SideState) -> int:
    """本回合能直接打到脸上的伤害（随从攻击 + 武器 + 手里的直伤法术）。"""
    total = sum(u.atk * (2 if u.windfury else 1) for u in side.board if u.alive and u.atk > 0 and not u.frozen)
    if side.weapon:
        total += side.weapon.atk
    mana = side.mana
    for card in sorted(side.hand, key=lambda c: c.cost):
        if card.type != "SPELL" or card.cost > mana:
            continue
        eff = parse_effect(card)
        if eff.damage and eff.scope in ("target", "enemy_hero", "random_enemy") and not eff.aoe:
            mana -= card.cost
            total += eff.damage
    return total


def evaluate(board: Board, weights: dict[str, float] | None = None, aoe_risk: float = 0.0,
             perspective: int = 0) -> float:
    """从 perspective 这一方视角给局面打分。数值本身无意义，只用于横向比较。"""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    me, foe = board.sides[perspective], board.sides[1 - perspective]

    my_val = sum(u.value for u in me.board if u.alive)
    foe_val = sum(u.value for u in foe.board if u.alive)

    # 嘲讽墙：对方有墙时，我们的攻击力打折
    taunt_val = sum(u.hp + (0 if u.divine else 0) for u in foe.board if u.alive and u.taunt)
    my_reach = me.board_attack + (me.weapon.atk if me.weapon else 0)
    blocked = min(my_reach, taunt_val) * 0.6

    score = 0.0
    score += w["tempo"] * (my_val - foe_val)
    score += w["face"] * ((30 - foe.hero_hp - foe.armor) - (30 - me.hero_hp - me.armor)) * 0.5
    score += w["hand"] * (len(me.hand) - len(foe.hand)) * 1.5
    score += w["health"] * ((me.hero_hp + me.armor) - (foe.hero_hp + foe.armor)) * 0.35
    score += w["taunt"] * blocked * -0.4

    # 铺场风险：我方随从越多、对方有 AOE 的概率越高，扣分越狠
    if aoe_risk > 0.05 and len(me.board) >= 2:
        exposure = sum(u.value for u in me.board if u.alive and u.hp <= 4)
        score -= w["aoe_risk"] * aoe_risk * exposure * 0.22

    # 斩杀优先：对方已经死了就是最大分
    if foe.hero_hp <= 0:
        score += 10_000
    if me.hero_hp <= 0:
        score -= 10_000

    # 被对手斩杀线覆盖时要开始防守（这个阶跃是想要的：杀死对方一个随从让
    # 他的总攻降到斩杀线以下，值得重奖）。
    #
    # 但**不要再加「我能斩杀」的阶跃加分**：打一下脸就会让「还能不能斩杀」翻转，
    # 评分出现 -800 的悬崖，AI 反而打一下就不打了。斩交由 policy._lethal_sequence
    # 单独处理（按伤害贪心拼完整顺序）。
    if w.get("lethal_bonus", 1.0):
        foe_reach = foe.board_attack + (foe.weapon.atk if foe.weapon else 0)
        if foe_reach >= me.hero_hp + me.armor:
            score -= 420 * w.get("lethal_bonus", 1.0)
    return score


def board_to_text(board: Board, idx: int) -> str:
    s = board.sides[idx]
    units = "、".join(f"{u.name} {u.atk}/{u.hp}" for u in s.board if u.alive) or "空场"
    return f"{s.hero_hp}+{s.armor} 血，{units}"


# ---------------------------------------------------------------- 从真实日志局面构造
def hero_power_effect(card_class: str, card: "Card | None" = None) -> tuple[str, int, Effect]:
    """英雄技能（名称, 费用, 效果）。

    优先用**这张真实的英雄技能卡**解析出来的效果：现代炉石里同一职业会有
    升级版/替换版技能，写死的表必然对不上。表只作为拿不到卡时的兜底。
    """
    if card is not None and (card.text or ""):
        eff = parse_effect(card)
        return card.name, card.cost or 2, eff
    table = {
        "MAGE": ("火焰冲击", 2, Effect(damage=1, scope="target")),
        "HUNTER": ("稳固射击", 2, Effect(damage=2, scope="enemy_hero")),
        "PRIEST": ("次级治疗术", 2, Effect(heal=2)),
        "WARRIOR": ("全副武装", 2, Effect()),
        "PALADIN": ("援军", 2, Effect(summon_atk=1, summon_hp=1)),
        "SHAMAN": ("图腾召唤", 2, Effect(summon_atk=0, summon_hp=2)),
        "ROGUE": ("匕首精通", 2, Effect()),
        "DRUID": ("变形", 2, Effect()),
        "WARLOCK": ("生命分流", 2, Effect(draw=1)),
        "DEMONHUNTER": ("恶魔之咬", 1, Effect()),
        "DEATHKNIGHT": ("召唤食尸鬼", 2, Effect(summon_atk=1, summon_hp=1)),
    }
    name, cost, eff = table.get(card_class, ("英雄技能", 2, Effect()))
    return name, cost, eff
