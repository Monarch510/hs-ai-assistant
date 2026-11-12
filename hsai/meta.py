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

import json
import os
from pathlib import Path
from typing import Any, Iterable

from .cards import CardDB
from .paths import META_DIR, META_JSON

# 门面：数据类 / 命名 / 聚类拆到 meta_* 子模块，显式 re-export 维持原有 import 面。
# MetaDB 与 _load_dir 和它紧密耦合，物理留在门面里。
from .meta_types import (
    DECK_MAX_CARDS, Archetype,
    _resolve_cards, _cards_to_json, _cards_from_json, _archetype_from_dict, _file_stem,
)
from .meta_naming import (
    RARITY_RANK, _copy_ratio, key_card_rank, pick_key_cards, content_id,
    _name_archetype, _jaccard,
)
from .meta_cluster import build_from_observations, import_archetypes

class MetaDB:
    def __init__(self, archetypes: Iterable[Archetype],
                 cards: CardDB | None = None) -> None:
        self.archetypes: list[Archetype] = list(archetypes)
        # 存一份 CardDB 引用：写文件时要把 id 翻成名称和费用。
        # 没有它也能存，只是退化成只有 id + count。
        self.cards = cards
        self.normalize_priors()

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
            tmp = f.with_suffix(f.suffix + ".tmp")
            tmp.write_text(
                json.dumps(a.as_dict(self.cards), ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, f)
        for f in path.glob("*.json"):
            if f.name in keep:
                continue
            # 只删「能解析成原型」的文件：手改出错、JSON 写坏的文件要留着给人看，
            # 静默删掉等于吞掉用户的数据
            try:
                payload = json.loads(f.read_text(encoding="utf-8"))
                if _archetype_from_dict(payload) is None:
                    continue
            except (OSError, ValueError, TypeError):
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
            a = _archetype_from_dict(d)
        except (OSError, ValueError, TypeError):
            continue
        if a is not None:
            out.append(a)
    return out
