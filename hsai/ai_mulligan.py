"""AI 给出起手留牌建议。

为什么交给 AI：留牌的信息量很小（3-4 张牌 + 双方职业），但判断依赖
对局理解（对手形态、卡组关键组件、换牌代价），正好是规则表最弱、
模型最擅长的部分。而且每局只调用一次，成本可以忽略。
"""

from __future__ import annotations

import json
from typing import Any

from .ai import _chat, load_config

MULLIGAN_SYSTEM = (
    "你在给炉石传说做留牌决策。用户会给出双方职业、我方卡组和起手手牌。"
    "请逐张判断「留」还是「换」，并给出一句不超过 25 字的理由。"
    "只输出 JSON 数组，元素形如 {\"name\": \"卡名\", \"keep\": true, \"reason\": \"理由\"}。"
    "不要输出数组以外的任何内容。"
)


def build_mulligan_prompt(snap: dict[str, Any]) -> str:
    our = snap.get("our") or {}
    opp = snap.get("opp") or {}
    deck = snap.get("deck") or {}
    pred = snap.get("prediction") or {}
    hand = our.get("hand") or []

    def card(c: dict[str, Any]) -> str:
        cost = c.get("cost")
        return f"{c.get('name')}" + (f"({cost}费)" if cost is not None else "")

    lines = [
        f"我方职业：{our.get('class_cn') or '?'}；对手职业：{opp.get('class_cn') or '?'}。",
        f"我方卡组：{deck.get('name') or '未知'}。",
        f"起手手牌（{len(hand)} 张）：" + "、".join(card(c) for c in hand),
    ]
    if len(hand) < 3:
        lines.append("注意：手牌数量偏少，可能有牌没被读到。")
    if pred.get("name"):
        lines.append(f"对手可能是：{pred.get('name')}（{float(pred.get('confidence') or 0):.0%}）。")
    lines.append("请逐张给出留或换。")
    return "\n".join(lines)


def parse_mulligan(text: str) -> list[dict[str, Any]]:
    if not text:
        return []
    body = text.strip().strip("`")
    start, end = body.find("["), body.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        items = json.loads(body[start:end + 1])
    except ValueError:
        return []
    out: list[dict[str, Any]] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        out.append({
            "name": name,
            "keep": bool(row.get("keep")),
            "reason": str(row.get("reason") or "")[:60],
        })
    return out[:6]


def advise(snap: dict[str, Any], cfg: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """请求留牌建议。失败返回空列表（调用方回退到本地规则）。"""
    cfg = cfg or load_config()
    if not (cfg.get("enabled") and cfg.get("api_key")):
        return []
    if not (snap.get("our") or {}).get("hand"):
        return []
    text = _chat([
        {"role": "system", "content": MULLIGAN_SYSTEM},
        {"role": "user", "content": build_mulligan_prompt(snap)},
    ], cfg, max_tokens=260)
    return parse_mulligan(text)
