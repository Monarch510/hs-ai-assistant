"""把「真实日志状态 + 预判 + 建议」组装成一份给界面用的快照。

这一层是纯胶水，但它决定了整个工具好不好用：界面只读一个 JSON，
所有判断都带理由和置信度，缺数据时降级而不是报错。
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from . import cards as cards_mod
from . import decks as decks_mod
from . import model as model_mod
from . import mulligan, policy
from . import review as review_mod
from .cards import DATA_DIR, CardDB
from .infer import DeckPredictor, Prediction, RetroTruth
from .meta import MetaDB
from .model import Board, SideState, Unit
from .paths import (
    LAST_REVIEW_JSON,
    META_DIR,
    MULLIGAN_WEIGHTS_JSON,
    OBSERVATIONS_JSONL,
    OUR_DECK_JSON,
    POLICY_WEIGHTS_JSON,
    TAG_WEIGHTS_JSON,
)
from .state import GameState
from .tag_weights import Situation, explain, tag_weights

# 路径别名。被测试 mock.patch 的是 TAG_WEIGHTS_JSON / LAST_REVIEW_JSON / DATA_DIR /
# META_DIR（在 __init__ 与 _finish_game 里读），所以它们必须物理留在这个模块。
OBSERVATIONS_PATH = OBSERVATIONS_JSONL
WEIGHTS_PATH = POLICY_WEIGHTS_JSON
MULLIGAN_WEIGHTS_PATH = MULLIGAN_WEIGHTS_JSON
OUR_DECK_PATH = OUR_DECK_JSON

from .advisor_board_helpers import (  # noqa: E402
    UNKNOWN,
    _attacks_left,
    _log_err,
    _still_dormant,
    _unit_from_view,
    _unknown_cards,
    _woke_this_turn,
)
from .advisor_config import _load_json, _positive_scale  # noqa: E402

_PASSIVE_RULE: dict = {}

def board_from_state(st: GameState) -> Board:
    ours, opp = st.sides()
    me = SideState(
        hero_hp=ours.health,
        armor=ours.armor,
        mana=ours.mana,
        max_mana=ours.max_mana,
        hero_class=ours.class_en or "NEUTRAL",
    )
    them = SideState(
        hero_hp=opp.health,
        armor=opp.armor,
        mana=opp.mana,
        max_mana=opp.max_mana,
        hero_class=opp.class_en or "NEUTRAL",
    )
    if st.cards:
        model_mod.set_card_db(st.cards)
        me.hand = []
        for v in ours.hand:
            card = st.cards.get(v.get("id") or "")
            if card is None:
                continue
            live = v.get("cost")
            if isinstance(live, int) and live != card.cost:
                card = dataclasses.replace(card, cost=live)   # 减费后的实付价
            me.hand.append(card)
        me.board = [_unit_from_view(v, st.cards) for v in ours.board]
        them.board = [_unit_from_view(v, st.cards) for v in opp.board]
    # 武器只在这里建一次：再建一遍会把耐久覆盖成 1，风怒也会丢
    if ours.weapon:
        w = ours.weapon
        wcard = st.cards.get(w.get("id") or "") if st.cards else None
        dur = max(1, int(w.get("durability") or w.get("health") or 1))
        me.weapon = Unit(card_id=w.get("id") or "", name=w.get("name") or "武器",
                         atk=max(0, int(w.get("atk") or 0)),
                         hp=dur, max_hp=dur,
                         windfury=bool(wcard is not None and wcard.has("WINDFURY")))
    if me.weapon is not None:
        hero = st.hero(st.our_id)
        can_hit = hero is not None and st.can_attack(hero)
        windfury_hero = "风怒" in str((ours.weapon or {}).get("keyword") or "")
        me.weapon_attacks_left = (2 if windfury_hero else 1) if can_hit else 0
    # 帝国之眼在场：每回合 2 次技能，已用过的不能重复（进度来自日志）
    if any("帝国之眼" in (u.name or "") for u in me.board):
        me.cthun_uses_left = 2
        me.cthun_used = set(getattr(st, "cthun_used", set()) or set())

    # 被动英雄技能：记录计数器进度（例如「每使用五张随从牌」）
    hp_card = st.cards.get((ours.hero_power or {}).get("id") or "") if ours.hero_power else None
    if hp_card is None and st.cards is not None:
        # 视图缺失时从实体里找。注意：客户端会把被动技能挂到错误的一方
        # （实测萨满的「兹伊的力量」挂在了法师对手身上），所以按「职业归属」认，
        # 不按 controller。
        our_class = (ours.class_en or "").upper()
        same_class = []
        mine = []
        for _e in st.entities.values():
            _c = st.cards.get(getattr(_e, "card_id", "") or "")
            if _c is None or getattr(_c, "type", "") != "HERO_POWER":
                continue
            if our_class and (getattr(_c, "card_class", "") or "").upper() != our_class:
                continue
            same_class.append(_c)
            if getattr(_e, "controller", None) == st.our_id:
                mine.append(_c)
        # 同职业对战时无法靠职业区分，只能退回 controller
        if len(same_class) == 1:
            hp_card = same_class[0]
        elif mine:
            hp_card = mine[0]
    # 英雄技能视图有时会缺失（用掉/被替换），一旦认出规则就缓存住，别每帧重算
    if hp_card is not None and not _PASSIVE_RULE:
        _PASSIVE_RULE.update(policy.passive_rule(hp_card))
    rule = _PASSIVE_RULE
    if rule:
        me.passive_rule = rule
        # 只数「从手牌打出的随从」，召唤物不算（和面板上的计数器同源）
        me.minions_played = int(getattr(st, "our_minions_played", 0) or 0)

    # 牌库内容我们看不到，但「还剩几张」影响摸牌与疲劳，用占位牌补齐。
    # 0 张（疲劳期）是合法值，不能拿 `or` 当「没数据」；只有 None 才回退到卡组张数。
    me.deck = _unknown_cards(ours.deck_count if ours.deck_count is not None
                             else st.deck_size)
    them.deck = _unknown_cards(opp.deck_count if opp.deck_count is not None
                               else st.deck_size)
    them.hand = _unknown_cards(opp.hand_count)
    hp_view = ours.hero_power or {}
    _hero = st.hero(st.our_id) if hasattr(st, "hero") else None
    if _hero is not None:
        # 英雄自己也可能有攻击力（没武器但被加过攻），日志里 ATK 标签就是它
        try:
            me.hero_atk = int(_hero.tags.get("ATK") or 0)
        except (TypeError, ValueError):
            me.hero_atk = 0
        # 风怒武器/风怒光环下英雄能攻击两次：按「总次数 − 已攻击」算
        _wind = bool(_hero.tags.get("WINDFURY")) or bool(
            (ours.weapon or {}).get("id")
            and st.cards.get((ours.weapon or {}).get("id"))
            and st.cards.get((ours.weapon or {}).get("id")).has("WINDFURY"))
        _total = 2 if _wind else 1
        _used = int(_hero.tags.get("NUM_ATTACKS_THIS_TURN") or 0)
        me.hero_attacks_left = max(0, _total - _used)

    if hp_view.get("id") and st.cards:
        me.hero_power = st.cards.get(hp_view["id"])
        # shortcut: 日志里没有可靠的「英雄技能已用」标记，这里恒 False，等拿到日志样本再接（见 00-现状 §3.5）
        me.hero_power_used = bool(hp_view.get("used"))
    if opp.weapon:
        them.weapon = Unit(card_id=opp.weapon.get("id", ""), name=opp.weapon.get("name", "武器"),
                           atk=max(0, opp.weapon.get("atk") or 0), hp=1, max_hp=1)
    board = Board([me, them], turn=max(1, st.turn), active=0 if st.effective_turn_owner() == st.our_id else 1,
                  cards=st.cards)
    return board

class Advisor:
    def __init__(self, cards: CardDB | None = None, *, meta_path: Path = META_DIR) -> None:
        self.cards = cards if cards is not None else cards_mod.load()
        self.meta = MetaDB.load(self.cards, meta_path)
        self.predictor = DeckPredictor(self.meta, self.cards)
        self.policy_weights = _load_json(WEIGHTS_PATH).get("weights", {})
        self.mulligan_weights = _load_json(MULLIGAN_WEIGHTS_PATH).get("weights", {})
        # 动态标签权重的覆盖配置：手写文件，坏形状（weight 不是 dict）跟坏 JSON 一样按没配处理
        tag_cfg = _load_json(TAG_WEIGHTS_JSON)
        raw_tag_weights = tag_cfg.get("weights")
        self.tag_weight_overrides: dict[str, float] = (
            raw_tag_weights if isinstance(raw_tag_weights, dict) else {})
        # 缩放系数同样来自这份配置；坏值回退到各模块自己的常量
        self.tag_mulligan_scale = _positive_scale(
            tag_cfg.get("mulligan_scale"), mulligan.MULLIGAN_SCALE)
        self.tag_play_scale = _positive_scale(
            tag_cfg.get("play_scale"), policy.TAG_WEIGHT_SCALE)
        deck = _load_json(OUR_DECK_PATH)
        self.our_deck: list[str] = deck.get("cards", [])
        self.saved_decks = decks_mod.load(self.cards)
        self._auto_deck: decks_mod.SavedDeck | None = None
        self._deck_manual = bool(self.our_deck)
        self.our_deck_name: str = deck.get("name", "")
        self.our_class_cn: str = deck.get("class_cn", "") or deck.get("class", "")
        self.our_tags: list[str] = deck.get("tags", [])
        self.our_archetype: str = deck.get("archetype", "")
        self._recorded = False
        self._mulligan_shown = False
        self._turn_rec: review_mod.TurnRecord | None = None
        self._last_advice: dict[str, Any] | None = None
        self._game_turns: list[dict] = []
        # 每回合的预测轨迹。局末只剩一个终局置信度，而它天然接近 1、
        # 没有信息量；要判断「70% 置信度时实际对了多少次」只能靠这条轨迹。
        self._trace: list[dict] = []
        self.learned_count = 0
        self.last_findings: list[str] = []
        self._issues: list[str] = []
        self._unknown_turn_seen: set[int] = set()

    def snapshot(self, st: GameState) -> dict[str, Any]:
        # 卡组张数优先用登记过的（40 张卡组也能算对）。同步给状态层：
        # 抽牌计数的物理上限和这里的牌库反推必须是同一个数。
        deck_total = len(self.our_deck) or 30
        st.deck_size = deck_total
        ours, opp = st.sides()
        pred = self._predict(st, opp)

        # 动态标签权重（DYNAMIC-WEIGHTS.md §3）：先按当前局势算一份，留牌/出牌各取所需。
        # 结算/未开局阶段没有「本回合」可言，按留牌口径（只看职业对位）降级。
        board = board_from_state(st)
        own_phase = "mulligan" if st.phase == "mulligan" else "playing"
        situation = self._situation(st, pred, board, phase=own_phase)
        tag_weight_table = tag_weights(situation, self.tag_weight_overrides)

        # 牌库张数：state 层按初始牌库实体计数给出真实读数，这里不再重算。
        # AI 解释层（build_prompt）一直读 snap["quest"]，快照却从没写过它，
        # 任务信息因此进不了复盘。取我方仍在 SECRET 区的第一个任务；
        # 进度读不到就别塞 None，让展示层降级成「?」。
        our_view = ours.as_dict()
        # 日志没有可靠的「技能已用」信号，取引擎同源的布尔降级值，别让读取方拿到 None
        our_view["hero_power_used"] = board.sides[0].hero_power_used
        quest_now = (our_view.get("quests") or [{}])[0]
        quest = {"name": quest_now.get("name") or ""}
        if quest_now.get("progress") is not None:
            quest["progress"] = quest_now["progress"]
        if quest_now.get("total") is not None:
            quest["total"] = quest_now["total"]
        snap: dict[str, Any] = {
            "type": "snapshot",
            "ts": datetime.now().strftime("%H:%M:%S"),
            "phase": st.phase,
            "turn": st.turn,
            "is_our_turn": st.effective_turn_owner() == st.our_id,
            "turn_known": st.effective_turn_owner() in (1, 2),
            # 结束回合阶段比回合归属切换早到：界面要立刻撤掉上一回合的建议
            "end_turn_seq": st.end_turn_seq,
            "turn_ending": st.turn_ending and st.phase == "playing",
            "our": our_view,
            "opp": opp.as_dict(),
            "quest": quest,
            "prediction": self._prediction_dict(pred),
            "feed": list(st.feed)[-10:],
            "choice": self._choice_view(st),
            "cthun": st.cthun_view(),
            # 这一回合我方实际做了什么（出牌 / 攻击），局后对照建议用
            "actions": list(getattr(st, "turn_actions", {}).get(st.turn, [])),
            # 被动英雄技能的计数器（如「每使用五张随从牌」）
            "passive": {"name": (ours.hero_power or {}).get("name") or "",
                        "played": int(getattr(st, "our_minions_played", 0) or 0),
                        "every": int((policy.passive_rule(
                            self.cards.get((ours.hero_power or {}).get("id") or "")) or {}).get("every") or 0)},

            "unmodelled": list(getattr(st, "unmodelled", [])),
            "opp_played": [st._name(c) for c in list(st.revealed_opp_order)[-12:] if c],
        }

        if snap.get("our"):
            # deck_count 已是实体计数的真实读数（起手 3/4 张已计入），
            # 不再用「卡组张数 − 已抽到」覆盖它
            snap["our"]["deck_count"] = ours.deck_count

        # 有些客户端在留牌阶段不给我方手牌的 cardId，等它揭示出来时已经进入对局了。
        # 所以兜底：第一回合开始、还没出牌、手牌 >=3 张时，补一次留牌建议。
        need_fallback = (
            st.phase == "playing"
            and st.turn <= 2
            and not self._mulligan_shown
            and len(ours.hand) >= 3
        )
        if (st.phase == "mulligan" or need_fallback) and ours.hand:
            hand = [c for c in (self.cards.get(v.get("id") or "") for v in ours.hand)
                    if c and not self.cards.is_token(c.id) and c.name != "幸运币"]
            # 起手没场面，留牌阶段要忽略局面信号；对局内补的那次留牌同理。
            mulligan_situation = situation if own_phase == "mulligan" else dataclasses.replace(
                situation, phase="mulligan")
            snap["mulligan"] = {
                "advice": mulligan.advise(
                    hand,
                    cards=self.cards,
                    prediction=pred,
                    our_tags=self.our_tags,
                    is_first=st.our_id == 1,
                    weights=self.mulligan_weights,
                    opp_class=opp.class_en or "",
                    situation=mulligan_situation,
                    tag_weight_overrides=self.tag_weight_overrides,
                    tag_scale=self.tag_mulligan_scale,
                )
            }
            self._mulligan_shown = True

        self._ensure_deck(st)
        snap["deck"] = self._deck_context(st)
        if st.phase == "playing":
            if opp.health > 0:
                snap["advice"] = self._advice(
                    st, pred, board=board, tag_weights=tag_weight_table,
                    tag_scale=self.tag_play_scale)
                self._last_advice = snap["advice"]
            else:
                # 伤害已经结算完、PLAYSTATE 还没到的那一帧：对面已经死了，
                # 再算一遍只会把斩杀提示换成废话。
                snap["advice"] = self._last_advice or {
                    "lethal": False, "lethal_damage": 0, "lines": []}
        elif st.phase == "game_over":
            # 结算帧同理：留最后一帧的建议，窗口收起前看到的就是刚才那手。
            snap["advice"] = self._last_advice or {
                "lethal": False, "lethal_damage": 0, "lines": []}
        else:
            snap["advice"] = {"lethal": False, "lethal_damage": 0, "lines": []}

        # 能斩杀时权重要「斩杀置顶」；plan_turn 已先按斩杀路线算过，这里只为把
        # 同一套权重和解释暴露给界面，不改已经算出来的路线。
        if snap["advice"].get("lethal") and not situation.lethal:
            situation = dataclasses.replace(situation, lethal=True)
            tag_weight_table = tag_weights(situation, self.tag_weight_overrides)
        snap["tag_weights"] = {
            "weights": tag_weight_table,
            "reasons": explain(situation),
            "mulligan_scale": self.tag_mulligan_scale,
            "play_scale": self.tag_play_scale,
        }

        snap["advice"]["defense"] = self._defense(st, pred, snap)
        snap["warning"] = self._warning(st, pred, snap["advice"])
        self._track_turn(st, snap['advice'], pred)
        # 胜负单独给一个字段：AI 复盘写教训、案例入库都要用它。
        # 以前 ui 里靠 `"win" if (... and False)` 占位，导致这两处的 result 永远是空串。
        snap["result"] = ("win" if st.winner == st.our_id else "loss") if st.game_over else ""
        if st.game_over and not self._recorded:
            won = st.winner == st.our_id
            self._record_result(st, pred)
            try:
                self._finish_game(st, won)
            except Exception as exc:      # 之前这里静默吞掉，导致复盘不更新却毫无线索
                _log_err(f"finish_game 失败：{type(exc).__name__}: {exc}")
            self._recorded = True
        if not st.game_over and self._recorded:
            # 新对局开始了：必须把「这一局只记一次」的开关重置，
            # 否则同一个进程里第二局之后都不会再记录/复盘/学习。
            self._recorded = False
            self._mulligan_shown = False
            self._turn_rec = None
            self._last_advice = None
            self._game_turns = []
            self._trace = []
            self.last_findings = []
            self._issues = []
            self._unknown_turn_seen = set()
            _PASSIVE_RULE.clear()
        self._collect_issues(st, snap)
        if st.game_over:
            # 每一帧都给，别只在第一次 game_over 时给（否则窗口二次渲染就丢了）
            snap["review"] = list(getattr(self, "last_findings", []))
            snap["issues"] = list(self._issues)
        return snap

    def _situation(self, st: GameState, pred: Prediction, board: Board, *,
                   phase: str, lethal: bool = False) -> Situation:
        """当前局面 → 权重层的 Situation（DYNAMIC-WEIGHTS.md §2）。

        数字全部来自 board_from_state 的 SideState 和 st.sides() 的展示视图；
        读到什么就传什么，缺的字段留给 dataclass 默认值，不猜。
        """
        ours, opp = st.sides()
        me, them = board.sides[0], board.sides[1]
        style = frozenset(pred.candidates[0].tags) if pred.candidates else frozenset()
        return Situation(
            our_class=ours.class_en or "",
            opp_class=opp.class_en or "",
            our_archetype=self.our_deck_name or self.our_archetype,
            opp_archetype=pred.name,
            opp_style=style,
            our_tags=frozenset(self.our_tags),
            our_hp=me.hero_hp,
            our_armor=me.armor,
            opp_hp=them.hero_hp,
            opp_armor=them.armor,
            our_board_attack=me.board_attack,
            opp_board_attack=them.board_attack,
            our_board_count=len(me.board),
            opp_board_count=len(them.board),
            our_hand_count=len(me.hand),
            opp_hand_count=len(them.hand),
            our_mana=me.mana,
            turn=max(1, st.turn),
            aoe_risk=float(pred.aoe_risk or 0.0),
            removal_risk=float(pred.removal_risk or 0.0),
            lethal=lethal,
            phase=phase,
        )

    def _collect_issues(self, st: GameState, snap: dict[str, Any]) -> None:
        """记下本局「识别失败」的地方，结束时会告诉用户。"""
        if (not snap.get("turn_known")) and st.turn >= 1 and st.turn not in self._unknown_turn_seen:
            self._unknown_turn_seen.add(st.turn)
            self._issues.append(f"第 {st.turn} 回合：没能判定该谁行动")
        for name in getattr(st, "unmodelled", [])[:5]:
            item = f"卡牌效果未建模：{name}"
            if item not in self._issues:
                self._issues.append(item)


    def _predict(self, st: GameState, opp) -> Prediction:
        # 只看「卡组自带的牌」：衍生物（复活的食尸鬼、骷髅这类）不代表卡组形态
        observed = [(cid, st.revealed_turns.get(cid))
                    for cid in st.revealed_opp_order
                    if cid and not self.cards.is_token(cid)]
        cls = opp.class_en or "UNKNOWN"
        pred = self.predictor.predict(
            cls,
            observed,
            turn=st.turn,
            hand_count=opp.hand_count,
            deck_count=opp.deck_count if opp.deck_count is not None else max(1, st.deck_size - st.turn),
            opp_mana=opp.max_mana,
        )
        return pred

    def _prediction_dict(self, pred: Prediction) -> dict[str, Any]:
        d = pred.as_dict()
        return d

    def _advice(self, st: GameState, pred: Prediction, *, board: Board | None = None,
                tag_weights: dict[str, float] | None = None,
                tag_scale: float | None = None) -> dict[str, Any]:
        if not self.cards:
            return {"lethal": False, "lethal_damage": 0, "lines": []}
        board = board if board is not None else board_from_state(st)
        if tag_scale is None:
            tag_scale = self.tag_play_scale
        # 用推断出来的回合归属：turn_owner 在日志里常常是 None，
        # 直接比较会让引擎永远以为「对手回合」，从而不给任何出牌建议
        # 日志里的 TURN 标签最权威；拿不到时才用推断值。
        # 只要不是我们的回合，就一个字都不给。
        _owner = st.turn_owner if st.turn_owner in (1, 2) else st.effective_turn_owner()
        turn_owner_ours = _owner == st.our_id
        if not turn_owner_ours:
            # 对手回合不给出牌建议：他的场面马上会被改掉，这时候的建议没意义。
            # 「他刚打了什么」放到对手预判那块显示。
            return {"lethal": False, "lethal_damage": 0, "lines": []}

        result = policy.plan_turn(
            board,
            self.cards,
            aoe_risk=pred.aoe_risk,
            weights=self.policy_weights,
            side=0,
            tag_weights=tag_weights,
            tag_scale=tag_scale,
        )
        lethal = result["lethal"]
        lines = policy.advice_lines(result)

        # 复活类牌：手牌里有「复活」字样时，告诉最值得复活的是谁
        grave = [g for g in (st.sides()[0].graveyard or []) if (g.get("atk") or 0) + (g.get("hp") or 0) > 0]
        revive_cards = [c for c in board.sides[0].hand
                        if c is not None and "复活" in (c.text or "")]
        if grave and revive_cards:
            best = max(grave, key=lambda g: (g.get("cost") or 0) * 2
                       + (g.get("atk") or 0) + (g.get("hp") or 0))
            lines.insert(0, {
                "title": f"复活 {best.get('name')} 最划算",
                "detail": (f"墓地里 {len(grave)} 只可选，它 {best.get('atk')}/{best.get('hp')}，"
                           f"{best.get('cost')} 费"),
            })

        return {
            "lethal": bool(lethal["lethal"]),
            "lethal_damage": int(lethal["damage"]),
            "lines": lines,
        }

    def _ensure_deck(self, st: GameState) -> None:
        """没有手动登记卡组时，从 Decks.log + 已经看到的牌自动认出这局用的是哪套。"""
        if self._deck_manual or self._auto_deck is not None:
            return
        if not self.saved_decks:
            return
        our_class = st.sides()[0].class_en or ""
        # 优先用「点开始对战时客户端写下的那套牌」——这是直接读到的，不是猜的
        queued = decks_mod.load_queued(self.cards)
        if queued and (not our_class or queued[-1].class_en == our_class):
            self._auto_deck = queued[-1]
            return
        seen = list(st.our_hand_ids) + list(st.our_played) + list(st.our_deck_ids)
        deck, score = decks_mod.best_match(self.saved_decks, seen, our_class)
        if deck is None:
            return
        # 同职业只有一套牌：直接认；多套牌就要求「看到的牌有 60% 命中」
        if score >= 0.6 or len([d for d in self.saved_decks if d.class_en == our_class]) == 1:
            self._auto_deck = deck

    def _deck_context(self, st: GameState) -> dict[str, Any]:
        """我方卡组还剩什么 —— 建议要「知道自己有什么牌」才谈得上取舍。"""
        if not self.cards:
            return {}
        if self._auto_deck is not None and not self._deck_manual:
            total = Counter(self._auto_deck.cards)
            name = self._auto_deck.name
            class_cn = self._auto_deck.class_cn
            source = "auto"
        else:
            if not self.our_deck:
                return {}
            total = Counter(self.our_deck)
            name = self.our_deck_name or "我的卡组"
            class_cn = self.our_class_cn
            source = "manual"
        used = Counter(st.our_hand_ids) + Counter(st.our_played)
        remaining = total - used
        left = sum(max(0, n) for n in remaining.values())
        cards = []
        for cid, n in remaining.items():
            if n <= 0:
                continue
            c = self.cards.get(cid)
            if c is None:
                continue
            cards.append({"id": cid, "name": c.name, "cost": c.cost, "count": n})
        # 关键牌 = 剩下的高费牌（通常是赢点）
        cards.sort(key=lambda x: (-x["cost"], x["name"]))
        key = [c for c in cards if c["cost"] >= 5][:3] or cards[:2]
        return {
            "name": name,
            "class_cn": class_cn,
            "left": left,
            "key_cards": key,
            "source": source,
        }

    def _choice_view(self, st: GameState) -> dict[str, Any] | None:
        ch = getattr(st, "choice", None)
        if not ch or not ch.get("options"):
            return None
        try:
            st.note_cthun_choice([o.get("name") for o in ch["options"]])
        except Exception:
            pass
        opts = []
        for o in ch["options"][:6]:
            card = self.cards.get(o.get("id")) if o.get("id") else None
            raw = (card.text if card else "") or ""
            opts.append({"name": o.get("name") or (card.name if card else "?"),
                         "cost": card.cost if card else None,
                         "text": re.sub(r"<[^>]+>", "", raw).replace("$", "")})
        # 克苏恩技能的选择里会混进打出的那张源牌，只保留技能项
        skills = ("克苏恩的愤怒", "克苏恩的领域", "克苏恩的凝视", "克苏恩的仆从")
        only_skills = [o for o in opts if o.get("name") in skills]
        if len(only_skills) >= 2:
            opts = only_skills
        return {"type": ch.get("type"), "min": ch.get("min"), "max": ch.get("max"), "options": opts}

    def _defense(self, st: GameState, pred: Prediction, snap: dict[str, Any]) -> list[str]:
        """「怎么防对面的针对牌」：把威胁翻译成具体动作，而不是只报概率。"""
        out: list[str] = []
        aoe = float(getattr(pred, "aoe_risk", 0) or 0)
        rem = float(getattr(pred, "removal_risk", 0) or 0)
        board = (snap.get("our") or {}).get("board") or []
        big = [m for m in board if (m.get("atk") or 0) >= 5]
        if aoe >= 0.35 and len(board) >= 3:
            out.append(f"对面清场 {aoe:.0%}：别一次铺满，手里留 1-2 个随从")
        elif aoe >= 0.2:
            out.append(f"对面可能清场（{aoe:.0%}）：重要的随从别全压上")
        if rem >= 0.45 and big:
            out.append(f"对面单解 {rem:.0%}：{big[0].get('name')} 会被直接换掉，先上小的探路")
        elif rem >= 0.45:
            out.append(f"对面单解 {rem:.0%}：上大怪前先确认它换不换得掉")
        likely = getattr(pred, "likely_hand", None) or []
        if likely and float(likely[0].get("p") or 0) >= 0.4:
            out.append(f"提防 {likely[0].get('name')}（{float(likely[0]['p']):.0%}）")
        if not out:
            out.append("暂无明显针对牌风险，按正常节奏打")
        return out[:2]

    def _warning(self, st: GameState, pred: Prediction, advice: dict[str, Any]) -> str:
        bits: list[str] = []
        if advice.get("lethal"):
            bits.append("这回合可以斩杀，先走脸")
        ours, opp = st.sides()
        if pred.aoe_risk >= 0.45 and len(ours.board) >= 3:
            bits.append(f"对方约 {pred.aoe_risk:.0%} 概率持有清场，别把随从铺满")
        if ours.health + ours.armor <= 12 and len(opp.board) >= 2:
            bits.append("血量已进入斩杀线，优先换场面或上嘲讽")
        if st.phase == "mulligan" and pred.prior_only:
            bits.append("还没有对方的牌，留牌建议只按职业常见形态给")
        return "；".join(bits)

    def _track_turn(self, st: GameState, advice: dict[str, Any], pred: Prediction) -> None:
        if st.phase not in ("playing", "game_over"):
            return
        if st.effective_turn_owner() != st.our_id:
            return
        if st.phase == "game_over":
            # 击杀和 PLAYSTATE=WON 常在同一批日志里到达：这一帧已经是 game_over，
            # 只补 killed，不新建回合记录。不补的话「赢的那回合」会被复盘冤枉成漏斩。
            if self._turn_rec is not None:
                self._turn_rec.killed = self._turn_rec.killed or st.sides()[1].health <= 0
            return
        road = (advice.get("lines") or [{}])[0].get("title") or ""
        if self._turn_rec is None or self._turn_rec.turn != st.turn:
            if self._turn_rec is not None and not self._turn_rec.killed:
                self._game_turns.append(asdict(self._turn_rec))
            self._trace.append({
                "t": st.turn,
                "arch": pred.archetype_id,
                "conf": round(pred.confidence, 3),
            })
            ours, opp = st.sides()
            self._turn_rec = review_mod.TurnRecord(
                turn=st.turn,
                lethal_available=bool(advice.get("lethal")),
                lethal_damage=int(advice.get("lethal_damage") or 0),
                mana_left=ours.mana,
                playable=sum(1 for h in ours.hand if (h.get("cost") or 0) <= ours.mana),
                hand=ours.hand_count,
                our_hp=ours.health,
                opp_hp=opp.health,
                advice=road,
            )
        else:
            ours, opp = st.sides()
            self._turn_rec.mana_left = ours.mana
            self._turn_rec.playable = sum(1 for h in ours.hand if (h.get("cost") or 0) <= ours.mana)
            self._turn_rec.hand = ours.hand_count
            self._turn_rec.opp_hp = opp.health
            self._turn_rec.killed = self._turn_rec.killed or opp.health <= 0

    def _finish_game(self, st: GameState, won: bool) -> None:
        if self._turn_rec is not None:
            self._game_turns.append(asdict(self._turn_rec))
            self._turn_rec = None
        if not self._game_turns:
            return
        ours, opp = st.sides()
        rec = review_mod.GameRecord(
            ts=datetime.now().isoformat(timespec="seconds"),
            result="win" if won else "loss",
            our_class=ours.class_en,
            opp_class=opp.class_en,
            turns=self._game_turns,
        )
        review_mod.append_game(rec)
        self.last_findings = review_mod.summarize(rec.as_dict())
        try:
            import json as _json

            path = LAST_REVIEW_JSON
            path.write_text(
                _json.dumps(
                    {
                        "ts": rec.ts,
                        "result": rec.result,
                        "our_class": rec.our_class,
                        "opp_class": rec.opp_class,
                        "turns": rec.turns[-1]["turn"] if rec.turns else 0,
                        "findings": list(getattr(self, "last_findings", [])),
                        "issues": list(self._issues),
                        "unmodelled": list(getattr(st, "unmodelled", [])),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        self._game_turns = []

    def _record_result(self, st: GameState, pred: Prediction) -> None:
        opp = st.sides()[1]
        # 局末回看：用全部已见牌重新判一次「它到底是什么」。
        # 这份才是能拿来学习的标签，pred 只是当时的猜测。
        truth = self.predictor.retrospect(opp.class_en or "", st.revealed_opp_order)
        row = observation_row(st, pred, truth, self._trace)
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with OBSERVATIONS_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return
        self._trace = []
        # 记对手形态也要用真值：拿预测值记，等于把偏差固化成先验。
        # 这里存显示名 —— posteriors() 是按名字（含「职业·形态」的短名）匹配的。
        if truth.trusted:
            try:
                from . import memory as _memory

                _memory.record_opponent(opp.class_en or "", truth.name)
            except Exception:
                pass
        # 自动学习：样本够了就长出新原型，下一次遇到同样的牌就认得
        try:
            from . import learn as learn_mod

            added = learn_mod.absorb_observations(self.cards, self.meta, min_games=2)
            if added:
                self.predictor = DeckPredictor(self.meta, self.cards)
                self.learned_count += added
        except Exception:
            pass
        try:
            self.meta.save()
        except OSError:
            pass

def observation_row(
    st: GameState, pred: Prediction, truth: RetroTruth, trace: list[dict]
) -> dict[str, Any]:
    """一局结束后写进 observations.jsonl 的那一行。

    单独抽出来是为了能被测：schema 一旦漂移，学习端会静默读到空值，
    而不会有任何报错。每个字段为什么存在都写在下面的注释里。
    """
    ours, opp = st.sides()
    return {
        "schema": 2,
        "game_id": _new_game_id(),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "our_class": ours.class_en,
        "opp_class": opp.class_en,
        "result": "win" if st.winner == st.our_id else "loss",
        "turns": st.turn,
        # 对手还剩多少牌、手上有几张：学习端靠它区分
        # 「这张牌不在卡组」和「这张牌没抽到」，这是 P(c|A) 最大的偏差来源
        "opp_deck_left": opp.deck_count,
        "opp_hand": opp.hand_count,
        # 带回合：早期权重以后能拟合，而不是继续手设 1.35/1.0/0.85
        "revealed": [[cid, st.revealed_turns.get(cid, 0)]
                     for cid in st.revealed_opp_order],
        # 每回合的预测轨迹：终局置信度天然接近 1，算不出校准曲线
        "trace": list(trace),
        # 预测与真值分开存。以前只有一个 opp_archetype，存的是预测值，
        # 先验学习却拿它当真值 —— 预测 A 就记成 A，A 的先验因此更高，
        # 下次更爱预测 A。这条回路没有纠错力，只会放大偏差。
        "pred_archetype": pred.archetype_id,
        "pred_confidence": round(pred.confidence, 3),
        "truth": truth.as_dict() if truth.archetype_id else None,
        "unexpected_cards": list(truth.unexplained),
        "our_hand": list(st.our_hand_ids),
    }

def _new_game_id() -> str:
    """一局一个 id，用来把 observations / games.jsonl / cases.jsonl 串起来。

    以前三份数据没有任何公共键，复盘数据挂不到观察上，只能按时间猜。
    """
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]

def load_observations(path: Path = OBSERVATIONS_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows
