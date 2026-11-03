"""对被标「不赞同」的回合做一次 AI 复盘，产出教训 + 可落地的规则建议。

和 ai.py 分开是因为这里的提示词和对局复盘不一样：输入是「引擎建议 vs 你实际打法」，
要的是「引擎错在哪」而不是「这局打得怎么样」。
"""

from __future__ import annotations

from typing import Any

from .ai import chat as _chat, enabled, json_array, load_config

FEEDBACK_SYSTEM = (
    "你是炉石传说对局复盘助手。用户对某些回合的出牌建议点了「不赞同」，"
    "说明引擎当时的判断和人不一样。请对照那个回合的局面、引擎建议、用户实际打法，"
    "判断哪边更对，然后产出："
    "1) lesson：一条可执行的教训（30 字内，写给引擎看，不要写给人）；"
    "2) rule：一条能落成代码的判断规则（40 字内，形式如「若 X 则优先 Y」）。"
    '只输出 JSON 数组，元素形如 {"lesson": "...", "rule": "..."}。'
)


def feedback(summary: str, cfg: dict[str, Any] | None = None) -> list[dict[str, str]]:
    """分析被标「不赞同」的回合，返回 [{lesson, rule}]；失败返回空列表。"""
    cfg = cfg or load_config()
    if not enabled(cfg):
        return []
    text = _chat(
        [
            {"role": "system", "content": FEEDBACK_SYSTEM},
            {"role": "user", "content": summary},
        ],
        cfg,
        max_tokens=400,
    )
    out: list[dict[str, str]] = []
    for it in json_array(text):
        if not isinstance(it, dict):
            continue
        lesson = str(it.get("lesson") or "").strip()[:60]
        rule = str(it.get("rule") or "").strip()[:80]
        if lesson:
            out.append({"lesson": lesson, "rule": rule})
    return out[:3]


def build_summary(cases: list[dict[str, Any]]) -> str:
    """把被标「不赞同」的回合拼成给 AI 的输入。"""
    parts: list[str] = []
    for c in cases:
        head = f"### 第 {c.get('turn')} 回合（法力 {c.get('mana')}）"
        lines = [head]
        if c.get("board"):
            lines.append("我方场面：" + "、".join(c["board"]))
        if c.get("foe_board"):
            lines.append("对方场面：" + "、".join(c["foe_board"]))
        if c.get("hand"):
            lines.append("我方手牌：" + "、".join(c["hand"]))
        if c.get("advice"):
            lines.append("引擎建议：" + " / ".join(str(x) for x in c["advice"][:3]))
        if c.get("played"):
            lines.append("我实际打的牌：" + "、".join(c["played"]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
