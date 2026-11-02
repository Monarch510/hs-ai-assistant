"""建议引擎：这一回合怎么打。

做三件事：
1. 斩杀检测 —— 先算「这回合能不能弄死对面」，这是最有用也最容易算准的。
2. 动作评分 —— 把每个合法动作模拟一遍，用 model.evaluate 比较局面优劣。
3. 组合成一条「本回合路线」—— 贪心地一步步来，形成可执行的顺序建议。

它是 1 层搜索（每步只看一步后果），不是 MCTS。所以它擅长：
斩杀、交换、站场、节奏；不擅长：多回合组合技、需要保留资源的运营。
这个边界会如实写在建议理由里，不假装懂更多。
"""

from __future__ import annotations

import dataclasses
import math
import re
from dataclasses import dataclass
from typing import Any

from .cards import Card, CardDB
from .model import (
    Board,
    Effect,
    Unit,
    evaluate,
    hero_power_effect,
    parse_effect,
    is_passive_hero_power,
)

EPS = 0.35          # 提升小于这个值就不算改进

# 用户定的优先级：斩杀 > 对场面有帮助 > 过牌 > 发现 > 防斩杀。
# 用「分类加分」实现：它不会推翻明显更好的选择，但同档收益时按这个顺序偏向。
PRIORITY_BONUS = {
    "lethal": 1000.0,
    "free_trade": 6.0,
    "board": 3.0,
    "draw": 1.6,
    "discover": 1.0,
    "defend": 0.6,
    "other": 0.0,
}


def categorize(action: Action, before: Board, after: Board, side: int) -> str:
    """给一个动作归类，用于优先级排序。"""
    if after.sides[1 - side].hero_hp <= 0:
        return "lethal"
    killed = sum(1 for u in before.sides[1 - side].board
                 if u.alive and not any(x is u or x.card_id == u.card_id for x in after.sides[1 - side].board))
    if killed:
        # 「白吃」：换掉对面随从，自己一个都没少 —— 保场面的最优交换，优先级要高
        lost = sum(1 for u in before.sides[side].board
                   if u.alive and not any(x is u or x.card_id == u.card_id for x in after.sides[side].board))
        return "free_trade" if lost == 0 else "board"
    if action.kind == "play":
        card = before.sides[side].hand[action.card_idx] if action.card_idx < len(before.sides[side].hand) else None
        if card is not None:
            eff = parse_effect(card)
            if eff.draw:
                return "draw"
            if "发现" in (card.text or ""):
                return "discover"
            if eff.heal or "护甲" in (card.text or ""):
                return "defend"
        return "board"      # 出随从/其它牌都算推进场面
    if action.kind == "hero_power":
        return "defend"
    if action.kind == "attack":
        return "board"
    return "other"
MAX_STEPS = 6


# ---------------------------------------------------------------- 动作

@dataclass
class Action:
    kind: str                      # play | attack | hero_power | end
    card_idx: int = -1
    unit_idx: int = -1
    target: str = "none"           # enemy_hero | enemy:<i> | friendly:<i> | none
    cost: int = 0
    desc: str = ""
    card_id: str = ""
    pos: int = -1                  # 打出的随从放在第几位（0=最左）

    def key(self) -> tuple:
        return (self.kind, self.card_idx, self.unit_idx, self.target)


CHOOSE_ONE_RE = re.compile(r"抉择[：:]\s*(.+)$", re.S)


def choose_one_branches(card, cards=None) -> list[tuple[str, "Effect"]]:
    """把「抉择：A。或者 B。」拆成各个分支，返回 [(标签, 效果)]。

    每个分支用「只含这段文字的临时卡面」重新解析，避免整段文字互相干扰。
    """
    text = (card.text or "").replace("<b>", "").replace("</b>", "")
    m = CHOOSE_ONE_RE.search(text)
    if not m:
        return []
    body = m.group(1).strip()
    parts = [p.strip(" 。.") for p in re.split(r"或者|或", body) if p.strip(" 。.")]
    if len(parts) < 2:
        return []
    out: list[tuple[str, Effect]] = []
    for part in parts[:3]:
        label = part[:14]
        try:
            eff = parse_effect(dataclasses.replace(card, text=part))
        except Exception:
            eff = parse_effect(card)
        out.append((label, eff))
    return out


def legal_actions(board: Board, side: int, cards: CardDB) -> list[Action]:
    me, foe = board.sides[side], board.sides[1 - side]
    acts: list[Action] = []

    taunts = [u for u in foe.board if u.alive and u.taunt]
    for i, u in enumerate(me.board):
        if not u.alive or u.atk <= 0 or u.attacks_left <= 0 or u.frozen or u.dormant:
            continue
        if taunts:
            for j, t in enumerate(foe.board):
                if t.alive and t.taunt and not t.dormant:
                    acts.append(Action("attack", unit_idx=i, target=f"enemy:{j}",
                                       desc=f"第{i+1}位 {u.name} {u.atk}/{u.hp} 攻击第{j+1}位 {t.name} {t.atk}/{t.hp}"))
        else:
            if not u.rush_only:
                acts.append(Action("attack", unit_idx=i, target="enemy_hero",
                                   desc=f"第{i+1}位 {u.name} {u.atk}/{u.hp} 攻击英雄"))
            for j, t in enumerate(foe.board):
                if t.alive and not t.dormant:
                    acts.append(Action("attack", unit_idx=i, target=f"enemy:{j}",
                                       desc=f"第{i+1}位 {u.name} {u.atk}/{u.hp} 攻击第{j+1}位 {t.name} {t.atk}/{t.hp}"))

    # 英雄拿武器也能攻击：打脸或打随从，同样受嘲讽限制
    weapon_atk = me.weapon.atk if (me.weapon is not None) else 0
    # 英雄自带攻击力（被加攻/装备效果）也能攻击，不只有武器
    hero_atk = max(weapon_atk, me.hero_atk)
    if hero_atk > 0 and (me.weapon_attacks_left > 0 or me.hero_attacks_left > 0):
        if taunts:
            for j, t in enumerate(foe.board):
                if t.alive and t.taunt and not t.dormant:
                    acts.append(Action("hero_attack", target=f"enemy:{j}",
                                       desc=f"英雄（{hero_atk}攻）攻击第{j+1}位 {t.name}"))
        else:
            acts.append(Action("hero_attack", target="enemy_hero",
                               desc=f"英雄（{hero_atk}攻）攻击英雄"))
            for j, t in enumerate(foe.board):
                if t.alive and not t.dormant:
                    acts.append(Action("hero_attack", target=f"enemy:{j}",
                                       desc=f"英雄（{hero_atk}攻）攻击第{j+1}位 {t.name}"))

    # 帝国之眼在场：每回合可用两次技能，已用过的不能重复
    if me.cthun_uses_left > 0:
        # 找到场上那只克苏恩，用它当实例标识（同一只内技能不能重复）
        key = "default"
        for idx, u in enumerate(me.board):
            if u.name and u.name.startswith("克苏恩"):
                key = f"{u.card_id}#{idx}"
                break
        used_here = (getattr(me, "_cthun_by_unit", {}) or {}).get(key, set())
        for skill in CTHUN_SKILL_NAMES:
            if skill in used_here:
                continue
            acts.append(Action("cthun_skill", target=f"cthun:{key}:{skill}",
                               desc=f"克苏恩技能：{skill}"))

    # 预备：把带「预备」的牌拖到牌库上，花掉剩余法力换减费
    if me.mana > 0:
        for i, card in enumerate(me.hand):
            if card is None or "预备" not in (card.text or ""):
                continue
            cut = me.mana + 1
            acts.append(Action("prepare", card_idx=i, cost=me.mana,
                               desc=f"预备 {card.name}（花 {me.mana} 费，费用 -{cut}）",
                               card_id=card.id))

    full_board = len([u for u in me.board if u.alive]) >= 7
    for i, card in enumerate(me.hand):
        if card is None:
            continue
        if card.cost > me.mana:
            continue
        branches = choose_one_branches(card, cards)
        if len(branches) >= 2:
            for k, (label, _eff) in enumerate(branches):
                acts.append(Action("play", card_idx=i, cost=card.cost,
                                   target=f"choose:{k}",
                                   desc=f"打出 {card.name}（{card.cost}费）→ {label}",
                                   card_id=card.id))
            continue
        eff = parse_effect(card)
        if card.is_minion and full_board:
            continue
        if card.is_spell and eff.damage and eff.scope == "target":
            # 直伤法术既能打随从也能打脸 —— 只有打随从是之前漏的，导致
            # 手里握着能斩杀的火球却永远不推荐
            for j, t in enumerate(foe.board):
                if t.alive and not t.dormant:
                    acts.append(Action("play", card_idx=i, target=f"enemy:{j}",
                                       cost=card.cost, desc=f"用 {card.name}（{card.cost}费）打第{j+1}位 {t.name}", card_id=card.id))
            acts.append(Action("play", card_idx=i, target="enemy_hero",
                               cost=card.cost, desc=f"用 {card.name}（{card.cost}费）打脸", card_id=card.id))
            continue
        if card.is_spell and eff.destroy and eff.scope == "target":
            for j, t in enumerate(foe.board):
                if t.alive and not t.dormant:
                    acts.append(Action("play", card_idx=i, target=f"enemy:{j}",
                                       cost=card.cost, desc=f"用 {card.name}（{card.cost}费）解第{j+1}位 {t.name}", card_id=card.id))
            continue
        if card.is_spell and (eff.buff_atk or eff.buff_hp or eff.heal):
            if any(u.alive for u in me.board):
                for j, t in enumerate(me.board):
                    if t.alive:
                        acts.append(Action("play", card_idx=i, target=f"friendly:{j}",
                                           cost=card.cost, desc=f"用 {card.name}（{card.cost}费）强化第{j+1}位 {t.name}", card_id=card.id))
                continue
        # 打出手牌不指定站位：给出名称、费用、身材就够照做了
        if card.is_minion or card.is_weapon:
            label = f"{card.cost}费 {max(0, card.atk)}/{max(1, card.health)}"
        else:
            label = f"{card.cost}费"
        note = ""
        if card.is_minion:
            # 这一只正好踩到被动技能的计数点，战吼会结算两次
            if battlecry_times(me.minions_played + 1, me.passive_rule) > 1:
                note = "（战吼触发两次）"
        if ("触发" in (card.text or "") and "技能" in (card.text or "")) or "克苏恩的技能" in (card.text or ""):
            # 这类牌打出时要四选一：每项技能各生成一个动作
            for skill in CTHUN_SKILL_NAMES:
                acts.append(Action("play", card_idx=i, cost=card.cost,
                                   target=f"cthun:{skill}",
                                   desc=f"打出 {card.name}（{card.cost}费）→ {skill}",
                                   card_id=card.id))
            continue
        acts.append(Action("play", card_idx=i, cost=card.cost,
                           desc=f"打出 {card.name}（{label}）{note}", card_id=card.id))

    # 手里有两张一模一样的牌时，只出一条建议（描述相同 = 同一件事）
    seen_play: set[str] = set()
    deduped: list[Action] = []
    for act in acts:
        if act.kind == "play":
            if act.desc in seen_play:
                continue
            seen_play.add(act.desc)
        deduped.append(act)
    acts = deduped

    name, hp_cost, hp_eff = hero_power_effect(me.hero_class, me.hero_power)
    passive_hp = is_passive_hero_power(me.hero_power)
    if not passive_hp and not me.hero_power_used and me.mana >= hp_cost:
        if hp_eff.damage and hp_eff.scope == "enemy_hero":
            acts.append(Action("hero_power", cost=hp_cost, target="enemy_hero",
                               desc=f"英雄技能：{name}（打脸）"))
        elif hp_eff.damage:
            acts.append(Action("hero_power", cost=hp_cost, target="enemy_hero",
                               desc=f"英雄技能：{name}（打脸）"))
            for j, t in enumerate(foe.board):
                if t.alive and not t.dormant:
                    acts.append(Action("hero_power", cost=hp_cost, target=f"enemy:{j}",
                                       desc=f"英雄技能：{name} 打 {t.name}"))
        else:
            acts.append(Action("hero_power", cost=hp_cost, desc=f"英雄技能：{name}"))

    acts.append(Action("end", desc="结束回合"))
    return acts


def _find_unit(board: Board, side: int, target: str) -> Unit | None:
    if not target.startswith(("enemy:", "friendly:")):
        return None
    which, idx = target.split(":")
    pool = board.sides[1 - side].board if which == "enemy" else board.sides[side].board
    try:
        u = pool[int(idx)]
    except (IndexError, ValueError):
        return None
    return u if u.alive else None


_GIGANTIC_CACHE: dict[str, list] = {}


def _gigantic_appendages(card, cards) -> list:
    """巨型+N 的附属物。按名字前缀在卡库里找（「奥拉基尔的灌能之手」这类）。"""
    text = (card.text or "")
    m = re.search(r"巨型\+(\d)", text)
    if not m or cards is None:
        return []
    n = int(m.group(1))
    cached = _GIGANTIC_CACHE.get(card.id)
    if cached is None:
        base = card.name.split("，")[0].split(",")[0].strip()
        found = []
        for other in cards.by_id.values():
            if other is card or not other.is_minion or not other.name:
                continue
            if other.name.startswith(base) and other.name != card.name:
                found.append(other)
        _GIGANTIC_CACHE[card.id] = found
        cached = found
    return cached[:n]


def apply_action(board: Board, side: int, action: Action, cards: CardDB) -> Board:
    """在副本上执行动作，返回新局面。"""
    nb = board.clone()
    nb.active = side
    me, foe = nb.sides[side], nb.sides[1 - side]

    if action.kind == "end":
        return nb

    if action.kind == "hero_power":
        name, cost, eff = hero_power_effect(me.hero_class, me.hero_power)
        me.mana -= cost
        me.hero_power_used = True
        if eff.damage and (u := _find_unit(nb, side, "enemy:0")):
            nb.damage_unit(foe, u, eff.damage)
        elif eff.damage:
            nb.damage_hero(foe, eff.damage)
        elif eff.heal:
            me.hero_hp = min(30, me.hero_hp + eff.heal)
        elif eff.summon_atk or eff.summon_hp:
            me.board.append(Unit(card_id="", name=name, atk=eff.summon_atk, hp=max(1, eff.summon_hp)))
        nb.note(f"英雄技能：{name}")
        return nb

    if action.kind == "attack":
        u = me.board[action.unit_idx] if action.unit_idx < len(me.board) else None
        if u is None or not u.alive:
            return nb
        if action.target == "enemy_hero":
            nb.damage_hero(foe, u.atk)
            if u.lifesteal:
                me.hero_hp = min(30, me.hero_hp + u.atk)
            if u.poisonous:
                pass
            nb.note(f"{u.name} 攻击英雄（{u.atk} 点）")
        else:
            t = _find_unit(nb, side, action.target)
            if t is None:
                return nb
            atk_t, atk_u = t.atk, u.atk
            nb.damage_unit(foe, t, atk_u)
            nb.damage_unit(me, u, atk_t)
            if u.lifesteal:
                me.hero_hp = min(30, me.hero_hp + min(atk_u, t.max_hp + 9))
            nb.note(f"{u.name} 交换 {t.name}")
        u.attacks_left -= 1
        return nb

    if action.kind == "hero_attack":
        atk = max(me.weapon.atk if (me.weapon is not None) else 0, me.hero_atk)
        if atk <= 0 or (me.weapon_attacks_left <= 0 and me.hero_attacks_left <= 0):
            return nb
        if action.target == "enemy_hero":
            nb.damage_hero(foe, atk)
            nb.note(f"英雄用武器攻击英雄（{atk} 点）")
        else:
            t = _find_unit(nb, side, action.target)
            if t is None:
                return nb
            nb.damage_unit(foe, t, atk)
            nb.damage_hero(me, t.atk)        # 打随从要挨反伤
            nb.note(f"英雄用武器攻击 {t.name}")
        if me.weapon_attacks_left > 0:
            me.weapon_attacks_left -= 1
        else:
            me.hero_attacks_left -= 1
        # 「在你的英雄攻击后」这类武器效果：攻击后再结算
        if me.weapon is not None and me.weapon.card_id and cards is not None:
            wcard = cards.get(me.weapon.card_id)
            weff = parse_effect(wcard) if wcard is not None else None
            if weff is not None and (weff.summon_atk or weff.summon_hp):
                for _ in range(max(1, weff.summon_count)):
                    me.board.append(Unit(card_id="", name="召唤物",
                                         atk=max(0, weff.summon_atk),
                                         hp=max(1, weff.summon_hp),
                                         attacks_left=1 if "突袭" in (wcard.text or "") else 0))
                nb.note(f"{wcard.name} 触发召唤")
        # 武器每次攻击掉 1 点耐久，掉光就碎
        if me.weapon is not None:
            me.weapon.hp -= 1
            if me.weapon.hp <= 0:
                nb.note(f"武器 {me.weapon.name} 碎裂")
                me.weapon = None
        return nb

    if action.kind == "cthun_skill":
        if me.cthun_uses_left <= 0:
            return nb
        _parts = (action.target or "").split(":")
        skill = _parts[-1]
        # 规则：强化需要「同一只克苏恩把四个技能各用一次」。
        # 不同克苏恩实例各自计数，所以同一个技能在别的克苏恩身上用不算用过。
        key = _parts[1] if len(_parts) > 2 else "default"
        per_unit = getattr(me, "_cthun_by_unit", None)
        if per_unit is None:
            per_unit = me._cthun_by_unit = {}
        used_here = per_unit.setdefault(key, set())
        if skill in used_here:
            return nb
        tripled = len(used_here | set(getattr(me, "_log_cthun", set()))) >= 4
        nb.note(apply_cthun_skill(nb, side, skill, tripled=tripled))
        used_here.add(skill)
        me.cthun_used.add(skill)
        me.cthun_uses_left -= 1
        return nb

    if action.kind == "prepare":
        if not (0 <= action.card_idx < len(me.hand)):
            return nb
        card = me.hand[action.card_idx]
        cut = me.mana + 1
        me.hand[action.card_idx] = dataclasses.replace(card, cost=max(0, card.cost - cut))
        me.mana = 0
        nb.note(f"预备 {card.name}：法力 -{cut}")
        return nb

    # play
    if action.card_idx < 0 or action.card_idx >= len(me.hand):
        return nb
    card = me.hand.pop(action.card_idx)
    if (action.target or "").startswith("choose:"):
        try:
            k = int(action.target.split(":", 1)[1])
            br = choose_one_branches(card, cards)
            if k < len(br):
                card = dataclasses.replace(card, text="抉择：" + br[k][0])
        except (ValueError, IndexError):
            pass
    if (action.target or "").startswith("cthun:"):
        skill = action.target.split(":", 1)[1]
        tripled = len(getattr(me, "cthun_used", set()) or []) >= 4
        nb.note(apply_cthun_skill(nb, side, skill, tripled=tripled))
    me.mana -= card.cost
    if card.is_minion:
        u = Unit(
            card_id=card.id,
            name=card.name,
            atk=max(0, card.atk),
            hp=max(1, card.health),
            max_hp=max(1, card.health),
            taunt=card.has("TAUNT"),
            divine=card.has("DIVINE_SHIELD"),
            charge=card.has("CHARGE"),
            rush=card.has("RUSH"),
            windfury=card.has("WINDFURY"),
            lifesteal=card.has("LIFESTEAL"),
            poisonous=card.has("POISONOUS"),
            spell_damage=_spell_damage(card),
            attacks_left=2 if card.has("WINDFURY") else 1,
            frozen=card.has("FREEZE"),
        )
        u.can_attack_now = u.charge or u.rush
        if not u.charge:
            # 突袭：入场当回合就能动，但只能吃随从，不能打脸
            u.attacks_left = 1 if u.rush else 0
            u.rush_only = u.rush
        if 0 <= action.pos <= len(me.board):
            me.board.insert(action.pos, u)      # 按建议的位置放置
        else:
            me.board.append(u)
        # 巨型+N：打出时同时召唤 N 个附属物（每个占一个随从位）
        for add in _gigantic_appendages(card, cards):
            me.board.append(Unit(
                card_id=add.id, name=add.name,
                atk=max(0, add.atk), hp=max(1, add.health),
                max_hp=max(1, add.health),
                taunt=add.has("TAUNT"), divine=add.has("DIVINE_SHIELD"),
                rush=add.has("RUSH"), windfury=add.has("WINDFURY"),
                attacks_left=0,
            ))
        eff = parse_effect(card)
        me.minions_played += 1
        times = battlecry_times(me.minions_played, me.passive_rule)
        if eff.damage or eff.buff_atk or eff.heal or eff.draw:
            tgt = _find_unit(nb, side, action.target)
            for _ in range(times):
                nb.apply_effect(eff, me, tgt)
            if times > 1:
                nb.note(f"被动技能触发：{card.name} 的战吼结算了 {times} 次")
        nb.note(f"打出 {card.name}")
    elif card.is_spell:
        me.spells_cast += 1
        eff = parse_effect(card)
        if eff.mana:
            me.mana += eff.mana          # 幸运币这类：打出来本回合多 1 费
        tgt = _find_unit(nb, side, action.target)
        if eff.damage and eff.scope == "target" and tgt is None:
            # 没有目标：直伤打脸
            nb.damage_hero(foe, eff.damage + sum(u.spell_damage for u in me.board if u.alive))
        else:
            nb.apply_effect(eff, me, tgt)
        nb.note(f"施放 {card.name}")
    elif card.is_weapon:
        me.weapon = Unit(card_id=card.id, name=card.name, atk=card.atk, hp=max(1, card.durability),
                         max_hp=max(1, card.durability))
        nb.note(f"装备 {card.name}")
    else:
        eff = parse_effect(card)
        nb.apply_effect(eff, me, _find_unit(nb, side, action.target))
    return nb


def _spell_damage(card: Card) -> int:
    if card.has("SPELL_DAMAGE") and card.text:

        m = re.search(r"法术伤害\+(\d+)", card.text)
        if m:
            return int(m.group(1))
    return 0


# ---------------------------------------------------------------- 单步最优

def best_action(
    board: Board,
    side: int,
    cards: CardDB,
    *,
    weights: dict[str, float] | None = None,
    aoe_risk: float = 0.0,
    min_gain: float = 0.25,
) -> tuple[Action, Board, float] | None:
    """返回 (动作, 执行后的局面, 局面分提升)。没有值得做的动作则返回 None。"""
    baseline = evaluate(board, weights, aoe_risk, side)
    best: tuple[Action, Board, float] | None = None
    for act in legal_actions(board, side, cards):
        if act.kind == "end":
            continue
        nb = apply_action(board, side, act, cards)
        score = evaluate(nb, weights, aoe_risk, side)
        if nb.sides[1 - side].hero_hp <= 0:
            score += 5_000
        if nb.sides[side].hero_hp <= 0:
            score -= 5_000
        gain = score - baseline
        if gain <= min_gain:
            continue
        if best is None or gain > best[2]:
            best = (act, nb, gain)
    return best


# ---------------------------------------------------------------- 斩杀检测

def max_face_damage(board: Board, side: int, cards: CardDB | None = None) -> dict[str, Any]:
    """估算本回合能打到对面脸上的最大伤害（已考虑嘲讽与费用）。"""
    me, foe = board.sides[side], board.sides[1 - side]
    taunts = [u for u in foe.board if u.alive and u.taunt]

    # 攻击是「整段」消耗的：一个 8/8 撞 2/5 嘲讽，溢出的 3 点不会转到脸上。
    # 所以不能简单用「总攻击力 - 嘲讽血量」，要把攻击段一个个分配出去。
    swings: list[int] = []
    for u in me.board:
        if not u.alive or u.atk <= 0 or u.frozen or u.attacks_left <= 0 or u.dormant:
            continue
        hits = 2 if (u.windfury and u.attacks_left >= 2) else 1
        swings.extend([u.atk] * hits)
    if me.weapon and me.weapon.atk > 0:
        swings.append(me.weapon.atk)

    attack = sum(swings)
    taunt_hp = sum((3 if u.divine else u.hp) for u in taunts)
    left = sorted(swings)
    for t in sorted(taunts, key=lambda u: 3 if u.divine else u.hp):
        need = 3 if t.divine else t.hp
        idx = next((i for i, sw in enumerate(left) if sw >= need), None)
        if idx is None:
            left = []          # 过不去墙：剩下的攻击全被嘲讽吃掉
            break
        left.pop(idx)          # 这一次攻击被嘲讽消耗掉，溢出伤害不转脸
    usable = sum(left)

    # 手里的直伤
    spell_dmg = 0
    mana = me.mana
    face_spells = []
    for card in sorted(me.hand, key=lambda c: c.cost):
        if not card.is_spell or card.cost > mana:
            continue
        eff = parse_effect(card)
        if eff.damage and eff.scope in ("target", "enemy_hero", "random_enemy"):
            face_spells.append((card.cost, eff.damage, card.name))
    for cost, dmg, name in face_spells:
        if cost <= mana:
            mana -= cost
            spell_dmg += dmg

    hp_name, hp_cost, hp_eff = hero_power_effect(me.hero_class, me.hero_power)
    hero_power = 0
    if (not is_passive_hero_power(me.hero_power) and not me.hero_power_used
            and mana >= hp_cost and hp_eff.damage):
        hero_power = hp_eff.damage

    total = usable + spell_dmg + hero_power
    need = foe.hero_hp + foe.armor
    return {
        "attack": attack,
        "taunt_block": taunt_hp,
        "spell": spell_dmg,
        "hero_power": hero_power,
        "damage": total,
        "need": need,
        "lethal": total >= need and need > 0,
        "has_taunt": bool(taunts),
        "face_spells": [n for _, _, n in face_spells],
    }


# ---------------------------------------------------------------- 路线规划

def _reason(before: Board, after: Board, action: Action, side: int) -> str:
    me0, foe0 = before.sides[side], before.sides[1 - side]
    me1, foe1 = after.sides[side], after.sides[1 - side]
    killed = [u.name for u in foe0.board if u.alive and not any(x is u or x.card_id == u.card_id for x in foe1.board)]
    hurt = []
    for u in foe0.board:
        if not u.alive:
            continue
        still = next((x for x in foe1.board if x.card_id == u.card_id and x.alive), None)
        if still is not None and still.hp < u.hp:
            hurt.append(f"{u.name} 剩 {still.hp} 血")
    dealt = max(0, (foe0.hero_hp + foe0.armor) - (foe1.hero_hp + foe1.armor))
    parts = []
    if killed:
        parts.append("解掉 " + "、".join(killed[:2]))
    if hurt and not killed:
        parts.append("打残 " + "、".join(hurt[:2]) + "（下一手可以换掉）")
    if dealt:
        parts.append(f"打脸 {dealt} 点")
    if len(me1.board) > len([u for u in me0.board if u.alive]):
        new = [u.name for u in me1.board if u not in me0.board][:1]
        if new:
            parts.append(f"站上 {new[0]}")
    if action.kind == "end":
        return "保留费用，等对方先动"
    if not parts:
        parts.append("调整站位与手牌")
    return "，".join(parts)


def _lethal_sequence(board: Board, side: int, cards: CardDB) -> list[Action] | None:
    """如果能斩杀，直接贪心拼出一条「真的能打死」的出牌顺序。

    为什么不交给 plan_turn 的评分搜索：评分里「有斩杀」是个阶跃加分，
    每打一下「还能不能斩杀」都会翻一次，导致它打一下就停（这正是之前的 bug）。
    斩杀是确定性问题，直接用伤害当目标函数更可靠。
    """
    need_check = max_face_damage(board, side, cards)
    if not need_check["lethal"]:
        return None

    work = board.clone()
    work.active = side
    seq: list[Action] = []
    for _ in range(12):
        foe = work.sides[1 - side]
        if foe.hero_hp <= 0:
            break
        best: tuple[float, Action, Board] | None = None
        for act in legal_actions(work, side, cards):
            if act.kind == "end":
                continue
            nb = apply_action(work, side, act, cards)
            nf = nb.sides[1 - side]
            dealt = (foe.hero_hp + foe.armor) - (nf.hero_hp + nf.armor)
            taunts_before = sum(1 for u in foe.board if u.alive and u.taunt)
            taunts_after = sum(1 for u in nf.board if u.alive and u.taunt)
            score = dealt * 10 + (taunts_before - taunts_after) * 4 + (1000 if nf.hero_hp <= 0 else 0)
            if best is None or score > best[0]:
                best = (score, act, nb)
        if best is None or best[0] <= 0:
            break
        seq.append(best[1])
        work = best[2]

    return seq if work.sides[1 - side].hero_hp <= 0 else None


def plan_turn(
    board: Board,
    cards: CardDB,
    *,
    aoe_risk: float = 0.0,
    weights: dict[str, float] | None = None,
    side: int = 0,
    steps: int = MAX_STEPS,
    with_alternatives: bool = True,
    opp_coeff: float = 1.5,
) -> dict[str, Any]:
    """贪心规划一整回合，返回路线 + 备选。"""
    working = board.clone()
    working.active = side
    baseline = evaluate(working, weights, aoe_risk, side)
    race = race_plan(board, side, cards, opp_coeff)
    mode = race["mode"]

    # 能斩杀就别做别的：先按伤害贪心拼出完整顺序
    lethal_seq = _lethal_sequence(board, side, cards)
    if lethal_seq:
        plan: list[dict[str, Any]] = []
        cur = board.clone()
        for act in lethal_seq:
            nb = apply_action(cur, side, act, cards)
            plan.append({"desc": act.desc, "reason": _reason(cur, nb, act, side), "gain": 999.0})
            cur = nb
        return {
            "plan": plan,
            "gain": 999.0,
            "alternatives": [],
            "lethal": max_face_damage(board, side, cards),
            "final": cur,
        }

    plan: list[dict[str, Any]] = []
    total_gain = 0.0
    for _ in range(steps):
        acts = legal_actions(working, side, cards)
        best: tuple[float, Action, Board] | None = None
        for act in acts:
            if act.kind == "end":
                continue
            if not _mode_allows(act, working, side, mode, cards):
                continue
            nb = apply_action(working, side, act, cards)
            score = evaluate(nb, weights, aoe_risk, side)
            # 斩杀直接置顶
            if nb.sides[1 - side].hero_hp <= 0:
                score += 5_000
            raw = score - evaluate(working, weights, aoe_risk, side)
            if raw <= 0.01 and nb.sides[1 - side].hero_hp > 0:
                continue          # 什么也没改变的动作（例如没有效果的英雄技能）不参与排序
            gain = raw + PRIORITY_BONUS.get(categorize(act, working, nb, side), 0.0)
            if act.kind == "attack" and (act.target or "").startswith("enemy:"):
                # 并列时用更不值钱的那只去换，保住高价值随从
                attacker = working.sides[side].board[act.unit_idx] \
                    if act.unit_idx < len(working.sides[side].board) else None
                gain -= unit_threat(attacker, cards, working, side) * 1e-3
            if gain <= EPS:
                continue
            if best is None or gain > best[0]:
                best = (gain, act, nb)
        if best is None:
            break
        gain, act, nb = best
        reason = _reason(working, nb, act, side)
        plan.append({"desc": act.desc, "reason": reason, "gain": round(gain, 2)})
        total_gain += gain
        working = nb
        if working.sides[1 - side].hero_hp <= 0:
            break

    # 剩下法力又没别的事可做时，把「预备」当作收尾动作推荐出来
    leftover = working.sides[side].mana
    if leftover > 0:
        prep = [a for a in legal_actions(working, side, cards) if a.kind == "prepare"]
        if prep and (not plan or leftover >= 3):
            act = prep[0]
            nb = apply_action(working, side, act, cards)
            plan.append({
                "desc": act.desc,
                "reason": f"剩下的 {leftover} 点法力留着浪费，预备后这张牌费用直降 {leftover + 1}",
                "gain": 0.0,
            })
            working = nb

    lethal = max_face_damage(board, side, cards)
    if lethal["lethal"] and not plan:
        # 理论伤害够、但枚举出的动作里没有能兑现的线路（例如只有法术却算错口径）。
        # 这时宁可说「不算斩杀」，也不要塞一条点不出来的假动作。
        lethal = dict(lethal, lethal=False, note="理论伤害够，但没有能兑现的出牌线路")

    # 备选路线：不同起手的 Top 动作（让用户有得选）
    alts: list[dict[str, Any]] = []
    planned = {p["desc"] for p in plan}
    for act in (legal_actions(board, side, cards) if with_alternatives else []):
        if act.kind == "end" or act.desc in planned:
            continue
        nb = apply_action(board, side, act, cards)
        gain = evaluate(nb, weights, aoe_risk, side) - baseline
        if nb.sides[1 - side].hero_hp <= 0:
            gain += 5_000
        sub = plan_turn(nb, cards, aoe_risk=aoe_risk, weights=weights, side=side, steps=2,
                        with_alternatives=False)
        if gain + sub.get("gain", 0) <= 0.01 and nb.sides[1 - side].hero_hp > 0:
            continue          # 负收益动作不进备选，否则会出现「建议打幸运币」这种
        alts.append({
            "desc": act.desc,
            "reason": _reason(board, nb, act, side),
            "gain": round(gain + sub.get("gain", 0), 2),
            "then": [s["desc"] for s in sub.get("plan", [])],
        })
    alts.sort(key=lambda a: -a["gain"])

    return {
        "mode": mode,
        "race": race,
        "plan": plan,
        "gain": round(total_gain, 2),
        "alternatives": alts[:3],
        "lethal": lethal,
        "final": working,
    }


def advice_lines(plan_result: dict[str, Any]) -> list[dict[str, Any]]:
    """把规划结果整理成 UI 需要的「1-3 条建议」。"""
    out: list[dict[str, Any]] = []
    plan = plan_result.get("plan") or []
    if plan:
        out.append({
            "rank": 1,
            "title": plan[0]["desc"] if len(plan) == 1 else f"{plan[0]['desc']} → 共 {len(plan)} 步",
            "score": _score_from_gain(plan_result.get("gain", 0.0)),
            "reason": "；".join(p["reason"] for p in plan[:3]),
            "actions": [p["desc"] for p in plan],
        })
    if not out and not (plan_result.get("alternatives") or []):
        return [{
            "rank": 1,
            "title": "结束回合",
            "score": 50,
            "reason": "没有值得做的事",
            "actions": ["结束回合"],
        }]

    for alt in plan_result.get("alternatives") or []:
        if len(out) >= 3:
            break
        if out and alt["desc"] == out[0]["title"]:
            continue
        out.append({
            "rank": len(out) + 1,
            # 没有主推荐时，第一条备选就是主推荐，别再挂「或者：」
            "title": ("或者：" if out else "") + alt["desc"],
            "score": _score_from_gain(alt["gain"]),
            "reason": alt["reason"] + (f"，之后可接 {alt['then'][0]}" if alt.get("then") else ""),
            "actions": [alt["desc"]] + list(alt.get("then") or []),
        })
    return out


def _score_from_gain(gain: float) -> int:
    if gain >= 100:
        return 99
    return max(1, min(97, int(round(55 + math.log1p(max(gain, 0)) * 9))))


def validate_sequence(board: Board, descs: list[str], cards: CardDB,
                      side: int = 0) -> list[dict[str, Any]]:
    """把一串「动作描述」按顺序模拟一遍，检查费用和可执行性。

    AI 规划路线时只看得到开局的法力，很容易排出「3+3+3 费但只有 5 费」的路线。
    这里逐步扣费，扣不起的那步标成费用不足，后面的也一并标出来。
    """
    work = board.clone()
    work.active = side
    out: list[dict[str, Any]] = []
    for desc in descs:
        acts = legal_actions(work, side, cards)
        act = next((a for a in acts if a.desc == desc), None)
        if act is None:
            out.append({"desc": desc, "ok": False, "why": "当前不可执行"})
            continue
        if act.cost > work.sides[side].mana:
            out.append({"desc": desc, "ok": False, "why": "费用不足"})
            continue
        out.append({"desc": desc, "ok": True, "why": ""})
        work = apply_action(work, side, act, cards)
    return out


CTHUN_SKILL_NAMES = ("克苏恩的愤怒", "克苏恩的领域", "克苏恩的凝视", "克苏恩的仆从")


def apply_cthun_skill(board: Board, side: int, name: str, tripled: bool = False) -> str:
    """结算一项克苏恩技能。返回一句说明（用于建议理由）。"""
    me, foe = board.sides[side], board.sides[1 - side]
    mult = 3 if tripled else 1

    if name == "克苏恩的愤怒":
        total = 7 * mult
        targets = [u for u in foe.board if u.alive]
        if targets:
            each = max(1, total // max(1, len(targets) + 1))
            for u in list(targets):
                board.damage_unit(foe, u, each)
            board.damage_hero(foe, max(0, total - each * len(targets)))
        else:
            board.damage_hero(foe, total)
        return f"愤怒：{total} 点伤害分散到敌方"

    if name == "克苏恩的凝视":
        total = 5 * mult
        target = max([u for u in foe.board if u.alive], key=lambda u: u.atk, default=None)
        if target is None:
            board.damage_hero(foe, total)
            me.hero_hp += total
            return f"凝视：{total} 点打脸并回血"
        dealt = board.damage_unit(foe, target, total)
        me.hero_hp += dealt
        return f"凝视：{total} 点打 {target.name} 并回血"

    if name == "克苏恩的领域":
        buffers = [u for u in me.board if u.alive][:2]
        for u in buffers:
            u.atk += 2 * mult
            u.hp += 3 * mult
        return f"领域：{len(buffers)} 个随从获得 +{2*mult}/+{3*mult}"

    if name == "克苏恩的仆从":
        count = 2 * mult
        for _ in range(count):
            if len([u for u in me.board if u.alive]) >= 7:
                break
            me.board.append(Unit(card_id="", name="狂热者", atk=2, hp=3, taunt=True, attacks_left=0))
        return f"仆从：召唤 {count} 个 2/3 嘲讽"

    return "未知技能"


def _deck_theme(deck_cards, cards) -> str:
    """从卡组构成判断这套牌靠什么赢：法伤 / 低费 / 随从。"""
    if not deck_cards:
        return ""
    total = len(deck_cards)
    spells = sum(1 for c in deck_cards if c is not None and getattr(c, "type", "") == "SPELL")
    cheap = sum(1 for c in deck_cards if c is not None and (c.cost or 0) <= 2)
    minions = sum(1 for c in deck_cards if c is not None and getattr(c, "type", "") == "MINION")
    if spells / total >= 0.45:
        return "spell"
    if cheap / total >= 0.5:
        return "aggro"
    if minions / total >= 0.6:
        return "minion"
    return ""


def _spell_damage_on_board(board: Board, side: int, cards) -> int:
    """场上「法术伤害 +N」的总量。"""
    total = 0
    for u in board.sides[side].board:
        if not u.alive or cards is None:
            continue
        card = cards.get(u.card_id)
        text = (card.text or "") if card is not None else ""
        if "法术伤害" in text:
            m = re.search(r"法术伤害\+(\d)", text)
            total += int(m.group(1)) if m else 1
    return total


def score_choice(card, effect, board: Board, side: int, mana: int,
                 *, deck_theme: str = "", spell_damage: int = 0,
                 hand_spells: int = 0, opp_fast: bool = False,
                 tripled: bool = False) -> tuple[float, str]:
    """给一个发现/抉择选项打分，返回 (分数, 理由)。

    分六层，从上到下依次叠加：当回合可打 → 立即收益 → 卡组主题
    → 场上加成 → 手牌组合 → 对手形态。
    """
    score = 0.0
    reasons: list[str] = []
    foe = board.sides[1 - side]
    cost = getattr(card, "cost", 0) or 0
    is_spell = getattr(card, "type", "") == "SPELL"
    is_minion = getattr(card, "type", "") == "MINION"

    # ① 当回合能不能打出去
    if cost <= mana:
        score += 2.0
        reasons.append("当回合可打")
    else:
        score -= 2.0

    # ② 立即收益
    mult = 3 if tripled else 1
    if effect.known:
        if effect.damage and effect.scope in ("any", "minion", "enemy", "target", "enemy_minion"):
            killable = [u for u in foe.board
                        if u.alive and u.hp <= effect.damage * mult + spell_damage]
            if killable:
                # 单体伤害只能解掉一个；群伤才算全部
                n = len(killable) if effect.aoe else 1
                best = max(killable, key=lambda u: u.atk)
                score += 3.0 * n + best.atk * 0.3
                reasons.append(f"能解掉{n}个随从（最痛 {best.atk} 攻）")
        if effect.destroy:
            score += 3.0
            reasons.append("硬解")
        if effect.draw:
            score += 1.8 * effect.draw
            reasons.append(f"过{effect.draw}张")
        if effect.heal:
            score += 0.8
            reasons.append("回血")
        if effect.armor:
            score += 0.7
            reasons.append("加甲")
        if effect.mana:
            score += 1.2
            reasons.append("加费")
        if effect.buff_atk or effect.buff_hp:
            # 加 buff：按「每个随从获得的属性价值 × 能加几个」算
            targets_n = min(2, len([u for u in board.sides[side].board if u.alive]))
            per = (effect.buff_atk or 0) * 1.25 * mult + (effect.buff_hp or 0) * mult
            if targets_n and per > 0:
                score += per * targets_n * 0.6
                reasons.append(f"{targets_n} 个随从 +{ (effect.buff_atk or 0)*mult }/+{ (effect.buff_hp or 0)*mult }")
    if is_minion:
        atk = getattr(card, "atk", 0) or 0
        hp = getattr(card, "health", 0) or 0
        value = (atk + hp) / max(1, cost)
        score += min(2.5, value * 0.8)

    # ③ 卡组主题
    if deck_theme == "spell" and is_spell:
        score += 0.8
    elif deck_theme == "aggro" and cost <= 2:
        score += 0.6
    elif deck_theme == "minion" and is_minion:
        score += 0.5

    # ④ 场上法伤加成
    if spell_damage > 0 and is_spell and effect.damage:
        score += 0.5 * spell_damage

    # ⑤ 手牌组合：手里法术多、场上又有法伤，法伤随从更值
    if hand_spells >= 3 and spell_damage > 0 and is_minion and "法术伤害" in (card.text or ""):
        score += 0.8
        reasons.append("和手牌法术配合")

    # ⑥ 对手形态：快攻优先解场/回血，控制优先大身材/过牌
    if opp_fast:
        if effect.damage or effect.destroy or effect.taunt if hasattr(effect, "taunt") else False:
            score += 0.8
        if effect.heal or effect.armor:
            score += 0.8
    else:
        if is_minion and (getattr(card, "atk", 0) or 0) >= 5:
            score += 0.6
        if effect.draw:
            score += 0.6

    return score, "，".join(reasons[:3])


def pick_choice(options: list, board: Board, cards, side: int, mana: int,
                *, deck_cards=None, opp_fast: bool = False,
                tripled: bool = False) -> tuple[int, str, float]:
    """从发现/抉择选项里挑一个。返回 (下标, 理由, 与第二名的分差)。"""
    if not options:
        return 0, "", 0.0
    # 克苏恩技能的选择里会混进打出的那张源牌，它不是候选
    # 过滤时保留原始下标，否则返回的 index 会对不上调用方传进来的列表
    pairs = [(idx, o) for idx, o in enumerate(options)]
    skill_pairs = [(idx, o) for idx, o in pairs if str(o.get("name")) in CTHUN_SKILL_NAMES]
    if len(skill_pairs) >= 2:
        pairs = skill_pairs
    theme = _deck_theme(deck_cards or [], cards)
    spell_damage = _spell_damage_on_board(board, side, cards)
    hand_spells = sum(1 for c in board.sides[side].hand
                      if c is not None and getattr(c, "type", "") == "SPELL")
    scored: list[tuple[float, int, str]] = []
    for idx, opt in pairs:
        card = cards.by_name_or_id(str(opt.get("name") or ""))
        effect = parse_effect(card) if card is not None else Effect(known=False)
        if card is None:
            scored.append((-9.0, idx, "认不出这张牌"))
            continue
        score, why = score_choice(card, effect, board, side, mana,
                                  deck_theme=theme, spell_damage=spell_damage,
                                  hand_spells=hand_spells, opp_fast=opp_fast,
                                  tripled=tripled)
        scored.append((score, idx, why))
    scored.sort(key=lambda x: -x[0])
    best_score, best_idx, best_why = scored[0]
    gap = best_score - scored[1][0] if len(scored) > 1 else 99.0
    return best_idx, best_why, gap


# 本局内的临时权重修正（实现在 model 里，这里只做转发）


# ---------------------------------------------------------------- 抢血 / 解场
def aggression_coefficient(deck_cards, db) -> tuple[float, str]:
    """从卡组组成判断对手有多快，返回 (每张手牌折合的伤害, 形态名)。

    用「低费牌占比 + 平均费用」，而不是手填表 —— 换卡组、遇到新卡组都能自动覆盖。
    """
    costs = [c.cost for c in deck_cards if c is not None and c.cost is not None]
    if not costs:
        return 1.5, "未知"
    low = sum(1 for c in costs if c <= 3) / len(costs)
    avg = sum(costs) / len(costs)
    if low >= 0.6 or avg <= 2.6:
        return 3.0, "快攻"
    if avg <= 3.6:
        return 1.5, "中速"
    return 0.8, "控制"


def _hand_burst(board: Board, side: int, cards) -> float:
    """手里能直接打脸的伤害（斩杀时能一次性砸出去的量）。"""
    me = board.sides[side]
    total = 0.0
    for card in me.hand:
        if card is None or getattr(card, "type", "") != "SPELL":
            continue
        try:
            eff = parse_effect(card)
        except Exception:
            continue
        if not eff.known or not eff.damage:
            continue
        if eff.scope in ("target", "any", "enemy_hero", "enemy") and not eff.aoe:
            total += eff.damage
    return total


def race_plan(board: Board, side: int, cards, opp_coeff: float = 1.5) -> dict[str, Any]:
    """判断这回合该抢血还是解场。

    比的是「谁先死」：
        我赢的回合 = (他血量 - 我手上的爆发) / 我每回合的持续伤害
        他赢的回合 = 我血量 / 他每回合的伤害
    """
    me, foe = board.sides[side], board.sides[1 - side]
    my_damage = float(me.board_attack)
    if me.weapon is not None:
        my_damage += me.weapon.atk
    if me.hero_power is not None and not is_passive_hero_power(me.hero_power):
        _, _, eff = hero_power_effect(me.hero_class, me.hero_power)
        if eff.damage and eff.scope in ("enemy_hero", "any"):
            my_damage += eff.damage

    burst = _hand_burst(board, side, cards)
    his_damage = float(foe.board_attack) + len(foe.hand) * opp_coeff

    foe_hp = max(1.0, foe.hero_hp + foe.armor - burst)
    my_hp = max(1.0, me.hero_hp + me.armor)
    my_turns = foe_hp / my_damage if my_damage > 0 else 99.0
    his_turns = my_hp / his_damage if his_damage > 0 else 99.0
    mode = "race" if my_turns < his_turns else "trade"
    return {
        "mode": mode,
        "my_turns": round(my_turns, 1),
        "his_turns": round(his_turns, 1),
        "my_damage": round(my_damage, 1),
        "his_damage": round(his_damage, 1),
        "burst": round(burst, 1),
        "reason": (
            f"我 {my_turns:.1f} 回合赢，他 {his_turns:.1f} 回合赢"
            if my_damage > 0 and his_damage > 0
            else "伤害信息不足"
        ),
    }


def _turns_after(board: Board, side: int, lose_attack: float, kill_attack: float) -> tuple[float, float]:
    """假设打掉对方一只随从（kill_attack）、同时损失我方一些攻击力，重算双方回合数。"""
    me, foe = board.sides[side], board.sides[1 - side]
    my_damage = max(1.0, float(me.board_attack) - lose_attack)
    his_damage = max(0.0, float(foe.board_attack) - kill_attack)
    my_turns = max(1.0, foe.hero_hp + foe.armor) / my_damage
    his_turns = (me.hero_hp + me.armor) / his_damage if his_damage > 0 else 99.0
    return my_turns, his_turns


def _mode_allows(action: Action, before: Board, side: int, mode: str, cards) -> bool:
    """按「抢血 / 解场」过滤动作。

    抢血：只有白吃不拖慢节奏、或者放着它下回合会打死我，才允许交换。
    解场：有划算的交换时不去打脸。
    """
    if action.kind != "attack":
        return True
    me, foe = before.sides[side], before.sides[1 - side]
    unit = me.board[action.unit_idx] if action.unit_idx < len(me.board) else None
    if unit is None:
        return True

    if action.target == "enemy_hero":
        if mode == "race":
            return True
        # 解场模式：只要还有划算的交换，就先换
        return not _has_profitable_trade(before, side, cards)

    if not (action.target or "").startswith("enemy:"):
        return True
    try:
        j = int(action.target.split(":")[1])
    except (IndexError, ValueError):
        return True
    target = foe.board[j] if j < len(foe.board) else None
    if target is None:
        return True

    free = unit.atk >= target.hp and target.atk < unit.hp
    if mode == "race":
        if not free:
            # 抢血模式不主动亏血换怪；除非放着它我会被打死
            return target.atk >= me.hero_hp
        # 白吃之后如果自己一点输出都不剩了，「更快」就是假的，不能吃
        if me.board_attack - unit.atk <= 0:
            return False
        # 和「现在的」对手回合数比，而不是和白吃之后的比 ——
        # 白吃会把他伤害打成 0，那样任何交换都显得划算
        _, his_now = _turns_after(before, side, 0.0, 0.0)
        my_after, _ = _turns_after(before, side, unit.atk, target.atk)
        return my_after <= his_now            # 白吃之后仍然更快，就吃
    # 解场模式：换完之后的「双方差值」更大才换（只看随从 + 武器）
    # 注意是差值：交换会降低我方价值，只看我方会永远判成亏
    try:
        after = apply_action(before, side, action, cards)
    except Exception:
        return True
    gain = ((board_threat(after, side, cards) - board_threat(after, 1 - side, cards))
            - (board_threat(before, side, cards) - board_threat(before, 1 - side, cards)))
    return gain > 0


def _has_profitable_trade(board: Board, side: int, cards=None) -> bool:
    me, foe = board.sides[side], board.sides[1 - side]
    for u in me.board:
        if not u.alive or u.attacks_left <= 0:
            continue
        for t in foe.board:
            if not t.alive:
                continue
            if u.atk >= t.hp and (t.atk < u.hp or t.value >= u.value):
                # 用净值复核：真的划算才算「有划算的交换」
                after_gain = ((unit_threat(u, cards) - unit_threat(t, cards)))
                if after_gain > 0:
                    return True
    return False



def passive_rule(hero_power) -> dict:
    """从被动英雄技能的文本里读出「计数器规则」。

    返回 {"count": 统计什么, "every": 阈值, "effect": 触发什么}，
    识别不了就返回 {}（宁可什么都不做，也不乱触发）。
    """
    text = (hero_power.text or "") if hero_power is not None else ""
    if not text:
        return {}
    # 兹伊的力量这类：每使用五张随从牌，第五个随从的战吼触发两次
    if "每使用五张随从牌" in text and "战吼" in text and "两次" in text:
        return {"count": "minion", "every": 5, "effect": "battlecry_double"}
    if "每使用三张" in text and "战吼" in text:
        return {"count": "minion", "every": 3, "effect": "battlecry_double"}
    return {}


def battlecry_times(played_minions: int, rule: dict) -> int:
    """这一张随从的战吼要结算几次。"""
    every = int(rule.get("every") or 0)
    if every <= 0 or rule.get("effect") != "battlecry_double":
        return 1
    return 2 if played_minions > 0 and played_minions % every == 0 else 1

def unit_threat(unit, cards, board: Board | None = None, side: int = 0) -> float:
    """随从的威胁值。

    身体：攻 ×1.25 + 血（攻击力决定能杀什么，比血更值钱）
    圣盾：接近翻倍（多一条命）
    嘲讽：加一点（只是逼你先打它）
    光环：按「加成 × 受影响随从数」算实际收益
    其余（突袭/吸血/风怒/亡语/剧毒）：固定小值
    """
    if unit is None:
        return 0.0
    body = unit.atk * 1.25 + max(0, unit.hp)
    value = body

    if unit.taunt:
        value += 1.5
    if unit.divine:
        # 圣盾相当于多出一个「同攻击、1 血」的随从
        value += unit.atk * 1.25 + 1.0
    if unit.rush:
        value += 1.0
    if unit.lifesteal:
        value += 1.0
    if unit.windfury:
        value += unit.atk * 0.6
    if unit.poisonous:
        value += 1.5

    card = cards.get(unit.card_id) if (cards is not None and unit.card_id) else None
    text = (card.text or "") if card is not None else ""
    keyword = str(getattr(card, "keyword", "") or "") if card is not None else ""

    if "光环" in keyword or "光环" in text:
        m = re.search(r"\+(\d+)/\+(\d+)", text)
        if m:
            bonus = int(m.group(1)) * 1.25 + int(m.group(2))
            if board is not None:
                others = len([u for u in board.sides[side].board if u.alive]) - 1
            else:
                others = 1
            value += bonus * max(1, others)
        else:
            value += 2.0
    if "亡语" in keyword or "亡语" in text:
        value += 1.5
    return value


def board_threat(board: Board, side: int, cards) -> float:
    """一方的场面价值：随从威胁 + 武器。"""
    me = board.sides[side]
    total = sum(unit_threat(u, cards, board, side) for u in me.board if u.alive)
    if me.weapon is not None:
        total += me.weapon.atk * 1.2 + max(0, me.weapon.hp) * 0.25
    return total
