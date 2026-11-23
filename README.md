# 炉石传说 AI 助手

本地副驾：只读游戏写的 `Power.log`，用两扇无边框置顶的 Tkinter 悬浮窗告诉玩家
「对面可能是什么卡组、这回合怎么打、上一局错在哪」。

- **纯标准库**（含 tkinter），无第三方依赖；只有局末 AI 复盘会联网，可关
- **只读日志**：不注入输入、不改客户端、不读内存
- **引擎做决策，AI 只在局末复盘**。未建模的牌只记录到 `data/hsai.log`，不自动补规则

---

## 上手

```powershell
python -m hsai ui              # 两窗悬浮窗（双击 启动AI助手.bat 等价）
python -m hsai ui --demo       # 示例对局，不接游戏
python -m hsai ui --check      # 只做环境检查
python -m unittest discover -s tests   # 全量测试（改完必须全绿）
```

首次运行会自动下载卡牌库（约 4 MB），之后离线可用。

## 两扇窗

| 窗 | 内容 |
|---|---|
| 左（主窗） | 对战信息（含对手预测卡组一行：卡组名 + 置信度）、本回合建议、斩杀步骤、风险提醒、留牌、局末复盘 |
| 右 | 我的剩余牌库：费用、牌名、张数、类型 + 功能标签 |

两扇窗**各自能拖动**，位置分别记在 `window.json` 的 `x/y`、`right_x/right_y`。
右键菜单调透明度、字号、收起；`F8` 全局显隐；不在对局时缩成细条并显示上一局复盘。

> 改动界面前先看 [docs/AGENTS.md](docs/AGENTS.md) 的「代码地图」和 [PITFALLS.md](docs/PITFALLS.md)：
> `ui*.py` 是门面 + Mixin 拆分，`board_from_state` 有源码切片断言，改错地方会静默失效。

---

## 数据流

```
Power.log ──► logwatch ──► powerlog ──► state ──┐
                                                 ├─► advisor ──► ui（两窗）
        infer / mulligan / policy / model ───────┘
                     （AI 只旁挂在局末复盘）
```

| 层 | 入口 | 职责 |
|---|---|---|
| 输入 | `logwatch` / `powerlog` / `state` | 找日志并跟随 → 词法/语法解析（双来源去重）→ 归约成对局状态 |
| 判断 | `infer` / `mulligan` / `policy` / `model` | 卡组预判 / 留牌打分 / 动作枚举与斩杀检测 / 共享局面模型 |
| 输出 | `advisor` | 组装快照，是窗口**唯一数据源**；缺数据降级而不是报错 |
| 旁路 | `ai` / `effects_ai` | 局末复盘；效果规则的验证器（不写盘、运行时零调用） |
| 学习 | `learn*` / `sim` / `meta*` | 记录对局、聚类原型、自对弈优化权重 |

`model.py` / `policy.py` / `state.py` / `advisor.py` / `ui.py` 等已拆成**门面 + 同名前缀子模块**：
门面原样 re-export，import 面不变。改实现去子模块，别在门面里加。可写模块全局必须留在门面，
子模块在函数内部 `from . import <门面>` 回查，不能 import 时拷值。

**关键不变量**：斩杀估算必须和 `apply_action` 同一口径（场上法伤、英雄攻击、武器风怒/耐久、
英雄技能、无法攻击英雄约束），两处只要有一处漏算就会「理论能斩却判不能」或反向虚报。

---

## 命令

```powershell
python -m hsai deck add|mine <卡组代码>       # 导入对手 / 登记自己的卡组
python -m hsai cards --update                 # 更新卡牌库
python -m hsai setup-log                      # 开启炉石日志（之后完全退出游戏重启）
python -m hsai replay <日志文件>              # 离线解析，输出快照 JSON
python -m hsai log                            # 终端跟随日志
python -m hsai mulligan WARRIOR 寒冰箭        # 离线测留牌
python -m hsai sim <原型A> <原型B>            # 自对弈
python -m hsai learn [--rounds N]             # 自对弈 + 坐标轮换爬山优化权重
python -m hsai learn --backfill               # 给老对局补真值标签
python -m hsai meta build|rebuild|calibrate   # 原型库：聚类 / 重聚类 / 校准先验
python -m hsai tags                           # 12 类卡牌功能标签
python -m hsai review [-n 3]                  # 复盘最近几局的斩杀/空过法力
python -m hsai stats                          # 对局统计
python -m hsai tunings [--apply]              # AI 攒下的参数建议（默认只读，--apply 才写权重）
```

`meta rebuild` 先备份；`tunings --apply` 走增量水位，重复执行不会叠加权重。
真要大幅改权重走 `hsai learn` 自对弈，不靠 AI 一句话。

## 测试

```powershell
python -m unittest discover -s tests          # 全量（当前 768 例）
python -m unittest discover tests -p "test_[!s]*.py"   # 跳过较慢的自对弈用例
```

`tests/fixtures/` 里三份日志：`sample_game.log`（构造）、`real_client_excerpt.log`（真实片段）、
`secret_zone_excerpt.log`。无 tkinter 时 `tests/_ui_support.py` 装控件替身，验的是渲染路径不抛异常，
不是像素。CI（GitHub Actions）在 Python 3.10 + 3.12 上跑全量测试，lint（ruff）只报不拦。

---

## 数据

除 `data/effects.json` 外，`data/` 全是运行产物，**整目录不入库**（`.gitignore`: `data/*` + `!data/effects.json`）。
路径统一在 `hsai/paths.py`。

| 文件 | 谁写 / 谁读 | 内容 |
|---|---|---|
| `cards_zhCN.json` | `cards.py` | 卡牌库缓存（首次下载） |
| `effects.json` | 人维护，`model.py` 读 | 效果正则规则表，**唯一入库**的 data 文件 |
| `meta/<id>.json` | `meta.py` | 卡组原型，一原型一文件，`cards` 带名称/费用/张数按费用升序 |
| `observations.jsonl` | `advisor.py` | 每局一行，预测值与真值分开存；学习只吃 `truth` |
| `games.jsonl` / `lessons.jsonl` / `cases.jsonl` | `review.py` / `ai.py` / `memory.py` | 对局记录 / 教训 / 案例 |
| `advice_tuning.jsonl` | `memory.py` / `cli_stats.py` | AI 参数建议 + 已应用水位 |
| `window.json` | `ui.py` | 两窗摆位、透明度、字号、折叠状态 |
| `ai.json` | `ai.py` | AI 配置，可能含 key，**绝不入库** |
| `hsai.log` | `logutil.py` | 诊断日志，出问题先看这里；超 1 MB 自动截断 |

---

## 文档导航

- [docs/AGENTS.md](docs/AGENTS.md) — 项目约定、完整代码地图、AI 复盘的两条产出路径
- [docs/CONVENTIONS.md](docs/CONVENTIONS.md) — 项目额外硬约束
- [docs/PITFALLS.md](docs/PITFALLS.md) — 踩过的坑，改代码前按关键字查

## 已知限制

1. 独占全屏下悬浮窗压不上去，用「窗口化」或「无边框窗口」。
2. 效果解释器只认常见文本；认不出的牌列进「未建模」并记录，补规则由人看过复盘后手动加。
3. 对手手牌内容看不到，只能给概率——规则决定。
4. 只支持常规对战；酒馆战棋、竞技场选牌、佣兵不解析。

## 隐私

卡牌库来自 `api.hearthstonejson.com`；AI 复盘把**对局摘要**（职业、卡组名、回合数、失误）
发给配置的 API。API Key 优先读环境变量 `HSAI_API_KEY`，也接受 `data/ai.json` 的 `api_key`
（该文件不入库）。程序不会把 key 写进文件。
