"""配色 / 布局常量：从 ui.py 拆出，门面会原样 re-export。

拆成单独模块是为了让渲染类只读常量，不牵出整个窗口实现。这些名字
在 ui.py 门面上必须仍能在模块级访问（测试和主题都要用）。
"""

# 窗口默认宽度（窄一点，贴边时不挡牌桌）
DEFAULT_WIDTH = 260
# 改了布局参数就 +1：老配置文件里的宽度会被忽略，位置照旧保留
LAYOUT_VERSION = 7

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
