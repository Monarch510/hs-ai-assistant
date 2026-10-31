"""把「真实日志状态 + 预判 + 建议」组装成一份给界面用的快照。

这一层是纯胶水，但它决定了整个工具好不好用：界面只读一个 JSON，
所有判断都带理由和置信度，缺数据时降级而不是报错。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import mulligan, policy
from collections import Counter

from . import decks as decks_mod
from . import review as review_mod

from .cards import DATA_DIR, Card, CardDB
from .infer import DeckPredictor, Prediction
from .meta import META_PATH, MetaDB
from .model import Board, SideState, Unit
from .state import GameState

OBSERVATIONS_PATH = DATA_DIR / "observations.jsonl"
WEIGHTS_PATH = DATA_DIR / "policy_weights.json"
MULLIGAN_WEIGHTS_PATH = DATA_DIR / "mulligan_weights.json"
OUR_DECK_PATH = DATA_DIR / "our_deck.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


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
        windfury=bool(card and card.has("WINDFURY")),
        lifesteal=bool(card and card.has("LIFESTEAL")),
        poisonous=bool(card and card.has("POISONOUS")),
        spell_damage=0,
        attacks_left=1 if view.get("can_attack") else 0,
    )


UNKNOWN = Card(id="__unknown__", name="未知牌", cost=99)


def _unknown_cards(n: int) -> list[Card]:
    """占位牌：只为「张数」和「摸牌不会疲劳」服务，费用 99 保证永远不会被当成可出的牌。"""
    return [UNKNOWN for _ in range(max(0, n))]


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
        me.hand = [c for c in (st.cards.get(v.get("id") or "") for v in ours.hand) if c is not None]
        me.board = [_unit_from_view(v, st.cards) for v in ours.board]
        them.board = [_unit_from_view(v, st.cards) for v in opp.board]
    # 牌库内容我们看不到，但「还剩几张」影响摸牌与疲劳，用占位牌补齐
    me.deck = _unknown_cards(ours.deck_count or max(0, 30 - len(me.hand) - len(me.board)))
    them.deck = _unknown_cards(opp.deck_count or max(0, 30 - opp.hand_count - len(them.board)))
    them.hand = _unknown_cards(opp.hand_count)
    if ours.weapon:
        me.weapon = Unit(card_id=ours.weapon.get("id", ""), name=ours.weapon.get("name", "武器"),
                         atk=max(0, ours.weapon.get("atk") or 0), hp=1, max_hp=1)
    if opp.weapon:
        them.weapon = Unit(card_id=opp.weapon.get("id", ""), name=opp.weapon.get("name", "武器"),
                           atk=max(0, opp.weapon.get("atk") or 0), hp=1, max_hp=1)
    board = Board([me, them], turn=max(1, st.turn), active=0 if st.turn_owner == st.our_id else 1,
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

        snap: dict[str, Any] = {
            "type": "snapshot",
            "ts": datetime.now().strftime("%H:%M:%S"),
            "phase": st.phase,
            "turn": st.turn,
            "is_our_turn": st.turn_owner == st.our_id,
            "turn_known": st.turn_owner in (1, 2),
            "format": "标准",
            "our": ours.as_dict(),
            "opp": opp.as_dict(),
            "prediction": self._prediction_dict(pred),
            "log": list(st.log)[-6:],
            "feed": list(st.feed)[-10:],
        }

        # 有些客户端在留牌阶段不给我方手牌的 cardId，等它揭示出来时已经进入对局了。
        # 所以兜底：第一回合开始、还没出牌、手牌 >=3 张时，补一次留牌建议。
        need_fallback = (
            st.phase == "playing"
            and st.turn <= 2
            and not self._mulligan_shown
            and len(ours.hand) >= 3
        )
        if (st.phase == "mulligan" or need_fallback) and ours.hand:
            hand = [c for c in (self.cards.get(v.get("id") or "") for v in ours.hand) if c]
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
        if st.game_over and not self._recorded:
            won = st.winner == st.our_id
            self._record_result(st, pred)
            try:
                self._finish_game(st, won)
            except Exception:
                pass
            self._recorded = True
        if not st.game_over and self._recorded:
            # 新对局开始了：必须把「这一局只记一次」的开关重置，
            # 否则同一个进程里第二局之后都不会再记录/复盘/学习。
            self._recorded = False
            self._mulligan_shown = False
            self._turn_rec = None
            self._game_turns = []
            self.last_findings = []
        if st.game_over:
            # 每一帧都给，别只在第一次 game_over 时给（否则窗口二次渲染就丢了）
            snap["review"] = list(getattr(self, "last_findings", []))
        return snap

    # ------------------------------------------------------------ 预判
    def _predict(self, st: GameState, opp) -> Prediction:
        observed = [(cid, st.revealed_turns.get(cid)) for cid in st.revealed_opp_order if cid]
        cls = opp.class_en or "UNKNOWN"
        return self.predictor.predict(
            cls,
            observed,
            turn=st.turn,
            hand_count=opp.hand_count,
            deck_count=opp.deck_count if opp.deck_count else max(1, 30 - st.turn),
            opp_mana=opp.max_mana,
        )

    def _prediction_dict(self, pred: Prediction) -> dict[str, Any]:
        d = pred.as_dict()
        return d

    # ------------------------------------------------------------ 建议
    def _advice(self, st: GameState, pred: Prediction) -> dict[str, Any]:
        if not self.cards:
            return {"lethal": False, "lethal_damage": 0, "lines": []}
        board = board_from_state(st)
        turn_owner_ours = st.turn_owner == st.our_id
        if not turn_owner_ours:
            # 对方回合：不猜动作，改为「记录他刚打了什么 + 预判变化 + 下回合威胁」
            danger = policy.max_face_damage(board, 1, self.cards)
            seen = [self.cards.display_name(c) for c in st.revealed_opp_order]
            recent = seen[-2:]
            lines = [{
                "rank": 1,
                "title": f"对手回合 · 已记录 {len(seen)} 张牌",
                "score": 50,
                "reason": (
                    "刚打出：" + "、".join(recent) + "。" if recent else "还没看到他出牌。"
                ) + f" 预判：{pred.name}" + (f" {pred.confidence:.0%}" if pred.confidence > 0 else ""),
                "actions": [],
            }]
            lines.append({
                "rank": 2,
                "title": f"场面最多能打 {danger['damage']} 点",
                "score": 40,
                "reason": (
                    f"你还有 {board.sides[0].hero_hp + board.sides[0].armor} 点血；"
                    + ("已经进斩杀线，注意留解或架墙" if danger["damage"] >= board.sides[0].hero_hp + board.sides[0].armor else "暂时安全，想好下回合怎么打")
                ),
                "actions": [],
            })
            return {"lethal": False, "lethal_damage": danger["damage"], "lines": lines}
        result = policy.plan_turn(
            board,
            self.cards,
            aoe_risk=pred.aoe_risk,
            weights=self.policy_weights,
            side=0,
        )
        lethal = result["lethal"]
        return {
            "lethal": bool(lethal["lethal"]),
            "lethal_damage": int(lethal["damage"]),
            "lines": policy.advice_lines(result),
        }

    def _ensure_deck(self, st: GameState) -> None:
        """没有手动登记卡组时，从 Decks.log + 已经看到的牌自动认出这局用的是哪套。"""
        if self._deck_manual or self._auto_deck is not None:
            return
        if not self.saved_decks:
            return
        seen = list(st.our_hand_ids) + list(st.our_played) + list(st.our_deck_ids)
        our_class = st.sides()[0].class_en or ""
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
        if st.turn_owner != st.our_id:
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
        self.last_findings = review_mod.summarize(rec.as_dict())
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
