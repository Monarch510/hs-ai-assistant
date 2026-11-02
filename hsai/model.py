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
import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .cards import Card

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
    from .paths import EFFECTS_JSON

    path = EFFECTS_JSON
    try:
        _EFFECT_TABLE = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _EFFECT_TABLE = {"keywords": {}, "patterns": []}
    return _EFFECT_TABLE


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
    steal: bool = False       # 夺取控制权
    buff_hand: bool = False   # 顺带 buff 手牌里的随从
    summon_self: bool = True  # 召唤给施法方
    summon_opp: bool = False  # 召唤给对方（「为每个玩家召唤」）
    opp_hand_delta: int = 0   # 对手手牌张数变化（偷牌为负）
    set_hp: int = 0           # 把随从血量设为固定值（生而平等）
    min_atk: int = 0          # 群体消灭的攻击力门槛（0 表示不限）
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


def _as_int(v) -> int:
    """把规则里的值转成整数；转不了就当 0（防止字符串混进数值字段）。"""
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        return int(m.group(0)) if m else 0
    return 0


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
    elif "消灭所有" in text and "随从" in text:
        # 「消灭所有攻击力大于或等于5的随从」这类带条件的群体消灭
        eff.destroy, eff.scope, eff.aoe = True, "all_minions", True
        if m := re.search(r"攻击力(?:大于|小于)(?:或等于)?(\d+)", text):
            eff.min_atk = int(m.group(1))
    elif "消灭一个" in text:
        eff.destroy, eff.scope = True, "target"
    if m := _DRAW_RE.search(text):
        eff.draw = _num(m.group(1))
    if m := _SUMMON_RE.search(text):
        eff.summon_atk, eff.summon_hp = int(m.group(1)), int(m.group(2))
    if m := re.search(r"手牌中的随从牌获得\+(\d+)生命值", text):
        eff.buff_hand = True
        eff.buff_hp = max(eff.buff_hp, int(m.group(1)))
    if "为每个玩家召唤" in text:
        eff.summon_self, eff.summon_opp = True, True
        eff.summon_count = 2
        eff.summon_atk, eff.summon_hp = 2, 3
        if "变形" in text:
            # 变形：用「+1/+1」近似「变成费用+1的随从」
            eff.buff_atk = max(eff.buff_atk, 1)
            eff.buff_hp = max(eff.buff_hp, 1)
    # 梦境碎片：卡面就是一行属性加成（「+2/+2和扰魔」「+4生命值和嘲讽」）
    if (card.id or "").startswith("EDR_100t") or text.strip().startswith("+"):
        m2 = re.search(r"\+(\d+)/\+(\d+)", text)
        m3 = re.search(r"\+(\d+)攻击力", text)
        m4 = re.search(r"\+(\d+)生命值", text)
        if m2:
            eff.buff_atk, eff.buff_hp = int(m2.group(1)), int(m2.group(2))
        if m3:
            eff.buff_atk = max(eff.buff_atk, int(m3.group(1)))
        if m4:
            eff.buff_hp = max(eff.buff_hp, int(m4.group(1)))
        if eff.buff_atk or eff.buff_hp:
            eff.buff_hand = True          # 这类是给手牌/随从的加成
            eff.note = "梦境碎片"
    if "梦魇的恩赠" in text:
        eff.buff_atk, eff.buff_hp = max(eff.buff_atk, 2), max(eff.buff_hp, 2)
        eff.note = "梦境系列（随机之一，按 +2/+2 估）"
    if "偷取" in text and "手牌" in text:
        if m := re.search(r"偷取[^。]*?(\d+)\s*张手牌", text):
            eff.draw = max(eff.draw, int(m.group(1)))
            eff.opp_hand_delta = -int(m.group(1))
    if (m := re.search(r"随机召唤消耗总计\s*(\d+)\s*点法力值", text)):
        # 按 1 点法力 = 1/2 的身材折算：N 点 = N/2 个 2/4
        total = int(m.group(1))
        n = max(1, total // 2)
        eff.summon_count = n
        eff.summon_atk, eff.summon_hp = 2, 4
    if m := re.search(r"将所有随从的生命值变为(\d+)", text):
        eff.set_hp = int(m.group(1))
    if "随机触发" in text and "亡语" in text:
        # 随机触发一个已死友方随从的亡语：具体效果无法预知，按「无收益」记录
        eff.known = True
        eff.note = "随机亡语（效果不计）"
        return eff
    if "拼装" in text:
        # 拼装：从卡池里取一张牌（等价于「发现/生成」这类资源）
        eff.discover = True
    if "夺取" in text and "控制权" in text:
        eff.steal = True
    if "双倍" in text and "法力值消耗" in text:
        # 加成 = 本牌费用的两倍（费用在解析时已知）
        eff.buff_atk = eff.buff_hp = max(1, (card.cost or 0) * 2)
        if "手牌" in text:
            eff.buff_hand = True
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
            eff.draw = _as_int(extra.draw)
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
    if m := re.search(r"召唤\s*(一|两|二|三|四|五|六|七|\d+)\s*[个只位]?\s*(\d+)\s*/\s*(\d+)", text):
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
    if "回溯这张牌" in text:
        eff.known = True
        eff.note = "回溯：暂按无效果处理"
        return eff
    eff.known = bool(
        eff.damage or eff.heal or eff.draw or eff.buff_atk or eff.buff_hp
        or eff.destroy or eff.summon_atk or eff.summon_hp or eff.summon_count > 1
        or eff.freeze or eff.mana or eff.armor
        or eff.discover or eff.steal or eff.set_hp
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
    minions_played: int = 0          # 本局已打出的随从数（被动技能计数器用）
    passive_rule: dict = field(default_factory=dict)   # 被动技能的计数规则
    cthun_uses_left: int = 0         # 这回合帝国之眼还能用几次技能
    cthun_used: set = field(default_factory=set)       # 这个克苏恩已经用过的技能

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
            weapon_attacks_left=self.weapon_attacks_left,
            minions_played=self.minions_played,
            passive_rule=dict(self.passive_rule),
            cthun_uses_left=self.cthun_uses_left,
            cthun_used=set(self.cthun_used),
        )

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

    # ------------------------------------------------------------ 效果执行
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


# ---------------------------------------------------------------- 评估函数
DEFAULT_WEIGHTS = {
    "tempo": 1.0,        # 场面交换优势
    "face": 0.9,         # 打脸进度
    "hand": 0.7,         # 手牌差
    "health": 0.55,      # 血量差
    "taunt": 0.8,        # 嘲讽墙对进攻的阻滞
    "aoe_risk": 1.2,
    "fatigue": 0.6,     # 牌库差 / 疲劳伤害（残局里牌多的一方更能拖）     # 铺场被清的风险
}






_DB = None


def set_card_db(db) -> None:
    """让评估函数能查到场上随从的卡面文本（场上资源用）。"""
    global _DB
    _DB = db


def resource_score(side_state, cards=None) -> float:
    """估算一方还有多少「资源」。

    = 手牌张数
      + 手牌里能抽牌/发现/加费的量（还能再生出多少张牌）
      + 英雄技能能否造资源
      + 场上带「抽牌/发现」的随从（光环、亡语，按半张算）

    为什么不用「手牌总费用」：费用高不代表能再生出牌，一张 8 费可能只是
    一张大随从；而「抽两张」才是真的多两条命。
    """
    # 牌库剩余也算资源：每 4 张折 1 点（离摸到还早，不能和手牌等价）
    score = 0.25 * len(side_state.deck) + float(len(side_state.hand))
    for card in side_state.hand:
        if card is None:
            continue
        eff = parse_effect(card)
        try:
            score += float(eff.draw or 0)
        except (TypeError, ValueError):
            pass
        if eff.discover:
            score += 1.0
        if eff.mana:
            score += 1.0

    hp = getattr(side_state, "hero_power", None)
    if hp is not None and not is_passive_hero_power(hp):
        _, _, eff = hero_power_effect(side_state.hero_class, hp)
        if eff.draw or eff.discover:
            score += 1.0

    cards = cards or _DB
    if cards is not None:
        for unit in side_state.board:
            if getattr(unit, "card_id", ""):
                card = cards.get(unit.card_id)
                text = (card.text or "") if card is not None else ""
                if "抽" in text or "发现" in text:
                    score += 0.5          # 站在场上的资源，按半张算
    return score


def _adjacency_value(side_state) -> float:
    """相邻光环带来的额外价值：只在两个随从真的挨着时才算。"""
    import re as _re

    total = 0.0
    board = [u for u in side_state.board if u.alive]
    for i, u in enumerate(board):
        card = _DB.get(u.card_id) if _DB is not None and u.card_id else None
        text = (card.text or "") if card is not None else ""
        if "相邻" not in text:
            continue
        m = _re.search(r"\+(\d+)/\+(\d+)", text)
        buff = (int(m.group(1)) + int(m.group(2))) if m else 2
        neighbors = 0
        if i > 0:
            neighbors += 1
        if i + 1 < len(board):
            neighbors += 1
        total += buff * neighbors * 0.5
    return total


def evaluate(board: Board, weights: dict[str, float] | None = None, aoe_risk: float = 0.0,
             perspective: int = 0) -> float:
    """从 perspective 这一方视角给局面打分。数值本身无意义，只用于横向比较。"""
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    me, foe = board.sides[perspective], board.sides[1 - perspective]

    # 武器也是「能持续输出的资源」：按攻击力计价，耐久略加权
    my_val = sum(u.value for u in me.board if u.alive)
    foe_val = sum(u.value for u in foe.board if u.alive)
    if me.weapon is not None:
        my_val += me.weapon.atk * 1.2 + max(0, me.weapon.hp) * 0.25
    if foe.weapon is not None:
        foe_val += foe.weapon.atk * 1.2 + max(0, foe.weapon.hp) * 0.25

    # 嘲讽墙：对方有墙时，我们的攻击力打折
    taunt_val = sum(u.hp + (0 if u.divine else 0) for u in foe.board if u.alive and u.taunt)
    my_reach = me.board_attack + (me.weapon.atk if me.weapon else 0)
    blocked = min(my_reach, taunt_val) * 0.6

    score = 0.0
    score += w["tempo"] * (my_val - foe_val)
    # 摆位会影响相邻光环的价值，这样「第几位」才有意义
    score += w["tempo"] * (_adjacency_value(me) - _adjacency_value(foe))
    score += w["face"] * ((30 - foe.hero_hp - foe.armor) - (30 - me.hero_hp - me.armor)) * 0.5
    score += w["hand"] * (resource_score(me) - resource_score(foe)) * 1.2
    score += w["health"] * ((me.hero_hp + me.armor) - (foe.hero_hp + foe.armor)) * 0.35
    score += w["taunt"] * blocked * -0.4

    # 疲劳伤害：牌库差已经算进「资源」，这里只管还在吃的疲劳
    fatigue_gap = foe.fatigue - me.fatigue
    score += w["fatigue"] * fatigue_gap * 0.8

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


# ---------------------------------------------------------------- 从真实日志局面构造
def is_passive_hero_power(card: "Card | None") -> bool:
    """被动英雄技能（文本以「被动」开头 / 含「被动」）不需要也不能主动使用。"""
    if card is None:
        return False
    import re as _re

    text = _re.sub(r"<[^>]+>", "", card.text or "")
    return "被动" in text[:40]


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
