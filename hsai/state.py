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
from typing import Any, Iterable

from . import powerlog as pl
from .model import parse_effect
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
T_TEMP_RESOURCES = "TEMP_RESOURCES"
T_ARMOR = "ARMOR"
T_TAUNT = "TAUNT"
T_DIVINE_SHIELD = "DIVINE_SHIELD"
T_COST = "COST"
T_HERO_ENTITY = "HERO_ENTITY"
T_ATTACKS_THIS_TURN = "NUM_ATTACKS_THIS_TURN"

ZONE_PLAY, ZONE_DECK, ZONE_HAND, ZONE_GRAVE, ZONE_SECRET, ZONE_SETASIDE = 1, 2, 3, 4, 5, 7


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

_EFFECT_CACHE: dict[str, tuple[str, bool]] = {}


def _cached_effect(state: "GameState", card_id: str | None) -> str:
    """效果摘要（带缓存）。给 AI 看牌面用，避免每帧重复解析。"""
    if not card_id:
        return ""
    hit = _EFFECT_CACHE.get(card_id)
    if hit is None:
        hit = state._effect_text(card_id)
        _EFFECT_CACHE[card_id] = hit
    summary, known = hit
    if not known:
        return ""
    # 随从没有可解析效果时，摘要就是「攻/血 + 关键字」，和前面重复了，丢掉
    if re.match(r"^\d+/\d+", summary):
        return ""
    return summary


def _clean_text(raw: str) -> str:
    """去掉富文本标记：<b>、$（伤害）、#（治疗）、&（换行）、{0}（占位符）。"""
    t = re.sub(r"<[^>]+>", "", raw or "")
    t = t.replace("$", "").replace("#", "").replace("&", "")
    t = re.sub(r"\{\d+\}", "", t)
    return " ".join(t.split())


def _int(v: Any) -> int | None:
    """把标签值转成整数；转不了就返回 None 并记一行日志（不崩）。"""
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v)
        except ValueError:
            from .logutil import log as _log
            _log(f"标签不是整数：{v!r}")
    return None


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
    # True 表示牌库数是「卡组张数 − 手牌 − 场面」的粗估（没赶上 CREATE_GAME，
    # 拿不到初始牌库实体），不是真值
    deck_estimated: bool = False
    secrets: list[str] = field(default_factory=list)
    graveyard: list[dict[str, Any]] = field(default_factory=list)   # 已死的随从
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
            "deck_estimated": self.deck_estimated,
            "secrets": self.secrets,
            "weapon": self.weapon,
            "hero_power": self.hero_power,
            # 只给数量：墓地内容在 advisor 里按需查（复活推荐/亡语池），
            # 面板上只关心「死了几个」，整份列表塞进快照没必要
            "graveyard_count": len(self.graveyard),
        }


class GameState:
    """一次对局的可变状态。每次 apply 事件后都可以随时读取视图。"""

    def __init__(self, cards: CardDB | None = None) -> None:
        self.cards = cards
        self.reset()

    # ------------------------------------------------------------ 生命周期
    def reset(self) -> None:
        # 局与局之间必须隔离：抽牌数、墓地、休眠/入场回合、克苏恩进度
        self.entities: dict[int, Entity] = {}
        self.player_ids: dict[int, int] = {}  # player_id -> entity id
        self.player_entities: dict[int, Entity] = {}
        self._last_class: dict[int, str] = {}
        self.our_id: int = 1
        self.opp_id: int = 2
        self._our_id_locked = False
        # 「按可见手牌翻面」只做一次：本局内有效，跨局必须清零，否则第二局
        # 永远提前 return，而第一次启动永远是对的 —— 极难复现。
        self._id_by_hand_done = False
        self.turn = 0
        self.turn_owner: int | None = None
        # NEXT_STEP 预告下一阶段；MAIN_END 比 TURN/CURRENT_PLAYER 早到，
        # 是「结束回合生效」的第一个信号。按回合号幂等：两条管线各打一遍。
        self.next_step: str | None = None
        self.end_turn_seq = 0
        self._end_turn_last_turn: int | None = None
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
        # 本局出现过「引擎看不懂效果」的牌，供对局结束时的说明使用
        self.unmodelled: list[str] = []
        # 任务牌进度：实体 id -> {"progress": n, "total": m}
        self.quest_progress: dict[int, dict[str, int]] = {}
        # 实体 -> 它进入战场的回合数（判断突袭刚入场用）
        self.played_turn: dict[int, int] = {}
        # 实体 -> 从休眠醒来的回合（醒来当回合不能攻击）
        self.woke_turn: dict[int, int] = {}
        # 我方抽到的牌数（牌库→手牌），用来算牌库剩余：30 - 抽到
        self.our_drawn: int = 0
        # 对手抽到的牌数。日志里这个计数器双方都有，只记我方会让
        # 「对手牌库数 = 30 − 我方抽牌数」，而预判拿它当分母算概率。
        self.opp_drawn: int = 0
        # 我方「从手牌打出」的随从数（被动技能按这个计数；召唤物不算）
        self.our_minions_played: int = 0
        self._counted_minions: set[int] = set()
        # 卡组张数，也是抽牌计数的物理上限；登记过卡组时由 advisor 同步。
        self.deck_size: int = 30
        # 日志里每回合有「本回合抽了几张」的计数器。按 方 → 回合 → 已见 value
        # 记一份：两条管线会把同一次抽牌各打一遍，段间还会回落再涨，
        # 只有「这个 (方, 回合, value) 没见过」才算一次，幂等不靠数值推断。
        self._draw_seen: dict[str, dict[int, set[int]]] = {}
        # 对手整局没有这条标签时 opp_drawn 是粗估；真标签一到就丢掉重算
        self._opp_drawn_estimated: bool = False
        # 初始牌库实体集合：CREATE_GAME 时「凭空出现在 DECK」的那批牌。
        # 牌库数 = 集合里 zone 还是 DECK 的实体数，不再用「30 − 抽牌数」去估。
        self._deck_ids: dict[int, set[int]] = {}
        # SHOW_ENTITY 块的裸 tag 行没有自己的实体引用，解析层会把它们挂在
        # 最近一个带 id 的实体上（实测经常是 GameEntity）。记下最后一次
        # SHOW_ENTITY 的目标，zone 挂到 GameEntity 头上时归还给真身。
        self._show_pending: int | None = None
        # 实体 -> (死亡回合, 死亡顺序)；顺序按发生先后递增
        self.attacked_this_turn: set[int] = set()
        # 每只克苏恩各自用过哪些技能（key = 触发源实体 id）
        self._choice_source: dict = {}
        self.grave_meta: dict[int, tuple[int, int]] = {}
        self._grave_seq: int = 0
        # 克苏恩的四个技能：整局里分别触发过没有（集齐四个后它的技能效果 ×4）
        self.cthun_used: set[str] = set()
        # 当前待做的选择（发现/抉择/弃牌等）：选项列表 + 来源牌
        self.choice: dict[str, Any] | None = None
        # 先手是谁：手里有幸运币的一定是后手（客户端固定规则）
        self.first_player: int | None = None
        self._pending_names: list[str] = []
        self._last_cp_name: str | None = None     # 最近一条「轮到他」但还没认出的名字
        self._first_player_name: str | None = None
        self._choice_player_name: str | None = None
        # 每回合我方真正做了什么（局后对照建议用）
        self.turn_actions: dict[int, list[str]] = {}
        self._hero_entities: dict[int, int] = {}
        self._name_player: dict[str, int] = {}
        self._create_ts: str = ""
        self._block_depth = 0
        self._last_block: dict[str, Any] | None = None
        self._last_attack_key: tuple[int, Any, Any] | None = None

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
        if kind == "block_start" and (ev.data.get("type") or "") == "ATTACK":
            r = ev.data.get("ref") or {}
            t = ev.data.get("target_ref") or {}
            if r.get("controller") == self.our_id and r.get("name") and t.get("name"):
                # 同一次攻击会被 GameState / PowerTaskList 各打一遍，按「回合+双方」去重
                key = (self.turn, r.get("id"), t.get("id"))
                if key != self._last_attack_key:
                    self._last_attack_key = key
                    self.turn_actions.setdefault(self.turn, []).append(f"{r['name']} 攻击 {t['name']}")
            return
        if kind == "choice_sent":
            self._on_choice_sent(ev)
            return
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
        elif kind == "choices":
            self._on_choices(ev)
        elif kind == "chosen":
            if self.choice is not None:
                self.choice = None
        elif kind == "entity_update":
            for ref in ev.data.get("refs") or []:
                self._sync_ref(ref)
        elif kind == "hide_entity":
            eid = ev.data.get("entity")
            e = self.entities.get(eid)
            if e is not None:
                # HIDE_ENTITY 是「把这张牌重新藏起来」（换牌回库、开局触发结束），
                # 实体本身还活着：只抹掉身份。整个删掉会把 zone/controller 一起丢，
                # 牌库计数和归属都会少一块。
                e.card_id = None

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
            # 不按 GameAccountId 判断「谁是我方」：练习模式下两边都有真账号，会判反。
            # 交给 _track_visibility 用「只有自己的手牌有 card_id」来定。

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
            e.zone = _int(ref["zone"])
            e.tags[T_ZONE] = e.zone
            if e.zone == ZONE_DECK:
                self._deck_collect(e, 0)
        if ref.get("zone_pos") is not None:
            e.zone_pos = _int(ref["zone_pos"])
        # 完整实体块里带着 ZONE_POSITION 标签，这才是场面从左到右的顺序
        tags = d.get("tags") or {}
        if tags.get("ZONE_POSITION") is not None:
            try:
                e.zone_pos = _int(tags["ZONE_POSITION"])
            except (TypeError, ValueError):
                pass
        if ref.get("controller") is not None:
            e.controller = _int(ref["controller"])
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
        # show_entity 是「让你看一眼这张牌」，头部引用带的是旧快照：
        # 实体已经有 zone 时绝不能被它盖回去（否则刚抽到手的牌会被打回牌库）。
        if ref.get("zone") is not None and e.zone in (0, None):
            e.zone = _int(ref["zone"])
        if ref.get("controller") is not None:
            e.controller = _int(ref["controller"])
        # 后面跟的裸 tag 行描述的就是这个实体，zone 挂错了归属时靠它归还
        self._show_pending = eid
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

        # SHOW_ENTITY 块的裸 tag 行被解析层挂到了 GameEntity 上（它自己没有
        # zone 的概念），块里的 zone 写的是真身的新位置：归还给真身。
        if tag == T_ZONE and eid == 1 and self._show_pending is not None:
            eid = self._show_pending

        # CURRENT_PLAYER=1 表示「现在轮到这个人」：上一条没绑上的名字只属于
        # 上一回合，必须作废。不清的话，对手上一回合没出牌时，这个名字会被
        # 我方下一回合的第一个动作绑给我方（实测两个名字都指向玩家 1，
        # 之后对手回合全被判成「我方回合」）。
        if tag == T_CURRENT_PLAYER and val == 1:
            self._last_cp_name = None

        # 玩家实体在日志里只按名字引用（`Entity=Player#1234`），没有实体 id。
        # CURRENT_PLAYER=1 出现时说明轮到这个人了 —— 这是唯一可靠的「名字 → 玩家」时机。
        pid_from_name: int | None = None
        if eid is None:
            if not name:
                return
            # FIRST_PLAYER 是按名字给的：不管这个名字认没认出来，先抓住它。
            # 名字已经绑定时也不能漏（否则轮到我们先手时永远推不出先手是谁）。
            if tag == "FIRST_PLAYER" and isinstance(val, int) and val == 1:
                self._first_player_name = name
                self._infer_first_from_name()
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
                    # 名字认不出：记下来，等「他打出一张牌」时反推；
                    # 但回合归属不能一直空着 —— 回合严格交替，从上一个归属翻过去。
                    if name not in self._pending_names:
                        self._pending_names.append(name)
                    self._last_cp_name = name
                    if self.turn_owner in (1, 2):
                        self.turn_owner = 3 - self.turn_owner
                    elif self.first_player in (1, 2) and self.turn:
                        self.turn_owner = (self.first_player if self.turn % 2 == 1
                                           else 3 - self.first_player)
                    return
                self._name_player[name] = pid_from_name
                self._infer_first_from_name()
                self._note(f"识别玩家：{name} = 玩家{pid_from_name}")
            eid = self.player_ids.get(pid_from_name)
            if eid is None:
                # 中途启动时没见过 CREATE_GAME，没有玩家实体。
                # 但「轮到谁」这件事只依赖玩家编号，不需要实体 —— 照常处理。
                if tag == T_CURRENT_PLAYER and isinstance(val, int):
                    if val == 1:
                        expected = None
                        if self.first_player in (1, 2) and self.turn:
                            expected = (self.first_player if self.turn % 2 == 1
                                        else 3 - self.first_player)
                        if expected is None or pid_from_name == expected:
                            if pid_from_name != self.turn_owner:
                                self.turn_owner = pid_from_name
                                self._wake_up(pid_from_name)
                    elif val == 0 and self.turn_owner == pid_from_name:
                        self.turn_owner = None
                return

        # 回合结束：留牌/开局流程也会走一次 MAIN_END，只认对局中；
        # GameState 与 PowerTaskList 各打一遍，按回合号只算一次。
        if tag == "NEXT_STEP" and eid == 1:
            self.next_step = val if isinstance(val, str) else None
            if (val == "MAIN_END" and self.phase == "playing"
                    and self.turn != self._end_turn_last_turn):
                self._end_turn_last_turn = self.turn
                self.end_turn_seq += 1
                self._note("回合结束")

        e = self._entity(eid)
        prev_zone = e.zone
        e.tags[tag] = val
        if tag == T_ZONE and isinstance(val, int):
            e.zone = val
            if val == ZONE_GRAVE and e.id not in self.grave_meta:
                self._grave_seq += 1
                self.grave_meta[e.id] = (self.turn, self._grave_seq)
            if (val == ZONE_PLAY and prev_zone in (ZONE_HAND, ZONE_DECK)
                    and e.controller == self.our_id and e.id not in self._counted_minions):
                _c = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
                if _c is not None and _c.is_minion:
                    self.our_minions_played += 1
                    self._counted_minions.add(e.id)
            if e.controller == self.our_id:
                # 进出牌库都要记：抽牌(-1)、换牌回库(+1)、塞进牌库(+1)、
                # 从牌库被拉出/爆牌(-1)。这样 30 张 + 净变化 才是真实剩余。
                if prev_zone == ZONE_HAND and val == ZONE_DECK:
                    # 换牌回库：牌库变多
                    self.our_drawn = max(0, self.our_drawn - 1)
                elif prev_zone != ZONE_DECK and val == ZONE_DECK:
                    # 往牌库里塞牌（洗入、置于牌库顶等）：牌库变多
                    self.our_drawn = max(0, self.our_drawn - 1)
                # 抽牌不再按区域变化计（会重复），统一走 NUM_CARDS_DRAWN_THIS_TURN
            if val == ZONE_DECK:
                self._deck_collect(e, prev_zone)
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
            # 新回合：所有随从的攻击机会恢复
            self.attacked_this_turn = set()
            # TURN 只在 GameEntity(1) 上是「总回合数」；玩家实体上也有这个 tag，
            # 含义不同（那个玩家的第几个回合），读错会把回合数搞乱。
            if eid == 1 and val != self.turn:
                self.turn = val
                # 新回合开始就清掉 MAIN_END：日志缺下一条 NEXT_STEP 时，
                # 不能让界面一直挂着「回合已结束」、把后面的建议全屏蔽。
                self.next_step = None
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
        elif tag in ("QUEST_PROGRESS", "QUEST_PROGRESS_TOTAL") and eid:
            if isinstance(val, int):
                slot = self.quest_progress.setdefault(eid, {})
                slot["progress" if tag == "QUEST_PROGRESS" else "total"] = val
        elif tag == "NUM_CARDS_DRAWN_THIS_TURN" and eid:
            try:
                val_i = int(val)
            except (TypeError, ValueError):
                val_i = None
            # 双方都有这个计数器，必须分开记；controller 拿不到时不猜。
            ctrl = e.controller if e.controller in (1, 2) else None
            if val_i is not None and ctrl is not None:
                mine = ctrl == self.our_id or eid == self.player_ids.get(self.our_id)
                bucket = "our" if mine else "opp"
                # GameState / PowerTaskList 会把同一次抽牌各打一遍，中间还夹
                # 1 → 0 → 1 的回落。按 (方, 回合, value) 幂等：同一个 value
                # 只累加一次，增量取本回合已见过的最大值，不靠回落判新段。
                seen = self._draw_seen.setdefault(bucket, {}).setdefault(
                    self.turn, set())
                if val_i not in seen:
                    seen_max = max(seen) if seen else 0
                    seen.add(val_i)
                    delta = max(0, val_i - seen_max)
                    # 一副牌只有 deck_size 张：重复计数再怎么漏，也不能把
                    # 抽牌数顶到超过卡组（疲劳为 0 时尤其明显）。
                    if mine:
                        self.our_drawn = min(self.deck_size, self.our_drawn + delta)
                    else:
                        # 真标签到了就丢掉粗估，不能拿假数当起点继续累加
                        if self._opp_drawn_estimated:
                            self.opp_drawn = 0
                            self._opp_drawn_estimated = False
                        self.opp_drawn = min(self.deck_size, self.opp_drawn + delta)
        elif tag == "EXHAUSTED" and eid:
            if e.zone == ZONE_PLAY and _int(val) == 1:
                self.attacked_this_turn.add(eid)
            elif _int(val) == 0:
                self.attacked_this_turn.discard(eid)
        elif tag == "DORMANT" and eid:
            if isinstance(val, int) and val == 0:
                self.woke_turn[eid] = self.turn
        elif tag == "ZONE_POSITION" and eid:
            # 场面/手牌的从左到右顺序，客户端用这个标签维护
            if isinstance(val, int):
                e2 = self.entities.get(eid)
                if e2 is not None:
                    e2.zone_pos = val
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
        elif tag == "FIRST_PLAYER" and isinstance(val, int) and name:
            # 客户端明确告诉我们谁先手（按名字），等这个名字绑上玩家后即可确定
            self._first_player_name = name
            self._infer_first_from_name()
        elif tag == T_CONTROLLER and isinstance(val, int):
            e.controller = val
            # controller 是后到的（裸 tag 里 ZONE 常排在 CONTROLLER 前面），
            # 补上这时才能归属的牌库实体
            if e.zone == ZONE_DECK:
                self._deck_collect(e, e.zone)
            self._track_visibility(e)
        elif tag == T_HERO_ENTITY and isinstance(val, int):
            if e.controller:
                self._hero_entities[e.controller] = val

    def _deck_collect(self, e: Entity, prev_zone: int) -> None:
        """把实体记进「初始牌库」集合（牌库数 = 集合里 zone 还是 DECK 的个数）。

        - 封口（留牌结束/第 1 回合开始）前，「凭空出现在 DECK」的都是卡组里的牌；
        - 封口后再创建、自带 zone=DECK 的实体是对局中生成的牌，不在卡组里
          （修 bug 前实测会虚高到 31/34）；
        - 从真实区域（手牌/场面/备用区）TAG_CHANGE 进 DECK 的照收：那是真·洗入牌库。
        """
        ctrl = e.controller
        if ctrl not in (1, 2):
            return
        ids = self._deck_ids.setdefault(ctrl, set())
        if e.id in ids:
            return
        sealed = self.mulligan_done or self.turn >= 1
        if sealed and prev_zone in (0, ZONE_DECK):
            # 封口后「生成时直接 zone=DECK」的实体分两种：带 card_id 的是洗入
            # 牌库的真实牌（如炸弹/疫病/洗入的衍生物），空 CardID 的只是客户端
            # 凭空造的占位实体（实测会虚高到 31/34），不能算进牌库。
            if not e.card_id:
                return
        ids.add(e.id)

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
        if eff.mana:
            parts.append(f"获得{eff.mana}费")
        if eff.armor:
            parts.append(f"+{eff.armor}护甲")
        if eff.discover:
            parts.append("发现")
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
        if "GAME_005" not in hand_ids and self.turn >= 3:
            pe = self.player_entity(self.our_id)
            crystals = _int(pe.tags.get(T_RESOURCES)) if pe is not None else None
            if crystals:
                # 第 N 回合：先手有 (N+1)//2 颗，后手有 N//2 颗
                if crystals >= (self.turn + 1) // 2:
                    self.first_player = self.our_id
                elif crystals <= self.turn // 2:
                    self.first_player = 3 - self.our_id
                if self.first_player:
                    self._bind_pending_names()
                return
        if "GAME_005" not in hand_ids:
            # 没有币不能断定自己先手：币可能只是还没揭示出来。
            # 真正的先手以日志里的 FIRST_PLAYER 为准（见 _bind_by_action）。
            return
        self.first_player = 3 - self.our_id          # 后手才有幸运币
        self._bind_pending_names()

    def _infer_first_from_name(self) -> None:
        """FIRST_PLAYER 给了名字：自己的名字绑好后，先手是不是我就确定了。

        比数水晶可靠得多：水晶法要等到第 3 回合，而且算错会把整局回合反过来。
        """
        if self.first_player is not None or not self._first_player_name:
            return
        mine = [n for n, pid in self._name_player.items() if pid == self.our_id]
        if not mine:
            return
        self.first_player = (self.our_id if self._first_player_name in mine
                             else self.opp_id)

    def _bind_by_action(self, pid: int) -> None:
        """用「刚刚轮到的人打出了一张牌」反推名字 → 玩家。

        日志里玩家实体只按名字引用，没有任何一行同时给出名字和编号，
        所以只能靠行为推断：CURRENT_PLAYER=1 之后第一个出牌的人就是他。
        """
        name = self._last_cp_name
        if not name or name in self._name_player or pid not in (1, 2):
            self._last_cp_name = None
            return
        self._name_player[name] = pid
        self._last_cp_name = None
        if self._first_player_name and self._first_player_name == name:
            self.first_player = pid
            self._bind_pending_names()
        elif self._first_player_name and name != self._first_player_name:
            other = 3 - pid
            if self._first_player_name not in self._name_player:
                self._name_player[self._first_player_name] = other
                self.first_player = other
                self._bind_pending_names()

    def _fix_our_id_by_name(self) -> None:
        """名字绑好后校正「谁是我」：只有战网 ID（带 #）的那个才是玩家本人。

        GameAccountId 在练习/对人机时并不可靠（实测会把两边都给出非零值，
        甚至先给对手），但名字一定能区分：'军文武#5142' vs 'UNKNOWN'/'旅店老板'。
        """
        # 谁的手牌能看到具体牌，谁就是我方（对手手牌没有 card_id）
        visible = {1: 0, 2: 0}
        for e in self.entities.values():
            if getattr(e, "zone", None) == ZONE_HAND and getattr(e, "card_id", None):
                ctrl = getattr(e, "controller", None)
                if ctrl in (1, 2):
                    visible[ctrl] += 1
        only = [p for p, n in visible.items() if n > 0 and visible[3 - p] == 0]
        if len(only) != 1:
            return
        pid = only[0]
        # 只有「当前认定的我方手牌一张都看不到」时才翻，避免对手回合来回跳
        if visible.get(self.our_id, 0) > 0:
            return
        # 一旦按可见手牌定过身份，就不再翻：中途翻面会让建议忽有忽无
        if getattr(self, "_id_by_hand_done", False):
            return
        if self.our_id != pid:
            self.our_id, self.opp_id = pid, 3 - pid
            self._id_by_hand_done = True
            self._note(f"按可见手牌校正：我方是玩家 {pid}")

    def _bind_pending_names(self) -> None:
        self._fix_our_id_by_name()
        """先手确定后，把之前没认出来的名字按出场顺序绑上。"""
        if self.first_player not in (1, 2):
            return
        if not self._first_player_name or self._first_player_name not in self._name_player:
            return          # 先手是谁还不知道，别按出场顺序猜（容易把两个人绑反）
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
        if not known and name not in self.unmodelled:
            self.unmodelled.append(name)
        return True

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
        if new != ZONE_PLAY:
            e.zone_pos = None      # 离场就清掉位置，避免下次入场时带着旧位置
        if new == ZONE_PLAY and e.zone != prev:
            self.played_turn[e.id] = self.turn
            if e.controller == self.opp_id:
                self._bind_by_action(e.controller)
                name = self._name(e.card_id)
                if name:
                    if self._push_feed("opp", e.card_id, name, e.id):
                        # 衍生物（召唤出来的随从、幸运币）不在对手卡组里，不能进
                        # revealed —— 否则学出来的原型会变成「40 张的卡组」，
                        # 而且靠「牌多所以解释得多」赢下覆盖率比较。
                        # 判据复用 cards.is_token，和预测侧口径保持一致。
                        if not (self.cards and self.cards.is_token(e.card_id)):
                            self.revealed_opp[e.card_id or "?"] += 1
                            if e.card_id not in self.revealed_opp_order:
                                self.revealed_opp_order.append(e.card_id or "?")
                                self.revealed_turns[e.card_id or "?"] = self.turn
                        self._note(f"对手打出 {name}")
            elif e.controller == self.our_id:
                self._bind_by_action(e.controller)
                name = self._name(e.card_id)
                if name:
                    if self._push_feed("our", e.card_id, name, e.id):
                        self._note(f"我方打出 {name}")
                        if e.card_id:
                            self.our_played.append(e.card_id)
                            self.turn_actions.setdefault(self.turn, []).append(f"打出 {name}")
        elif new == ZONE_GRAVE:
            if e.controller == self.opp_id:
                self._track_visibility(e)

    def _on_choice_sent(self, ev: pl.Event) -> None:
        """玩家提交的选择。克苏恩四技能只在这里能知道「到底选了哪个」。"""
        src = self._choice_source or {}
        # 不管是谁触发的，用掉一个技能就记一次：
        # 这样本体触发时「四个技能各用过一次」才可能成立，从而翻四倍。
        # （触发者本身是不是本体，只影响「这一次是否翻倍」，不影响计数）
        for ref in ev.data.get("refs") or []:
            cid = ref.get("card_id") or ""
            card = self.cards.get(cid) if (self.cards and cid) else None
            name = card.name if card else ""
            if name in self.CTHUN_SKILLS:
                self.cthun_used.add(name)

    def _on_choices(self, ev: pl.Event) -> None:
        """发现/抉择/弃牌：记录待选选项。MULLIGAN 由留牌模块单独处理。"""
        d = ev.data
        # 选择块里同时有「谁在选」和「选项属于哪个玩家」——
        # 这是日志里唯一能直接把名字和编号对上号的地方（留牌时就有）
        pname = d.get("player_name")
        if pname:
            self._choice_player_name = pname      # 选项行本身没有 Player=，要沿用上一行
        refs = d.get("refs") or []
        # 「UNKNOWN HUMAN PLAYER」这类占位名不是玩家，绑上去会占掉另一个编号，
        # 让「新名字只能是另一个人」的推断失效，后面真正的名字只能靠猜
        # （实测两个名字都指向玩家 1，整局我方/对方回合判反）。真战网 ID 一定带 #。
        if (refs and self._choice_player_name and "#" in self._choice_player_name
                and self._choice_player_name not in self._name_player):
            ctrls = [r.get("controller") for r in refs if r.get("controller") in (1, 2)]
            if ctrls and len(set(ctrls)) == 1:
                pid = ctrls[0]
                self._name_player[self._choice_player_name] = pid
                self._note(f"识别玩家：{self._choice_player_name} = 玩家{pid}")
                if self._first_player_name == self._choice_player_name:
                    self.first_player = pid
                elif self._first_player_name and self._first_player_name not in self._name_player:
                    self._name_player[self._first_player_name] = 3 - pid
                    self.first_player = 3 - pid
                self._bind_pending_names()

        if d.get("source"):
            self._choice_source = d["source"]

        ctype = d.get("choice_type")
        if ctype == "MULLIGAN":
            return
        if ctype:
            self.choice = {"type": ctype, "min": d.get("min") or 1,
                           "max": d.get("max") or 1, "options": []}
        if self.choice is None:
            return
        # 选项候选：优先用「备用区/手牌」里的实体。
        # 打出触发牌时，那张牌自己也在 PLAY 区并被客户端列进来 —— 它不是选项。
        cand = [r for r in refs
                if r.get("card_id") and r.get("zone") in (ZONE_SETASIDE, ZONE_HAND)]
        if not cand:
            cand = [r for r in refs
                    if r.get("card_id") and r.get("zone") == ZONE_PLAY]
        src_id = (self._choice_source or {}).get("id")
        for ref in cand:
            # 触发源自己（本体）会被客户端列进选项里，它不是可选项
            if src_id and ref.get("id") == src_id:
                continue
            cid = ref.get("card_id")
            entry = {"id": cid, "name": self._name(cid), "eid": ref.get("id")}
            if all(o["eid"] != entry["eid"] for o in self.choice["options"]):
                self.choice["options"].append(entry)

    def _sync_ref(self, ref: dict[str, Any]) -> None:
        """把一行里看到的实体引用同步进状态（选择/目标行的 zone、cardId 是权威的）。"""
        eid = ref.get("id")
        if not eid:
            return
        e = self._entity(eid)
        if ref.get("card_id"):
            e.card_id = ref["card_id"]
        if ref.get("controller") is not None:
            e.controller = _int(ref["controller"])
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
            e.zone_pos = _int(ref["zone_pos"])
        self._track_visibility(e)

    def _on_block_start(self, ev: pl.Event) -> None:
        self._block_depth += 1
        d = ev.data
        self._last_block = d
        # 出牌/攻击/英雄技能都会开 block；block 的实体引用里带 controller，
        # 正好可以把「刚才 CURRENT_PLAYER=1 的那个名字」绑定到玩家上。
        ctrl = (d.get("ref") or {}).get("controller")
        if ctrl in (1, 2):
            self._bind_by_action(int(ctrl))
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
    def effective_turn_owner(self) -> int | None:
        """当前该谁行动。日志没给 CURRENT_PLAYER 时，用「先手 + 回合奇偶」推。

        回合 1 属于先手，之后严格交替，所以 turn 的奇偶足以确定归属。
        返回 None 表示既没有事件、也没有先手信息，真的不知道。
        """
        if self.turn_owner in (1, 2):
            return self.turn_owner
        if self.first_player in (1, 2) and self.turn:
            return self.first_player if self.turn % 2 == 1 else 3 - self.first_player
        return None

    def sides(self) -> tuple[Side, Side]:
        """(我方, 对方)。统一入口，避免外部到处写 _side(self.our_id)。"""
        self._fix_our_id_by_name()
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

    def _name(self, card_id: str | None) -> str | None:
        if not card_id:
            return None
        if self.cards:
            c = self.cards.get(card_id)
            if c:
                return c.name
        return card_id

    def _grave_view(self, e) -> dict[str, Any]:
        """墓地里的一只随从。用卡面原始数值，因为死掉的实体标签会被清零。"""
        card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
        text = (card.text if card else "") or ""
        return {
            "id": e.card_id,
            "name": card.name if card else self._name(e.card_id),
            "cost": card.cost if card else 0,
            "atk": card.atk if card else 0,
            "hp": card.health if card else 0,
            "text": text,
            "deathrattle": ("亡语" in text) or bool(card and card.has("DEATHRATTLE")),
            "dropped_turn": (self.grave_meta.get(e.id) or (0, 0))[0],
            "seq": (self.grave_meta.get(e.id) or (0, 0))[1],
        }

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
                # 幸运币这类会给「临时法力」，不算进上限但可用来出牌
                temp = _int(pe.tags.get(T_TEMP_RESOURCES)) or 0
                s.mana, s.max_mana = max(0, res - used + temp), res
                s.fatigue = _int(pe.tags.get("FATIGUE")) or 0
        if s.max_mana == 0:
            s.max_mana, s.mana = self._mana_from_turn(pid)

        hand, deck, board, secrets, grave, weapon, hp = [], [], [], [], [], None, None
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
            elif e.zone == ZONE_GRAVE:
                card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
                if card is not None and card.is_minion:
                    grave.append(e)
            elif e.zone == ZONE_SECRET:
                secrets.append(self._card_view(e))

        hand.sort(key=lambda x: (x.zone_pos or 0, x.id))
        board.sort(key=lambda x: (x.zone_pos or 0, x.id))
        s.hand = [self._card_view(e) for e in hand if e.card_id]
        s.hand_count = len(hand)
        s.board = [self._card_view(e) for e in board if self._is_minion(e)]
        # 牌库数首选实体计数：初始牌库实体集合里 zone 还是 DECK 的个数，
        # 起手 3/4 张在换牌阶段就离开了 DECK，天然计入。
        # 没赶上 CREATE_GAME（中途启动/日志片段）才退回「卡组张数 − 已抽到」估算。
        deck_ids = self._deck_ids.get(pid)
        if deck_ids:
            s.deck_count = sum(
                1 for eid in deck_ids
                if self.entities.get(eid) is not None
                and self.entities[eid].zone == ZONE_DECK
            )
        else:
            drawn = self.our_drawn if pid == self.our_id else self.opp_drawn
            if pid != self.our_id and "opp" not in self._draw_seen:
                # 对手玩家实体整局没有 NUM_CARDS_DRAWN_THIS_TURN：只能按
                # 30 − 手牌 − 场面 粗估（墓地/奥秘/疲劳都没算），标成不可靠，
                # 别让预判把恒 30 或这个粗估当成真实牌库。
                drawn = min(self.deck_size, len(hand) + len(board))
                self.opp_drawn = drawn
                self._opp_drawn_estimated = True
                s.deck_estimated = True
            s.deck_count = max(0, self.deck_size - drawn)
        s.graveyard = [self._grave_view(e) for e in grave if e.card_id]
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

    CTHUN_SKILLS = ("克苏恩的愤怒", "克苏恩的领域", "克苏恩的凝视", "克苏恩的仆从")

    def note_cthun_choice(self, names) -> None:
        """从「选择并触发克苏恩技能」的选项里记录已经用过哪几个。"""
        offered = set()
        for name in names or ():
            text = str(name or "")
            for skill in self.CTHUN_SKILLS:
                if skill in text:
                    offered.add(skill)
        # 同一个克苏恩的技能不能重复选，所以「没出现在选项里」= 已经用过。
        # 之前把「选项里出现的」当成用过的，四个技能一出现就全记上了，
        # 结果第一次触发就显示强化 ×4。
        if len(offered) >= 2:
            self.cthun_used = {s for s in self.CTHUN_SKILLS if s not in offered}

    def cthun_view(self) -> dict[str, Any]:
        """强化看的是「哪一只克苏恩」，不同实例各自计数。"""
        key = None
        for e in self.entities.values():
            if e.zone == ZONE_PLAY and e.controller == self.our_id:
                card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
                if card is not None and card.id == "BE_013":
                    key = e.id
                    break
        if key is None:
            key = self._choice_source.get("id")
        used = set(self.cthun_used)
        ordered = [s for s in self.CTHUN_SKILLS if s in used]
        return {"used": ordered, "count": len(ordered), "total": 4,
                "tripled": len(ordered) >= 4}

    def _card_view(self, e: Entity) -> dict[str, Any]:
        card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
        base_hp = card.health if card else 0
        # 当前血量 = 上限（HEALTH 标签或卡面）- 累计伤害（DAMAGE）
        # 之前只看 HEALTH，受伤的随从会显示成满血
        limit = _int(e.tags.get(T_HEALTH))
        if limit is None:
            limit = base_hp or 0
        hp = max(0, limit - (e.damage or 0))
        atk = e.atk
        if atk is None:
            atk = card.atk if card else 0
        view: dict[str, Any] = {
            "id": e.card_id or "",
            "entity_id": e.id,
            "name": card.name if card else (e.card_id or "?"),
            "cost": (_int(e.tags.get("COST")) if _int(e.tags.get("COST")) is not None
                     else (card.cost if card else 0)),
            "type_cn": card.type_cn if card else "",
            "atk": atk,
            "health": max(0, hp),
            "durability": e.tags.get("DURABILITY"),
            "dormant": e.tags.get("DORMANT") in (1, "1", True),
            "attackable": e.tags.get("ATTACKABLE"),
            "woke_turn": self.woke_turn.get(e.id),
            "attacks_used": _int(e.tags.get(T_ATTACKS_THIS_TURN)) or 0,
            "played_turn": self.played_turn.get(e.id),
            "turn_now": self.turn,
            "keyword": card.keyword_cn if card else "",
            "damage": e.damage,
            "taunt": bool(card.has("TAUNT") if card else e.tags.get(T_TAUNT)),
            "divine_shield": bool(card.has("DIVINE_SHIELD") if card else e.tags.get(T_DIVINE_SHIELD)),
            "can_attack": self.can_attack(e),
            "exhausted": e.tags.get("EXHAUSTED") in (1, "1", True),
            "text": _clean_text(card.text if card else ""),
            "effect": _cached_effect(self, e.card_id),
            "fresh": self.played_turn.get(e.id) == self.turn,
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
        # turn_owner 在日志里经常是 None，用推断出来的归属，否则谁都攻击不了
        if self.effective_turn_owner() != e.controller:
            return False
        card = self.cards.get(e.card_id) if (self.cards and e.card_id) else None
        atk = e.atk if e.atk is not None else (card.atk if card else 0)
        if atk <= 0:
            return False
        # 客户端给了明确状态就以它为准（比拿 EXHAUSTED 推断准）
        if e.tags.get("ATTACKABLE") is not None:
            return bool(_int(e.tags.get("ATTACKABLE")) or 0)
        # 不能用 EXHAUSTED 直接判死：有些随从在手牌阶段就被打了 EXHAUSTED=1，
        # 进场后客户端不再下发 0，会一直「不能攻击」。改成按「入场回合 + 本回合是否攻击过」。
        played = self.played_turn.get(e.id)
        if played is not None and played >= self.turn:
            return False
        if e.id in self.attacked_this_turn:
            return False
        if e.tags.get("FROZEN") in (1, "1", True):
            return False
        limit = 2 if (card is not None and card.has("WINDFURY")) else 1
        if (e.tags.get(T_ATTACKS_THIS_TURN) or 0) >= limit:
            return False
        return True

    # ------------------------------------------------------------ 对外快照
    def snapshot(self) -> dict[str, Any]:
        # 视图和 advisor 必须看到同一套身份：sides() 会校正，snapshot() 以前不会
        self._fix_our_id_by_name()
        return {
            "phase": self.phase,
            "turn": self.turn,
            # 日志里 turn_owner 经常是 None，直接比它会把「我方回合」判成对方回合
            "is_our_turn": self.effective_turn_owner() == self.our_id,
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
        mine = self.effective_turn_owner() == self.our_id
        return (
            f"T{self.turn} {'我方' if mine else '对方'}回合 | "
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


