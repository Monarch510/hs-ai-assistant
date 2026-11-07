# 代码审查与清理报告

2026-11-07 · Team Lead + 5 名审查员（core-engine / infra-data / ui-cli / dead-code-hunter / tests-crosscut）

## 结论摘要

| 项 | 结果 |
|---|---|
| 审查范围 | `hsai/` 全部 70 个模块、`tests/` 48 个测试文件、构建配置与文档；全仓 `ast.parse` 干净 |
| 清除无用代码 | **31 个文件、−164 行**（未用 import、零引用函数、只写不读状态、恒不可达分支、重复代码） |
| 订正文档 | **4 个文件、22 处**（README 18 / AGENTS.md 2 / effects_ai.py 文件头 / 测试陈旧注释） |
| 用户已确认的改动 | `.gitignore` + AGENTS.md 硬规则 4 改为 `data/*` + `!data/effects.json`（整目录忽略、例外放行） |
| 行为代码 | **一行未改**；缺陷只报告、不修（用户选定「只删死代码 + 出缺陷报告」） |
| 验证 | 全仓 121 文件 `ast.parse` 干净；结构契约断言 `VERIFY OK`；**507 tests OK、退出码 0**（删除后与文档订正后各跑一次） |

> 工作区原本就有约 60 个文件的未提交改动（他人工作，含 `tag_rules.py`/`test_tag_rules.py` 大改与 `auto-task/` 删除），本报告不改、不回滚、也不把那些改动算作本次成果。

---

## 一、缺陷清单（只报告，未修）

### 必须修 · 13 条（已跨报告去重）

| # | 位置 | 触发场景 | 后果 | 建议修法（最小） |
|---|---|---|---|---|
| 1 | `hsai/ui.py:434-441` | 不关悬浮窗连打 2 局 | 按局重置块漏了 `_ai_reviewed`，第 2 局 `_maybe_ai` 恒不起线程 → `lessons.jsonl`/`advice_tuning.jsonl` 不再新增，局末复盘与「不赞同」复盘全停摆，静默 | 重置块补 `self._ai_reviewed = False` |
| 2 | `hsai/ui_ai.py:331` | 回合结束后反馈复盘 | `_log_advice` 用新 dict 整桶覆盖 `_advice_by_turn[turn]`，吞掉 `ui.py:430-433` 每帧写的 `actions` → `rec.get("actions")` 恒空 → `feedback.build_summary`「我实际打的牌」永不输出 | 改 `setdefault` + `update`（局部变量 `rec` 已是日志 dict，桶要换名） |
| 3 | `hsai/ui.py:77-80`、`hsai/ui_state.py:15-20` | `data/window.json` 顶层被写成 `null` / `[]` / 字符串 | 两处 `_load_state` 只 catch `(OSError, ValueError)`，这些值不抛异常 → `saved.get("layout",0)` 抛 `AttributeError` → 弹「启动失败」，pythonw 下双击无反应 | `data = json.loads(...); return data if isinstance(data, dict) else {}` |
| 4 | `hsai/live.py:41` | `python -m hsai ui --demo` 的日志含非 UTF-8 字节 | `read_text(encoding="utf-8")` 无 `errors` → `UnicodeDecodeError` 启动失败；而 `cmd_replay` 与 `PowerLogWatcher.poll` 都有 `errors="replace"` | 加 `errors="replace"` |
| 5 | `hsai/cli_setup_log.py:29-31` | 炉石装在 `C:\Program Files`、非管理员双击启动 bat | `changed` 只认「已创建/已覆盖」，`setup_log_config` 权限不足返回「写入失败」→ 打印「日志开关：已开启」`return 0`，实际无 `log.config`、游戏不写 Power.log | 收紧为 `all("已开启" in r[1] for r in results)` |
| 6 | `hsai/logwatch.py:233` | Power.log 轮转 / 清空重建、半局无 `CREATE_GAME` | `tail_start_offset` `return 0` → `_offset=0` 从头重放历史行，界面显示几局拼起来的半局；与 `:262-265` 注释「绝不要从头重放」相反 | `return size`（`:213` 已取） |
| 7 | `hsai/advisor_board_helpers.py:89-91` | `_finish_game` 抛任意异常 | `_log_err` 调了未 import 的 `logutil` → `NameError: name 'logutil' is not defined` 冒出 `snapshot()`，UI 每帧刷新崩；且 `advisor.py:373 self._recorded = True` 永不可达 → `observations.jsonl`、`games.jsonl` 重复追加一局、重复 absorb | 优先删 `_log_err` wrapper 与 `advisor.py:52` 的 import，`advisor.py:372` 直接 `logutil.log(...)`；或 helpers 顶部补 `from . import logutil` |
| 8 | `hsai/meta.py:163`（及 `:109`） | 手改 `data/meta/<id>.json` 把 `prior` 写「高」、`count` 写「x」 | `_archetype_from_dict(d)` 在 try 块外，`meta_types.py` 的 `float()`/`int()` 抛 `ValueError`/`TypeError` → `MetaDB.load()` 抛 → `Advisor.__init__` 崩 → `hsai ui` 起不来 | 两调用点收进 try、except 扩成 `(OSError, ValueError, TypeError)`，字段用 `isinstance` 兜底返回 `None` |
| 9 | `hsai/state_types.py:133-154` / `hsai/ai.py:153` / `hsai/ui_ai.py:39` | `turn_advice=true`（逐回合 AI） | `Side.as_dict()` 从不输出 `hero_power_used`，两处读取方永远拿到 `None`（引擎侧 `advisor.py:169` 只写 Board）→ 每回合误触发白烧 token、AI prompt 技能状态长期错 | Side 加字段并在 `as_dict` 透出，或读取方改用引擎 Board 同名字段 |
| 10 | `hsai/policy_lethal.py:58` | 对面有随从 + 手里有「随机对一个敌人造成 N」+ 对手血量差 N | 白名单含 `"random_enemy"` 无条件计入 `spell_dmg`，但 `model_units.py:307-312` 实际打随从 → `lethal=True` 虚报；`policy_plan.py:202-205` 兜底只在 `not plan` 生效 | 去掉 `"random_enemy"`，或仅当 `foe.board` 无存活随从才计入 |
| 11 | `hsai/policy_actions.py:282-295` | 对面有随从 + 法力够技能 + 血量差 1~2 点斩杀线 | hero_power 分支硬编码 `"enemy:0"` 忽略 `action.target`，而 `legal_actions:226-235` 同时生成打脸/打随从两种动作 → `_lethal_sequence` 兑现不了，斩杀被吞；带随从目标的伤害型英雄技能在 1 层搜索/斩杀序列算错 | `:286-289` 按 `action.target` 分流（两份报告同一处，已合并） |
| 12 | `hsai/policy_choice.py:66` | 发现/抉择里出现「对所有敌方随从造成 2 点伤害」 | scope 白名单拼成单数 `"enemy_minion"`，而 `parse_effect` 产 `"enemy_minions"` → `:70-74` 整段 AOE 解场加分（`3.0*2 + 0.3*最大攻击`）跳过，只拿 2.0 分被无关牌压过；`"minion"` 也从不被产出 | 改复数 `"enemy_minions"` 并去掉 `"minion"`（一行） |
| 13 | `hsai/sim/engine.py:146-153` | 跑 `hsai learn` 自对弈 | 每回合只重置 `hero_power_used`/`attacks_left`/`frozen`，从不设 `weapon_attacks_left`/`hero_attacks_left`/`hero_atk`（默认 0）→ `legal_actions:112` 不生成 hero_attack，武器只贡献站场评分、永不打脸 → 学出的权重对武器牌系统性偏差 | 补 `weapon_attacks_left`（风怒 2 否则 1）与 `hero_attacks_left = 1` |

### 建议修 · 20 条（按主题）

**已在本次清理中直接消除的 4 条**（属「无用代码」而非行为缺陷）：
`hsai/state.py` 恒不可达的 `_on_full_entity` ZONE_POSITION 分支（7 行）、`hsai/policy_choice.py:207-208` 重复计算 `skill_pairs`、`hsai/model_units.py:134-137` 零调用 `Board.me()`/`Board.them()`、`hsai/cards.py:244` 不可达的重复 `return True`。

**未处理（行为代码，按用户范围不改）**：

| 位置 | 问题 / 触发 | 建议修法 |
|---|---|---|
| `hsai/powerlog.py:133` | `return key if key in KNOWN_TAGS else key` 恒等三元，过滤从未生效 → 未知 tag 全进 `e.tags[tag]` 无界增长 | `return key if key in KNOWN_TAGS else None`（`:188-190` 已处理 None）或删 `KNOWN_TAGS` |
| `hsai/model_eval.py:113` | `u.hp + (0 if u.divine else 0)` 恒等表达式，圣盾加值写了一半；与 `policy_lethal.py:38` 的 `3 if u.divine else u.hp` 口径不一致 | 删恒等项或补 `+3`，两处统一 |
| `hsai/policy_actions.py:25-30`、`hsai/policy_plan.py:25,30` | 用 `card_id` 判「同一只」；`board.clone()` 后 `x is u` 恒 False → 两只同 card_id 随从 AOE 死一只时两只都判没死 → 解场加分丢失 | 给 `Unit` 加 clone 保留的 uid |
| `hsai/ui.py:56-93`、`:77-88` | 门面重定义 20 个主题常量与 `_load_state/_save_state`，与 `ui_theme`/`ui_state` 双份真相；改一处配色/实现则界面一半旧一半新（`hud_row` 有 assertIs 保护，这批没有） | 删重复定义只留 `:47-51` re-export（注意 `tests/test_deckcode.py:126` 用 `ui.LAYOUT_VERSION`） |
| `hsai/ui.py:101`、`:148` | `__init__` 读两遍 `window.json` | `:148` 改 `set(saved.get("collapsed_sections") or [])` |
| `hsai/cli.py:78-81` | `serve` 别名缺 `--check`，help 却称「等同于 ui」→ `hsai serve --check` 退出 2 | 优先删整个别名（3 个 .bat 全用 ui、无其它调用方），或补参数 |
| `hsai/cli.py:89` / `hsai/cli_sim.py:28,33` | `--max-turns` 非 `--trace` 分支静默失效（`sim.match` 不透传、内部默认 50） | 删参数，或 help 注明「仅 --trace 生效」 |
| `hsai/advisor.py:60,134-135,386` | `_PASSIVE_RULE` 模块级单槽全局，`board_from_state` 只在 `not _PASSIVE_RULE` 时 update → 同进程另一个不同职业 `Advisor` 永不重算 | 改为按实例/职业缓存 |
| `hsai/memory.py:45-61` | `_append_jsonl` cap 截断用裸 `write_text`，而 `learn_calibrate.backfill_truth`/`cards.update` 用 `.tmp + os.replace` → 进程被杀留半截文件、`_read_jsonl` 静默跳坏行 | 照抄原子写 |
| `hsai/decks.py:117-147` | `best_match` 按 card_id 硬比，与 `cards.siblings()`/`infer` 按牌名认亲口径不一致 → `CS2_029` vs `CORE_CS2_029` 认不出 → 面板整局不显示我方剩余牌 | 加可选 `CardDB` 用 siblings 归一 |
| `hsai/cards.py:325-326` | 合法 JSON 但缺 `rows` 键 → `payload["rows"]` `KeyError` → `hsai ui` 在 `cards_mod.load()` 崩 | `payload.get("rows") or []` + 包 `(OSError, ValueError)` |
| `hsai/review.py:3-7,53-64` | docstring 承诺「手牌溢出」「最多 5 条」，实现只有 `lethal_available` 一支、最多 1 条；`TurnRecord.hand` 只写不读 | 删未实现承诺或补 `hand >= 10` |
| `hsai/tags_ai.py:178,383` | 写死「固定 10 类」，实际 `TAG_ORDER` 已是 12 类（多了「保护」）；结果按 12 判没错但提示误导 | 改动态 `len(order)`/`len(TAG_ORDER)` |
| `hsai/advisor.py:743-777` | `observations.jsonl` 7 个字段只写不读；`_new_game_id()` 注释承诺串联 observations/games/cases，但 `GameRecord.as_dict()` 无 `game_id` → 串联根本没实现 | 删注释里未实现的承诺（保留 schema：`tests/test_learn.py:270,283` 断言 `pred_archetype`/`trace`） |
| `hsai/ui_ai.py:341-343` | 每写一条 advice_log 就全量 `read_text().splitlines()` 才判 >400 行截断，O(n)（上限几十 KB，优先级最低） | 可不修；要修改为先 `stat` 再读 |
| `hsai/paths.py:55` / `hsai/ui_ai.py:338-343` | `advice_log.jsonl` 程序侧只写不读（人工对账用途），README 已注明 | 保持现状；若不需要对账可连写入一起删 |

### 测试缺口（未补用例，按优先级）

- **G1** 跨局 / 第二局 `Advisor` 生命周期无用例：删 `hsai/advisor.py:374-386` 任一行，第一局全绿、第二局静默错（第 1 条缺陷正是这个坑的新实例）。
- **G2** `Side.as_dict()` 字段全集 + `build_prompt` 技能状态无契约用例（`test_secret_zone.py:160` 只断 secrets/quests/ongoing）→ 第 9 条缺陷无人能发现。
- **G3** 斩杀 ↔ `apply_action` 对账只覆盖 `scope=="target"` 一种（`test_lethal_complete.py:32-41`）→ 第 10 / 11 条漏网。
- **G4** `_has_options` 边界（`hsai/ui_ai.py:30-41`）无用例，`test_end_turn.py:178` 只测 turn_ending。
- **G5** 坏数据展示降级：`hsai/ui_text.py:31-32` 对 `deck_count=None` 会显示「牌库 None」，违硬规则 6「读不到显示未知」。

### 保留判活（确认不是死代码，未删）

门面 re-export（`model/policy/state/advisor/meta/learn/tags/ui/cli/infer`）；`board_from_state` 本体必须物理留 `hsai/advisor.py`（`test_advisor_board.py:43` 源码切片 + 后面须有 `\ndef `）；可写全局（`model._EFFECT_TABLE` / `state._EFFECT_CACHE` / `advisor._PASSIVE_RULE` / ui 路径常量）；`ui.hud_row`（`assertIs`）；`cli._print_calibrate_report`（`test_learn_report.py:120,129`）；`state.py` 的 `elif kind == "block_start"` 计数须为 1（`test_state_reset.py:166`）；`ui.py` 主题常量重定义与重复 `_load_state/_save_state`（两份各有调用方，见建议修）；`effects_ai` 模块整体（tests 依赖 `validate_rule`/`_apply_one`）；`_log_advice`/`ADVICE_LOG_JSONL`（供反馈复盘 + 3 处测试 `mock.patch`）；`Effect.is_empty()`（`tests/test_cards.py:63` 调用，删须同时改测试，收益低）；`ai.py` `SESSION_SYSTEM` 两处赋值（导入时一次性追加）；`tools/tags_ai.py` `cmd_run`/`cmd_validate`（该文件 `main()` 分派）；CLI `cmd_*`（`set_defaults(func=...)` 分派）；`tests/_ui_support.py` 的 8 个 tkinter 替身桩。

---

## 二、本次清除的无用代码（31 文件 / −164 行）

删除脚本先 `DRY OK: 31 files, -164 lines total; nothing written.` 全部编辑精确匹配 1 次、`ast.parse` 通过、被删 import 名无残留 `Name(Load)`，才正式写盘；写盘后回读再 `ast.parse` 一次。

| 组 | 内容 | 证据 |
|---|---|---|
| A · 27 处 | `cli_cards.py:6,7,8,11`、`cli_deck.py:7`、`cli_learn.py:7,8`、`cli_live.py:7`、`cli_meta.py:7,12`、`cli_mulligan.py:6,7,8`、`cli_review.py:6,7,8`、`cli_setup_log.py:6,7`、`cli_sim.py:6,7,8`、`cli_stats.py:13`、`cli_tags.py:6,7,8`、`cli_ui.py:6,7` 的未用 import（`json`/`sys`/`Path`/`_db`/`Archetype`/`Any`） | 本文件内零 Load、零字符串引用；全仓无 `cli_<mod>.<name>` 访问；不在 `__all__`；非 CLI 分派、非 `patch.object` 目标。`cli_ui.py:18` 的 `import tkinter` 是函数内局部 import，保留 |
| B · ~24 处 | `ui_ai.py:12,21,24`、`ui_render.py:5,10,11,12,15,16`、`ui_review.py:8,11,12`、`ui_widgets.py` 的 `TEXT_SOFT`、`model_eval.py` 的 `SideState`、`model_units.py:12` 的 `Any`、`policy_actions.py:14-18` 的 `EPS/MAX_STEPS/PRIORITY_BONUS/TAG_WEIGHT_SCALE/passive_rule`、`policy_threat.py:7,8` 的 `Card/Effect/Unit/hero_power_effect/is_passive_hero_power/parse_effect` | 陷阱已避开：`me.passive_rule` 是实例属性不是 Name Load；`policy_threat.py` 的 `Board` 在类型注解有用，只删同行其它符号；`ui.py:30` 的 `logutil` 在 `:518` 真实使用，保留 |
| C · 门面多余符号 | `advisor.py:11 math`、`:22 logutil`、`:26 Card`；`cli.py:16 LOG_PATH`、`:20 _deck_from_code`、`:22 _print_live`、`:24 _backup_meta`、`:26 _rebuild` | `.audit_extrefs.py` AST 扫全仓 `<mod>.<attr>` 外部访问集确认零引用。`cli.py:17 _db`（兼容旧引用）、`:25 _print_calibrate_report`（测试调用）、`infer.py:30-33`（注释明写门面 re-export）全部保留 |
| D · 拆分遗留 | `learn.py:22-28,30-32,35,36,37` 顶层孤儿 import；`:34` 改成只留 `OBSERVATIONS_PATH, WEIGHTS_PATH` | 真正的 re-export 是 `:42-53` 四组 `from .learn_* import`；`DEFAULT_WEIGHTS`(`:38`)、`OBSERVATIONS_PATH`、`WEIGHTS_PATH` 被 `cli_learn.py:33,47,58` 与 `tests/test_sim.py:76` 使用，保留 |
| E · 结构性 | `effects_ai.py:182-202 commit_rule()`（21 行）及连带 `EFFECTS_BACKUP`；`paths.py:34 EFFECTS_BACKUP`、`:49 META_BACKUP`；`cards.py:244` 重复 `return True`；`advisor.py` `Collected` 类 / `last_collected` / snapshot 里 collected 4 行 / 类属性 `_passive_rule`；`state.py` 6 个只写不读属性 + `:776` 死局部变量 + 恒不可达分支 + QUEST_PROGRESS elif 段；`powerlog.py` 无读取方的 `target`/`effect_card_id`；`logwatch.py` `follow_from_end` 参数/分支、`generation`；`policy_choice.py:207-208` 重复 `skill_pairs`；`model_units.py` `me()`/`them()`；`ui.py`/`ui_ai.py`/`ui_render.py` 的 `_hint_busy`/`_hint_result`/`_ai_busy` 赋值 | `commit_rule` 全仓仅定义 1 命中，功能已下线（`ui_ai.py:6`、AGENTS.md:10-13）；`target_ref` 有读取方（`state.py:161`）保留；连带孤儿 `Counter`（state.py:15）与 `dataclass`（advisor.py:15）一并收口 |

**控制流难点（均已验证）**：`state.py` 删 `elif kind == "block_end":` 整个分支以免空分支 SyntaxError，且 `elif kind == "block_start"` 计数仍为 1；`powerlog.py` 删数字 target 后把 `elif tm2 := ...` 改成独立 `if`；`logwatch.py` 删 `follow_from_end` 时一并删掉变成悬空的 `stat()` try 块；`advisor.py` 删 Collected 但保留 `pred = self._predict(st, opp)`。

---

## 三、文档订正（4 文件 / 22 处）

- **README.md（18 处）**：标题改为「AI 只在局末复盘」；:68 未建模的牌改「引擎只记录到 `data/hsai.log`、人看过复盘后手动补」；:70「1 次（局末复盘）」；:172 `.gitignore` 改「`data/*` 整目录 + `!` 放行、不再逐条列」；:191 `__main__` 不再说「只有一行」；:193 cli 命令表补齐 `serve/tunings/tags`（共 15）；:194/:225 去掉过时的「1400+ 行」「1300+ 行」改「实现拆进子模块、门面只 re-export」；:239「一局大约调一次（局末）」；:240 `effects_ai.py` 改为「验证器，写回已下线、运行时零调用」；:248 去掉不存在的 `random_agent`；:251/:253 去掉写死的「266 个用例」（按 docs/plan/07-文档同步.md 的「只写命令不写数字」方针）、命令改 `python -m unittest discover -s tests`、fixtures 两份→三份（补 `secret_zone_excerpt.log`）；:285-286 data/ 口径；:291-292 删 `effects.json.bak` 行、`effects.json` 写方改「人手动维护，`model.py` 读」；:294 `meta_backup/` 写方 `cli.py`→`cli_meta.py`；:301 `advice_log` 注明「程序不读、超 400 行滚动截断」；:317 已知限制改「只记录、人手动补」。
- **AGENTS.md（2 处）**：:20 去掉 `# 266 个用例`；:74-76 常量名订正为 `STATE_PATH / LAST_REVIEW_JSON / ADVICE_FEEDBACK_JSONL`（由 `tests/_ui_support.py` 赋值）与 `ADVICE_LOG_JSONL`（测试 `mock.patch` 改道）。硬规则 4 保持「`data/` 整目录忽略，只放行 `effects.json`」。
- **hsai/effects_ai.py**：文件头从「让 AI 补规则、验证后写进 effects.json」改为「验证器；写回已下线、不写盘、运行时不调」，保留三道闸门与「为什么这么严」。
- **tests/test_tags_audit.py:15**：去掉对已删除的 `tests/test_inbox_cli.py` 的引用。

**有意未改**：`hsai/paths.py` 文件头历史说明、`docs/plan/*`（`00-现状.md:53-56`、`99-冲突检查.md:126` 明写 `weights.py`/`classify.py` 已删，属正确历史记录）、README 隐私段、`hsai/ui_ai.py` 文件头（本就正确）。

---

## 四、验证记录

```
.audit_verify.py   INFO ast.parse: 121 files clean
                   INFO state.py elif kind == "block_start": 1 (must be 1)
                   INFO advisor.py board_from_state at char 1482, later '\ndef ' present: True
                   INFO advisor.py me.weapon = Unit(: 1 (must be 1)
                   VERIFY OK: all structural contracts hold

python -m unittest discover -s tests   →  Ran 507 tests ... OK   (EXITCODE=0)
```

> 环境要点：必须设 `$env:PYTHONIOENCODING="utf-8"`，并把 `$env:TMP`/`$env:TEMP` 指到工作区内的 `.audit_tmp`（否则 `tempfile` 落到系统临时目录会 `PermissionError: [WinError 5]` 假失败）。PowerShell 下必须把 stdout/stderr 重定向到文件再读 `$LASTEXITCODE`；`2>&1` 管道会把 tkinter 替身桩的 `invalid command name "..._do_fit"` 当成 error record，导致 `OK` 却报 exit 1。

---

## 五、建议的后续顺序

1. 先修必须修 1 / 2 / 7（都是「静默、第二局才显现或每帧崩」），每条先补一个复现用例（对应 G1 / G3）。
2. 再修必须修 10 / 11 / 13（斩杀与自对弈口径），配 G3 的对账测试；改 `max_face_damage` / `apply_action` / `model.apply_effect` 时先 grep 另一处（AGENTS.md 坑里有同名要求）。
3. 然后修 3 / 4 / 5 / 6 / 8（启动与降级），配 G5 的坏数据用例。
4. 最后处理建议修里的「双份真相」与口径不一致项；每改一处跑一次 `python -m unittest discover -s tests`。
