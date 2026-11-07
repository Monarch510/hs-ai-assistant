"""命令行入口：python -m hsai <命令>

这个模块现在是**门面**：只留 main（argparse 装配）和 re-export。
各命令族在 cli_*.py，启动底座（_db / _ensure_console / _fix_streams）在 cli_boot.py。
拆开是为了多个 AI 同时改不同命令时不会在同一文件里撞车。

外部依赖（别断）：
  · hsai/__main__.py      from .cli import main
  · tests/test_learn_report.py  cli._print_calibrate_report
"""

from __future__ import annotations

import argparse

from .cli_boot import _ensure_console, _fix_streams
from .cli_boot import _db as _db  # noqa: F401  兼容旧的 cli._db 引用

from .cli_cards import cmd_cards  # noqa: E402
from .cli_deck import cmd_deck  # noqa: E402
from .cli_learn import cmd_learn  # noqa: E402
from .cli_live import cmd_log, cmd_replay  # noqa: E402
from .cli_meta import (  # noqa: E402
    _print_calibrate_report,
    cmd_meta,
)
from .cli_mulligan import cmd_mulligan  # noqa: E402
from .cli_review import cmd_review  # noqa: E402
from .cli_setup_log import cmd_setup_log  # noqa: E402
from .cli_sim import cmd_sim  # noqa: E402
from .cli_stats import cmd_stats, cmd_tunings  # noqa: E402
from .cli_tags import cmd_tags  # noqa: E402
from .cli_ui import cmd_ui  # noqa: E402

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

    tn = sub.add_parser("tunings", help="AI 复盘攒下的参数建议（只读，--apply 才写权重）")
    tn.add_argument("--min-count", type=int, default=3,
                    help="只显示被至少这么多局提到的参数（默认 3）")
    tn.add_argument("--apply", action="store_true",
                    help="把达标且方向一致的建议写进权重文件（会先备份）")
    tn.set_defaults(func=cmd_tunings)

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

    tg = sub.add_parser("tags", help="卡牌功能标签（12 类）")
    tg.add_argument("action", choices=["build", "report", "pending"],
                    help="build 规则打标并落盘 / report 覆盖率报告 / pending 导出待定清单")
    tg.set_defaults(func=cmd_tags)

    args = p.parse_args(argv)
    return int(args.func(args) or 0)

if __name__ == "__main__":
    raise SystemExit(main())
