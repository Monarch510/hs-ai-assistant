"""动作枚举与模拟底座：legal_actions / apply_action / best_action / 克苏恩技能。"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass

from .cards import Card, CardDB
from .model import (
    Board, Effect, Unit, evaluate, hero_power_effect, is_passive_hero_power,
    parse_effect, warden_buff,
)
from .policy_consts import (
    CHOOSE_ONE_RE, CTHUN_MULTIPLIER, CTHUN_SKILL_NAMES, _GIGANTIC_CACHE,
)
from .policy_threat import battlecry_times


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
        if eff.damage:
            # 按动作目标分流：打脸别落到随从身上，目标找不到也别把伤害丢了
            u = (_find_unit(nb, side, action.target)
                 if (action.target or "").startswith("enemy:") else None)
            if u is not None:
                nb.damage_unit(foe, u, eff.damage)
            else:
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
        tripled = len(me.cthun_used) >= 4   # 口径：任何来源用过的技能都计入
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
        # 场上有玛维这类光环：之后每个打出的随从都 +N/+N，但要休眠一回合
        _wb = 0
        for _u2 in me.board:
            _n = warden_buff(_u2, cards)
            if _n > _wb:
                _wb = _n
        if _wb:
            u.atk += _wb
            u.hp += _wb
            u.max_hp += _wb
            u.dormant = True
            u.attacks_left = 0
            nb.note(f"{u.name} 获得 +{_wb}/+{_wb}，休眠一回合")
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
        # 费用修改作用于后续回合，Board 只模拟本回合：记下来，不凭空加费
        if eff.cost_delta_self:
            nb.note(f"{card.name}：我方手牌费用 {eff.cost_delta_self:+d}")
        if eff.cost_delta_opp:
            nb.note(f"{card.name}：敌方手牌费用 {eff.cost_delta_opp:+d}")
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

def apply_cthun_skill(board: Board, side: int, name: str, tripled: bool = False) -> str:
    """结算一项克苏恩技能。返回一句说明（用于建议理由）。"""
    me, foe = board.sides[side], board.sides[1 - side]
    mult = CTHUN_MULTIPLIER if tripled else 1

    if name == "克苏恩的愤怒":
        total = 6 * mult
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
            u.hp += 2 * mult
        return f"领域：{len(buffers)} 个随从获得 +{2*mult}/+{2*mult}"

    if name == "克苏恩的仆从":
        # 卡牌文本「两个 2/2」→「两个 8/8」：四倍放大的只有身材，个数一直是 2
        count, stat = 2, 2 * mult
        for _ in range(count):
            if len([u for u in me.board if u.alive]) >= 7:
                break
            me.board.append(Unit(card_id="", name="狂热者", atk=stat, hp=stat,
                                 taunt=True, attacks_left=0))
        return f"仆从：召唤 {count} 个 {stat}/{stat} 嘲讽"

    return "未知技能"
