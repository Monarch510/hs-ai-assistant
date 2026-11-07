"""命令行：原型库：列表 / 导入 / 校准 / 重建。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import learn
from .cli_boot import _db
from .meta import Archetype, MetaDB, import_archetypes, pick_key_cards
from .paths import META_BACKUP_DIR, META_DIR


def _backup_meta() -> Path:
    """把 data/meta/ 整份复制到 data/meta_backup/。

    逐文件复制 + 清掉备份里多余的文件，不用 rmtree —— 备份目录是用户数据，
    重建命令不该有「先删干净再写」这种动作。
    """
    META_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    keep: set[str] = set()
    for f in META_DIR.glob("*.json"):
        (META_BACKUP_DIR / f.name).write_bytes(f.read_bytes())
        keep.add(f.name)
    for f in META_BACKUP_DIR.glob("*.json"):
        if f.name not in keep:
            try:
                f.unlink()
            except OSError:
                pass
    return META_BACKUP_DIR

def _print_calibrate_report(report: dict, meta_dir=None) -> None:
    """把校准说清楚：读了多少、剔了什么、每个原型改前改后。

    纯打印，不读 data/ —— 测试可以直接喂一份报告进来。
    """
    updated = report.get("updated") or []
    skipped = report.get("skipped") or []
    rows = report.get("rows", 0)
    total_rows = report.get("total_rows", rows)
    print(f"读入 {rows} 行：可信 {report.get('trusted', 0)} 行，"
          f"无职业 {report.get('no_class', 0)} 行，"
          f"真值不可信 {report.get('untrusted', 0)} 行")
    if total_rows > rows:
        print(f"另有 {total_rows - rows} 行解析失败，已跳过")
    if meta_dir is not None:
        print(f"原型库：{meta_dir}")

    # 每个职业的可信局数：条目里的 samples 就是本职业样本，取最大值即可
    by_class: dict[str, int] = {}
    for item in updated + skipped:
        cls = item.get("class") or "?"
        by_class[cls] = max(by_class.get(cls, 0), item.get("samples", 0))
    for cls in sorted(by_class):
        print(f"{cls}：可信 {by_class[cls]} 局")
    for item in updated:
        print(f"  {item['id']}  {item['name']}  先验 "
              f"{item['before']:.2f} -> {item['after']:.2f}"
              f"（样本 {item['samples']}，权重 {item['weight']:.2f}）")
    for item in skipped:
        print(f"  {item['id']}  {item['name']}  跳过：{item['reason']}")

    print(f"修正 {len(updated)} 个原型（共 {len(updated) + len(skipped)} 个）；"
          "没有对局记录的职业保持先验不变")
    if not rows:
        print("没有对局记录，先验不变")
    elif not updated:
        print("本职业可信样本均不足 5 局，先验不变")

def cmd_meta(args: argparse.Namespace) -> int:
    db = _db()
    if args.action == "list":
        meta = MetaDB.load(db)
        for a in meta:
            # 按「关键牌」顺序显示，不是按费用/插入顺序 —— 名字就是用第一张取的，
            # 列表里却显示四张便宜牌的话，名字和依据对不上，没法核对。
            keys = pick_key_cards(a.cards, db, limit=4)
            top = "、".join(db.display_name(c) for c in keys)
            print(f"{a.id:<20} {a.name:<16} {a.class_cn:<5} 先验 {a.prior:.2f}  关键牌：{top}")
        print(f"共 {len(meta)} 个原型（{META_DIR}）")
        return 0
    if args.action == "import":
        payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
        meta = MetaDB.load(db)
        meta = import_archetypes(payload, db, meta)
        meta.save()
        print(f"已导入，共 {len(meta)} 个原型 -> {META_DIR}")
        return 0
    if args.action == "calibrate":
        meta = MetaDB.load(db)
        report = learn.calibrate_priors(meta, db)
        meta.save()
        _print_calibrate_report(report, META_DIR)
        return 0
    if args.action in ("rebuild", "build"):
        # build 就是 rebuild 的旧名字：以前它整份覆盖 meta.json，会把导入的
        # 卡组一起冲掉。现在两者都只重建学出来的原型。
        return _rebuild(db, args)
    return 1

def _rebuild(db, args) -> int:
    meta = MetaDB.load(db)
    before = [a for a in meta if a.source == "learned"]
    backup = None if args.dry_run else _backup_meta()
    report = learn.rebuild_learned(db, meta, min_games=args.min_games)
    if not args.dry_run:
        meta.save()
    print(f"删除 {len(report['removed'])} 个学出来的原型：")
    for aid, name, total in report["removed"]:
        print(f"  - {aid:<24} {name:<12} {total:3d} 张")
    print(f"聚类样本 {report.get('observations', 0)} 行")
    fresh = [a for a in meta if a.source == "learned"]
    print(f"重新聚类：新增 {report['added']} 个")
    for a in fresh:
        print(f"  + {a.id:<24} {a.name:<12} {a.total():3d} 张（{len(a.cards)} 种）")
    if backup is not None:
        print(f"备份: {backup}")
    print(f"原型总数 {len(meta)}（学出来的 {len(before)} -> {len(fresh)}）")
    return 0
