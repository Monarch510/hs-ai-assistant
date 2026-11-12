"""卡牌效果解释：把卡面文本翻译成可执行的 Effect。

依赖只有 .cards.Card 和标准库；不顶层 import 门面（会成环）。
需要读门面上会被 mock.patch 覆盖的规则表时，在函数内部延迟回查。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .cards import Card

# 中文数字（只到十，抽牌/召唤够用了）
_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

_CN_NUM_EXTRA = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}


def _pattern_effect(text: str) -> "Effect | None":
    """按表里的顺序匹配；更具体的规则排在前面，命中的字段不会被后面的覆盖。"""
    # 规则表放在门面上，且会被 mock.patch 覆盖赋值，必须运行时回查，不能用本地全局副本
    from . import model
    table = model.effect_table().get("patterns") or []
    hit = None
    written: set[str] = set()      # 已经由更靠前的规则写过的字段
    for pat in table:
        m = re.search(pat.get("regex", ""), text or "")
        if not m:
            continue
        eff = hit or Effect()
        for field_name, raw in (pat.get("effect") or {}).items():
            value: Any = raw
            # -$1 表示「取捕获值再取负」：减费规则要用，否则负号会被 _as_int 吃掉
            if isinstance(raw, str) and raw.lstrip("-").startswith("$"):
                neg = raw.startswith("-")
                key = raw.lstrip("-")[1:]
                if key.startswith("cn"):
                    token = m.group(int(key[2:]))
                    value = _CN_NUM_EXTRA.get(token, 1)
                else:
                    value = int(m.group(int(key)))
                if neg:
                    value = -value
            if field_name in ("scope",):
                eff.scope = str(value)
            elif field_name == "aoe":
                eff.aoe = bool(value)
            # 按「谁写过」判断，不看字段默认值：summon_count 默认 1、summon_self 默认 True，
            # 用真值判断会让这些字段永远写不进去
            elif field_name not in written:
                setattr(eff, field_name, value)
                written.add(field_name)
        hit = eff
    return hit


_DMG_AMOUNT_RE = re.compile(r"造成(\d+)点伤害")
_HEAL_RE = re.compile(r"恢复(\d+)点生命值")
_BUFF_RE = re.compile(r"获得\+(\d+)/\+(\d+)")
_SUMMON_RE = re.compile(r"召唤(?:一个)?(\d+)/(\d+)")
_DRAW_RE = re.compile(r"抽(一张|两张|三张|四张|\d+张)牌")
_SPELL_DAMAGE_RE = re.compile(r"法术伤害\+(\d)")


def spell_damage_from_card(card) -> int:
    """卡面「法术伤害+N」的加成；只写了字样没写数字按 1 算。"""
    text = (card.text or "") if card is not None else ""
    if "法术伤害" not in text:
        return 0
    m = _SPELL_DAMAGE_RE.search(text)
    return int(m.group(1)) if m else 1


def unit_spell_damage(unit, cards) -> int:
    """场上法伤：字段优先，日志视图里只有 card_id 时回查卡面文本。"""
    if unit.spell_damage:
        return unit.spell_damage
    card = cards.get(unit.card_id) if (cards and unit.card_id) else None
    return spell_damage_from_card(card)


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
    pending_buff: int = 0     # 延迟加成：下一个随从 +N/+N（玛维）
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
    cost_delta_self: int = 0  # 我方手牌费用变化（负数=变便宜）
    cost_delta_opp: int = 0   # 敌方手牌费用变化（正数=变贵）
    armor: int = 0           # 获得护甲（战士/德鲁伊英雄技能）
    discover: int = 0        # 发现/获取几张：0 没有，1 一张，2 拿两张（表里也写数字）
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


def warden_buff(unit, cards_ref) -> int:
    """场上的「在你使用一张随从牌后，使其获得+X/+X并休眠」光环，返回 X。"""
    if cards_ref is None or unit is None or not getattr(unit, "card_id", ""):
        return 0
    card = cards_ref.get(unit.card_id)
    text = (card.text if card else "") or ""
    if "使用一张随从牌后" not in text or "休眠" not in text:
        return 0
    eff = parse_effect(card)
    return int(eff.pending_buff or 0) or 3


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
        eff.discover = 1
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
        if extra.buff_hp and not eff.buff_hp:
            # 只加血不加攻的规则（真言术：障）走不到上面那条
            eff.buff_hp = _as_int(extra.buff_hp)
        if extra.destroy and not eff.destroy:
            eff.destroy, eff.scope, eff.aoe = True, extra.scope, extra.aoe
        if extra.summon_atk and not eff.summon_atk:
            eff.summon_atk, eff.summon_hp = extra.summon_atk, extra.summon_hp
        if extra.freeze:
            eff.freeze = True
        # 这几项以前漏拷了，规则进了表却一点作用都没有（21 条规则白写）：
        # 表里写了、这里不搬，等于静默丢弃
        if extra.mana and not eff.mana:
            eff.mana = _as_int(extra.mana)
        if extra.cost_delta_self and not eff.cost_delta_self:
            eff.cost_delta_self = _as_int(extra.cost_delta_self)
        if extra.cost_delta_opp and not eff.cost_delta_opp:
            eff.cost_delta_opp = _as_int(extra.cost_delta_opp)
        if extra.armor and not eff.armor:
            eff.armor = _as_int(extra.armor)
        if extra.discover and not eff.discover:
            # discover 是 int（1 张 / 2 张…），别压成 True 丢掉张数
            eff.discover = _as_int(extra.discover)
        if extra.summon_hp and not eff.summon_hp:
            eff.summon_hp = _as_int(extra.summon_hp)
        if extra.steal:
            eff.steal = True
        if extra.set_hp and not eff.set_hp:
            eff.set_hp = _as_int(extra.set_hp)
        if extra.min_atk and not eff.min_atk:
            eff.min_atk = _as_int(extra.min_atk)

    if re.search(r"获得\s*(?:一|1)?\s*个?\s*(?:空的)?\s*法力水晶", text):
        eff.mana = 1
    if "发现" in text:
        eff.discover = 1
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
    # 表里写了明确数量就以表为准，放在内置正则之后：内置的「召唤一只5/5」
    # 会被卡面里引用的亡语文本带偏（死亡绽放），表里那条才是这张牌的召唤
    if extra is not None and _as_int(extra.summon_count) > 1:
        eff.summon_count = _as_int(extra.summon_count)
        if extra.summon_atk or extra.summon_hp:
            eff.summon_atk, eff.summon_hp = _as_int(extra.summon_atk), _as_int(extra.summon_hp)
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
    # 「在你使用一张随从牌后，使其获得+X/+Y并使其休眠」：加成是延迟的，且目标会休眠，
    # 不能当成现在就到手的属性（否则这张牌会被严重高估）
    if "使用一张随从牌后" in text and "休眠" in text:
        # 加成不是现在到手，而是「下一个随从」吃到；同时那个随从会休眠一回合
        eff.pending_buff = max(eff.pending_buff, eff.buff_atk or 3)
        eff.buff_atk, eff.buff_hp = 0, 0
        eff.buff_hand = False
        eff.note = "下一个随从 +N/+N（会休眠一回合）"
    eff.known = bool(
        eff.damage or eff.heal or eff.draw or eff.buff_atk or eff.buff_hp
        or eff.destroy or eff.summon_atk or eff.summon_hp or eff.summon_count > 1
        or eff.freeze or eff.mana or eff.armor
        or eff.cost_delta_self or eff.cost_delta_opp
        or eff.discover or eff.steal or eff.set_hp
    ) or card.type in ("MINION", "WEAPON", "HERO")
    if not eff.known:
        eff.note = "效果未能建模"
    return eff


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
        return card.name, (card.cost if card.cost is not None else 2), eff
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
