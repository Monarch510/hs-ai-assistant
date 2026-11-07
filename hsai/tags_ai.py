"""AI 只补「规则拿不准」的待定牌。

链路：
    data/card_tags_pending.jsonl（worker B 产出的待定清单）
    → 每 40 张拼一段中文 prompt
    → AI 只返回 {"rows": {卡牌 id: {"tags": [...], "note": "..."}}}
    → 校验（标签必须在 TAG_ORDER、id 必须在本批、缺项记失败）
    → 合并写入 data/card_tags_ai.json（格式见 docs/card-tagging/CONTEXT.md §4）

为什么只补待定：规则能定下来的牌不需要 AI 猜。AI 猜错的代价是将来出牌决策错，
而且不报错、很难发现。所以失败策略从简（一次重试），剩下的交给人工兜底。

当前环境没有 key 也必须能跑：命令打印「AI 未配置，跳过」并返回 2，
不联网、不崩溃，待定清单原样保留。

命令（cli.py 是 worker B 的作用域，这里用模块入口，不改它）：
    python -m hsai.tags_ai run
    python -m hsai.tags_ai validate data/card_tags_manual.json
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from . import ai
from .cards import TYPE_CN
from .cards import load as load_card_db
from .paths import CARD_TAGS_AI_JSON, CARD_TAGS_PENDING_JSONL
from .tag_rules import TAG_ORDER

PENDING_JSONL = CARD_TAGS_PENDING_JSONL
AI_JSON = CARD_TAGS_AI_JSON

MAX_BATCH = 40          # 一批 40 张：再大模型容易漏项，再小请求次数多
MAX_TOKENS = 1500       # 40 行 JSON 大约 600-1000 token，留足余量防止截断
RETRIES = 1             # 重试一次：第二次还错说明是模型的问题，再试只是烧钱

# prompt 里带上每类的短定义：只给标签名，模型会把「战吼打脸」也打上斩杀
TAG_HINTS = {
    "单解": "能处理对面一只随从（单体伤害/消灭/变形/回手/夺控）；打英雄的直伤不算",
    "群解": "能同时处理对面多只随从（所有/全场/相邻/随机多次/每个）",
    "站场": "打出后场上多出或留下东西（召唤/武器/地标/复制或复活召唤）",
    "增益": "强化我方（加身材、贴关键词、给手牌/牌库/武器/英雄加值）；护甲不算",
    "减益": "削弱或限制敌方（减攻/换攻/沉默/冻结/加费/弃或摧毁手牌）；冻结沉默都归这里",
    "治疗": "恢复生命值（英雄/随从/全体）；吸血也算",
    "护甲": "获得护甲值；只是提到护甲的条件牌不算",
    "资源": "非抽牌式补牌或费用优势（发现/随机获得/生成或复制牌/从牌库找牌/跳费/减费）",
    "抽牌": "从牌库抽牌",
    "斩杀": "能直接对敌方英雄造成伤害（直伤法术/冲锋/武器/英雄加攻）；突袭不算",
    "未知": "效果动作依赖本局历史或当前场面，单看卡面定不了（触发亡语/重放用过的牌/延迟触发/交换/复制身材）",
    "保护": "保护己方英雄（免疫/无法受到伤害/每次伤害上限/受到的伤害减半或降低）；获得护甲不算，随从自身免疫不算",
}

AI_SYSTEM = (
    f"你在给炉石传说卡牌打功能标签。用户会给出一批待定卡牌和固定的 {len(TAG_ORDER)} 类标签定义。"
    f"请为每张牌从这 {len(TAG_ORDER)} 类里选标签（可多选，也可以一个都不选）。"
    "只根据卡牌原文判断，不要因为职业、费用、稀有度去猜。"
    '只输出 JSON：{"rows": {"卡牌id": {"tags": ["标签"], "note": "不超过20字的依据"}}}。'
    "id 必须原样照抄清单里的；不要输出 JSON 以外的任何内容。"
)


def _plain(text: Any) -> str:
    """去掉卡牌文本里的富文本标记，prompt 里只留人话。"""
    return re.sub(r"<[^>]+>", "", str(text or "")).replace("$", "").replace("#", "").strip()


def load_pending(path: Path | str = PENDING_JSONL) -> tuple[list[dict[str, Any]], list[str]]:
    """读待定清单 JSONL。坏行跳过并记下来，不因为一行坏数据丢整批。"""
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        return [], [f"读不到待定清单 {path}：{exc}"]
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            errors.append(f"第 {lineno} 行不是 JSON，已跳过")
            continue
        card_id = str(item.get("id") or "").strip() if isinstance(item, dict) else ""
        if not card_id:
            errors.append(f"第 {lineno} 行缺少 id，已跳过")
            continue
        if card_id in seen:
            errors.append(f"第 {lineno} 行 id 重复（{card_id}），已跳过")
            continue
        seen.add(card_id)
        item["id"] = card_id
        items.append(item)
    return items, errors


def pending_stats(items: list[dict[str, Any]]) -> dict[str, int]:
    """按 why 分组统计待定数。排序带完整兜底，保证两次运行输出一致。"""
    counts: dict[str, int] = {}
    for item in items:
        why = str(item.get("why") or "").strip() or "（未写原因）"
        counts[why] = counts.get(why, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def build_prompt(batch: list[dict[str, Any]]) -> str:
    lines = ["固定标签（只能从这里选，可多选，也可以空）："]
    for tag in TAG_ORDER:
        lines.append(f"- {tag}：{TAG_HINTS.get(tag, '')}")
    lines += ["", "待标卡牌（id｜名称｜费用类型｜卡牌原文）："]
    for item in batch:
        card_id = str(item.get("id") or "")
        name = str(item.get("name") or "?")
        cost = item.get("cost")
        cost_txt = f"{cost}费" if isinstance(cost, int) else ""
        raw_type = str(item.get("type") or "")
        type_txt = TYPE_CN.get(raw_type, raw_type)
        line = f"- {card_id}｜{name}｜{cost_txt}{type_txt}｜{_plain(item.get('text'))[:120]}"
        why = str(item.get("why") or "").strip()
        if why:
            line += f"｜规则没把握的原因：{why}"
        lines.append(line)
    lines += ["", f"清单共 {len(batch)} 张，每张都必须给结果，id 原样照抄。"]
    return "\n".join(lines)


def parse_reply(text: str, batch_ids: list[str],
                order: tuple[str, ...] = TAG_ORDER) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """解析并校验一批 AI 回复。返回 (合法行, 问题清单)。

    宁可把有问题的行丢掉并把原因报出来，也不写一条未经校验的标签 ——
    脏标签一旦进 card_tags.json 就会被当成真值用。
    """
    errors: list[str] = []
    body = (text or "").strip().strip("`")
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return {}, ["AI 没有返回 JSON 对象"]
    try:
        data = json.loads(body[start:end + 1])
    except ValueError:
        return {}, ["AI 返回的不是合法 JSON"]
    raw_rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(raw_rows, dict):
        return {}, ['AI 返回缺少 rows 对象']

    known = set(batch_ids)
    rows: dict[str, dict[str, Any]] = {}
    for card_id, value in raw_rows.items():
        card_id = str(card_id).strip()
        if card_id not in known:
            errors.append(f"未知 id：{card_id}（不在本批清单里）")
            continue
        if not isinstance(value, dict):
            errors.append(f"{card_id}：结果不是对象")
            continue
        tags = value.get("tags")
        if not isinstance(tags, list):
            errors.append(f"{card_id}：tags 不是数组")
            continue
        cleaned: list[str] = []
        unknown: list[str] = []
        for tag in tags:
            tag = str(tag).strip()
            if tag in order:
                if tag not in cleaned:
                    cleaned.append(tag)
            else:
                unknown.append(tag)
        if unknown:
            errors.append(f"{card_id}：未知标签 {'、'.join(unknown)}")
        # 有标签但一个都不在固定 10 类里，等于没标，整行丢掉交给重试/人工
        if tags and not cleaned:
            continue
        cleaned.sort(key=order.index)
        rows[card_id] = {"tags": cleaned, "note": str(value.get("note") or "").strip()[:60]}

    missing = [card_id for card_id in batch_ids if card_id not in rows]
    if missing:
        shown = "、".join(missing[:5]) + (f" 等 {len(missing)} 张" if len(missing) > 5 else "")
        errors.append(f"AI 漏了 {len(missing)} 张：{shown}")
    return rows, errors


def _ask_batch(batch: list[dict[str, Any]], cfg: dict[str, Any],
               chat_fn) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """问一批，失败（坏 JSON/未知 id/未知标签/漏项）就整批重试一次。"""
    batch_ids = [str(item["id"]) for item in batch]
    messages = [
        {"role": "system", "content": AI_SYSTEM},
        {"role": "user", "content": build_prompt(batch)},
    ]
    rows: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for _ in range(RETRIES + 1):
        rows, errors = parse_reply(chat_fn(messages, cfg, max_tokens=MAX_TOKENS), batch_ids)
        if not errors:
            break
    return rows, errors


def load_ai_rows(path: Path | str = AI_JSON) -> dict[str, dict[str, Any]]:
    """读已有的 AI 结果，用于「这次只补一部分」时保留历史（缺文件/坏文件返回空）。"""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    rows = payload.get("rows") if isinstance(payload, dict) else None
    return dict(rows) if isinstance(rows, dict) else {}


def save_ai_rows(rows: dict[str, dict[str, Any]], path: Path | str = AI_JSON) -> Path:
    """写 AI 补标文件。按 id 排序，两次运行内容一致（只有版本号是常量）。"""
    path = Path(path)
    payload = {"version": 1, "rows": {card_id: rows[card_id] for card_id in sorted(rows)}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def run(pending_path: Path | str | None = None, out_path: Path | str | None = None,
        cfg: dict[str, Any] | None = None, chat_fn=None,
        batch_size: int = MAX_BATCH) -> int:
    """待定清单 → AI 补标 → 写 card_tags_ai.json。返回进程退出码。"""
    pending_path = Path(pending_path) if pending_path else PENDING_JSONL
    out_path = Path(out_path) if out_path else AI_JSON
    chat_fn = chat_fn or ai.chat

    items, errors = load_pending(pending_path)
    for msg in errors:
        print(f"[warn] {msg}")
    if items:
        print(f"待定 {len(items)} 张（{pending_path}）")
        for why, count in pending_stats(items).items():
            print(f"  {count:3d} 张  {why}")

    cfg = cfg or ai.load_config()
    if not ai.enabled(cfg):
        if not cfg.get("enabled", True):
            print("AI 已关闭（data/ai.json 的 enabled=false），跳过。")
        else:
            print("AI 未配置，跳过：没有环境变量 HSAI_API_KEY，data/ai.json 里也没有 api_key。")
        print("待定清单已保留；人工兜底方式见 docs/card-tagging/pending.md。")
        return 2
    if not items:
        if pending_path.exists():
            print(f"待定清单为空（{pending_path}），不需要补标。")
            return 0
        print(f"没有待定清单：{pending_path}")
        print("先跑 `python -m hsai tags pending` 生成（worker B 的 tags 子命令）。")
        return 2

    merged = load_ai_rows(out_path)
    covered: set[str] = set()
    failed = 0
    total_batches = (len(items) + batch_size - 1) // batch_size
    for index, start in enumerate(range(0, len(items), batch_size), 1):
        batch = items[start:start + batch_size]
        rows, batch_errors = _ask_batch(batch, cfg, chat_fn)
        merged.update(rows)
        covered.update(rows)
        status = "通过" if not batch_errors else "有问题"
        print(f"第 {index}/{total_batches} 批（{len(batch)} 张）：{status}，拿到 {len(rows)} 张")
        if batch_errors:
            failed += 1
            for msg in batch_errors:
                print(f"  [warn] {msg}")

    print(f"AI 补标 {len(covered)}/{len(items)} 张")
    if not covered and not merged:
        print("AI 一张也没给结果，不写文件。")
        return 1
    save_ai_rows(merged, out_path)
    print(f"写入 {len(merged)} 张 -> {out_path}")
    if failed:
        print(f"{failed}/{total_batches} 批重试 {RETRIES} 次后仍有问题：这些牌请走人工兜底。")
        return 1
    return 0


def _parse_keep_dups(text: str) -> tuple[Any, list[str]]:
    """解析 JSON，同时把重复的键记下来（普通 json 会静默保留最后一个）。"""
    dups: list[str] = []

    def _hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        seen: set[str] = set()
        for key, _value in pairs:
            if key in seen:
                dups.append(str(key))
            seen.add(key)
        return dict(pairs)

    try:
        return json.loads(text, object_pairs_hook=_hook), dups
    except ValueError:
        return None, dups


def validate_file(path: Path | str, known_ids: set[str] | None = None,
                  order: tuple[str, ...] = TAG_ORDER) -> dict[str, Any]:
    """校验 ai/manual 文件。返回报告（纯数据，打印交给 print_validate）。

    known_ids 传卡牌库里的全部大写 id；传 None 表示查不了（卡牌库为空），跳过 id 检查。
    """
    report: dict[str, Any] = {
        "path": str(path), "error": "", "rows": 0,
        "unknown_ids": [], "unknown_tags": {}, "duplicate_keys": [],
        "duplicate_tags": {}, "empty_tags": [], "bad_rows": [],
    }
    try:
        # utf-8-sig：人工文件常用记事本存成带 BOM 的 UTF-8，别让 BOM 把整份文件判成坏 JSON
        text = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        report["error"] = f"读不到文件：{exc}"
        return report
    data, dups = _parse_keep_dups(text)
    if data is None:
        report["error"] = "不是合法 JSON"
        return report
    if not isinstance(data, dict):
        report["error"] = "顶层不是 JSON 对象"
        return report
    rows = data.get("rows")
    if not isinstance(rows, dict):
        report["error"] = '缺少 rows 对象（格式：{"version": 1, "rows": {...}}）'
        return report

    report["duplicate_keys"] = sorted(set(dups))
    report["rows"] = len(rows)
    for card_id, value in sorted(rows.items()):
        if known_ids is not None and str(card_id).upper() not in known_ids:
            report["unknown_ids"].append(card_id)
        if not isinstance(value, dict):
            report["bad_rows"].append(f"{card_id}：值不是对象")
            continue
        tags = value.get("tags")
        if not isinstance(tags, list):
            report["bad_rows"].append(f"{card_id}：tags 不是数组")
            continue
        names = [str(tag).strip() for tag in tags]
        for tag in names:
            if tag not in order:
                report["unknown_tags"][tag] = report["unknown_tags"].get(tag, 0) + 1
        repeated = sorted({tag for tag in names if names.count(tag) > 1})
        if repeated:
            report["duplicate_tags"][card_id] = repeated
        if not any(tag in order for tag in names):
            report["empty_tags"].append(card_id)
    return report


def validate_ok(report: dict[str, Any]) -> bool:
    """硬错误才算不通过；空标签、同牌重复标签只是提醒（格式允许）。"""
    return not (
        report["error"]
        or report["unknown_ids"]
        or report["unknown_tags"]
        or report["duplicate_keys"]
        or report["bad_rows"]
    )


def print_validate(report: dict[str, Any]) -> None:
    print(f"校验 {report['path']}")
    if report["error"]:
        print(f"  [错误] {report['error']}")
        return
    print(f"  行数：{report['rows']}")
    if report["duplicate_keys"]:
        print(f"  [错误] 重复的键：{'、'.join(report['duplicate_keys'])}")
    if report["unknown_ids"]:
        ids = report["unknown_ids"]
        shown = "、".join(ids[:8]) + (f" 等 {len(ids)} 个" if len(ids) > 8 else "")
        print(f"  [错误] 未知 id（卡牌库里没有）：{shown}")
    if report["unknown_tags"]:
        pairs = "、".join(f"{tag}×{n}" for tag, n in sorted(report["unknown_tags"].items()))
        print(f"  [错误] 未知标签（只能来自固定 10 类）：{pairs}")
    if report["bad_rows"]:
        for line in report["bad_rows"][:8]:
            print(f"  [错误] {line}")
        if len(report["bad_rows"]) > 8:
            print(f"  [错误] 还有 {len(report['bad_rows']) - 8} 行格式不对")
    if report["duplicate_tags"]:
        for card_id, tags in sorted(report["duplicate_tags"].items())[:8]:
            print(f"  ! {card_id} 重复写了标签：{'、'.join(tags)}（build 时按并集去重）")
    if report["empty_tags"]:
        ids = report["empty_tags"]
        shown = "、".join(ids[:8]) + (f" 等 {len(ids)} 个" if len(ids) > 8 else "")
        print(f"  [提示] 空标签 {len(ids)} 张（格式允许，确认不是漏标）：{shown}")
    if validate_ok(report):
        print("  [OK] 未发现问题")


def cmd_run(_args: argparse.Namespace) -> int:
    return run()


def cmd_validate(args: argparse.Namespace) -> int:
    known = set(load_card_db(auto_update=False).by_id) or None
    if known is None:
        print("[warn] 卡牌库缓存为空，跳过未知 id 检查（先跑 python -m hsai cards --update）")
    report = validate_file(args.file, known_ids=known)
    print_validate(report)
    return 0 if validate_ok(report) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m hsai.tags_ai", description="AI 补待定卡牌标签 / 校验标签文件")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="对待定清单调用 AI 补标，写 data/card_tags_ai.json")
    r.set_defaults(func=cmd_run)
    v = sub.add_parser("validate", help="校验 ai/manual 标签文件（未知 id/未知标签/重复/空标签）")
    v.add_argument("file", help="要校验的 JSON 文件")
    v.set_defaults(func=cmd_validate)
    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
