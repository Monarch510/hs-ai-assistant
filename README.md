# 炉石传说 AI 助手（本地 / 只读日志 / 原生悬浮窗）

一个跑在自己电脑上的炉石副驾：**一个无边框、始终置顶的原生窗口**，读游戏日志，
告诉你对面可能是什么卡组、这回合怎么打、局后复盘这局错在哪。

- 纯 Python 标准库（含 tkinter），无第三方依赖；只有 AI 复盘会联网（可关）
- **不注入输入、不改客户端**，只读 `Power.log`
- 没有浏览器、没有本地端口、没有黑框：双击就是一个悬浮窗

---

## 一键启动

双击 **`启动AI助手.bat`**，或者：

```powershell
python -m hsai ui          # 悬浮窗（默认）
python -m hsai ui --demo   # 用示例对局演示，不接游戏
```

第一次运行会自动下载卡牌库（约 10 MB，含衍生物/英雄技能）。之后完全离线。

### 窗口怎么用

| 操作 | 效果 |
|---|---|
| 按住顶部拖动 | 移动窗口（位置会记住） |
| 右键 | 透明度、字号、收起/展开、退出 |
| `F8` | 全局显示/隐藏 |
| 关闭按钮 `×` | 退出 |

不在对局时窗口缩成一条细条，并显示**上一局的复盘**；新对局开始自动展开。

---

## 面板上是什么

```
T7 · 我方回合 · vs 死亡骑士            对局中
┌ 对手预判 ─────────────────── 核心 ┐
│ 死亡骑士·掘骨恶鬼                82% │
│ 清场 45% · 单解 38%                │
│ 最可能：暗言术：毁（4费）           │
└───────────────────────────────────┘
┌ 本回合建议 ───────────────── 决策 ┐
│ ① 第2位 女巫的学徒 3/4 攻击第1位 … │
│    或者：打出 癫狂元素（3费 2/2）   │
└───────────────────────────────────┘
│ 对方约 45% 概率持有清场，别铺满      │
```

- **对手预判**：卡组名 + 置信度 + 清场/单解风险 + 最可能出现的牌
- **留牌建议**：只在起手出现
- **本回合建议**：1–3 条路线；出现红条和「斩杀步骤」就是能斩杀
- **一行风险提醒**：清场概率、斩杀线这类「看了场面才知道」的警告
- **局后复盘**：失误、未建模的牌、AI 总结的教训

---

## 决策是引擎做的，AI 只做两件事

| 环节 | 谁做 |
|---|---|
| 留牌建议 | 引擎（权重表） |
| 发现 / 抉择 | 引擎打分（当回合可打、立即收益、卡组主题、场上法伤、手牌组合、对手形态）|
| 出牌建议 | 引擎（抢血 / 解场两种模式 + 斩杀检测）|
| **复盘教训** | **AI**（写进 `data/lessons.jsonl`，下次开局按对局标签检索）|
| **补卡牌效果规则** | **AI**（遇到未建模的牌即时生成正则规则，验证通过才入库）|

一局大约 1–2 次 AI 调用（复盘 + 按需补规则）。逐回合的 AI 路线默认关闭，
可在 `data/ai.json` 里设 `"turn_advice": true` 打开。

### 引擎怎么判断「抢血还是解场」

```
我赢的回合 = (他血量 − 我手上爆发) / 我每回合持续伤害
他赢的回合 = 我血量 / 他每回合伤害
我更快 → 抢血；否则 → 解场
```

- **抢血模式**：白吃不拖慢节奏才吃；放着某只随从下回合会打死我才解它
- **解场模式**：只做「换完场面净值更高」的交换；没有划算的就打脸
- **交换用谁**：用价值最低的随从去换，保住高价值的

随从价值 = `攻击力 × 1.25 + 血量 + 特效`
（圣盾≈多一个同攻 1 血随从、嘲讽 +1.5、光环按「加成 × 受影响随从数」）

---

## 支持的机制

| 机制 | 状态 |
|---|---|
| 斩杀检测 | 精确到每点伤害（含武器、法术、英雄技能、嘲讽阻挡、亡语召唤出的嘲讽）|
| 武器 | 攻击/反伤/耐久碎裂/风怒/攻击后召唤 |
| 突袭 | 入场当回合只能打随从 |
| 休眠 | 按 `DORMANT` 标签 + 「N回合后唤醒」推算，**醒来当回合不能攻击** |
| 复生 | 死亡后以 1 血复活一次 |
| 亡语 | 召唤 / 回血 / 加护甲 / 抽牌 / 群体伤害（区分「所有随从」与「所有敌人」）|
| 被动英雄技能 | 计数器（如「每使用五张随从牌，战吼触发两次」）|
| 克苏恩四技能 | 触发牌四选一；帝国之眼每回合两次、已用不能重复、集齐四个后 ×3 |
| 巨型 | 打出时召唤附属物（按名字前缀识别）|
| 预备 | 花剩余法力降低该牌费用（-花费-1），无事可做时推荐 |
| 发现 / 抉择 | 引擎打分选择 |
| 场景位置 | 按 `ZONE_POSITION` 排序；建议里的「第N位」= 屏幕从左到右 |

---

## 卡组代码导入（让预判真的能用）

```powershell
python -m hsai deck add "AAECAf0EB...=="      # 加入对手卡组库
python -m hsai deck mine "AAECAf0EB...=="     # 登记自己的卡组
python -m hsai deck show                      # 看看现在登记了什么
```

我方卡组会优先读 `Decks.log` 里「开始对战时记录的那套牌」，读不到才用已见牌匹配。

---

## 让游戏写日志

工具靠 `Power.log`。启动脚本会自动检查并在缺少时写入 `log.config`（有备份）。
手动执行：`python -m hsai setup-log`，之后**完全退出游戏再启动**。

---

## 命令

```powershell
python -m hsai ui [--demo] [--log PATH]   # 悬浮窗
python -m hsai ui --check                 # 只做环境检查
python -m hsai setup-log                  # 写入 log.config
python -m hsai deck add|mine <卡组代码>    # 卡组代码导入
python -m hsai meta build                 # 从自己的对局记录聚类出本地环境
python -m hsai cards --update             # 更新卡牌库
python -m hsai log                        # 终端里跟日志
python -m hsai replay <日志文件>           # 离线解析，输出快照 JSON
python -m hsai mulligan WARRIOR 寒冰箭     # 离线测留牌
python -m hsai sim <原型A> <原型B>        # 自对弈两套卡组，看谁赢
python -m hsai learn [--rounds N]         # 自对弈 + 坐标轮换爬山优化权重
python -m hsai review [-n 3]              # 复盘最近几局的斩杀/空过法力
python -m hsai stats                      # 对局统计
```

---

## 文件地图

### 根目录

| 文件 | 用途 |
|---|---|
| `启动AI助手.bat` | **主入口**。找 Python → 首次运行下载卡牌库 → 写 `log.config` → 环境自检 → 用 `pythonw` 起悬浮窗（不弹黑框）。传 `demo` 参数可走演示 |
| `演示模式.bat` | 转调主入口并传 `demo`：用示例对局回放，不接游戏 |
| `start.bat` | 旧名字的兼容壳，等价于 `启动AI助手.bat` |
| `pyproject.toml` | 打包与工具配置：`pip install -e .` 后可直接敲 `hsai`（等价 `python -m hsai`），并统一放 ruff / pytest 配置 |
| `.gitignore` | 挡掉 `data/` 下的运行产物与 `__pycache__`，仓库里只留代码和规则表。与 `hsai/paths.py` 一一对应 |

### hsai/ —— 主程序

按数据流分成五层，加一条 AI 旁路：

```
Power.log ──► logwatch ──► powerlog ──► state ──┐
                                                ├─► advisor ──► ui
        infer / mulligan / policy / model ──────┘
                    （AI 只旁挂在这条链路外侧）

横切各层的纯工具：classify.py（卡牌文本分类）、paths.py（data/ 路径）、logutil.py（诊断日志）
```

**入口与界面**

| 文件 | 用途 |
|---|---|
| `__main__.py` | `python -m hsai` 的入口，只有一行：转 `cli.main()` |
| `__init__.py` | 版本号 + 三条设计原则（只读日志、推断带依据、离线优先） |
| `cli.py` | 命令行总调度：`ui / cards / meta / deck / replay / log / sim / learn / review / stats / setup-log / mulligan` |
| `ui.py` | 悬浮窗本体（1400+ 行，最大单文件）：无边框、置顶、拖动、F8 全局显隐、右键菜单、自动收细条、三块信息区渲染。界面只读 `advisor` 给的快照，自己不做判断 |

**输入层：日志 → 状态**

| 文件 | 用途 |
|---|---|
| `logwatch.py` | 跟随 `Power.log`：增量读取、断点续读、换局重置。并负责**找日志在哪**——先问运行中的 `Hearthstone.exe`，再看缓存，最后才用默认路径（炉石至少有三种安装/会话子目录形态） |
| `powerlog.py` | `Power.log` 词法/语法解析。处理三件现实：tag 打的是名字不是数字、实体引用有四种形态、同一次变更在 `GameState` 和 `PowerTaskList` 里重复出现（幂等去重） |
| `state.py` | 把事件流归约成对局状态：双方英雄、手牌、场面、牌库、回合、阶段，以及 `zone_pos` / 休眠 / 亡语 / 克苏恩进度。每处读取都有回退，编号变了只降级不崩 |
| `live.py` | 数据源开关：决定现在看真实日志还是示例回放，并支持中途切换（先开助手后开游戏、游戏重启换会话目录） |
| `decks.py` | 从客户端 `Decks.log` 解析**自己的**卡组（卡组名 + 30 张 + deck ID），不用手动导入。对手的日志里没有，只能靠打出来的牌反推 |

**数据与知识**

| 文件 | 用途 |
|---|---|
| `cards.py` | 卡牌库：从 HearthstoneJSON 抓取、规范化、缓存到 `data/cards_zhCN.json`，加载 8000+ 张约 100ms |
| `classify.py` | 卡牌文本分类：`is_aoe` / `is_single_removal` / `is_taunt` / `is_heal`。纯函数、只依赖 `cards.Card`，预判和留牌都从这里取，避免下层依赖上层的私有实现 |
| `logutil.py` | 运行时诊断日志的唯一写入口：`log()` 统一时间戳、超 1 MB 自动截断保留最后 2000 行。此前 advisor / state / ui 三处各写各的、格式不一且无轮转 |
| `paths.py` | **`data/` 下所有路径的唯一出处**，也是唯一允许写文件名的地方。此前 `lessons.jsonl`、`effects.json`、`hsai.log` 各有 2–3 份重复定义，改一处忘一处就会「记了但查不到」且不报错 |
| `deckcode.py` | 卡组代码（`AAEC...`）编解码：LEB128 varint + base64，格式在文件头写全了 |
| `meta.py` | 卡组原型库：一个原型 = 职业 + 标志牌及张数，剩余槽位用职业牌池平滑补齐。三来源：`data/meta.json` > 内置种子 > 自举聚类；同职业重复原型自动合并 |
| `model.py` | 共享局面模型 + 卡牌效果解释器 + `evaluate()` 评估函数。policy、sim、learn 三处共用，避免规则写三遍。**不是完整规则引擎**，建模不了的牌标记「效果未知」并降低推荐权重 |

**判断层**

| 文件 | 用途 |
|---|---|
| `infer.py` | 对手卡组预判：朴素贝叶斯 + 职业先验，按回合给牌加权。除了「对面是什么」，还回答「手上大概率有什么」「有没有清场/单解」「还可能抽到什么」 |
| `mulligan.py` | 留牌打分：费用基础分 + 对位修正 + 功能修正 + 重复惩罚，权重在 `DEFAULT_WEIGHTS` 里可调 |
| `policy.py` | **建议引擎核心**（1300+ 行）：斩杀检测（精确到每点伤害）、动作枚举与模拟评分、抢血/解场模式判定、路线贪心组合。1 层搜索，不是 MCTS——擅长斩杀/交换/站场，不擅长多回合 combo |

**输出层**

| 文件 | 用途 |
|---|---|
| `advisor.py` | 把「日志状态 + 预判 + 建议」组装成一份快照 JSON，是**窗口唯一数据源**。纯胶水层，但决定了好不好用：缺数据时降级而不是报错 |
| `review.py` | 局后事实层复盘：只比对日志里确定发生过的事（有斩杀没斩、手牌溢出烧牌），不猜你的想法 |
| `memory.py` | 长期记忆：带「我方职业/对手职业/对手形态」标签的教训 + 可检索的历史案例，用时只取匹配的那些 |

**AI 旁路（可选，不参与决策）**

| 文件 | 用途 |
|---|---|
| `ai.py` | AI 客户端。出牌建议永远来自 `policy.py`，这里只负责复盘教训和「为什么这么打」的人话解释。只在回合变化时调一次，失败静默降级 |
| `effects_ai.py` | 让 AI 补卡牌效果正则，但**三道闸门**验证：目标牌必须从「未建模」变「能解析」、已能解析的牌结果一个都不能变、单条规则最多新覆盖 3 张牌。通过才写进 `data/effects.json`（先备份） |
| `feedback.py` | 对被点了「不赞同」的回合做复盘：输入是「引擎建议 vs 实际打法」，产出教训和可落地规则。提示词和 `ai.py` 的对局复盘不同，所以单独一层 |

**自对弈与学习**

| 文件 | 用途 |
|---|---|
| `sim/engine.py` | 自对弈引擎：两套牌交给两个 agent 真打完一局。是学习闭环的训练场，也是验证建议引擎的工具。模拟胜率只能做**相对比较**，不等于天梯胜率 |
| `sim/__init__.py` | 导出 `match / run_game / greedy_agent / random_agent` 等接口 |
| `learn.py` | 自主学习三件事：记录对局到 `observations.jsonl`、从自己的对局里聚类出本地环境（自举 meta）、用自对弈坐标轮换爬山优化权重 |

### tests/ —— 106 个用例

`python -m unittest discover tests` 跑全部。`fixtures/` 里两份日志：`sample_game.log`（构造的示例局）和 `real_client_excerpt.log`（真实客户端片段，用来锁解析行为）。

`test_sim` 一个文件就占约 50 秒（自对弈真在打），只想跑快的：

```powershell
python -m unittest discover tests -p "test_[!s]*.py"
```

| 文件 | 覆盖什么 |
|---|---|
| `test_advice.py` | 预判概率归一、标志牌改变结论、留牌打分、建议生成、快照组装（用例最多） |
| `test_log.py` / `test_logwatch.py` | 日志解析与跟随：tag、实体引用、我方判定（看手牌不看账号）、换局重置、路径探测、安装目录缓存 |
| `test_policy_rules.py` / `test_risk.py` / `test_race.py` | 斩杀线、风险提示、抢血/解场模式切换 |
| `test_refs.py` | 实体引用四种形态的解析 |
| `test_cards.py` / `test_deckcode.py` | 卡牌库加载、卡组代码往返编解码 |
| `test_sim.py` | 自对弈引擎跑通与结果稳定 |
| `test_ai_layer.py` / `test_effects_ai.py` | AI 层降级行为、三道闸门、JSON 容错（**不联网**） |
| `test_render_smoke.py` | 界面冒烟：各种快照（斩杀、空建议、复盘、留牌、未知卡组）渲染不报错。**缺 tkinter 时用 `_ui_support.py` 的替身跑**，验的是渲染路径不抛异常，不是像素 |
| `_ui_support.py` | 界面测试的公共准备：没有 tkinter 时装替身（只记控件树和文字），并把 `window.json`、`install_dir.txt` 改道到临时目录，免得跑测试冲掉用户的窗口摆位 |

### tools/ 与 examples/

| 文件 | 用途 |
|---|---|
| `tools/preview_layout.py` | 布局预览：只用假数据画三块区域，改界面前先看效果，不接真实对局 |
| `examples/my_deck.json` | 手写卡组格式示例（`name / class / tags / cards`），`deck mine` 的输入参考 |
| `docs/overlay.png` | 悬浮窗截图 |
| `docs/preview.png` | 完整效果预览图 |

### data/ —— 运行产物

除 `effects.json` 外**全部不进仓库**（见 `.gitignore`），删掉会自动重建。
所有路径集中在 `hsai/paths.py`，`.gitignore` 与它一一对应。

| 文件 | 谁写 / 谁读 | 内容 |
|---|---|---|
| `cards_zhCN.json` | `cards.py` | 卡牌库缓存（约 4 MB，首次运行下载） |
| `effects.json` | `effects_ai.py` 写，`model.py` 读 | 卡牌效果正则规则表。**这份入库**，因为它是可复现的行为定义 |
| `effects.json.bak` | `effects_ai.py` | 改规则前的自动备份 |
| `meta.json` / `meta.backup.json` | `meta.py` | 卡组原型库（导入或自举聚类的结果）及其备份 |
| `observations.jsonl` | `learn.py` | 每局的对手职业、已暴露的牌、胜负 |
| `games.jsonl` | `review.py` | 完整对局记录，复盘的事实来源 |
| `lessons.jsonl` | `ai.py` / `memory.py` | AI 总结的教训，带对局标签 |
| `cases.jsonl` | `memory.py` | 具体局面 → 当时怎么打 → 结果，比抽象教训更容易被模仿 |
| `opp_history.jsonl` | `memory.py` | 对手形态历史（近期打法先验） |
| `advice_log.jsonl` | `ui.py` | 每条发出去的建议，事后对账用 |
| `advice_feedback.jsonl` | `ui.py` | 被标「不赞同」的回合 + AI 复盘结果 |
| `last_review.json` | `advisor.py` / `ui.py` | 上一局复盘，窗口收起时显示 |
| `our_deck.json` | `advisor.py` | `deck mine` 登记的自己的卡组 |
| `mulligan_weights.json` | `learn.py` / `advisor.py` | 留牌权重（自对弈调出来的） |
| `policy_weights.json` | `learn.py` / `advisor.py` | 出牌评估权重 |
| `ai.json` | `ai.py` | AI 配置（可能含 key，**绝不入库**）；`turn_advice: true` 可开逐回合解释 |
| `window.json` | `ui.py` | 窗口位置、透明度、字号、折叠状态 |
| `install_dir.txt` | `logwatch.py` | 探测到的游戏安装目录缓存 |
| `hsai.log` | `logutil.py`（由 advisor / state / ui 调用） | 运行时错误与诊断日志，出问题先看这里。超 1 MB 自动截断保留最后 2000 行 |

---

## 已知限制

1. **独占全屏下悬浮窗压不上去**，用「窗口化」或「无边框窗口」。
2. 卡组效果解释器只认常见文本；认不出的牌会列进「未建模」，并尝试让 AI 补规则。
3. 判定「谁是我方」靠标签推断，同职业对战且客户端挂错归属时可能判错（已按职业+controller 三层兜底）。
4. 对手手牌内容看不到，只能给概率——规则决定。
5. 只支持常规对战；酒馆战棋、竞技场选牌、佣兵不解析。

## 隐私

卡牌库来自 `api.hearthstonejson.com`；AI 复盘会把**对局摘要**（职业、卡组名、回合数、失误）
发给配置的 API。密钥只从环境变量 `HSAI_API_KEY` 读，不写入任何文件。
`data/` 下的对局记录、教训、建议日志全部本地保存且不进仓库。
