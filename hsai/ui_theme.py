"""配色 / 布局常量：从 ui.py 拆出，门面会原样 re-export。

拆成单独模块是为了让渲染类只读常量，不牵出整个窗口实现。这些名字
在 ui.py 门面上必须仍能在模块级访问（测试和主题都要用）。
"""

from __future__ import annotations

import math

# 窗口默认宽度（窄一点，贴边时不挡牌桌）
DEFAULT_WIDTH = 260
# 改了布局参数就 +1：老配置文件里的宽度/字号会被忽略，位置照旧保留
LAYOUT_VERSION = 8

# 字号一起放大：默认 12，右键菜单可调 9–16。老 window.json 的 10 会在
# 布局版本升级时让位给新默认，否则「调大默认字号」对老用户根本不生效。
DEFAULT_FONT_SIZE = 12
FONT_MIN = 9
FONT_MAX = 16

# BG 同时是「透明键」：窗口上凡是这个颜色的像素都会被抠掉，露出下面的游戏。
# 所以它只能用在「缝隙」上（root / container / body），卡片必须用 PANEL。
BG = "#0f131a"
PANEL = "#171d26"
BORDER = "#30363d"
TEXT = "#e6edf3"
TEXT_SOFT = "#c9d1d9"
MUTED = "#8b949e"
GOLD = "#d4a340"
GREEN = "#3fb950"
RED = "#f85149"
YELLOW = "#d29922"
BLUE = "#58a6ff"

# 动效的两档高亮色：底色必须留在 PANEL 系上，别把透明键 BG 铺到卡片里
GOLD_HI = "#ffe6a0"
RED_HI = "#ff8a82"

FONT = "Microsoft YaHei UI"
COLLAPSED_H = 28

# 「本回合偏重」最多列几类：一眼扫过的信息，列多了反而要读。
WEIGHT_TOP_N = 3
# 解释行只留带实质变化的，跳过「留牌阶段：只看职业…」这类元说明。
WEIGHT_REASON_WORDS = ("加权", "降权", "偏低", "血线", "场攻", "置顶")

# 默认折叠的都是「参考性」信息；要动手的块（建议/待你选择/对战信息/留牌建议）默认摊开。
# window.json 里没有 collapsed_sections 键 = 用户从没手动改过，就用这套默认；
# 一旦用户点过标题存过这个键，就完全尊重他存的集合，不再往里塞默认项。
DEFAULT_COLLAPSED = frozenset({
    "对手预判", "本回合偏重", "被动计数", "克苏恩", "牌库剩余",
})
# 牌库剩余展开后一次只画几行，其余靠滚轮翻 —— 不碰 _apply_geometry
DECK_VISIBLE_ROWS = 6
# 建议卡同理：一次只画几行，一串建议不会把窗口越撑越高
ADVICE_VISIBLE_ROWS = 6


def _rgb(value: str) -> tuple[int, int, int]:
    s = value.lstrip("#")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def mix_color(a: str, b: str, t: float) -> str:
    """两个 #rrggbb 之间线性插值；只改颜色不改布局，动效循环靠它算中间色。"""
    try:
        ca, cb = _rgb(a), _rgb(b)
        f = max(0.0, min(1.0, float(t)))
    except (ValueError, TypeError, IndexError):
        return a
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * f) for x, y in zip(ca, cb))


def pulse(elapsed: float, period: float = 2.4, *, wave: str = "sine") -> float:
    """0..1 的相位值：sine 是呼吸，blink 是硬闪；period 非法就停在 0。"""
    if period <= 0:
        return 0.0
    phase = (float(elapsed) % period) / period
    if wave == "blink":
        return 1.0 if phase < 0.5 else 0.0
    return (1.0 - math.cos(2.0 * math.pi * phase)) / 2.0
