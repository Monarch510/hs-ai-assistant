"""把 Power.log 事件流归约成对局状态。

对外只暴露「怎么打牌的人会关心的东西」：双方英雄、手牌、场面、牌库、回合、阶段。

关于 tag 编号的一点说明
----------------------
客户端日志只给数字 tag，不给语义名。这里用的编号取自长期稳定的公开解析约定，
但为了不让单个编号出错就毁掉整个工具，每一处读取都有回退：
血量/攻击优先读 tag，读不到就用卡牌库基础值减去 DAMAGE；
法力优先读 RESOURCES，读不到就按回合数推算。这样即使某个编号变了，工具只会降级，不会崩。
"""

from __future__ import annotations

import re

from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from . import powerlog as pl
from .model import Effect, parse_effect
from .cards import CardDB

T_ZONE = "ZONE"
T_CONTROLLER = "CONTROLLER"
T_ATK = "ATK"
T_HEALTH = "HEALTH"
T_DAMAGE = "DAMAGE"
T_PLAYER_ID = "PLAYER_ID"
T_PLAYSTATE = "PLAYSTATE"
T_TURN = "TURN"
T_CURRENT_PLAYER = "CURRENT_PLAYER"
T_RESOURCES = "RESOURCES"
T_RESOURCES_USED = "RESOURCES_USED"
T_ARMOR = "ARMOR"
T_TAUNT = "TAUNT"
T_DIVINE_SHIELD = "DIVINE_SHIELD"
T_COST = "COST"
T_HERO_ENTITY = "HERO_ENTITY"
T_ATTACKS_THIS_TURN = "NUM_ATTACKS_THIS_TURN"

ZONE_PLAY, ZONE_DECK, ZONE_HAND, ZONE_GRAVE, ZONE_SECRET = 1, 2, 3, 4, 5


@dataclass
class Entity:
    id: int
    card_id: str | None = None
    controller: int | None = None
    zone: int = 0
    zone_pos: int | None = None
    tags: dict[int, Any] = field(default_factory=dict)

    @property
    def atk(self) -> int | None:
        return _int(self.tags.get(T_ATK))

    @property
    def health(self) -> int | None:
        return _int(self.tags.get(T_HEALTH))

    @property
    def damage(self) -> int:
        return _int(self.tags.get(T_DAMAGE)) or 0

    @property
    def armor(self) -> int:
        return _int(self.tags.get(T_ARMOR)) or 0

    @property
    def cost(self) -> int | None:
        return _int(self.tags.get(T_COST))

    @property
    def is_hero(self) -> bool:
        return (self.card_id or "").upper().startswith("HERO")


def _clean_text(raw: str) -> str:
    """去掉富文本标记：<b>、$（伤害）、#（治疗）、&（换行）、{0}（占位符）。"""
    t = re.sub(r"<[^>]+>", "", raw or "")
    t = t.replace("$", "").replace("#", "").replace("&", "")
    t = re.sub(r"\{\d+\}", "", t)
    return " ".join(t.split())


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) else None


@dataclass
class Side:
    """一方的展示视图。"""

    player_id: int
    class_cn: str = "未知"
    class_en: str = ""
    health: int = 30
    armor: int = 0
    mana: int = 0
    max_mana: int = 0
    fatigue: int = 0
    hand: list[dict[str, Any]] = field(default_factory=list)
    hand_count: int = 0
    board: list[dict[str, Any]] = field(default_factory=list)
    deck_count: int = 0
    secrets: list[str] = field(default_factory=list)
    weapon: dict[str, Any] | None = None
    hero_power: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "class_cn": self.class_cn,
            "class_en": self.class_en,
            "health": self.health,
            "armor": self.armor,
            "mana": self.mana,
            "max_mana": self.max_mana,
            "hand": self.hand,
            "hand_count": self.hand_count,
            "board": self.board,
            "deck_count": self.deck_count,
            "secrets": self.secrets,
            "weapon": self.weapon,
            "hero_power": self.hero_power,
        }


class GameState:
    """一次对局的可变状态。每次 apply 事件后都可以随时读取视图。"""

    def __init__(self, cards: CardDB | None = None) -> None:
        self.cards = cards
        self.reset()

    # ------------------------------------------------------------ 生命周期
    def reset(self) -> None:
        self.entities: dict[int, Entity] = {}
        self.player_ids: dict[int, int] = {}  # player_id -> entity id
        self.player_entities: dict[int, Entity] = {}
        self.our_id: int = 1
        self.opp_id: int = 2
        self._our_id_locked = False
        self.turn = 0
        self.turn_owner: int | None = None
        self.playstate = 0
        self.phase = "idle"
        self.game_started = False
        self.game_over = False
        self.winner: int | None = None
        self.mulligan_done = False
        self.revealed_opp: Counter[str] = Counter()
        self.revealed_opp_order: list[str] = []
        self.revealed_turns: dict[str, int] = {}
        self.our_hand_ids: list[str] = []
        self.our_played: list[str] = []
        self.our_deck_ids: list[str] = []
        self.log: deque[str] = deque(maxlen=60)
        # 出牌流水：谁、打了什么、引擎把它理解成了什么
        self.feed: deque[dict[str, Any]] = deque(maxlen=24)
        # 已经离手的实体：防止「过期的引用行」把打出的牌又塞回手牌
        self._left_hand: set[int] = set()
        # (谁, 实体 id) -> 记录时的回合号。同一张牌在同一回合只记一次：
        # 两条管线会把同一次出牌打印两遍，间隔可能超过几条记录。
        self._fed: dict[tuple[str, int], int] = {}
        # 先手是谁：手里有幸运币的一定是后手（客户端固定规则）
        self.first_player: int | None = None
        self._pending_names: list[str] = []
        self.turn_events: list[str] = []
        self._hero_entities: dict[int, int] = {}
        self._name_player: dict[str, int] = {}
        self._first_player_id: int | None = None
        self._create_ts: str = ""
        self._block_depth = 0
        self._last_block: dict[str, Any] | None = None

    # ------------------------------------------------------------ 事件入口
    def apply_many(self, events: Iterable[pl.Event]) -> None:
        for ev in events:
            self.apply(ev)

    def apply(self, ev: pl.Event) -> None:
        kind = ev.kind
        # 任何事件只要带了实体引用，就顺手同步（选择/目标行里的 zone 是权威的）
        for ref in ev.data.get("refs") or ():
            self._sync_ref(ref)
        if kind == "create_game":
            # 同一局会从 GameState 和 PowerTaskList 两个来源各打一条 CREATE_GAME。
            # 时间戳相同 -> 是同一条，不能当成新局把已解析的状态清掉。
            if ev.time and ev.time == self._create_ts:
                return
            self.reset()
            self._create_ts = ev.time
            self.game_started = True
            self.phase = "mulligan"
            self._note("对局开始")
            return
        if not self.game_started:
            # 有些日志片段没有 CREATE_GAME 头，也尽量解析
            self.game_started = True
            self.phase = "mulligan"
        if kind == "entity":
            self._on_entity(ev)
        elif kind == "full_entity":
            self._on_full_entity(ev)
        elif kind == "show_entity" or kind == "change_entity":
            self._on_show_entity(ev)
        elif kind == "tag_change":
            self._on_tag_change(ev)
        elif kind == "block_start":
            self._on_block_start(ev)
        elif kind == "block_end":
            self._block_depth = max(0, self._block_depth - 1)
        elif kind == "entity_update":
            for ref in ev.data.get("refs") or []:
                self._sync_ref(ref)
        elif kind == "hide_entity":
            eid = ev.data.get("entity")
            if eid in self.entities:
                del self.entities[eid]

    # ------------------------------------------------------------ 事件处理
    def _entity(self, eid: int) -> Entity:
        e = self.entities.get(eid)
        if e is None:
            e = Entity(id=eid)
            self.entities[eid] = e
        return e

    def _on_entity(self, ev: pl.Event) -> None:
        d = ev.data
        eid = d.get("entity")
        if eid is None:
            return
        e = self._entity(eid)
        if d.get("role") == "player" and d.get("player_id"):
            pid = int(d["player_id"])
            self.player_ids[pid] = eid
            e.controller = pid
            e.tags[T_PLAYER_ID] = pid
            # 对手的 GameAccountId 是 [hi=0 lo=0]（隐藏），我方是真实账号 ——
            # 这是判断「哪个玩家是我」最可靠、且不依赖手牌可见性的信号
            account = d.get("account")
            if account and account != (0, 0) and not self._our_id_locked:
                self.our_id, self.opp_id = pid, 3 - pid
                self._our_id_locked = True
                self._note(f"我方是玩家 {pid}")

    
        self._track_visibility(e)


    def _on_entity(self, ev: pl.Event) -> None:
        d = ev.data
        eid = d.get("entity")
        if eid is None:
            return
        e = self._entity(eid)
        if d.get("role") == "player" and d.get("player_id"):
            pid = int(d["player_id"])
            self.player_ids[pid] = eid
            e.controller = pid
            e.tags[T_PLAYER_ID] = pid
            # 对手的 GameAccountId 是 [hi=0 lo=0]（隐藏），我方是真实账号 ——
            # 这是判断「哪个玩家是我」最可靠、且不依赖手牌可见性的信号
            account = d.get("account")
            if account and account != (0, 0) and not self._our_id_locked:
                self.our_id, self.opp_id = pid, 3 - pid
                self._our_id_locked = True
                self._note(f"我方是玩家 {pid}")

    def _on_full_entity(self, ev: pl.Event) -> None:
        d = ev.data
        eid = d.get("entity")
        if eid is None:
            return
        e = self._entity(eid)
        ref = d.get("ref") or {}
        if d.get("card_id"):
            e.card_id = d["card_id"]
        if ref.get("zone") is not None:
            e.zone = int(ref["zone"])
            e.tags[T_ZONE] = e.zone
        if ref.get("zone_pos") is not None:
            e.zone_pos = int(ref["zone_pos"])
        if ref.get("controller") is not None:
            e.controller = int(ref["controller"])
            e.tags[T_CONTROLLER] = e.controller
        if e.card_id:
            self._track_visibility(e)

    def _on_show_entity(self, ev: pl.Event) -> None:
        d = ev.data
        eid = d.get("entity")
        if eid is None:
            return
        e = self._entity(eid)
        if d.get("card_id"):
            e.card_id = d["card_id"]
        ref = d.get("ref") or {}
        if ref.get("zone") is not None:
            e.zone = int(ref["zone"])
        if ref.get("controller") is not None:
            e.controller = int(ref["controller"])
        self._track_visibility(e)

    def _on_tag_change(self, ev: pl.Event) -> None:
        d = ev.data
        tag = d.get("tag")
        val = d.get("value")
        if tag is None:
            return
        ref = d.get("ref") or {}
        name = ref.get("name")
        eid = d.get("entity")

        # 玩家实体在日志里只按名字引用（`Entity=军文武#5142`），没有实体 id。
        # CURRENT_PLAYER=1 出现时说明轮到这个人了 —— 这是唯一可靠的「名字 → 玩家」时机。
        pid_from_name: int | None = None
        if eid is None:
            if not name:
                return
            pid_from_name = self._name_player.get(name)
            if pid_from_name is None:
                if tag != T_CURRENT_PLAYER or val != 1:
                    return
                bound = set(self._name_player.values())
                if len(bound) == 1:
                    # 已经认出一个人的名字了，那这个新名字只能是另一位
                    pid_from_name = 3 - next(iter(bound))
                elif self.turn_owner in (1, 2) and self.turn_owner not in bound:
                    # 开局时 GameEntity 那条数字引用已经告诉我们先手是谁
                    pid_from_name = self.turn_owner
                elif self.first_player in (1, 2) and self.turn >= 1:
                    # 先手由「谁拿了幸运币」确定（后手才有币），据此推回合归属
                    pid_from_name = (
                        self.first_player if self.turn % 2 == 1 else 3 - self.first_player
                    )
                else:
                    # 先手还没确定，别乱猜 —— 猜错会一直错这一整局。
                    # 先记下来，等手牌揭示出幸运币后再绑定。
                    if name not in self._pending_names:
                        self._pending_names.append(name)
                    return
                self._name_player[name] = pid_from_name
                self._note(f"识别玩家：{name} = 玩家{pid_from_name}")
            eid = self.player_ids.get(pid_from_name)
            if eid is None:
                return

        e = self._entity(eid)
        prev_zone = e.zone
        e.tags[tag] = val
        if tag == T_ZONE and isinstance(val, int):
            e.zone = val
            self._on_zone_change(e, prev_zone, val)
        elif tag == T_PLAYER_ID and isinstance(val, int):
            self.player_ids[val] = eid
        elif tag == T_PLAYSTATE and isinstance(val, int):
            self.playstate = val
            if val in (3, 4, 5):
                self.game_over = True
                self.phase = "game_over"
                # PLAYSTATE 永远是「这个实体自己」的结果：他要嘛 WON 要嘛 LOST，
                # 不能忽略是谁就套到我方头上（否则对面 LOST 会被记成我方输）
                pid = e.controller if e.controller in (1, 2) else None
                if pid is not None:
                    self.winner = pid if val == 4 else (3 - pid if val == 3 else None)
                if self.winner is not None and self.our_id is not None:
                    won = self.winner == self.our_id
                else:
                    won = val == 4
                self._note("对局结束：" + ("胜利" if won else "失败" if val in (3, 4) else "平局"))
        elif tag == T_TURN and isinstance(val, int):
            # TURN 只在 GameEntity(1) 上是「总回合数」；玩家实体上也有这个 tag，
            # 含义不同（那个玩家的第几个回合），读错会把回合数搞乱。
            if eid == 1 and val != self.turn:
                self.turn = val
                self.turn_events = []
                self._note(f"第 {val} 回合")
            elif self.turn == 0:
                self.turn = val
        elif tag == T_CURRENT_PLAYER:
            # 两种日志方言都要认：
            #   老式：GameEntity 上直接写 player_id（value=2 表示轮到玩家 2）
            #   新式：玩家实体上是布尔标记（value=1 表示「轮到他」，0 表示回合结束）
            # 注意：这里**不能**用来判断「留牌结束」——留牌阶段也会置位。
            if not isinstance(val, int):
                pass
            elif eid == 1 and val in (1, 2):
                if val != self.turn_owner:
                    self.turn_owner = val
                    self._wake_up(val)
            elif val == 1:
                pid = pid_from_name or (e.controller if e.controller in (1, 2) else None)
                if pid is None and self.our_id is not None:
                    # 名字没绑上也能判：回合是严格交替的，谁不是上一个就轮到谁。
                    # 否则对手的名字没绑定时，这个事件被丢掉，turn_owner 会永远
                    # 停在「我方」，界面就会一直说「我的回合」。
                    if self.turn_owner in (1, 2):
                        pid = 3 - self.turn_owner
                    elif self.turn:
                        pid = 1 if self.turn % 2 == 1 else 2
                if pid and pid != self.turn_owner:
                    # 两条管线会把同一事件各打一遍：上一回合「我方回合开始」的
                    # 副本可能在新回合开始之后才到，直接把回合权翻回上一个人。
                    # 用「当前回合号 + 先手」算出的应有归属做交叉校验，不符就丢掉。
                    expected = None
                    if self.first_player in (1, 2) and self.turn:
                        expected = (self.first_player if self.turn % 2 == 1
                                    else 3 - self.first_player)
                    if expected is not None and pid != expected:
                        return
                    self.turn_owner = pid
                    self._wake_up(pid)
            elif val == 0 and pid_from_name and self.turn_owner == pid_from_name:
                self.turn_owner = None
        elif tag == "MULLIGAN_STATE":
            # INPUT/DEALING/WAITING/DONE —— DONE 才是「留牌结束」的真信号
            text = str(val).upper()
            if text == "DONE":
                if not self.mulligan_done:
                    self.mulligan_done = True
                    self.phase = "playing"
                    self._note("留牌结束")
            elif text in ("INPUT", "DEALING", "WAITING") and not self.mulligan_done:
                self.phase = "mulligan"
        elif tag == T_CONTROLLER and isinstance(val, int):
            e.controller = val
            self._track_visibility(e)
        elif tag == T_HERO_ENTITY and isinstance(val, int):
            if e.controller:
                self._hero_entities[e.controller] = val

    def _effect_text(self, card_id: str | None) -> tuple[str, bool]:
        """把一张牌的效果说成人话；known=False 表示引擎看不懂。"""
        card = self.cards.get(card_id) if (self.cards and card_id) else None
        if card is None:
            return "", False
        eff = parse_effect(card)
        parts: list[str] = []
        if eff.destroy:
            parts.append("消灭随从" if eff.scope != "all_minions" else "消灭所有随从")
        if eff.damage:
            where = {"enemy_minions": "敌方随从", "all_minions": "所有随从",
                     "enemy_hero": "敌方英雄", "random_enemy": "随机敌人"}.get(eff.scope, "目标")
            parts.append(f"{eff.damage}点伤害→{where}")
        if eff.heal:
            parts.append(f"恢复{eff.heal}")
        if eff.buff_atk or eff.buff_hp:
            parts.append(f"+{eff.buff_atk}/+{eff.buff_hp}")
        if eff.draw:
            parts.append(f"抽{eff.draw}张")
        if eff.summon_atk:
            parts.append(f"召唤{eff.summon_atk}/{eff.summon_hp}")
        if eff.freeze:
            parts.append("冻结")
        if parts:
            return " · ".join(parts), True
        if card.is_minion:
            kw = card.keyword_cn
            return (f"{card.atk}/{card.health}" + (f" {kw}" if kw else "")), True
        return "效果未建模", False

    def _infer_first_player(self) -> None:
        if self.first_player is not None or self.our_id is None:
            return
        hand_ids = [e.card_id for e in self.entities.values()
                    if e.controller == self.our_id and e.zone == ZONE_HAND and e.card_id]
        if "GAME_005" in hand_ids:
            self.first_player = 3 - self.our_id      # 后手才有幸运币
        elif len(hand_ids) >= 3:
            self.first_player = self.our_id          # 手牌齐了还没有币 → 我是先手
        else:
            return
        self._bind_pending_names()

    def _bind_pending_names(self) -> None:
        """先手确定后，把之前没认出来的名字按出场顺序绑上。"""
        if self.first_player not in (1, 2):
            return
        order = [self.first_player, 3 - self.first_player]
        for name in self._pending_names:
            if name in self._name_player:
                continue
            for pid in order:
                if pid not in self._name_player.values():
                    self._name_player[name] = pid
                    self._note(f"识别玩家：{name} = 玩家{pid}")
                    break
        # 名字绑好后，用先手规则把当前回合归属修正过来
        if self.turn and self.turn_owner is None:
            self.turn_owner = (self.first_player if self.turn % 2 == 1
                               else 3 - self.first_player)

    def _wake_up(self, pid: int) -> None:
        """该玩家回合开始：他的随从解除疲劳、攻击次数清零。

        客户端不一定补发 EXHAUSTED=0，如果只认「置 1」不认「清 0」，
        随从会永远处于疲劳状态 —— 表现就是「明明能攻击，却算不出斩杀」。
        """
        for e in self.entities.values():
            if e.controller == pid and e.zone == ZONE_PLAY:
                e.tags["EXHAUSTED"] = 0
                e.tags[T_ATTACKS_THIS_TURN] = 0

    def _push_feed(self, who: str, card_id: str | None, name: str, eid: int | None = None) -> bool:
        """记录一次出牌，并去掉重复。

        GameState.DebugPrintPower 和 PowerTaskList.DebugPrintPower 会把同一次
        出牌各打印一遍，直接 append 就会看到「对面出牌统计两次」。
        """
        key = (who, eid) if eid is not None else (who, hash(card_id or name))
        if key in self._fed:
            return False        # 同一张牌（同一实体）只算一次；
                                # 回手后再打会被 ZONE=HAND 清掉记录，那时才重新计数
        self._fed[key] = self.turn
        summary, known = self._effect_text(card_id)
        self.feed.append({"turn": self.turn, "who": who, "card_id": card_id, "eid": eid,
                          "name": name, "summary": summary, "known": known})
        return True

    def _is_enchant(self, card_id: str | None) -> bool:
        card = self.cards.get(card_id) if (self.cards and card_id) else None
        return card is not None and card.type == "ENCHANTMENT"

    def _is_not_a_card_play(self, card_id: str | None) -> bool:
        """英雄、英雄技能、附魔都不是用户「打出的牌」，不能进流水/出牌记录。"""
        card = self.cards.get(card_id) if (self.cards and card_id) else None
        if card is None:
            return False
        return card.type in ("ENCHANTMENT", "HERO", "HERO_POWER") or (card_id or "").upper().startswith("HERO_")

    def _on_zone_change(self, e: Entity, prev: int, new: int) -> None:
        self._track_visibility(e)
        if e.controller == self.our_id and new == ZONE_HAND:
            self._infer_first_player()
        if new == ZONE_PLAY:
            self._left_hand.add(e.id)
        elif new == ZONE_HAND:
            self._left_hand.discard(e.id)
            if e.controller in (self.our_id, self.opp_id):
                self._fed.pop(("our" if e.controller == self.our_id else "opp", e.id), None)
        if new == ZONE_PLAY and self._is_not_a_card_play(e.card_id):
            return          # 英雄/技能/附魔不是「打出的牌」
        if new == ZONE_PLAY and e.zone != prev:
            if e.controller == self.opp_id:
                name = self._name(e.card_id)
                if name:
                    if self._push_feed("opp", e.card_id, name, e.id):
                        self.revealed_opp[e.card_id or "?"] += 1
                        if e.card_id not in self.revealed_opp_order:
                            self.revealed_opp_order.append(e.card_id or "?")
                            self.revealed_turns[e.card_id or "?"] = self.turn
                        self._note(f"对手打出 {name}")
                        self.turn_events.append(f"对手打出 {name}")
            elif e.controller == self.our_id:
                name = self._name(e.card_id)
                if name:
                    if self._push_feed("our", e.card_id, name, e.id):
                        self._note(f"我方打出 {name}")
                        if e.card_id:
                            self.our_played.append(e.card_id)
        elif new == ZONE_GRAVE:
            if e.controller == self.opp_id:
                self._track_visibility(e)

    def _sync_ref(self, ref: dict[str, Any]) -> None:
        """把一行里看到的实体引用同步进状态（选择/目标行的 zone、cardId 是权威的）。"""
        eid = ref.get("id")
        if not eid:
            return
        e = self._entity(eid)
        if ref.get("card_id"):
            e.card_id = ref["card_id"]
        if ref.get("controller") is not None:
            e.controller = int(ref["controller"])
        zone = ref.get("zone")
        # 引用行里的 zone 可能是旧快照（比如选择/目标行引用的是打牌前的手牌），
        # 已经离手的牌不许被它拉回手牌 —— 否则建议会一直让你打一张已经打出去的牌。
        if isinstance(zone, int) and zone == ZONE_HAND and eid in self._left_hand:
            return
        if isinstance(zone, int) and zone != e.zone:
            prev = e.zone
            e.zone = zone
            e.tags[T_ZONE] = zone
            self._on_zone_change(e, prev, zone)
        if ref.get("zone_pos") is not None:
            e.zone_pos = int(ref["zone_pos"])
        self._track_visibility(e)

    def _on_block_start(self, ev: pl.Event) -> None:
        self._block_depth += 1
        d = ev.data
        self._last_block = d
        eid = d.get("entity")
        e = self.entities.get(eid) if eid else None
        if d.get("type") == "PLAY" and not self.mulligan_done:
            self.mulligan_done = True
            self.phase = "playing"
        if e is not None and d.get("type") == "PLAY":
            self._track_visibility(e)

    # ------------------------------------------------------------ 可见性
    def _track_visibility(self, e: Entity) -> None:
        if not e.card_id:
            return
        if not self._our_id_locked and e.zone == ZONE_HAND:
            # 起手时只有自己的手牌是可见的 —— 用这一点判断谁是「我们」
            if e.controller:
                self.our_id = e.controller
                self.opp_id = 3 - e.controller
                self._our_id_locked = True
        if e.controller == self.our_id and e.zone == ZONE_HAND and e.card_id not in self.our_hand_ids:
            self.our_hand_ids.append(e.card_id)
        if e.controller == self.our_id and e.zone == ZONE_DECK and e.card_id not in self.our_deck_ids:
            self.our_deck_ids.append(e.card_id)
        if e.controller == self.our_id and e.zone == ZONE_HAND:
            # 手牌一出现就尝试判定先手（开局 dump 走的是 FULL_ENTITY，
            # 不经过 _on_zone_change，所以这里也要触发）
            self._infer_first_player()

    # ------------------------------------------------------------ 读取视图
    def sides(self) -> tuple[Side, Side]:
        """(我方, 对方)。统一入口，避免外部到处写 _side(self.our_id)。"""
        return self._side(self.our_id), self._side(self.opp_id)

    def player_entity(self, pid: int) -> Entity | None:
        eid = self.player_ids.get(pid)
        if eid is not None:
            return self.entities.get(eid)
        # 回退：找 tag 30 == pid 的实体
        for e in self.entities.values():
            if e.tags.get(T_PLAYER_ID) == pid:
                self.player_ids[pid] = e.id
                return e
        return None

    def hero(self, pid: int) -> Entity | None:
        # 最可靠：玩家实体上的 HERO_ENTITY 直接给出英雄实体 id
        pe = self.player_entity(pid)
        if pe is not None:
            hero_id = pe.tags.get(T_HERO_ENTITY)
            if isinstance(hero_id, int) and hero_id in self.entities:
                self._hero_entities[pid] = hero_id
                return self.entities[hero_id]
        if pid in self._hero_entities:
            e = self.entities.get(self._hero_entities[pid])
            if e is not None:
                return e
        for e in self.entities.values():
            if e.controller == pid and (e.card_id or "").upper().startswith("HERO"):
                self._hero_entities[pid] = e.id
                return e
        return None

    def hero_entity_id(self, pid: int) -> int | None:
        h = self.hero(pid)
        return h.id if h else None

    def _name(self, card_id: str | None) -> str | None:
        if not card_id:
            return None
        if self.cards:
            c = self.cards.get(card_id)
            if c:
                return c.name
        return card_id

    def _side(self, pid: int) -> Side:
        s = Side(player_id=pid)
        h = self.hero(pid)
        if h is not None:
            self._hero_stats(s, h)
        pe = self.player_entity(pid)
        if pe is not None:
            res = _int(pe.tags.get(T_RESOURCES))
            used = _int(pe.tags.get(T_RESOURCES_USED)) or 0
            if res is not None:
                s.mana, s.max_mana = max(0, res - used), res
            s.fatigue = _int(pe.tags.get("FATIGUE")) or 0
        if s.max_mana == 0:
            s.max_mana, s.mana = self._mana_from_turn(pid)

        hand, deck, board, secrets, weapon, hp = [], [], [], [], None, None
        for e in self.entities.values():
            if e.controller != pid or e.id == 1:   # 1 是 GameEntity，别当手牌/随从
                continue
            if e.zone == ZONE_HAND:
                card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
                # 附魔（挂在手牌上的 buff）不是牌，不能算进手牌
                if card is not None and card.type == "ENCHANTMENT":
                    continue
                hand.append(e)
            elif e.zone == ZONE_DECK:
                deck.append(e)
            elif e.zone == ZONE_PLAY:
                card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
                if card is not None and card.type == "HERO_POWER":
                    hp = e
                elif card is not None and card.is_weapon:
                    weapon = self._card_view(e)
                elif card is None or card.is_minion:
                    board.append(e)
            elif e.zone == ZONE_SECRET:
                secrets.append(self._card_view(e))

        hand.sort(key=lambda x: (x.zone_pos or 0, x.id))
        board.sort(key=lambda x: (x.zone_pos or 0, x.id))
        s.hand = [self._card_view(e) for e in hand if e.card_id]
        s.hand_count = len(hand)
        s.board = [self._card_view(e) for e in board if self._is_minion(e)]
        s.deck_count = len(deck)
        s.secrets = [x["id"] for x in secrets if x.get("id")]
        s.weapon = weapon
        s.hero_power = self._card_view(hp) if hp is not None and hp.card_id else None
        return s

    def _hero_stats(self, s: Side, h: Entity) -> None:
        s.armor = h.armor
        class_raw: str | None = None
        if self.cards:
            c = self.cards.get(h.card_id)
            if c and c.card_class and c.card_class != "NEUTRAL":
                class_raw = c.card_class
        if class_raw is None:
            class_raw = self._class_from_hero_power(h)
        if class_raw:
            from .cards import CLASS_CN

            s.class_en = class_raw
            s.class_cn = CLASS_CN.get(class_raw, class_raw)
        # 英雄血量 = HEALTH - DAMAGE；HEALTH 缺失时按 30 处理
        base = _int(h.tags.get(T_HEALTH)) or 30
        s.health = max(0, base - h.damage)

    def _class_from_hero_power(self, hero: Entity) -> str | None:
        """英雄皮肤牌的 cardClass 常常是 NEUTRAL/缺失，用英雄技能反推职业。"""
        for e in self.entities.values():
            if e.controller != hero.controller or e.zone != ZONE_PLAY or not e.card_id:
                continue
            if not self.cards:
                continue
            c = self.cards.get(e.card_id)
            if c and c.type == "HERO_POWER" and c.card_class and c.card_class != "NEUTRAL":
                return c.card_class
        return None

    def _mana_from_turn(self, pid: int) -> tuple[int, int]:
        """日志里没给法力水晶标签时的回退：TURN 是半回合计数，第 1/3/5… 回合属于先手方。"""
        t = max(1, self.turn or 1)
        mx = (t + 1) // 2 if pid == 1 else t // 2
        mx = max(0, min(10, mx))
        return mx, mx

    def _is_minion(self, e: Entity) -> bool:
        if e.zone != ZONE_PLAY:
            return False
        if (e.card_id or "").upper().startswith("HERO"):
            return False
        if self.cards and e.card_id:
            c = self.cards.get(e.card_id)
            if c is not None:
                # 附魔/地标/英雄技能会挂着 ATK/HEALTH 标签，但它们不是随从
                return c.is_minion
        return e.atk is not None and e.health is not None

    def _card_view(self, e: Entity) -> dict[str, Any]:
        card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
        base_hp = card.health if card else 0
        hp = e.health
        if hp is None:
            hp = base_hp or 0
        if card and card.health and e.tags.get(T_HEALTH) is None and e.damage:
            hp = card.health - e.damage
        atk = e.atk
        if atk is None:
            atk = card.atk if card else 0
        view: dict[str, Any] = {
            "id": e.card_id or "",
            "entity_id": e.id,
            "name": card.name if card else (e.card_id or "?"),
            "cost": card.cost if card else 0,
            "type_cn": card.type_cn if card else "",
            "atk": atk,
            "health": max(0, hp),
            "keyword": card.keyword_cn if card else "",
            "damage": e.damage,
            "taunt": bool(card.has("TAUNT") if card else e.tags.get(T_TAUNT)),
            "divine_shield": bool(card.has("DIVINE_SHIELD") if card else e.tags.get(T_DIVINE_SHIELD)),
            "can_attack": self.can_attack(e),
            "exhausted": e.tags.get("EXHAUSTED") in (1, "1", True),
            "text": _clean_text(card.text if card else ""),
            "zone": e.zone,
        }
        return view

    def can_attack(self, e: Entity) -> bool:
        """这个随从现在能不能攻击。

        依据三条：①它是我方/行动方 ②没被 EXHAUSTED（刚上场/已攻击过/被冻结）
        ③攻击次数没用完（风怒是 2 次）。以前只数攻击次数，会把「刚下的随从」
        误判成能攻击 —— 那会让建议里出现根本打不出去的操作。
        """
        if e.zone != ZONE_PLAY or e.controller is None:
            return False
        if self.turn_owner != e.controller:
            return False
        card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
        atk = e.atk if e.atk is not None else (card.atk if card else 0)
        if atk <= 0:
            return False
        if e.tags.get("EXHAUSTED") in (1, "1", True):
            return False
        if e.tags.get("FROZEN") in (1, "1", True):
            return False
        limit = 2 if (card is not None and card.has("WINDFURY")) else 1
        if (e.tags.get(T_ATTACKS_THIS_TURN) or 0) >= limit:
            return False
        return True

    # ------------------------------------------------------------ 对外快照
    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "turn": self.turn,
            "is_our_turn": self.turn_owner == self.our_id,
            "playstate": self.playstate,
            "our_id": self.our_id,
            "opp_id": self.opp_id,
            "our": self._side(self.our_id).as_dict(),
            "opp": self._side(self.opp_id).as_dict(),
            "revealed_opp": list(self.revealed_opp_order),
            "feed": list(self.feed)[-10:],
            "log": list(self.log)[-6:],
            "game_over": self.game_over,
        }

    # ------------------------------------------------------------ 摘要
    def summary(self) -> str:
        ours, opp = self._side(self.our_id), self._side(self.opp_id)
        return (
            f"T{self.turn} {'我方' if self.turn_owner == self.our_id else '对方'}回合 | "
            f"我方 {ours.class_cn} {ours.health}+{ours.armor} ({len(ours.board)}随从, {ours.hand_count}手牌) vs "
            f"对方 {opp.class_cn} {opp.health}+{opp.armor} ({len(opp.board)}随从, {opp.hand_count}手牌)"
        )

    def _note(self, text: str) -> None:
        self.log.append(text)


def replay_log_text(text: str, cards: CardDB | None = None) -> GameState:
    """把一整段日志跑成最终状态（离线复盘/测试用）。"""
    st = GameState(cards)
    st.apply_many(pl.parse_text(text))
    return st
