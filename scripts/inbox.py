"""任务收件箱：投递、认领、完成、回收。

设计约束（照抄 docs/plan/inbox/_README.md 的约定）：
- 只碰 `docs/plan/inbox/` 下的文件；不执行任务内容、不联网、不改代码。
- 认领是**文件系统原子操作**（O_CREAT|O_EXCL），所以两个 AI 同时抢同一个任务，
  只有一个能成功 —— 这是「互不干扰」的地基。
- 认领凭据里记 worker 和开始时间：超时的凭据可以用 `reap` 回收，
  否则一个崩溃的 worker 会让任务永远卡在「已领取」。

用法：
    python scripts/inbox.py list
    python scripts/inbox.py claim 01-foo --as worker-a
    python scripts/inbox.py release 01-foo --as worker-a
    python scripts/inbox.py finish 01-foo --as worker-a --status ok --note "..."
    python scripts/inbox.py reap --ttl 3600
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path

# 测试可以指到临时目录：HSAI_INBOX_ROOT=<tmp>
_ROOT_ENV = "HSAI_INBOX_ROOT"
DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "docs" / "plan" / "inbox"
TASK_SUFFIXES = {".md", ".txt"}


def inbox_root() -> Path:
    custom = os.environ.get(_ROOT_ENV)
    return Path(custom) if custom else DEFAULT_ROOT


def _tasks(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*")
                  if p.is_file() and p.suffix.lower() in TASK_SUFFIXES
                  and not p.name.startswith((".", "_")))


def _stem(name: str) -> str:
    """把 list 看到的文件名（01-foo.md）和手写的 stem（01-foo）统一成 stem。

    claim 会自己补 .md 再取 stem，但 release/finish 以前直接拿参数当 stem，
    于是从 list 复制名字（带后缀）去 finish 会查不到凭据，任务卡在「已领」。
    """
    base = Path(name).name
    suffix = Path(base).suffix.lower()
    return base[: -len(suffix)] if suffix in TASK_SUFFIXES else base


def _unique_path(path: Path) -> Path:
    """目标已存在就加时间戳另存，别覆盖上一次的记录。

    同名任务重复投递很常见（改一版再投一次），覆盖会让上一次的执行记录
    无声消失 —— 归档和 done 都要留历史。
    """
    if not path.exists():
        return path
    stamp = time.strftime("%Y%m%d-%H%M%S")
    cand = path.with_name(f"{path.stem}__{stamp}{path.suffix}")
    n = 2
    while cand.exists():
        cand = path.with_name(f"{path.stem}__{stamp}-{n}{path.suffix}")
        n += 1
    return cand


def _claim_path(root: Path, stem: str) -> Path:
    return root / ".claims" / f"{stem}.json"


def _read_claim(root: Path, stem: str) -> dict | None:
    p = _claim_path(root, stem)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cmd_list(args: argparse.Namespace) -> int:
    root = inbox_root()
    tasks = _tasks(root)
    if not tasks:
        print("收件箱为空")
        return 0
    for p in tasks:
        claim = _read_claim(root, p.stem)
        if claim:
            print(f"[已领] {p.name:<32} {claim.get('worker')} @ {claim.get('ts')}")
        else:
            print(f"[待领] {p.name}")
    done = root / "done"
    if done.is_dir():
        n = len(list(done.glob("*.md")))
        if n:
            print(f"（已完成 {n} 个，见 done/）")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    root = inbox_root()
    path = root / args.name if args.name.endswith((".md", ".txt")) else root / f"{args.name}.md"
    stem = path.stem
    if not path.is_file():
        print(f"没有这个任务：{path.name}", file=sys.stderr)
        return 2
    claims = root / ".claims"
    claims.mkdir(parents=True, exist_ok=True)
    target = _claim_path(root, stem)
    payload = json.dumps({
        "worker": args.worker,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "task": path.name,
    }, ensure_ascii=False)
    try:
        # O_EXCL：原子。两个 worker 同时抢，只有一个能建出来
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        holder = _read_claim(root, stem) or {}
        print(f"已被 {holder.get('worker')} 领取（{holder.get('ts')}）", file=sys.stderr)
        return 3
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(payload + "\n")
    print(f"CLAIMED {stem} as {args.worker}")
    return 0


def _owned(root: Path, stem: str, worker: str) -> tuple[bool, str]:
    claim = _read_claim(root, stem)
    if claim is None:
        return False, "没有认领记录"
    if claim.get("worker") != worker:
        return False, f"已被 {claim.get('worker')} 领取"
    return True, ""


def cmd_release(args: argparse.Namespace) -> int:
    root = inbox_root()
    name = _stem(args.name)
    ok, why = _owned(root, name, args.worker)
    if not ok:
        print(why, file=sys.stderr)
        return 3
    _claim_path(root, name).unlink(missing_ok=True)
    print(f"RELEASED {name}")
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    root = inbox_root()
    stem = _stem(args.name)
    ok, why = _owned(root, stem, args.worker)
    if not ok and not args.force:
        print(why + "（确实要收尾用 --force）", file=sys.stderr)
        return 3

    done = root / "done"
    done.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    body = [
        f"# {stem}",
        "",
        f"- 状态：{args.status}",
        f"- 执行：{args.worker}",
        f"- 完成时间：{stamp}",
        "",
    ]
    if args.note:
        body += ["## 说明", "", args.note, ""]
    # 同名任务重复投递时**不要覆盖**上一次的记录：加时间戳另存一份。
    # 不撞名时保持 <stem>.md，命令和文档里写的路径都不变。
    (done_target := _unique_path(done / f"{stem}.md")).write_text("\n".join(body), encoding="utf-8")

    # 原文件进归档（同样避免覆盖旧档）
    src = next((p for p in _tasks(root) if p.stem == stem), None)
    if src is not None:
        arch = root / "archive"
        arch.mkdir(parents=True, exist_ok=True)
        os.replace(src, _unique_path(arch / src.name))
    _claim_path(root, stem).unlink(missing_ok=True)
    print(f"FINISHED {stem} status={args.status} -> done/{done_target.name}")
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    root = inbox_root()
    claims = root / ".claims"
    if not claims.is_dir():
        print("没有认领记录")
        return 0
    now = time.time()
    reaped = 0
    for p in sorted(claims.glob("*.json")):
        try:
            age = now - p.stat().st_mtime
        except OSError:
            continue
        if age < args.ttl:
            continue
        holder = _read_claim(root, p.stem) or {}
        p.unlink(missing_ok=True)
        reaped += 1
        print(f"REAPED {p.stem}（原持有人 {holder.get('worker')}，{int(age)}s 未收尾）")
    if not reaped:
        print(f"没有超过 {args.ttl}s 的认领")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="任务收件箱（只做认领/状态，不执行任务）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="看收件箱").set_defaults(func=cmd_list)

    c = sub.add_parser("claim", help="认领一个任务（原子，抢不到会失败）")
    c.add_argument("name")
    c.add_argument("--as", dest="worker", required=True)
    c.set_defaults(func=cmd_claim)

    r = sub.add_parser("release", help="放弃认领，任务回到待领")
    r.add_argument("name")
    r.add_argument("--as", dest="worker", required=True)
    r.set_defaults(func=cmd_release)

    f = sub.add_parser("finish", help="写结果并归档")
    f.add_argument("name")
    f.add_argument("--as", dest="worker", required=True)
    f.add_argument("--status", choices=["ok", "fail"], default="ok")
    f.add_argument("--note", default="")
    f.add_argument("--force", action="store_true", help="不是自己领的也强制收尾")
    f.set_defaults(func=cmd_finish)

    p = sub.add_parser("reap", help="回收超时未收尾的认领")
    p.add_argument("--ttl", type=float, default=3600.0, help="超过多少秒算超时")
    p.set_defaults(func=cmd_reap)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
