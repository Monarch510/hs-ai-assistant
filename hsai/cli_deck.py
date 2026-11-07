"""命令行：卡组：导入对手卡组库 / 设置自己的卡组。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .advisor import OUR_DECK_PATH
from .cards import DATA_DIR, CardDB
from .cli_boot import _db
from .meta import Archetype, MetaDB


def _deck_from_code(db: CardDB, code: str, name: str = ""):
    """解码卡组代码；出错时给出人话解释。"""
    from . import deckcode

    try:
        parsed = deckcode.decode(code)
    except deckcode.DeckCodeError as exc:
        print(f"卡组代码解析失败：{exc}")
        print("请确认复制的是客户端「复制卡组」出来的那串（一般以 AAEC 开头）。")
        return None
    resolved = deckcode.resolve(parsed, db, name=name)
    if resolved.unknown:
        print(f"有 {len(resolved.unknown)} 张牌在卡牌库里查不到（dbfId: {resolved.unknown[:6]}）")
        print("通常是版本太新，先跑一次 python -m hsai cards --update")
    return resolved

def cmd_deck(args: argparse.Namespace) -> int:
    db = _db()
    action = args.action

    if action == "show":
        if OUR_DECK_PATH.exists():
            print(OUR_DECK_PATH.read_text(encoding="utf-8"))
        else:
            print("尚未设置自己的卡组（python -m hsai deck mine <卡组代码>）")
        return 0

    if action == "list":
        from . import decks as decks_mod

        saved = decks_mod.load(db)
        if not saved:
            print("没读到卡组。确认游戏写过日志（Decks.log）。")
            return 2
        print(f"从 Decks.log 读到 {len(saved)} 套卡组：")
        for d in saved:
            print(f"  {d.class_cn:5s} {d.size:2d}张  {d.name}  (id {d.deck_id})")
        return 0

    if action in ("add", "import"):
        if not args.code:
            print("用法：python -m hsai deck add <卡组代码>")
            return 2
        resolved = _deck_from_code(db, args.code, args.name or "")
        if resolved is None:
            return 2
        print(f"解出卡组：{resolved.name}｜{resolved.card_class}｜{resolved.size} 张｜{resolved.format_cn}")
        for c in resolved.cards:
            print(f"  {c.cost}  {c.name} ×{resolved.counts[c.id]}")
        if resolved.size < 15:
            print(f"\n[注意] 只有 {resolved.size} 张，解出来明显不完整，先不写入库。")
            return 2
        if resolved.size < 25:
            print(f"\n[提示] 只有 {resolved.size} 张（不是满编 30），按标志牌入库，预判仍可用。")

        meta = MetaDB.load(db)
        # 用 md5 而不是内置 hash()：Python 的字符串 hash 每个进程都不一样，
        # 导致同一套牌重复导入会生成新 id、库里出现重复项。
        import hashlib

        arch_id = "deck_" + hashlib.md5(args.code.encode("utf-8")).hexdigest()[:8]
        # 新导入的牌不要压过同职业已有原型：先验取该职业现有原型的平均值
        same = [b for b in meta if b.card_class == resolved.card_class]
        prior = sum(b.prior for b in same) / len(same) if same else 1.0
        meta.upsert(
            Archetype(
                id=arch_id,
                name=args.name or resolved.name,
                card_class=resolved.card_class,
                prior=prior,
                cards=dict(resolved.counts),
                tags=["导入"],
                source="deckcode",
                unexpected=0.02 if resolved.size < 25 else 1e-4,
            )
        )
        meta.save()
        print(f"\n已加入对手卡组库（{len(meta)} 个原型）。之后遇到同样职业会用它来预判。")
        return 0

    if action == "mine":
        if not args.code:
            print("用法：python -m hsai deck mine <卡组代码>")
            return 2
        resolved = _deck_from_code(db, args.code, args.name or "")
        if resolved is None:
            return 2
        out = {
            "name": args.name or resolved.name,
            "class": resolved.card_class,
            "tags": [],
            "cards": [c.id for c in resolved.cards for _ in range(resolved.counts[c.id])],
        }
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        OUR_DECK_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"已保存我方卡组：{out['name']}（{len(out['cards'])} 张）")
        return 0

    # 兼容老用法：deck set --file my_deck.json
    if not args.file:
        print("用法：python -m hsai deck mine <卡组代码>  /  python -m hsai deck add <卡组代码>")
        return 2
    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
    resolved, missing = [], []
    for key in payload.get("cards", []):
        c = db.by_name_or_id(key)
        if c is None:
            missing.append(key)
        else:
            resolved.append(c.id)
    out = {
        "name": payload.get("name") or "我的卡组",
        "class": (payload.get("class") or "").upper(),
        "tags": payload.get("tags") or [],
        "cards": resolved,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUR_DECK_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已保存 {len(resolved)} 张牌 -> {OUR_DECK_PATH}")
    if missing:
        print("以下名字没在卡牌库里找到：" + "、".join(missing))
    return 0
