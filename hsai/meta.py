"""卡组原型（archetype）库。

一个 archetype = 职业 + 一组「标志性卡牌 + 张数」。它不需要是完整的 30 张卡组：
推断只需知道「哪些牌在这个卡组里出现、出现几张」。剩余槽位用职业牌池平滑补齐。

数据来源有三种，按优先级：
1. `data/meta.json` —— 本工具的实际数据（用户导入 / 自举聚类生成）
2. 内置种子（SEED）—— 用长期存在的真实卡牌拼出的示例原型，**不是当前天梯强度榜**
3. `hsai meta build` —— 从你自己的对局记录里聚类出来的本地环境
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .cards import CLASS_CN, CardDB
from .paths import META_JSON

META_PATH = META_JSON

# 一副合法卡组最多 40 张（雷诺/40 张模式）。超过这个数的原型不是「一套牌」，
# 多半是两套牌被聚类合并了，或者把衍生物当成了卡组牌 —— 这种模板会靠
# 「牌多所以解释得多」赢下覆盖率比较，不该参与判断。
DECK_MAX_CARDS = 40

# 卡牌 id 形如 CORE_CS2_029 / NEW1_025，用来区分「这是 id 还是中文名」
@dataclass
class Archetype:
    id: str
    name: str
    card_class: str
    prior: float
    cards: dict[str, int] = field(default_factory=dict)  # card_id -> 张数
    tags: list[str] = field(default_factory=list)
    source: str = "seed"
    note: str = ""
    # 看到「不在这套牌里」的牌时给多大概率。普通卡组极低（几乎不可能），
    # 但「备阵/偷牌/复制」类卡组会真的打出对手的牌，必须放宽，否则会被误杀。
    unexpected: float = 1e-4

    @property
    def class_cn(self) -> str:
        return CLASS_CN.get(self.card_class, self.card_class)

    def total(self) -> int:
        return sum(self.cards.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "class": self.card_class,
            "prior": round(self.prior, 4),
            "cards": dict(self.cards),
            "tags": list(self.tags),
            "source": self.source,
            "note": self.note,
            "unexpected": self.unexpected,
        }


# ---------------------------------------------------------------- 内置种子
# 说明：这些原型用「多年一直存在的真实卡牌」拼出来，目的是让推断器有可解释的起点，
# 而不是宣称它们是当前环境的最强卡组。真实使用时请用 `hsai meta import` 导入，
# 或者用 `hsai meta build` 从你自己的对局里自举。张数是示意，不必等于实战配置。
SEED: list[dict[str, Any]] = [
    {"id": "tempo_mage", "name": "节奏法", "class": "MAGE", "prior": 0.55,
     "tags": ["快攻", "节奏"],
     "cards": {"法力浮龙": 2, "寒冰箭": 2, "火球术": 2, "水元素": 2, "奥术智慧": 1, "工程师学徒": 2}},
    {"id": "control_mage", "name": "控制法", "class": "MAGE", "prior": 0.45,
     "tags": ["控制", "清场"],
     "cards": {"烈焰风暴": 2, "暴风雪": 2, "变形术": 2, "火球术": 2, "冰霜新星": 1, "憎恶": 1}},
    {"id": "tempo_warrior", "name": "节奏战", "class": "WARRIOR", "prior": 0.5,
     "tags": ["节奏", "武器"],
     "cards": {"血帆海盗": 2, "暴乱狂战士": 2, "黑铁矮人": 2, "盾牌猛击": 1, "格罗玛什·地狱咆哮": 1}},
    {"id": "control_warrior", "name": "控制战", "class": "WARRIOR", "prior": 0.5,
     "tags": ["控制", "护甲"],
     "cards": {"绝命乱斗": 2, "斩杀": 2, "盾牌猛击": 2, "憎恶": 1, "石拳食人魔": 2}},
    {"id": "aggro_hunter", "name": "快攻猎", "class": "HUNTER", "prior": 0.6,
     "tags": ["快攻", "打脸"],
     "cards": {"麻风侏儒": 2, "飞刀杂耍者": 2, "关门放狗": 2, "杀戮命令": 2, "鹰角弓": 2}},
    {"id": "midrange_hunter", "name": "中速猎", "class": "HUNTER", "prior": 0.4,
     "tags": ["中速", "野兽"],
     "cards": {"驯兽师": 2, "苔原犀牛": 2, "杀戮命令": 2, "冰风雪人": 2, "鹰角弓": 1}},
    {"id": "zoo_warlock", "name": "动物园术", "class": "WARLOCK", "prior": 0.55,
     "tags": ["快攻", "铺场"],
     "cards": {"烈焰小鬼": 2, "虚空行者": 2, "恐狼前锋": 2, "阿古斯防御者": 2}},
    {"id": "control_warlock", "name": "控制术", "class": "WARLOCK", "prior": 0.45,
     "tags": ["控制", "后期"],
     "cards": {"虚空领主": 1, "憎恶": 1, "石拳食人魔": 2, "血法师萨尔诺斯": 1, "冰风雪人": 2}},
    {"id": "control_priest", "name": "控制牧", "class": "PRIEST", "prior": 1.0,
     "tags": ["控制", "治疗"],
     "cards": {"暗言术：灭": 2, "神圣新星": 2, "血法师萨尔诺斯": 1, "冰风雪人": 2, "憎恶": 1}},
    {"id": "tempo_rogue", "name": "节奏贼", "class": "ROGUE", "prior": 1.0,
     "tags": ["节奏", "连击"],
     "cards": {"背刺": 2, "军情七处特工": 2, "血帆海盗": 2, "工程师学徒": 2}},
    {"id": "ramp_druid", "name": "跳费德", "class": "DRUID", "prior": 1.0,
     "tags": ["跳费", "中速"],
     "cards": {"野性成长": 2, "横扫": 2, "石拳食人魔": 2, "冰风雪人": 2}},
    {"id": "midrange_paladin", "name": "中速骑", "class": "PALADIN", "prior": 1.0,
     "tags": ["中速", "铺场"],
     "cards": {"奉献": 2, "银色侍从": 2, "阿古斯防御者": 2, "黑铁矮人": 2}},
    {"id": "midrange_shaman", "name": "中速萨", "class": "SHAMAN", "prior": 1.0,
     "tags": ["中速", "法术"],
     "cards": {"闪电箭": 2, "石化武器": 2, "火元素": 2, "飞刀杂耍者": 2}},
    {"id": "aggro_dh", "name": "快攻瞎", "class": "DEMONHUNTER", "prior": 1.0,
     "tags": ["快攻", "打脸"],
     "cards": {"双刃斩击": 2, "邪能弹幕": 2, "麻风侏儒": 2}},
    {"id": "frost_dk", "name": "冰霜DK", "class": "DEATHKNIGHT", "prior": 1.0,
     "tags": ["中速", "法术"],
     "cards": {"冰霜打击": 2, "铁喙猫头鹰": 1, "冰风雪人": 2}},
]


def _resolve_cards(raw: dict[str, int], cards: CardDB) -> dict[str, int]:
    """把「中文名或 id -> 张数」统一成 id -> 张数，丢弃数据库里找不到的。"""
    out: dict[str, int] = {}
    for key, count in raw.items():
        card = cards.by_name_or_id(key)
        if card is None:
            continue
        out[card.id] = out.get(card.id, 0) + int(count)
    return out


def seeds(cards: CardDB) -> list[Archetype]:
    out: list[Archetype] = []
    for spec in SEED:
        resolved = _resolve_cards(spec["cards"], cards)
        if not resolved:
            continue
        out.append(
            Archetype(
                id=spec["id"],
                name=spec["name"],
                card_class=spec["class"],
                prior=float(spec.get("prior", 1.0)),
                cards=resolved,
                tags=list(spec.get("tags", [])),
                source="seed",
            )
        )
    return out


class MetaDB:
    def __init__(self, archetypes: Iterable[Archetype]) -> None:
        self.archetypes: list[Archetype] = list(archetypes)
        self.normalize_priors()

    # ------------------------------------------------------------ 基本操作
    def __len__(self) -> int:
        return len(self.archetypes)

    def __iter__(self):
        return iter(self.archetypes)

    def normalize_priors(self) -> None:
        """按职业内部归一化，保证「同职业里选哪个卡组」是一个概率分布。"""
        by_class: dict[str, list[Archetype]] = {}
        for a in self.archetypes:
            by_class.setdefault(a.card_class, []).append(a)
        for group in by_class.values():
            total = sum(max(a.prior, 0.0) for a in group) or 1.0
            for a in group:
                a.prior = max(a.prior, 0.0) / total

    def for_class(self, card_class: str) -> list[Archetype]:
        return [a for a in self.archetypes if a.card_class == card_class]

    def get(self, archetype_id: str) -> Archetype | None:
        return next((a for a in self.archetypes if a.id == archetype_id), None)

    def upsert(self, arch: Archetype) -> None:
        for i, a in enumerate(self.archetypes):
            if a.id == arch.id:
                self.archetypes[i] = arch
                break
        else:
            self.archetypes.append(arch)
        self.normalize_priors()

    # ------------------------------------------------------------ 持久化
    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "archetypes": [a.as_dict() for a in self.archetypes],
        }

    def save(self, path: Path = META_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> MetaDB:
        out = []
        for d in payload.get("archetypes", []):
            out.append(
                Archetype(
                    id=d["id"],
                    name=d.get("name") or d["id"],
                    card_class=d.get("class") or "NEUTRAL",
                    prior=float(d.get("prior", 1.0)),
                    cards={k: int(v) for k, v in (d.get("cards") or {}).items()},
                    tags=list(d.get("tags") or []),
                    source=d.get("source") or "file",
                    note=d.get("note") or "",
                    unexpected=float(d.get("unexpected", 1e-4)),
                )
            )
        return cls(out)

    @classmethod
    def load(cls, cards: CardDB, path: Path = META_PATH) -> MetaDB:
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                db = cls.from_json(payload)
                if len(db):
                    return db
            except (OSError, ValueError, KeyError):
                pass
        db = cls(seeds(cards))
        try:
            db.save(path)
        except OSError:
            pass
        return db


# ---------------------------------------------------------------- 自举聚类
def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def build_from_observations(
    observations: list[dict[str, Any]],
    cards: CardDB,
    *,
    min_games: int = 3,
    threshold: float = 0.34,
) -> MetaDB:
    """从「你自己的对局记录」里聚类出本地环境卡组。

    observations: [{"opp_class": "WARRIOR", "revealed": ["CORE_EX1_604", ...]}, ...]
    只用已经观测到的牌做 Jaccard 相似度，做单链凝聚聚类。
    这一个函数就是「不依赖任何外部网站也能跟上环境」的实现。
    """
    groups_by_class: dict[str, list[set[str]]] = {}
    for obs in observations:
        cls = (obs.get("opp_class") or "").upper()
        # schema 2 之后 revealed 是 [[card_id, 回合], ...]，老数据是 [card_id, ...]，
        # 两种都要能读 —— 否则升级一次就把历史样本全废了。
        revealed = {str(item[0] if isinstance(item, (list, tuple)) else item)
                    for item in (obs.get("revealed") or []) if item}
        if not cls or len(revealed) < 2:
            continue
        groups_by_class.setdefault(cls, []).append(revealed)

    archetypes: list[Archetype] = []
    for cls, samples in groups_by_class.items():
        clusters: list[list[set[str]]] = []
        for sample in samples:
            best_i, best_sim = -1, 0.0
            for i, cluster in enumerate(clusters):
                sim = max(_jaccard(sample, other) for other in cluster)
                if sim > best_sim:
                    best_i, best_sim = i, sim
            if best_i >= 0 and best_sim >= threshold:
                clusters[best_i].append(sample)
            else:
                clusters.append([sample])
        clusters.sort(key=len, reverse=True)
        total = sum(len(c) for c in clusters) or 1
        for idx, cluster in enumerate(clusters):
            if len(cluster) < min_games:
                continue
            freq: dict[str, int] = {}
            for sample in cluster:
                for cid in sample:
                    freq[cid] = freq.get(cid, 0) + 1
            # 出现率 >= 50% 视为标志牌；但「幸运币」这种人人可能有的牌要排除，
            # 否则学出来的原型名字会变成「死亡骑士·战场通灵师/幸运币」这种怪东西
            noisy = {"GAME_005"}
            signature = {cid: min(2, max(1, round(n / len(cluster) * 2))) for cid, n in freq.items()
                         if n / len(cluster) >= 0.5 and cid not in noisy}
            if not signature:
                continue
            # 牌数超过合法卡组上限，说明这个簇其实是两套牌被单链聚类串到了一起，
            # 或者把衍生物也数了进去。留着它只会污染判断，不如整簇放弃。
            if sum(signature.values()) > DECK_MAX_CARDS:
                continue
            top = sorted(signature, key=lambda c: -freq[c])[:2]
            label = cards.display_name(top[0]) if top else "新形态"
            archetypes.append(
                Archetype(
                    id=f"{cls.lower()}_{idx}",
                    name=f"{CLASS_CN.get(cls, cls)}·{label}",
                    card_class=cls,
                    prior=len(cluster) / total,
                    cards=signature,
                    tags=["本地环境"],
                    source="bootstrap",
                    note=f"{len(cluster)} 局样本聚类",
                )
            )

    if not archetypes:
        return MetaDB(seeds(cards))
    return MetaDB(archetypes)


def import_archetypes(payload: dict[str, Any], cards: CardDB, db: MetaDB | None = None) -> MetaDB:
    """导入外部卡组表（例如从第三方站点导出的 json）。

    payload 形如：
        {"archetypes": [{"name": "节奏战", "class": "WARRIOR", "share": 12.5,
                         "cards": {"血帆海盗": 2, ...}}]}
    share 是使用率百分比，会被归一化成先验。
    """
    db = db or MetaDB(seeds(cards))
    incoming = payload.get("archetypes") or []
    total_share = sum(float(a.get("share", 1.0)) for a in incoming) or 1.0
    for i, a in enumerate(incoming):
        resolved = _resolve_cards(a.get("cards") or {}, cards)
        if not resolved:
            continue
        cls = (a.get("class") or "NEUTRAL").upper()
        db.upsert(
            Archetype(
                id=a.get("id") or f"import_{i}",
                name=a.get("name") or f"导入卡组 {i}",
                card_class=cls,
                prior=float(a.get("share", 1.0)) / total_share,
                cards=resolved,
                tags=list(a.get("tags") or []),
                source="import",
            )
        )
    db.normalize_priors()
    return db


