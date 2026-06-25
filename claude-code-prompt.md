# 任务:写一个「最小但完整」的多工具 Agent(供人学习架构)

## 背景 & 用途
我已有一个单轮骨架(intent→policy→router→tool→memory),它只是 **data plane**,跑一次就返回——本质是带记忆的 Router,还不算 agent。
现在要你把 **control plane(控制环)** 和 **生产边界** 补齐,做成一个真正的 agent。**目的是学习「一个 agent 该有哪些组件、谁调用谁」**,不是上生产。

## 最高优先级原则(冲突时按此排序)
1. **可读** > 2. **完整** > 3. **最小**
- 三者冲突时:先保证学习者顺着 `run()` 一眼看清整条调用链;再保证每个组件都在;最后才追求行数最少。
- **每个组件必须在 demo 的真实执行路径上被调用,并在 trace 里留下可见痕迹。严禁为凑数写没人调用的摆设代码。**
- 允许小组件自然塌缩进一个类,但 **不允许任何 concern 消失成一句注释**——它得是可 grep 到的命名单元。

## 技术约束
- **Python 3.10+,只用标准库,零第三方依赖。** 不许 langchain / pydantic / 任何 agent 框架。
- **代码全部放一个文件 `agent.py`**(用 section banner 分区),让调用关系在一处可见;外加一个 `ARCHITECTURE.md`。
- 运行时可落地一个 `memory.json`(long-term memory 持久化)。
- **大脑确定性化**:用规则实现 `decide()`,不接网络/不调真 LLM。但要在 `decide()` 正上方用注释标明:**这里是换成真实 LLM 的唯一接缝**,换的时候入参已是拼好的 prompt 材料,返回解析成 Action 即可。
- 注释用中文,技术名词保留英文。
- `agent.py` 目标 ~450 行,**硬上限 600 行**。超了就是过度设计——砍实现细节,别砍组件。

## 必须实现的组件(按 4 层分组,每个一行 spec)

**保留的 data plane(沿用骨架)**
- `IntentParser`(Controller):输入 → 结构化意图。
- `Policy`(Service):意图 + 状态 → 决定调哪个工具、传什么参(把上下文注入 args)。
- 工具(DAO,纯函数,**≥3 个**):至少含 `lookup`、`calculator`,以及 **≥1 个有副作用/高风险的工具**(如 `write_note`)。出错就抛,交上层。
- `Router`:按名字指派工具。
- `ErrorHandler`:工具异常 → 结构化 Result(只管「崩了」)。
- `evaluate()`:跑一批 case 算通过率。

**Layer 1 — 控制环(让它配叫 agent 的核心)**
- `Planner`:goal → 有序 steps(规则分解,标 LLM 接缝);支持 `replan(state)`。
- `Terminator.should_stop(state)`:goal 满足 / 超 max_steps / 超 budget / 连续失败超阈值 → 任一即停。这是 agent≠pipeline 的出口。
- `Reflector.review(step, result, state) -> {continue|retry|replan|done}`:对每步结果自评,驱动循环走向。**这是把 single-shot 变成真正 loop 的关键。**

**Layer 2 — 上下文**
- `ContextBuilder.build(step, state, memory, tools)`:把进「决策」的东西拼起来(system + working memory + 召回的 long-term + tool schema),含截断/摘要的最小实现。
- **Tool schema**:每个工具自带 `description`,给大脑选工具用(不只是 name 路由表)。
- **语义召回**:用 token 重叠打分做最小替身(注释:生产里换 embedding + 向量检索)。**它必须和 recent-N 是两个不同方法**,以证明你懂 retrieval ≠ recency。

**Layer 3 — 分层记忆(三种,职责不同,别混成一张表)**
- **working memory**:当前 run 的 scratchpad(≈ State,内存态)。
- **long-term memory**:跨 run 持久化(写 `memory.json`)。
- **semantic memory**:上面那个语义召回,查 long-term。

**Layer 4 — 生产边界(demo→prod 的分水岭;每条都要 demo 里真触发)**
- `Guardrails`:校验「合法但危险」——参数白名单/范围、输出过滤、对高风险工具的拦截。**与 try/except 分开**。demo 要有一条 input 被 guardrail 拦下/改写的可见证据。
- `Approval`(human-in-the-loop):高风险工具调用前必过此门。demo 用可注入的 auto-approve 策略,但必须打印 `[APPROVE]` 证明门被触发,并注释出真实场景这里 `block` 等 `input()`。低风险工具不过门。
- **Permissions/sandbox**:每个工具声明 `risk` 等级;Executor 调用前校验当前 run 是否被授权调该工具(最小实现 = 工具上一个 risk 字段 + 一张 allowed 集合)。
- **Retry/backoff + 幂等**:瞬时失败(用一个「第一次必失败」的注入式 flaky 工具)要按 backoff 重试 N 次;带副作用的工具用 idempotency key(args 的 hash)保证重复调用不重复生效。
- `Tracer` + budget:记录 per-step(工具 / 耗时 / 伪 token / 伪 cost),维护 `max_steps + max_cost`,超了由 Terminator 停;结束打印 run summary。**与 Memory 是两回事**(trace 给 ops/debug,memory 给 agent 决策)。

## 控制环结构(请严格按这个形状写 `run()`,并保留编号标记)
```
run(goal):
  plan = Planner.plan(goal)                              # goal → 有序 steps
  while not Terminator.should_stop(state):               # ← agent 的出口
    step   = plan.next() or Planner.replan(state)        # ① 现在做哪一步
    ctx    = ContextBuilder.build(step, state, mem, tools)# ② 拼决策上下文(含语义召回)
    action = Brain.decide(ctx)                           # ③ 选工具+定参(规则; LLM 接缝)
    Guardrails.check_input(action)                       # ④ 执行前:合法但危险? 拦/改
    if action.risk == HIGH: Approval.request(action)     # ⑤ 高风险 → 过人审门
    result = Executor.run(action)                        # ⑥ Router→Tool(含权限/重试/幂等)
    Guardrails.check_output(result)                      # ⑦ 输出过滤
    verdict = Reflector.review(step, result, state)      # ⑧ 自评 → continue/retry/replan/done
    State.update(...)                                    # ⑨ 写 working memory
    Memory.write(...)                                    # ⑩ 写 long-term/semantic
    Tracer.record(...)                                   # ⑪ per-step trace + budget
  return final_answer + Tracer.summary()
```

## 演示场景(必须点亮上面每个组件)
设计一个 **需要多步链式调用** 的 goal,例如:
> "查 <某个数值型事实> → 用公式算一下 → 把结果记进笔记"

要求:
- KV 库里放几个数值型事实,**calc 的输入来自上一步 lookup 的输出**(演示 state 在步骤间传递)。
- `write_note` 是高风险工具 → 一次性触发 **guardrail + approval + permission + idempotency**。
- 给 `write_note` 注入「第一次调用失败」→ 触发 **retry/backoff**,且重试后靠 idempotency key 保证只生效一次。
- 流程天然多步 → 触发 **planner + termination + reflection**。
- `__main__` 里:跑一遍完整 goal;再 **第二次单独运行进程** 能从 `memory.json` 读到上次写入(演示 long-term 持久化,可在 README 写「跑两次看差异」)。

## 可见性要求(这是学习的关键)
- **带标签的 trace 输出**:每步打印 `[PLAN] [DECIDE] [GUARD] [APPROVE] [EXEC] [REFLECT] [MEM] [TRACE]` 等标记,让人 `python agent.py` 时直接 **看着 loop 走过每个组件**。
- 文件头画一张 **调用链 ASCII 图**。
- 文件头给一张 **「组件 → 类 → 它像什么(Controller/Service/DAO/...)」映射表**。
- `run()` 内保留 ①②③… 编号注释。

## `ARCHITECTURE.md` 内容要求(把心智模型写清楚,这是给人学的)
1. 四层结构:data plane / 控制环 / 上下文 / 分层记忆 / 生产边界——每层为什么存在。
2. **关键区分**(各 2 句):Guardrails vs ErrorHandler;Trace vs Memory;Retrieval vs Recency;working vs long-term vs semantic memory;Router 路由表 vs Tool schema。
3. **「没有一张标准完整组件表」**:哪些组件在小 agent 里会塌缩进别的模块,说明本实现里哪几个塌缩了。
4. **三个轴决定组件是否 load-bearing**:single-step↔autonomous、read-only↔会对世界 take action、single-session↔持久。越靠右,生产边界从「可选」变「必需」。指出本 demo 在三个轴的位置。
5. **扩展点**:加工具怎么加、`decide()` 怎么换成真 LLM、语义召回怎么换 embedding。

## 验收标准(你交付前自己逐条核对)
- [ ] `python agent.py` 零依赖直接跑通。
- [ ] tagged trace 里 **每个组件都至少出现一次**(没出现 = 那个组件是死的,修掉)。
- [ ] 高风险路径 **确实** 命中 `[APPROVE]` 且 guardrail 有一条可见的拦截/改写。
- [ ] flaky 工具:**重试一次后成功**;同一 write 触发两次 **只生效一次**(幂等)。
- [ ] 连续跑两次进程,第二次能读到第一次写进 `memory.json` 的内容。
- [ ] `evaluate()` 打印通过率;每个 case 用全新 agent + 临时 memory,互不污染。
- [ ] `agent.py` 在 600 行硬上限内;每个组件名可被 grep 到。
- [ ] 控制环 `run()` 的形状和上面给的骨架一致。

## 反面约束(别做)
- 别引任何框架/网络/真 LLM 调用。
- 别为凑组件写没人调用的代码。
- 别让它膨胀成千行怪物——砍实现,不砍组件。
- 别把代码拆得到处都是导致看不清调用链:**loop 和全部 wiring 留在 `agent.py` 一个文件里**。
