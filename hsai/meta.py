"""卡组原型（archetype）库。

一个 archetype = 职业 + 一组「标志性卡牌 + 张数」。它不需要是完整的 30 张卡组：
推断只需知道「哪些牌在这个卡组里出现、出现几张」。剩余槽位用职业牌池平滑补齐。

存储：**一个原型一个文件**，放在 `data/meta/<id>.json`。
单个 meta.json 要改一个原型就得重写整份，也没法手工增删一套牌。
老的单文件格式会在第一次加载时自动拆开，原文件改名留档。

来源两种：
1. 导入（`hsai deck import` / `hsai meta import`）—— 外部卡组表
2. 自举（`hsai meta rebuild`）—— 从你自己的对局记录聚类出来的本地环境

内置的示例原型（SEED）已经删掉了：它们不是真实天梯数据，却和真实卡组一起
参与概率比较，等于给推断掺了噪声。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .cards import CLASS_CN, CardDB
from .paths import META_DIR, META_JSON

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
    source: str = "import"
    note: str = ""
    # 看到「不在这套牌里」的牌时给多大概率。普通卡组极低（几乎不可能），
    # 但「备阵/偷牌/复制」类卡组会真的打出对手的牌，必须放宽，否则会被误杀。
    unexpected: float = 1e-4
    # 校准前的先验。`meta calibrate` 每次都从它重新混合，而不是在当前 prior 上
    # 再混一次 —— 否则同一个命令跑三遍，先验会被逐步「洗」成观测频率。
    # None 表示老文件没这个字段，退化成用当前的 prior。
    prior_base: float | None = None

    @property
    def class_cn(self) -> str:
        return CLASS_CN.get(self.card_class, self.card_class)

    @property
    def base_prior(self) -> float:
        """校准的起点：导入/聚类时给的原始先验。"""
        return self.prior if self.prior_base is None else self.prior_base

    def total(self) -> int:
        return sum(self.cards.values())

    def as_dict(self, cards: CardDB | None = None) -> dict[str, Any]:
        """写成能直接读的文件。

        `cards` 写成带名称和费用的数组并按费用升序 —— 光看 id 和数字没法核，
        而费用顺序正好是出牌的先后顺序，扫一眼就知道这套牌怎么展开。
        没有 CardDB 时退化成只有 id 和 count（依然合法，能再读回来）。
        """
        return {
            "id": self.id,
            "name": self.name,
            "class": self.card_class,
            "prior": round(self.prior, 4),
            "prior_base": round(self.base_prior, 4),
            "cards": _cards_to_json(self.cards, cards),
            "tags": list(self.tags),
            "source": self.source,
            "note": self.note,
            "unexpected": self.unexpected,
        }


def _resolve_cards(raw: dict[str, int], cards: CardDB) -> dict[str, int]:
    """把「中文名或 id -> 张数」统一成 id -> 张数，丢弃数据库里找不到的。"""
    out: dict[str, int] = {}
    for key, count in raw.items():
        card = cards.by_name_or_id(key)
        if card is None:
            continue
        out[card.id] = out.get(card.id, 0) + int(count)
    return out


def _cards_to_json(cards: dict[str, int], db: CardDB | None) -> list[dict[str, Any]]:
    """`{id: 张数}` -> 带名称和费用的数组，按费用升序。

    光有 id 和张数没法人工核对；费用顺序又正好是这套牌的展开顺序，
    扫一眼就知道它是几费曲线。没有 CardDB 时退化成只有 id 和 count。
    """
    rows = []
    for cid, count in cards.items():
        card = db.get(cid) if db else None
        rows.append({
            "id": cid,
            "name": getattr(card, "name", "") or "",
            "cost": getattr(card, "cost", None),
            "count": int(count),
        })
    # 费用升序，同费用按名称再按 id —— 保证同一份数据每次写出来逐字一样
    rows.sort(key=lambda r: (r["cost"] if isinstance(r["cost"], int) else 99,
                             r["name"], r["id"]))
    return rows


def _cards_from_json(raw: Any) -> dict[str, int]:
    """读两种写法：新的是 `[{id,name,cost,count}]`，老的是 `{id: 张数}`。"""
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items()}
    out: dict[str, int] = {}
    for row in raw or []:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        cid = str(row["id"])
        out[cid] = out.get(cid, 0) + int(row.get("count") or 1)
    return out


def _archetype_from_dict(d: dict[str, Any]) -> Archetype | None:
    if not isinstance(d, dict) or not d.get("id"):
        return None
    base = d.get("prior_base")
    return Archetype(
        id=str(d["id"]),
        name=d.get("name") or str(d["id"]),
        card_class=d.get("class") or "NEUTRAL",
        prior=float(d.get("prior", 1.0)),
        cards=_cards_from_json(d.get("cards") or []),
        tags=list(d.get("tags") or []),
        source=d.get("source") or "file",
        note=d.get("note") or "",
        unexpected=float(d.get("unexpected", 1e-4)),
        # 老文件没有这个字段 —— 留 None，校准时会退化用当前 prior 当起点
        prior_base=float(base) if base is not None else None,
    )


def _file_stem(archetype_id: str) -> str:
    """id 直接当文件名，只把路径分隔符之类换成下划线。"""
    return re.sub(r"[^0-9A-Za-z_.-]", "_", archetype_id) or "archetype"


class MetaDB:
    def __init__(self, archetypes: Iterable[Archetype],
                 cards: CardDB | None = None) -> None:
        self.archetypes: list[Archetype] = list(archetypes)
        # 存一份 CardDB 引用：写文件时要把 id 翻成名称和费用。
        # 没有它也能存，只是退化成只有 id + count。
        self.cards = cards
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
            "version": 2,
            "archetypes": [a.as_dict(self.cards) for a in self.archetypes],
        }

    def save(self, path: Path = META_DIR) -> None:
        """一个原型一个文件：`<path>/<id>.json`。

        写完再清掉目录里已经不在内存中的文件 —— 否则删掉的原型会留在磁盘上，
        下次加载又冒出来。
        """
        path.mkdir(parents=True, exist_ok=True)
        keep: set[str] = set()
        for a in self.archetypes:
            f = path / f"{_file_stem(a.id)}.json"
            keep.add(f.name)
            f.write_text(
                json.dumps(a.as_dict(self.cards), ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
            )
        for f in path.glob("*.json"):
            if f.name in keep:
                continue
            # 只删「能解析成原型」的文件：手改出错、JSON 写坏的文件要留着给人看，
            # 静默删掉等于吞掉用户的数据
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if _archetype_from_dict(payload) is None:
                continue
            try:
                f.unlink()
            except OSError:
                pass

    @classmethod
    def from_json(cls, payload: dict[str, Any],
                  cards: CardDB | None = None) -> MetaDB:
        """读老的「整份 meta.json」结构（迁移和 import 用）。"""
        out = [a for a in (_archetype_from_dict(d)
                           for d in payload.get("archetypes", [])) if a]
        return cls(out, cards)

    @classmethod
    def load(cls, cards: CardDB, path: Path = META_DIR) -> MetaDB:
        if path.is_dir():
            return cls(_load_dir(path), cards)
        if path.exists():
            return cls._migrate(path, cards)
        if path == META_DIR and META_JSON.exists():
            # 默认路径下目录还没建，但老的单文件在 —— 第一次加载时拆开
            return cls._migrate(META_JSON, cards)
        return cls([], cards)

    @classmethod
    def _migrate(cls, legacy: Path, cards: CardDB) -> MetaDB:
        """把老的单文件拆成一堆文件，原文件改名留档，不删。

        目标目录由老文件的路径推出来（`foo/meta.json` -> `foo/meta/`），不能写死
        `META_DIR` —— 否则测试或 advisor 传了自定义路径，数据会落到真实 data 目录里。
        """
        try:
            payload = json.loads(legacy.read_text(encoding="utf-8"))
            db = cls.from_json(payload, cards)
        except (OSError, ValueError, KeyError):
            return cls([], cards)
        if not len(db):
            return db
        try:
            db.save(legacy.with_suffix(""))
            legacy.rename(legacy.with_suffix(legacy.suffix + ".migrated"))
        except OSError:
            pass
        return db


def _load_dir(path: Path) -> list[Archetype]:
    out: list[Archetype] = []
    for f in sorted(path.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        a = _archetype_from_dict(d)
        if a is not None:
            out.append(a)
    return out


# ---------------------------------------------------------------- 命名

# 稀有度档次：数字大的优先。命名是**硬优先级**——有传说就用传说，
# 不管那张传说出现率多低、费用多便宜。
RARITY_RANK = {
    "LEGENDARY": 3,
    "EPIC": 2,
    "RARE": 1,
    "COMMON": 0,
    "FREE": -1,      # 基本牌没有稀有度，排在普通之下
}


def _copy_ratio(count: int, card) -> float:
    """张数 -> 「满编程度」0..1，用作没有真实出牌率时的替代。

    传说牌上限就是 1 张，不能因为只有 1 张就被判成边缘牌 —— 否则每套牌的
    传说牌都会被扣一半分，正好把最该当招牌的那张排掉。
    """
    cap = 1 if (card is not None and (getattr(card, "rarity", "") or "").upper() == "LEGENDARY") else 2
    return min(1.0, max(0.0, count / cap))


def key_card_rank(card, presence: float) -> tuple:
    """命名用的排序键：**先比稀有度，再比出牌率**。

    为什么不用加权乘积：乘积会把两件事混成一个数，于是「15 费的史诗」能压过
    「3 费的传说」—— 但用户要的规则是「有传说就用传说」，这是一条硬优先级，
    不该被费用乘数稀释。费用只用来打破「稀有度和出牌率都完全相同」的死局。

    返回元组而不是分数，是因为「先按 A 再按 B」本来就是字典序，写成乘积
    还得挑权重去近似它，纯属自找麻烦。
    """
    rarity = RARITY_RANK.get((getattr(card, "rarity", "") or "").upper(), 0)
    cost = getattr(card, "cost", 0)
    cost = cost if isinstance(cost, int) else 0
    return (-rarity,
            -round(presence, 6),
            -cost,                       # 仅用于打破完全相同的死局
            getattr(card, "name", "") or "",
            getattr(card, "id", "") or "")   # id 兜底，保证可复现


def pick_key_cards(deck: dict[str, int], cards: CardDB, *,
                   presence: dict[str, float] | None = None,
                   limit: int = 3) -> list[str]:
    """挑出最能代表这套牌的几张牌，最像的排在最前（返回 card_id）。

    排序规则见 `key_card_rank`：稀有度优先，同稀有度比出牌率。

    presence 不给时按张数推（满编 2 张 = 1.0）。聚类那边有真实出牌率，直接用
    真实值 —— 张数是从出牌率反推的，比它粗。
    """
    ranked = []
    for cid, count in deck.items():
        card = cards.get(cid) if cards else None
        if card is None:
            continue
        ratio = presence[cid] if presence and cid in presence else _copy_ratio(count, card)
        ranked.append((key_card_rank(card, ratio), card))
    # 排序键自带名称和 id 兜底，所以同一份数据每次排出来逐字一致 ——
    # 否则两次重建名字会变，而 opp_history 是按名字匹配的，名字一变先验就断档。
    ranked.sort(key=lambda p: p[0])
    return [c.id for _, c in ranked[:limit]]


def content_id(card_class: str, cards: dict[str, int]) -> str:
    """由标志牌集合算出稳定 id（8 位十六进制）。

    为什么不用「同职业第几个」：那个编号会随原型库的增删整体平移 ——
    删掉一个战士原型，`learned_warrior_2` 就变成 `learned_warrior_0`。
    而 `observations.jsonl` 的 `truth.arch` 是按 id 记的，历史真值当场对不上，
    先验校准会把那些局算成「这套牌从没出现过」。

    用内容哈希后，只要这套牌还是这套牌，id 就不变。
    """
    payload = "|".join(f"{cid}:{n}" for cid, n in sorted(cards.items()))
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:8]


def _name_archetype(cls: str, keys: list[str], cards: CardDB, used: set[str]) -> str:
    """`职业·关键牌`，**只取一张牌的名字**。

    撞名时不是把两张拼起来（`战士·A/B`），而是往后挪一张候选牌 —— 名字要能
    一眼读完，拼接只会让列表里每行都变长，反而更难扫。
    """
    prefix = CLASS_CN.get(cls, cls)
    for cid in keys:
        name = f"{prefix}·{cards.display_name(cid)}"
        if name not in used:
            return name
    # 候选牌全撞了（同一个职业里两套牌共用全部招牌牌）—— 加序号保证唯一，
    # 名字重复会让 opp_history 的先验记到两套牌头上。
    base = f"{prefix}·{cards.display_name(keys[0]) if keys else '新形态'}"
    i = 2
    while f"{base}#{i}" in used:
        i += 1
    return f"{base}#{i}"


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
    只用已经观测到的牌算 Jaccard 相似度，按「与簇内样本的平均相似度」凝聚聚类。
    这一个函数就是「不依赖任何外部网站也能跟上环境」的实现。
    """
    groups_by_class: dict[str, list[set[str]]] = {}
    for obs in observations:
        cls = (obs.get("opp_class") or "").upper()
        # schema 2 之后 revealed 是 [[card_id, 回合], ...]，老数据是 [card_id, ...]，
        # 两种都要能读 —— 否则升级一次就把历史样本全废了。
        #
        # 衍生物必须在这里滤掉，不能只靠采集端：老数据里召唤物和幸运币占了
        # 28%，它们会进「出现率 >= 50% 就是标志牌」的统计，把原型撑到 40 张。
        revealed: set[str] = set()
        for item in (obs.get("revealed") or []):
            cid = item[0] if isinstance(item, (list, tuple)) else item
            if cid and not cards.is_token(str(cid)):
                revealed.add(str(cid))
        if not cls or len(revealed) < 2:
            continue
        groups_by_class.setdefault(cls, []).append(revealed)

    archetypes: list[Archetype] = []
    used_names: set[str] = set()
    for cls, samples in groups_by_class.items():
        clusters: list[list[set[str]]] = []
        for sample in samples:
            best_i, best_sim = -1, 0.0
            for i, cluster in enumerate(clusters):
                # 单链会让 A~B、B~C 但 A≁C 的三套牌串成一簇，签名只剩中间那套的牌；
                # 与簇内所有样本取平均，B 就当不成桥了。
                sim = sum(_jaccard(sample, other) for other in cluster) / len(cluster)
                if sim > best_sim:
                    best_i, best_sim = i, sim
            if best_i >= 0 and best_sim >= threshold:
                clusters[best_i].append(sample)
            else:
                clusters.append([sample])
        clusters.sort(key=len, reverse=True)
        total = sum(len(c) for c in clusters) or 1
        for cluster in clusters:
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
            # 牌数超过合法卡组上限，说明这个簇其实是几套不同的牌被串到了一起，
            # 或者把衍生物也数了进去。留着它只会污染判断，不如整簇放弃。
            if sum(signature.values()) > DECK_MAX_CARDS:
                continue
            # 用关键牌命名，不是「出牌率最高的牌」：出牌率最高的是 1 费填充牌，
            # 那样会得到「战士·斩杀」这种看不出是哪套牌的名字。
            # 多取几个候选（5 个），撞名时往后挪，而不是把两张名字拼起来。
            keys = pick_key_cards(
                signature, cards, limit=5,
                presence={cid: n / len(cluster) for cid, n in freq.items() if cid in signature},
            )
            name = _name_archetype(cls, keys, cards, used_names)
            used_names.add(name)
            archetypes.append(
                Archetype(
                    id=f"{cls.lower()}_{content_id(cls, signature)}",
                    name=name,
                    card_class=cls,
                    prior=len(cluster) / total,
                    prior_base=len(cluster) / total,
                    cards=signature,
                    tags=["本地环境"],
                    source="bootstrap",
                    note=f"{len(cluster)} 局样本聚类",
                )
            )

    if not archetypes:
        return MetaDB([], cards)
    return MetaDB(archetypes, cards)


def import_archetypes(payload: dict[str, Any], cards: CardDB, db: MetaDB | None = None) -> MetaDB:
    """导入外部卡组表（例如从第三方站点导出的 json）。

    payload 形如：
        {"archetypes": [{"name": "节奏战", "class": "WARRIOR", "share": 12.5,
                         "cards": {"血帆海盗": 2, ...}}]}
    share 是使用率百分比，会被归一化成先验。
    """
    db = db or MetaDB([], cards)
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
                prior_base=float(a.get("share", 1.0)) / total_share,
                cards=resolved,
                tags=list(a.get("tags") or []),
                source="import",
            )
        )
    db.normalize_priors()
    return db


