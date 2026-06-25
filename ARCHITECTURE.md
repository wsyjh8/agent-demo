# ARCHITECTURE —— 这个最小 agent 的心智模型

> 配套代码:[`agent.py`](agent.py)(单文件、零依赖)。本文不重复代码,只讲**为什么这样分层、组件之间的边界在哪**。
> 想先上手跑、想看「每个组件像 Spring 里的什么」,去读 [`README.md`](README.md);本文是给已经跑过一遍、想搞清楚设计的人看的。

---

## 0. 一句话总览

一个**单轮骨架**(intent→policy→router→tool→memory)只是 **data plane**:给它一个输入,它跑一次、返回一次,本质是「带记忆的 Router」。
把它变成 **agent**,加的不是更多工具,而是一圈 **control plane(控制环)**——让它能*自己决定下一步、自己判断要不要停、出错自己改道*——再补上**生产边界**,让它敢对真实世界动手。本 demo 就是把这两圈补齐的最小样板。

---

## 1. 四层结构:每层为什么存在

| 层 | 组件 | 这一层回答的问题 | 没有它会怎样 |
|----|------|-----------------|-------------|
| **Data plane(数据平面)** | IntentParser, Policy, Tools, Router, ErrorHandler | 「**这一步**具体怎么做完」 | 啥也没有,连一次工具调用都跑不起来 |
| **Layer 1 · 控制环** | Planner, Terminator, Reflector | 「现在做**哪一步**、要不要**停**、上一步结果**好不好**」 | 它只是 pipeline:固定跑一遍就结束,不会自我纠偏 |
| **Layer 2 · 上下文** | ContextBuilder, Tool schema, 语义召回 | 「做决策前,**给大脑看什么**」 | 大脑(LLM/规则)瞎决策,看不到记忆、不知道有哪些工具 |
| **Layer 3 · 分层记忆** | working / long-term / semantic memory | 「**记住什么、记多久、怎么取回**」 | 跨步骤丢状态、跨进程丢经验、取回只会按时间不会按相关性 |
| **Layer 4 · 生产边界** | Guardrails, Approval, Permissions, Retry/幂等, Tracer+budget | 「能不能**安全地对真实世界动手**」 | demo 能跑,但一旦工具有副作用 / 联网 / 花钱,就会闯祸 |

**关键直觉:data plane 是「跑一次」,控制环是「跑成一个 loop」。** agent 与 pipeline 的唯一区别,就是那句 `while not Terminator.should_stop(state)` —— 出口由 agent 自己根据状态判断,而不是写死的步数。

---

## 2. 五组「最容易混」的概念区分

学这套东西,80% 的困惑来自把下面这几对东西当成一回事。每对各两句。

**Guardrails vs ErrorHandler**
ErrorHandler 管「**崩了**」:工具抛异常 → 包成结构化 `Result(ok=False)`,是事后兜底(`try/except`)。
Guardrails 管「**合法但危险**」:参数没毛病、不会抛异常,但范围越界 / key 在禁写名单 / 文本太长,需要事前**拦截或改写**(`agent.py` 里 note 文本被截断、key=`system` 被拦,都是它干的)。

**Trace vs Memory**
Trace 给**人/运维**看:每步的工具、耗时、伪 token、伪 cost,用来 debug 和算预算,run 完就打印 summary。
Memory 给**agent 自己**看:是它做决策的依据。两者绝不能混——你不会让 agent 把「自己花了多少 token」当成业务事实去回忆。

**Retrieval vs Recency**
Recency = 「最近 N 条」(`recent()`),按**时间**取,简单但可能取到一堆不相关的近期噪声。
Retrieval = 「最相关 K 条」(`semantic_recall()`),按**语义相关度**取(本 demo 用 token 重叠当替身,生产换 embedding 向量检索)。本实现特意把它们写成**两个不同方法**,就是为了证明:取回 ≠ 看最近。

**working vs long-term vs semantic memory**
working memory = `State`:**当前这一次 run** 的便签纸(计划进度、中间结果),纯内存,run 结束即弃。
long-term memory = `memory.json`:**跨 run / 跨进程**持久化的经验(连跑两次能读到上次的就是它)。
semantic memory:不是第三个仓库,而是**查 long-term 的方式**——按相关度召回,而非按时间。

**Router 路由表 vs Tool schema**
Router 路由表只认 **name**:`"write_note"` → 那个函数,负责「**指派**」。
Tool schema 是每个工具自带的 **description + risk**,负责让大脑「**选**」哪个工具、以及风险分流(高风险才过审批)。一句话:schema 决定*选谁*,路由表决定*怎么把选中的调起来*。

---

## 3. 「没有一张标准完整组件表」—— 哪些组件塌缩了

网上没有一份「agent 必须有这 N 个类」的权威清单。**组件是 concern(关注点),不是类。** 一个 concern 可以独占一个类,也可以塌缩进别的类——只要它还**看得见、grep 得到**,没有消失成一句注释。小 agent 里大量组件会塌缩。本实现里明确塌缩的有:

| Concern(关注点) | 标准教材里可能是独立组件 | 本 demo 塌缩进了哪里 | 为什么可以塌缩 |
|---|---|---|---|
| **Permissions / sandbox** | 独立的鉴权服务 | `Executor._check_permission` | 最小实现 = 工具上一个 `risk` 字段 + 一张 `allowed` 集合,够用 |
| **Retry/backoff** | 独立的弹性/重试中间件 | `Executor.run` 的循环 | 它和「执行」强绑定,放一起调用链更清楚 |
| **幂等** | 独立的去重层 | `Executor.idem` 缓存 | 同上,key=args 的 hash,十几行搞定 |
| **Brain(决策)** | 独立的 LLM 客户端 | `Brain.decide` 一层薄壳,内部调 `Policy` | demo 用规则,所以「大脑」当前就是 Policy;换 LLM 时这层壳变厚 |
| **semantic memory** | 独立的向量库 | `LongTermMemory.semantic_recall` 一个方法 | 它只是「查 long-term 的一种姿势」,没必要单独建仓 |
| **ErrorHandler** | 独立的异常处理器 | 一个函数 `error_handler` | 只做一件事:异常 → 结构化 Result |

> 反过来,**绝不该塌缩消失**的是「概念」本身:哪怕只有一行,Guardrails ≠ ErrorHandler、Trace ≠ Memory 也必须是两个独立命名的东西。塌缩的是**代码组织**,不是**职责划分**。

---

## 4. 三个轴:决定一个组件到底「承不承重」

不是每个 agent 都需要全部 Layer 4。一个组件是不是 load-bearing(承重),由你的 agent 在这三个轴上的位置决定。**越往右,生产边界从「可选」变「必需」。**

```
single-step  ●──────────────►  autonomous(多步自驱)      → 需要 Planner / Terminator / Reflector
read-only    ●──────────────►  对真实世界 take action       → 需要 Guardrails / Approval / Permissions / 幂等
single-session ●────────────►  跨会话持久                   → 需要 long-term memory / semantic recall
```

**本 demo 的位置(三个轴都偏右,所以五脏俱全):**
- **autonomous**:goal 天然要 `查→算→记` 多步链式调用,calc 的输入来自上一步 lookup 的输出 → 控制环承重。
- **会 take action**:`write_note` 有副作用、是高风险工具 → guardrail + approval + permission + 重试 + 幂等全部承重。
- **持久**:连跑两次能从 `memory.json` 读到上次写入 → long-term / semantic memory 承重。

如果你的 agent 是「单步、只读、单会话」的(比如一个纯查询机器人),那么 Layer 4 的大半可以不要——**这也是为什么没有标准组件表:组件清单是你这三个轴的位置的函数。**

---

## 5. 扩展点(从 demo 往真实系统走)

**① 加一个工具**
1. 写一个纯函数(出错就抛);2. 在 `Agent._build_tools` 里登记 `Tool(name, description, risk, fn)`——`description` 让大脑选它、`risk` 决定它要不要过审批;3. 如果它该被某个 step 选中,在 `Policy.choose` 加一条规则(或等你接了 LLM 后,大脑自己会按 description 选)。
> 它会**自动**享受 Router 分发、权限校验、重试、幂等、guardrail、trace——因为这些都在执行链路上,不在工具里。

**② 把 `decide()` 换成真实 LLM**
唯一的接缝在 `Brain.decide`。现在它调 `Policy` 规则;真实版本:把 `ctx`(已经是拼好的 system + working + recalled + tool schemas)序列化成 messages 发给 LLM,让 LLM 返回一个 JSON,再解析成 `Action` 返回。**控制环、生产边界、记忆一行都不用改**——这就是把决策隔离成单一接缝的价值。

**③ 把语义召回换成 embedding**
唯一要改的是 `LongTermMemory.semantic_recall`:现在用 token 重叠打分,换成「query 过 embedding 模型 → 在向量库里 ANN 检索 top-k」。`recent()`(recency)保持不变,正好对照出 retrieval 与 recency 是两条独立的取回路径。

---

## 6. 控制环为什么长这样(逐行回看 `run()`)

```
plan = Planner.plan(goal)                 # 先把目标拆成有序步骤
while not Terminator.should_stop(state):  # ← 出口由状态决定,这是 agent ≠ pipeline 的唯一标志
  ① step   = plan.next() / replan         #   没有现成步骤就重规划(改道能力)
  ② ctx    = ContextBuilder.build         #   决策前先组装「给大脑看的东西」
  ③ action = Brain.decide(ctx)            #   ★ 唯一接缝:规则 or LLM
  ④ Guardrails.check_input                #   动手前:合法但危险?拦/改
  ⑤ Approval.request   (HIGH 才过)        #   高风险:过人审门
  ⑥ Executor.run                          #   权限 → 幂等 → 重试 → Router → Tool
  ⑦ Guardrails.check_output               #   出手后:输出脱敏
  ⑧ verdict = Reflector.review            #   自评:continue / retry / replan / done —— 它驱动下一圈
  ⑨ State.update                          #   写 working memory(本次 run 的便签)
  ⑩ Memory.write                          #   写 long-term + semantic(给未来的自己)
  ⑪ Tracer.record                         #   记 trace + 累计 budget(给运维/你)
```

读懂这 11 步,你就读懂了「一个 agent 由哪些组件构成、谁调用谁」。剩下的全是实现细节。
