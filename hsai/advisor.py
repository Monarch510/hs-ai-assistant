"""把「真实日志状态 + 预判 + 建议」组装成一份给界面用的快照。

这一层是纯胶水，但它决定了整个工具好不好用：界面只读一个 JSON，
所有判断都带理由和置信度，缺数据时降级而不是报错。
"""

from __future__ import annotations

import dataclasses

import re

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import mulligan, policy
from collections import Counter

from . import decks as decks_mod
from . import model as model_mod
from . import logutil
from . import review as review_mod

from .cards import DATA_DIR, Card, CardDB
from .infer import DeckPredictor, Prediction
from .meta import META_PATH, MetaDB
from .model import Board, SideState, Unit
from .paths import (
    LAST_REVIEW_JSON,
    MULLIGAN_WEIGHTS_JSON,
    OBSERVATIONS_JSONL,
    OUR_DECK_JSON,
    POLICY_WEIGHTS_JSON,
)
from .state import GameState

OBSERVATIONS_PATH = OBSERVATIONS_JSONL
WEIGHTS_PATH = POLICY_WEIGHTS_JSON
MULLIGAN_WEIGHTS_PATH = MULLIGAN_WEIGHTS_JSON
OUR_DECK_PATH = OUR_DECK_JSON


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _woke_this_turn(card, view: dict[str, Any]) -> bool:
    """休眠随从是不是本回合刚醒——刚醒的这回合不能攻击。"""
    text = (card.text or "") if card is not None else ""
    m = re.search(r"(\d+)\s*(?:或\s*\d+\s*)?回合后唤醒", text)
    played = view.get("played_turn")
    now = view.get("turn_now")
    if not m or played is None or not isinstance(now, int):
        return False
    return (now - int(played)) == int(m.group(1))


def _still_dormant(card, view: dict[str, Any]) -> bool:
    """还没到唤醒回合的休眠随从。

    卡面写「N回合后唤醒」，入场回合记为 T，那么到 T+N 才醒。
    """
    text = (card.text or "") if card is not None else ""
    # 文本不一定写「休眠」，但只要有「N回合后唤醒」就是休眠牌
    m = re.search(r"(\d+)\s*(?:或\s*\d+\s*)?回合后唤醒", text)
    if not m:
        return False
    played = view.get("played_turn")
    now = view.get("turn_now")
    if played is None or not isinstance(now, int):
        return bool(view.get("dormant"))
    return (now - int(played)) < int(m.group(1))


def _unit_from_view(view: dict[str, Any], cards: CardDB) -> Unit:
    card = cards.get(view.get("id") or "")
    keyword = view.get("keyword") or ""
    return Unit(
        card_id=view.get("id") or "",
        name=view.get("name") or "?",
        atk=max(0, int(view.get("atk") or 0)),
        hp=max(1, int(view.get("health") or 1)),
        max_hp=max(1, int(view.get("health") or 1)),
        taunt=bool(view.get("taunt")),
        divine=bool(view.get("divine_shield")),
        charge=bool(card and card.has("CHARGE")),
        rush=bool(card and card.has("RUSH")),
        # 突袭刚入场：这回合能打随从，不能打脸
        rush_only=bool(card and card.has("RUSH") and view.get("fresh")),
        dormant=bool(view.get("dormant")) or _still_dormant(card, view),
        windfury=bool(card and card.has("WINDFURY")),
        lifesteal=bool(card and card.has("LIFESTEAL")),
        poisonous=bool(card and card.has("POISONOUS")),
        spell_damage=0,
        attacks_left=(0 if (_woke_this_turn(card, view)
                            or (view.get("woke_turn") is not None
                                and view.get("woke_turn") == view.get("turn_now")))
                      else (1 if (view.get("attackable") or view.get("can_attack")) else 0)),
    )


UNKNOWN = Card(id="__unknown__", name="未知牌", cost=99)


def _unknown_cards(n: int) -> list[Card]:
    """占位牌：只为「张数」和「摸牌不会疲劳」服务，费用 99 保证永远不会被当成可出的牌。"""
    return [UNKNOWN for _ in range(max(0, n))]


def _log_err(msg: str) -> None:
    """把复盘/记录阶段的异常写进 data/hsai.log，别再静默吞掉。"""
    logutil.log(msg)


_PASSIVE_RULE: dict = {}


def board_from_state(st: GameState) -> Board:
    """把日志状态翻译成可以模拟的局面模型。"""
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
    # 武器攻击：装备了武器、且英雄本回合还没攻击过，才算可用
    if ours.weapon:
        w = ours.weapon
        me.weapon = Unit(card_id=w.get("id") or "", name=w.get("name") or "武器",
                         atk=max(0, int(w.get("atk") or 0)),
                         hp=int(w.get("durability") or w.get("health") or 1))
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
        played = [st.cards.get(cid) for cid in (st.our_played or [])]
        me.minions_played = sum(1 for c in played
                                if c is not None and getattr(c, "type", "") == "MINION")

    # 牌库内容我们看不到，但「还剩几张」影响摸牌与疲劳，用占位牌补齐
    me.deck = _unknown_cards(ours.deck_count or max(0, 30 - len(me.hand) - len(me.board)))
    them.deck = _unknown_cards(opp.deck_count or max(0, 30 - opp.hand_count - len(them.board)))
    them.hand = _unknown_cards(opp.hand_count)
    hp_view = ours.hero_power or {}
    if hp_view.get("id") and st.cards:
        me.hero_power = st.cards.get(hp_view["id"])
        me.hero_power_used = bool(hp_view.get("used"))
    if ours.weapon:
        me.weapon = Unit(card_id=ours.weapon.get("id", ""), name=ours.weapon.get("name", "武器"),
                         atk=max(0, ours.weapon.get("atk") or 0), hp=1, max_hp=1)
    if opp.weapon:
        them.weapon = Unit(card_id=opp.weapon.get("id", ""), name=opp.weapon.get("name", "武器"),
                           atk=max(0, opp.weapon.get("atk") or 0), hp=1, max_hp=1)
    board = Board([me, them], turn=max(1, st.turn), active=0 if st.effective_turn_owner() == st.our_id else 1,
                  cards=st.cards)
    return board


@dataclass
class Collected:
    """一次快照过程中顺带记下来的东西，用于自学习。"""

    prediction: Prediction | None = None
    our_hand: list[str] = None  # type: ignore[assignment]
    our_class: str = ""
    opp_class: str = ""

    def __post_init__(self) -> None:
        if self.our_hand is None:
            self.our_hand = []


class Advisor:
    def __init__(self, cards: CardDB | None = None, *, meta_path: Path = META_PATH) -> None:
        self.cards = cards if cards is not None else CardDB.load()
        self.meta = MetaDB.load(self.cards, meta_path)
        self.predictor = DeckPredictor(self.meta, self.cards)
        self.policy_weights = _load_json(WEIGHTS_PATH).get("weights", {})
        self.mulligan_weights = _load_json(MULLIGAN_WEIGHTS_PATH).get("weights", {})
        deck = _load_json(OUR_DECK_PATH)
        self.our_deck: list[str] = deck.get("cards", [])
        self.saved_decks = decks_mod.load(self.cards)
        self._auto_deck: decks_mod.SavedDeck | None = None
        self._deck_manual = bool(self.our_deck)
        self.our_deck_name: str = deck.get("name", "")
        self.our_class_cn: str = deck.get("class_cn", "") or deck.get("class", "")
        self.our_tags: list[str] = deck.get("tags", [])
        self.our_archetype: str = deck.get("archetype", "")
        self.last_collected: Collected | None = None
        self._recorded = False
        self._mulligan_shown = False
        self._turn_rec: review_mod.TurnRecord | None = None
        self._game_turns: list[dict] = []
        self.learned_count = 0
        self.last_findings: list[str] = []
        self._issues: list[str] = []
        self._unknown_turn_seen: set[int] = set()

    # ------------------------------------------------------------ 主入口
    def snapshot(self, st: GameState) -> dict[str, Any]:
        ours, opp = st.sides()
        collected = Collected(
            our_class=ours.class_en or "",
            opp_class=opp.class_en or "",
            our_hand=list(st.our_hand_ids),
        )
        self.last_collected = collected

        pred = self._predict(st, opp)
        collected.prediction = pred

        # 牌库张数：日志里没有「剩余牌库」字段，只能 卡组张数 - 已抽到。
        # 卡组张数优先用登记过的（40 张卡组也能算对）。
        # 只在这里算一次，不放进快照 —— 界面和 AI 都只关心算好的 deck_count。
        deck_total = len(self.our_deck) or 30

        snap: dict[str, Any] = {
            "type": "snapshot",
            "ts": datetime.now().strftime("%H:%M:%S"),
            "phase": st.phase,
            "turn": st.turn,
            "is_our_turn": st.effective_turn_owner() == st.our_id,
            "turn_known": st.effective_turn_owner() in (1, 2),
            "our": ours.as_dict(),
            "opp": opp.as_dict(),
            "prediction": self._prediction_dict(pred),
            "feed": list(st.feed)[-10:],
            "choice": self._choice_view(st),
            "cthun": st.cthun_view(),
            "unmodelled": list(getattr(st, "unmodelled", [])),
            "opp_played": [st._name(c) for c in list(st.revealed_opp_order)[-12:] if c],
        }

        if snap.get("our"):
            drawn = int(getattr(st, "our_drawn", 0) or 0)
            snap["our"]["deck_count"] = max(0, deck_total - drawn)

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
            snap["mulligan"] = {
                "advice": mulligan.advise(
                    hand,
                    cards=self.cards,
                    prediction=pred,
                    our_tags=self.our_tags,
                    is_first=st.our_id == 1,
                    weights=self.mulligan_weights,
                    opp_class=opp.class_en or "",
                )
            }
            self._mulligan_shown = True

        self._ensure_deck(st)
        snap["deck"] = self._deck_context(st)
        if st.phase in ("playing", "game_over"):
            snap["advice"] = self._advice(st, pred)
        else:
            snap["advice"] = {"lethal": False, "lethal_damage": 0, "lines": []}

        snap["advice"]["defense"] = self._defense(st, pred, snap)
        snap["warning"] = self._warning(st, pred, snap["advice"])
        self._track_turn(st, snap['advice'])
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
            self._game_turns = []
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

    def _collect_issues(self, st: GameState, snap: dict[str, Any]) -> None:
        """记下本局「识别失败」的地方，结束时会告诉用户。"""
        if (not snap.get("turn_known")) and st.turn >= 1 and st.turn not in self._unknown_turn_seen:
            self._unknown_turn_seen.add(st.turn)
            self._issues.append(f"第 {st.turn} 回合：没能判定该谁行动")
        for name in getattr(st, "unmodelled", [])[:5]:
            item = f"卡牌效果未建模：{name}"
            if item not in self._issues:
                self._issues.append(item)


    # ------------------------------------------------------------ 预判
    _last_pred_name: str = ""
    _passive_rule: dict = {}

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
            deck_count=opp.deck_count if opp.deck_count else max(1, 30 - st.turn),
            opp_mana=opp.max_mana,
        )
        self._last_pred_name = pred.name or ""
        return pred

    def _prediction_dict(self, pred: Prediction) -> dict[str, Any]:
        d = pred.as_dict()
        return d

    # ------------------------------------------------------------ 建议
    def _advice(self, st: GameState, pred: Prediction) -> dict[str, Any]:
        if not self.cards:
            return {"lethal": False, "lethal_damage": 0, "lines": []}
        board = board_from_state(st)
        # 用推断出来的回合归属：turn_owner 在日志里常常是 None，
        # 直接比较会让引擎永远以为「对手回合」，从而不给任何出牌建议
        turn_owner_ours = st.effective_turn_owner() == st.our_id
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

    # ------------------------------------------------------------ 回合追踪（复盘用）
    def _track_turn(self, st: GameState, advice: dict[str, Any]) -> None:
        if st.phase != "playing":
            return
        if st.effective_turn_owner() != st.our_id:
            return
        road = (advice.get("lines") or [{}])[0].get("title") or ""
        if self._turn_rec is None or self._turn_rec.turn != st.turn:
            if self._turn_rec is not None and not self._turn_rec.killed:
                self._game_turns.append(asdict(self._turn_rec))
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
        try:
            from . import memory as _memory
            _memory.record_opponent(opp.class_en or "", self._last_pred_name or "")
        except Exception:
            pass
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

    # ------------------------------------------------------------ 自学习落盘
    def _record_result(self, st: GameState, pred: Prediction) -> None:
        ours, opp = st.sides()
        won = st.winner == st.our_id
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "our_class": ours.class_en,
            "opp_class": opp.class_en,
            "opp_archetype": pred.archetype_id,
            "confidence": round(pred.confidence, 3),
            "result": "win" if won else "loss",
            "revealed": list(st.revealed_opp_order),
            "our_hand": list(st.our_hand_ids),
        }
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with OBSERVATIONS_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            return
        if pred.archetype_id:
            self.meta.record_result(pred.archetype_id, won)
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
