"""命令行入口：python -m hsai <命令>"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import cards as cards_mod
from . import learn, mulligan, sim
from .advisor import OUR_DECK_PATH, Advisor
from .cards import DATA_DIR, CardDB
from .paths import CARD_TAGS_AI_JSON, CARD_TAGS_MANUAL_JSON, LOG_FILE

LOG_PATH = LOG_FILE
from .infer import DeckPredictor
from .logwatch import PowerLogWatcher, find_power_log
from .meta import Archetype, MetaDB, import_archetypes, pick_key_cards
from .paths import META_BACKUP_DIR, META_DIR
from .state import replay_log_text


def _ensure_console() -> None:
    """让中文能打印出来，但不硬把编码改成 UTF-8。

    Windows 控制台常见两种：代码页 936（GBK，能直接显示中文）和 65001（UTF-8）。
    如果当前编码已经能表示中文就别动它 —— 强行 reconfigure(utf-8) 反而会让
    936 控制台显示成乱码。只有真的编码不了中文时（例如 cp1252）才切到 UTF-8。
    """
    try:
        enc = (sys.stdout.encoding or "").lower()
        "中文".encode(enc or "utf-8")
        return
    except (LookupError, UnicodeEncodeError):
        pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _db() -> CardDB:
    db = cards_mod.load()
    if not len(db):
        print("卡牌库为空：请先运行 `python -m hsai cards --update`")
        raise SystemExit(2)
    return db


# ---------------------------------------------------------------- 命令

def cmd_cards(args: argparse.Namespace) -> int:
    if args.update or not cards_mod.cache_path().exists():
        db = cards_mod.update()
    else:
        db = cards_mod.load()
    print(f"卡牌库：{len(db)} 张，缓存于 {cards_mod.cache_path()}")
    if args.search:
        for c in list(db.by_id.values()):
            if args.search in c.name or args.search.upper() == c.id:
                print(f"  {c.id:<18} {c.name:<12} {c.cost}费 {c.class_cn}{c.type_cn} {c.atk}/{c.health} {c.keyword_cn}")
    return 0


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


def cmd_replay(args: argparse.Namespace) -> int:
    db = _db()
    path = Path(args.file)
    st = replay_log_text(path.read_text(encoding="utf-8", errors="replace"), db)
    advisor = Advisor(db)
    snap = advisor.snapshot(st)
    print(st.summary())
    print(json.dumps(snap, ensure_ascii=False, indent=1))
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    db = _db()
    path = Path(args.log) if args.log else find_power_log()
    if path is None:
        print("没找到 Power.log。请确认游戏装过并至少启动过一次，或手动指定 --log。")
        return 2
    print(f"跟随日志：{path}")
    advisor = Advisor(db)
    watcher = PowerLogWatcher(path, db)
    last = -1
    try:
        while True:
            if watcher.poll() and watcher.version != last:
                last = watcher.version
                snap = advisor.snapshot(watcher.state)
                _print_live(snap)
            import time

            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n已停止。")
    return 0


def _print_live(snap: dict) -> None:
    print("-" * 70)
    print(f"[{snap['phase']}] T{snap['turn']} 我方{snap['our']['class_cn']} {snap['our']['health']} "
          f"vs 对方{snap['opp']['class_cn']} {snap['opp']['health']}")
    pred = snap.get("prediction") or {}
    if pred:
        print(f"预判：{pred.get('name')}  {pred.get('confidence', 0):.0%}")
        for line in pred.get("evidence", [])[:4]:
            print(f"   · {line}")
    if snap.get("warning"):
        print(f"⚠ {snap['warning']}")
    adv = snap.get("advice") or {}
    for line in adv.get("lines", [])[:2]:
        print(f"建议{line['rank']}（{line['score']}）：{line['title']} —— {line['reason']}")


def cmd_ui(args: argparse.Namespace) -> int:
    """启动悬浮窗（无控制台）。这是默认入口。"""
    if getattr(args, "check", False):
        # 给启动脚本用的预检：环境不对就当场报错，而不是启动后什么都没出现
        try:
            import tkinter  # noqa: F401
        except Exception as exc:  # pragma: no cover - 取决于 Python 发行版
            print(f"缺少 tkinter（图形界面组件）：{exc}")
            return 3
        db = cards_mod.load(auto_update=False)
        if not len(db):
            print("卡牌库为空，先运行：python -m hsai cards --update")
            return 4
        print(f"环境检查通过：卡牌库 {len(db)} 张")
        return 0
    from . import ui

    return ui.main(demo=args.demo, log_path=Path(args.log) if args.log else None)


def cmd_sim(args: argparse.Namespace) -> int:
    db = _db()
    meta = MetaDB.load(db)
    a = meta.get(args.a) or next((x for x in meta if x.name == args.a), None)
    b = meta.get(args.b) or next((x for x in meta if x.name == args.b), None)
    if a is None or b is None:
        print("找不到这两个原型。用 `python -m hsai meta list` 查看可用 id。")
        return 2
    if args.trace:
        import random

        rng = random.Random(args.seed)
        out = sim.run_game(sim.make_deck(a, db, rng), sim.make_deck(b, db, random.Random(args.seed + 1)),
                           db, seed=args.seed, trace=True, max_turns=args.max_turns,
                           class_a=a.card_class, class_b=b.card_class)
        print("\n".join(out.log))
        print(f"\n结果：{'A 胜' if out.winner == 0 else 'B 胜' if out.winner == 1 else '平/超时'}，{out.turns} 回合")
        return 0
    result = sim.match(a, b, db, games=args.games, seed=args.seed)
    print(f"{a.name} vs {b.name}：{result['games']} 局")
    print(f"  A {result['wins'][0]} 胜 / B {result['wins'][1]} 胜 / 平 {result['draws']}")
    print(f"  A 胜率 {result['winrate_a']:.1%}")
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    db = _db()
    meta = MetaDB.load(db)
    if args.backfill:
        # 一次性迁移：老行没有 truth，先验校准会把它们全跳过（而且不报错）
        report = learn.backfill_truth(db, meta, dry_run=args.dry_run)
        if not report["total"]:
            print("没有对局记录，不用回填。")
            return 2
        verb = "将回填" if args.dry_run else "已回填"
        print(f"{verb} {report['total']} 局：新补上真值 {report['filled']} 局，"
              f"其中可信 {report['trusted']} 局")
        if args.dry_run:
            print("（--dry-run：没有写盘）")
        elif not report["written"]:
            print("写盘失败，原文件未改动。")
            return 1
        else:
            print(f"备份：{learn.OBSERVATIONS_PATH}.bak")
            print("接着跑 `python -m hsai meta calibrate` 让先验真正吃到这批数据。")
        return 0
    if args.mulligan:
        w, report = learn.optimize_mulligan(db, meta, games=args.games, rounds=args.rounds, seed=args.seed,
                                            progress=print)
        learn.save_mulligan_weights(w)
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
        return 0
    w, report = learn.optimize(db, meta, games=args.games, rounds=args.rounds, seed=args.seed,
                               mulligan_weights=learn.load_mulligan_weights(), progress=print)
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=1))
    # 换一组种子再测一次：训练集上的提升必须能在没见过的局面上站住才值得信
    hold_wr, hold_n = learn.mirror_winrate(
        db, meta, w, dict(learn.DEFAULT_WEIGHTS), games=args.games, seed=args.seed + 977,
    )
    if hold_wr >= 0.52:
        verdict = "站得住，写入权重"
    elif hold_wr >= 0.48:
        verdict = "基本持平（没有可确认的提升）"
    else:
        verdict = "没站住，大概率是过拟合"
    print(f"换种子验证（{hold_n} 局）：{hold_wr:.1%} vs 基准 50% —— {verdict}")
    if hold_wr >= 0.52 or args.force:
        learn.save_weights(w)
        print(f"已写入权重 -> {learn.WEIGHTS_PATH}")
    else:
        print("未写入：验证没过就不改你的权重（确实想写就加 --force）")
    return 0


def cmd_setup_log(args: argparse.Namespace) -> int:
    """一键把 log.config 写到炉石安装目录。

    为什么需要这个命令：炉石不写 Power.log 的唯一原因就是它所在目录没有 log.config。
    而不同版本/安装方式的客户端，读的是不同目录 —— 这里靠「运行中的 Hearthstone.exe」
    来定位，比硬编码 %LOCALAPPDATA% 靠谱。
    """
    from .logwatch import find_power_log, log_config_targets, setup_log_config

    extra = [Path(args.dir)] if args.dir else []
    targets = log_config_targets(extra)
    if not targets:
        print("没找到炉石安装目录（需要包含 Hearthstone.exe 或 Hearthstone_Data）。")
        print('可以手动指定：python -m hsai setup-log --dir "D:\\software\\Hearthstone"')
        return 2

    results = setup_log_config(extra, dry_run=args.dry_run)
    changed = [r for r in results if r[1].startswith(("已创建", "已覆盖"))]
    if args.auto and not changed and not args.dry_run:
        print(f"日志开关：已开启（{targets[0]}）")
        return 0

    print("日志开关：")
    for path, msg in results:
        print(f"  {path}\n    -> {msg}")
    if not args.dry_run and changed:
        print()
        print("生效方式：完全退出炉石客户端（不是最小化），重新打开，进一局对战。")
        print("之后 Power.log 会出现在安装目录的 Logs 子目录里，助手会自动接上。")
    found = find_power_log(extra)
    print(f"当前找到的日志：{found}" if found else "当前还没有 Power.log（重启游戏后会出现）。")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    from . import review

    games = review.load_games()
    if not games:
        print("还没有对局记录。打完一局（并且胜负已判定）再来看。")
        return 2
    for g in games[-args.n:]:
        print(f"{g.get('ts','')}  {g.get('result','')}  {g.get('our_class','')} vs {g.get('opp_class','')}  "
              f"{len(g.get('turns') or [])} 个我方回合")
        for line in review.summarize(g):
            print("   -", line)
        print()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    data = learn.stats()
    print(json.dumps(data, ensure_ascii=False, indent=1))
    return 0


def cmd_mulligan(args: argparse.Namespace) -> int:
    """离线检查：给定手牌（卡牌名）和对手职业，看留牌建议。"""
    db = _db()
    meta = MetaDB.load(db)
    pred = DeckPredictor(meta, db).predict(args.opp, [], turn=1, hand_count=4)
    hand = [c for c in (db.by_name_or_id(n) for n in args.hand) if c]
    if not hand:
        print("没有可识别的卡牌名")
        return 2
    rows = mulligan.advise(hand, cards=db, prediction=pred, is_first=not args.second)
    print(mulligan.summary(rows))
    for r in rows:
        print(f"  [{r['action']:<4}] {r['name']:<10} {r['cost']}费  分数 {r['score']:>3}  {r['reason']}")
    return 0


def cmd_tags(args: argparse.Namespace) -> int:
    """卡牌功能标签：构建全量数据 / 覆盖率报告 / 导出待定清单。"""
    from . import tags

    if args.action == "build":
        db = _db()
        payload = tags.build(
            db,
            ai=tags.load_source(CARD_TAGS_AI_JSON),
            manual=tags.load_source(CARD_TAGS_MANUAL_JSON),
        )
        path = tags.save(payload)
        print(tags.format_report(payload))
        print(f"已写入 {path}")
        return 0

    payload = tags.load_payload()
    if not payload.get("rows"):
        print("还没有标签数据：先运行 `python -m hsai tags build`")
        return 2
    if args.action == "report":
        print(tags.format_report(payload))
        return 0
    if args.action == "pending":
        path = tags.save_pending(payload)
        print(f"待定 {len(payload.get('pending') or [])} 张 -> {path}")
        return 0
    return 1


def _fix_streams() -> None:
    """pythonw 启动时没有控制台，sys.stdout/stderr 可能是 None。

    那样任何一个 print 都会抛 AttributeError —— 表现为「双击后什么都没出现」。
    这里把它们接到日志文件上，既不会崩，也留下了排查线索。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fh = open(LOG_PATH, "a", encoding="utf-8", buffering=1)
    except OSError:
        import os

        fh = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = fh
    if sys.stderr is None:
        sys.stderr = fh


def main(argv: list[str] | None = None) -> int:
    _fix_streams()
    _ensure_console()
    p = argparse.ArgumentParser(prog="hsai", description="炉石传说 AI 助手")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("cards", help="卡牌库")
    c.add_argument("--update", action="store_true")
    c.add_argument("--search", default="")
    c.set_defaults(func=cmd_cards)

    m = sub.add_parser("meta", help="卡组原型库")
    m.add_argument("action", choices=["list", "build", "rebuild", "import", "calibrate"],
                   help="list 看全部 / rebuild 重建学出来的 / import 导入外部卡组表 / calibrate 修正先验")
    m.add_argument("--file", default="")
    m.add_argument("--min-games", type=int, default=3)
    m.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="rebuild 时只演练，不写盘")
    m.set_defaults(func=cmd_meta)

    d = sub.add_parser("deck", help="卡组：导入对手卡组库 / 设置自己的卡组")
    d.add_argument("action", choices=["set", "show", "add", "import", "mine", "list"])
    d.add_argument("code", nargs="?", default="", help="客户端「复制卡组」得到的那串（AAEC...）")
    d.add_argument("--name", default="", help="给这套牌起个名字")
    d.add_argument("--file", default="", dest="file", help="旧用法：从 json 文件导入")
    d.set_defaults(func=cmd_deck)

    r = sub.add_parser("replay", help="离线解析一段日志并输出快照")
    r.add_argument("file")
    r.set_defaults(func=cmd_replay)

    l = sub.add_parser("log", help="在终端里跟随 Power.log")
    l.add_argument("--log", default="")
    l.set_defaults(func=cmd_log)

    s = sub.add_parser("ui", help="启动悬浮窗（默认入口）")
    s.add_argument("--demo", action="store_true", help="回放示例日志（不接游戏）")
    s.add_argument("--log", default="")
    s.add_argument("--check", action="store_true", help="只做环境检查，不弹窗")
    s.set_defaults(func=cmd_ui)

    s2 = sub.add_parser("serve", help="旧名字，等同于 ui")
    s2.add_argument("--demo", action="store_true")
    s2.add_argument("--log", default="")
    s2.set_defaults(func=cmd_ui)

    sp = sub.add_parser("sim", help="自对弈")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.add_argument("--games", type=int, default=10)
    sp.add_argument("--seed", type=int, default=1)
    sp.add_argument("--trace", action="store_true")
    sp.add_argument("--max-turns", type=int, default=40)
    sp.set_defaults(func=cmd_sim)

    ln = sub.add_parser("learn", help="自对弈 + 权重优化")
    ln.add_argument("--games", type=int, default=4)
    ln.add_argument("--rounds", type=int, default=2)
    ln.add_argument("--seed", type=int, default=0)
    ln.add_argument("--mulligan", action="store_true", help="只优化留牌权重")
    ln.add_argument("--force", action="store_true", help="验证没通过也写入权重")
    ln.add_argument("--backfill", action="store_true",
                    help="把历史对局的真值按当前原型库补一遍（一次性迁移）")
    ln.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="配合 --backfill：只统计，不写盘")
    ln.set_defaults(func=cmd_learn)

    rv = sub.add_parser("review", help="实战复盘：斩杀/空过法力检查")
    rv.add_argument("-n", type=int, default=3, help="看最近几局")
    rv.set_defaults(func=cmd_review)

    st = sub.add_parser("stats", help="对局统计")
    st.set_defaults(func=cmd_stats)

    sl = sub.add_parser("setup-log", help="一键开启炉石日志（写 log.config）")
    sl.add_argument("--dir", default="", help="手动指定炉石安装目录")
    sl.add_argument("--dry-run", action="store_true", help="只看会做什么，不写文件")
    sl.add_argument("--auto", action="store_true", help="已开启时只打印一行（给启动脚本用）")
    sl.set_defaults(func=cmd_setup_log)

    mu = sub.add_parser("mulligan", help="离线测试留牌建议")
    mu.add_argument("opp", help="对手职业，如 WARRIOR")
    mu.add_argument("hand", nargs="+", help="手牌中文名")
    mu.add_argument("--second", action="store_true", help="我是后手")
    mu.set_defaults(func=cmd_mulligan)

    tg = sub.add_parser("tags", help="卡牌功能标签（11 类）")
    tg.add_argument("action", choices=["build", "report", "pending"],
                    help="build 规则打标并落盘 / report 覆盖率报告 / pending 导出待定清单")
    tg.set_defaults(func=cmd_tags)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
