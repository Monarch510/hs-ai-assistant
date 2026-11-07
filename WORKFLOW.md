# WORKFLOW.md

给 Lead 看的分流操作手册：哪些活丢给 WorkBuddy、怎么丢、怎么收。
给干活的 agent 看的规范在 [AGENTS.md](AGENTS.md)，别混。

## 为什么这么定

Agent Teams 的成员不能单独换模型：当前只有 `lead` 一个（`hy4free`），
`spawn_teammate` 签名没有 model/provider 参数；`subagent` / `workflow` 虽有参数，
但注册表只有 `workbuddy-global/hy4free`（推理档只有 `high`），设了等于没设。
所以「异构」不靠给成员换模型，靠**谁干活**：把独立自包含的活整包路由给
WorkBuddy，DSH 自己干需要本会话上下文的活，两者不重叠。

WorkBuddy 那三个聊天会话（`cn`×2、`global`×1）不是派单对象：走 `reply` 续的是聊天
上下文，不干净。派单对象是临时起的 `subagent`（票）——一票一单，票里调 `codebuddy_ask`
另起独立 CLI agent。用 `subagent` 而不是 `spawn_teammate`：它不吃 Team 的 8 个成员名额。

## 通道与并发

- **环境前提**：`codebuddy_ask` 依赖本机 CodeBuddy CLI。没装时插件**直接报错、不回退 PATH**。
  装 `@tencent-ai/codebuddy-code`，或在 `~/.dsh/profiles/<档>/cordis.patch.yml` 的
  `dsh-connect-workbuddy` 段用 `cliBridge.binary` 指向包内 `bin/codebuddy`（别指向 `.cmd`）。
- 改那几行配置会触发插件热重载，中间可能瞬时 `unknown tool`，重试一次即恢复。
- 用 DSH 的 `codebuddy_ask`，每轮另起一个独立 CLI agent，不续现有会话。
- **派单对象 = 临时起的 `subagent`（票）**：一票一单，用完即弃。任务书直接写进
  `subagent` 的 prompt，票只负责调一次 `codebuddy_ask`。
- **一任务 = 一票 = 一次调用 = 一个独立 WorkBuddy**：它看不到本会话，也不知道别的票
  在跑什么，自隔离天然成立。
- **`subagent` 不吃 Team 名额**：Team 成员上限 8 且无删除接口，`spawn_teammate` 满了
  就起不来；`subagent` 不受这个约束（实测团队满员时仍能起两张票）。
- **并行靠 `agent.id`**：插件按调用方 `agent.id` 计并发车道，同一 agent 同时只能跑一个
  CodeBuddy 回合（`allowConcurrentPerSession` 硬编码关）。不同票 id 不同，所以能真并行；
  实测两票同时各调一单，均成功、无 busy、各拿独立 session。
- **票不复用**：复用会让同一 CLI 会话跨任务续接（`sessionContinuity=auto`），任务串味。
  要复用同一张票，先在配置里设 `sessionContinuity: off`。
- **派不动就自己干**：票的 `codebuddy_ask` 失败（429 / `unknown tool` / 超时 / 报错）时，
  该单由 Lead 亲自做，**不空转重试烧额度**。
- **单包 ≤3 个文件**：外派轮看不到本会话，包越大它越会自己猜。
- `reply` 只作诊断：续的是聊天上下文，能调通也不当任务通道。
- 任务是 Lead 点对点派单，任务书当场发、当场收，不落文件、不留队列。

## 分流闸门（三条硬门槛，Lead 自判，不逐单问人）

同时满足才外派：

1. 不依赖本会话上下文（外派轮看不到本会话）。
2. 有明确范围：改文件的是涉及文件白名单，只读的是可看范围（只读活没有「涉及文件」，别误杀）。
3. 验收命令能独立跑通。

任一不满足 → 自己干，或回来问人。仓库规矩是「不清楚就别猜」，
任务书漏一块它就会自己猜，所以三条是硬门槛不是形容。

## 任务书（四块，一单一份）

```
【目标】<一句话，可验证>
【涉及文件】<白名单；只读单写可看范围>
【禁改与硬约束】
- 只读日志，不注入输入（不改客户端、不发按键、不读内存）
- 只用标准库；API Key 只在 HSAI_API_KEY，绝不写文件或提交
- data/ 不写入、不提交；删除 / 批量改名 / 提交推送 / 对外发送一律不做
- 不动无关代码、注释、格式；中文注释保持短句说「为什么」
【验收命令】python -m unittest discover -s tests 跑通；改完 ast.parse 验证
```

`AGENTS.md` 的硬规则优先级高于任务书。权限 `acceptEdits` 能改文件，
所以「涉及文件」必须写成白名单；禁改项照抄硬规则。

## 票的 prompt 模板（一票一单）

派单时把下面整段填好，作为 `subagent` 的 prompt；要并行就在同一回合同时起 N 个。

```text
你是派单票，只做一件事：把这一单交给 WorkBuddy，然后把结论回传。不要做别的。

调用一次 codebuddy_ask：
- message：<四块任务书原文>
- working_dir："D:\workspace\炉石传说AI助手"
- permission_mode："dontAsk"（只读）/ "acceptEdits"（改文件）
- max_turns：12
- timeout_secs：300

回来之后：
1. ok=false → 把错误原文回传 lead，不重试、不自己改做。
2. ok=true 只读单 → 把结论原文回传。
3. ok=true 改文件单 → 先核改动范围（git status / mtime），改动清单和结论一起回传。
```

## 写隔离

各票的白名单互不相交，由 Lead 派单时分配；有重叠就合并成一单或改串行。
`acceptEdits` 能改文件，撞车会静默互相覆盖，所以这条不能省。改文件单也允许并行。

## 回收

按有没有改文件分级：

- 只读类（审查 / 调查 / 跑测试）：结论直接采信，直接进汇报。
- 改文件类：Lead 复核 diff + 亲自跑 `python -m unittest discover -s tests` +
  `ast.parse`，全绿才收。**外派交回的「验收 OK」一律不算结论。**

多票并行改文件时统一收齐 → 跑一次全量 unittest + `ast.parse` → 整批收
（白名单不相交，一次全量就够证明叠加没坏事）。哪单有问题只回炉那一单，
不堵其他票。只读单不进这一轮。

## 额度

- 票数按任务数起 `subagent`，不吃 Team 成员名额；
  **禁止试投 / 空转 / 同一活投两遍**——一空转白烧一份。
- 平台硬限：每个 agent 每小时 6 次、全场每小时 20 次，超了会具名拒绝。
- 并行只受票数影响，没有「一批最多 3 单」这种限制。
- `codebuddy_ask` 每轮实耗一份账号额度；派不动（全部 429 / 报错）时一律由 Lead 自己干，不空转。
