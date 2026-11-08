"""HUD / 标签权重的纯展示函数：无 self、无窗口状态，从 ui.py 拆出。

门面会 re-export 这些名字（hud_row / _weight_summary 被测试直接 import）。
依赖的主题常量与 TAG_ORDER 都在本模块顶层解析，不反向依赖 ui 模块。
"""

from __future__ import annotations

from typing import Any

from .tag_rules import TAG_ORDER
from .ui_theme import WEIGHT_REASON_WORDS, WEIGHT_TOP_N


def hud_row(side: dict[str, Any], label: str) -> str:
    """面板「对战信息」：第一行是双方数值，任务 / 持续法术各自成行。

    SECRET 区混着奥秘、任务、咒符三类，别把任务数量算进「奥秘」。
    抽成模块级函数是为了能直接测：以前它是 _render 里的闭包，只能靠截图看。
    """
    hp = side.get("health") or 0
    armor = side.get("armor") or 0
    live = f"{hp + armor} 血"
    if armor:
        live += f"（{hp}+{armor}甲）"
    board = [m for m in (side.get("board") or []) if m.get("atk")]
    atk = sum(int(m.get("atk") or 0) for m in board)
    weapon = side.get("weapon") or {}
    weapon_atk = weapon.get("atk") or 0
    # 对手没有抽牌标签时牌库是粗估，界面上要看得出来，别当成实测值
    deck = side.get("deck_count")
    if deck is None:
        deck_text = "未知"          # 读不到就显示未知，别把 None 摆上面板
    else:
        deck_text = f"约{deck}" if side.get("deck_estimated") else str(deck)
    hand = side.get("hand_count")
    parts = [f"{label} {live}", f"手牌 {'未知' if hand is None else hand}",
             f"牌库 {deck_text}", f"场攻 {atk + weapon_atk}"]
    gy = side.get("graveyard_count")
    if isinstance(gy, int):
        parts.append(f"墓地 {gy}")
    secrets = side.get("secrets") or []
    if secrets:
        # 隐藏奥秘没有 cardId：数量照算，但标出「认不出是哪张」
        hidden = sum(1 for s in secrets if not s)
        text = f"奥秘 {len(secrets)}"
        if hidden:
            text += f"（未知 {hidden}）"
        parts.append(text)
    if weapon_atk:
        dur = weapon.get("durability")
        parts.append(f"武器 {weapon_atk}/{dur if isinstance(dur, int) else 1}")
    lines = [" · ".join(parts)]
    # 任务 / 持续法术各自成行：混进「奥秘 N」会把任务的进度也当成奥秘数
    for quest in side.get("quests") or []:
        name = quest.get("name") or "未知任务"
        progress = quest.get("progress")
        total = quest.get("total")
        lines.append(f"任务 {name} "
                     f"{'?' if progress is None else progress}/"
                     f"{'?' if total is None else total}")
    ongoing = side.get("ongoing") or []
    if ongoing:
        names = "、".join(str(o.get("name") or "未知") for o in ongoing)
        lines.append(f"持续法术 {len(ongoing)}（{names}）")
    return "\n".join(lines)


def _fmt_weight(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text or "0"


def _weight_summary(tag_weights: Any) -> tuple[list[str], list[str]]:
    """快照里的 tag_weights → (偏重前 3 类, 最多 2 行解释)。

    只做展示选择，不改权重本身：「未知」恒 0 且没有实际含义，负数是降权不是偏重，
    两类都过滤。缺字段/坏形状返回空，_render 据此不画卡片。
    """
    if not isinstance(tag_weights, dict):
        return [], []
    weights = tag_weights.get("weights")
    if not isinstance(weights, dict):
        return [], []
    order = {tag: i for i, tag in enumerate(TAG_ORDER)}
    picked: list[tuple[float, int, str]] = []
    for tag, value in weights.items():
        if tag == "未知":
            continue
        try:
            weight = float(value)
        except (TypeError, ValueError):
            continue
        if weight <= 0:
            continue
        picked.append((weight, order.get(tag, len(order)), tag))
    # 同分按 TAG_ORDER 兜底：输出顺序不能随 dict 遍历/哈希变
    picked.sort(key=lambda item: (-item[0], item[1]))
    tops = [f"{tag} {_fmt_weight(weight)}" for weight, _, tag in picked[:WEIGHT_TOP_N]]

    reasons = tag_weights.get("reasons")
    lines: list[str] = []
    if isinstance(reasons, list):
        for reason in reasons:
            text = str(reason or "").strip()
            if text and any(word in text for word in WEIGHT_REASON_WORDS):
                lines.append(text)
            if len(lines) >= 2:
                break
    return tops, lines
