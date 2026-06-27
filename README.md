# 最小 Agent —— 对标「最小 Spring Boot」的教学项目

你已经会看「最小 Spring Boot」:**配置 + Controller + Service + DAO + 单测**,就能跑通一个真实后端。
但你不知道**一个真实的 agent 是怎么写的**——它由哪些组件构成、谁调用谁。

这个项目就是来回答这个问题的。它是一个**单文件、零第三方依赖、可直接运行**的最小 agent。把 LLM 换成了**确定性规则**(方便你复现、调试、看清逻辑),但**架构和真实 agent 一模一样**:你以后接 GPT/Claude,只需要换掉一个函数(见下文「LLM 接缝」)。

> 三份文件:
> - **`agent.py`** —— 全部代码在这一个文件里(用 section banner 分区),中文注释,~520 行。
> - **`README.md`(本文)** —— 怎么跑、每个组件「是什么 / 为什么 / 在哪 / 像 Spring 的什么」。
> - **`ARCHITECTURE.md`** —— 心智模型:四层为什么这么分、最容易混的概念区分、往真实系统怎么扩展。

> 🔀 **本仓库还有第二个 agent:`agent2.py`** —— LLM-native 的「现代主流形态」(真接 Claude / DeepSeek,认知组件塌进模型的 tool-calling 循环)。它和这里的 `agent.py` 怎么对照、区别到底在哪,看 **[`agent-vs-agent2.md`](agent-vs-agent2.md)**。建议先读懂本文的 `agent.py`,再去看那篇对照。

---

## 一、30 秒跑起来

```bash
python agent.py
```

零依赖(只用 Python 3.10+ 标准库)。终端会**带标签**地打印出 loop 走过的每一个组件——你是「看着」它跑,而不是看一个黑盒结果。

**强烈建议连跑两次**:第二次开头的 `[BOOT]` 行会显示「从 memory.json 读到上次写入」——这就是 **long-term memory 跨进程持久化**的现场证据。

> Windows 终端如果中文乱码,先执行 `chcp 65001`(切到 UTF-8)再运行。

### 调试入口

- VS Code:按 F5,选择 `Agent2: single run (step from entry)`。
- 命令行 pdb: `debug_agent.cmd` 或 `python -m pdb debug_agent2.py`。
- `debug_agent2.py` 默认只跑一次目标,并使用临时内存;需要完整演示时设置 `AGENT_DEBUG_FULL_DEMO=1`。
- 需要调试持久化时设置 `AGENT_DEBUG_MEMORY=memory.json`。

---

## 二、它在演示一个什么任务?

一个**需要多步链式调用**的目标:

> 「查 `btc_price` 的当前价格 → 按 10 倍估算 → 把结果写进笔记 `portfolio`」

这个任务被刻意设计成能**一次点亮所有组件**:

- `查→算→记` 天然是**多步**,且 calc 的输入来自上一步 lookup 的输出 → 逼出 **Planner / Terminator / Reflector**(控制环)和 **working memory**(步骤间传状态)。
- `write_note` 是**高风险、有副作用**的工具 → 一次触发 **Guardrail(文本截断)+ Approval(人审门)+ Permission(鉴权)+ 幂等**。
- `write_note` 被注入「**第一次调用必失败**」→ 触发 **retry/backoff**,且重试后靠**幂等 key** 保证只生效一次。
- 写笔记落盘到 `memory.json` → 演示 **long-term / semantic memory** 持久化。

---

## 三、你会在终端看到什么(标签速查)

| 标签 | 哪个组件在说话 | 你应该看到什么 |
|------|---------------|---------------|
| `[BOOT]` | long-term memory 启动加载 | 首次为空;第二次显示上次写入 |
| `[INTENT]` | IntentParser | 自然语言 goal → 结构化字典 |
| `[PLAN]` | Planner | `查事实 → 算公式 → 记笔记`;失败时显示 `replan` |
| `[CTX]` | ContextBuilder | working(recency)/ recalled(semantic)/ schemas 各几条 |
| `[DECIDE]` | Brain + Policy | 选了哪个工具、传了什么参、为什么 |
| `[GUARD]` | Guardrails | note 文本 `37>30 截断改写`、或 `key=system` 被拦 |
| `[APPROVE]` | Approval | 只在高风险工具前出现 |
| `[PERM]` | Permissions | 执行前鉴权通过 |
| `[ROUTE]` | Router | 按 name 指派到具体工具 |
| `[EXEC]` | Executor | 第 1 次瞬时失败→backoff→第 2 次成功;幂等命中 |
| `[REFLECT]` | Reflector | `continue / retry / replan / done` |
| `[MEM]` | long-term memory | 记入情节记忆 |
| `[TRACE]` | Tracer | 每步的 tool / verdict / 伪 token / 累计 cost |
| `[CASE]` | evaluate() | 4 个回归用例的 PASS/FAIL + 通过率 |

---

## 四、和「最小 Spring Boot」的对照表

如果你脑子里有 Spring 的分层,直接看这张表就能把新概念挂上去:

| Spring 里的角色 | 这个 agent 里的对应 | 在 `agent.py` 哪 |
|----------------|---------------------|------------------|
| `@RestController`(解析入参) | `IntentParser.parse` | SECTION 3 |
| `@Service`(业务决策) | `Policy.choose` | SECTION 3 |
| `@Repository` / DAO | `tool_lookup` / `tool_calculator` / `LongTermMemory.write_note` | SECTION 2 / 6 |
| `DispatcherServlet`(按名分发) | `Router.dispatch` | SECTION 3 |
| `@ExceptionHandler` | `error_handler` | SECTION 3 |
| `HttpSession` / 请求作用域 | `State`(working memory) | SECTION 1 |
| DB / `JpaRepository` | `LongTermMemory`(`memory.json`) | SECTION 6 |
| 全文/向量检索 | `LongTermMemory.semantic_recall` | SECTION 6 |
| 参数校验 `@Valid` + 出参脱敏 | `Guardrails.check_input/output` | SECTION 7 |
| 审批流 | `Approval.request` | SECTION 7 |
| Spring Security(鉴权) | `Executor._check_permission` | SECTION 7 |
| `@Transactional` / 重试模板 | `Executor.run`(重试 + 幂等) | SECTION 7 |
| AOP 日志 / Micrometer 监控 | `Tracer` | SECTION 7 |
| `@SpringBootTest` | `evaluate()` | SECTION 10 |
| **(Spring 没有的部分)** | `Planner` / `Terminator` / `Reflector` —— **控制环** | SECTION 4 |
| **(Spring 没有的部分)** | `Brain.decide` —— **LLM 接缝** | SECTION 8 |

**最大的认知差异**:Spring 应用是「**请求进来 → 走一遍分层 → 响应出去**」的**单向管道**。
Agent 多出来的是一圈 **control plane(控制环)**:它会**自己决定下一步、自评结果、出错改道、满足条件才停**。`Controller→Service→DAO` 那条链在 agent 里只是「跑一步」的内部细节,外面套着一个 `while` 循环——**这圈循环,就是 pipeline 和 agent 的分界线。**

---

## 五、逐个组件:是什么 / 为什么 / 怎么搭

按「搭一个 agent 的顺序」讲,每个组件回答三件事:**它是什么、为什么需要它、在代码里怎么落地**。

### 1) 数据平面(沿用单轮骨架)—— 先让「一步」能跑完

- **IntentParser(像 Controller)**:把自然语言 goal 解析成结构化意图。*为什么*:大脑/规则没法直接吃一句话,得先有结构。
- **Policy(像 Service)**:根据「当前是哪一步 + 当前状态」决定**调哪个工具、传什么参**,并把上下文(上一步的输出)**注入到参数**里。*为什么*:这是「业务决策」,和「怎么把工具调起来」要分开。
- **Tools(像 DAO)**:纯函数 / 有副作用的最小执行单元(`lookup`、`calculator`、高风险的 `write_note`)。*为什么*:工具只管做事、出错就抛,把「崩了怎么办」留给上层。
- **Router(像 DispatcherServlet)**:按 name 把 action 指派到具体工具。*为什么*:决策层只说「调 write_note」,不该关心它具体是哪个函数。
- **ErrorHandler(像 @ExceptionHandler)**:工具抛异常 → 包成结构化 `Result(ok=False)`。*为什么*:让上层用统一的「成功/失败」对象推进循环,而不是到处 try/except。

### 2) 控制环(Layer 1)—— 让它从 pipeline 变成 agent

- **Planner**:把 goal 拆成有序 steps,并支持 `replan`(出错改道)。*为什么*:agent 要「先想清楚分几步」,而不是写死一条路径。
- **Terminator**:`should_stop(state)` —— goal 达成 / 超步数 / 超预算 / 连续失败超阈值,任一即停。*为什么*:**这是 agent 的出口**。没有它,要么死循环,要么退化成跑一遍的 pipeline。
- **Reflector**:对每步结果自评,输出 `continue / retry / replan / done`,驱动循环走向。*为什么*:**这是把「单发」变成「真正的 loop」的关键**——结果好就前进,不好就重试或改道。

### 3) 上下文(Layer 2)—— 决策前,给大脑看对的东西

- **ContextBuilder**:把 system 提示 + working memory(最近几条)+ 语义召回的 long-term + tool schema 拼成一个「决策上下文」。*为什么*:真实场景里这一坨就是要发给 LLM 的 prompt;拼得好不好直接决定决策质量。
- **Tool schema**:每个工具自带 `description` 和 `risk`。*为什么*:让大脑「**按描述选工具**」(而不只是有个名字表),并按 risk 分流到审批。
- **语义召回**:`semantic_recall` 按相关度取回(token 重叠当替身),和「按时间取最近」的 `recent` 是**两个不同方法**。*为什么*:取回相关经验 ≠ 取回最近经验,这是 retrieval 与 recency 的本质区别。

### 4) 分层记忆(Layer 3)—— 三种记忆,别混成一张表

- **working memory(`State`)**:本次 run 的便签纸,内存态,run 完即弃。*为什么*:步骤之间要传中间结果(calc 要用 lookup 的输出)。
- **long-term memory(`memory.json`)**:跨 run / 跨进程持久化。*为什么*:agent 要能记住「以前做过什么」——连跑两次能读到上次,就是它。
- **semantic memory**:不是第三个仓库,是「**查 long-term 的方式**」——按语义召回。*为什么*:经验多了以后,得能按相关度找回,而不是全量翻或只看最近。

### 5) 生产边界(Layer 4)—— 敢不敢对真实世界动手的分水岭

- **Guardrails**:管「合法但危险」——参数范围、禁写 key、超长文本截断/改写。**和 ErrorHandler 分开**(那个管「崩了」,这个管「没崩但危险」)。
- **Approval(human-in-the-loop)**:高风险工具执行前必须过审批门。demo 用自动放行,但会打印 `[APPROVE]`;真实场景这里 `input()` 等人。
- **Permissions / sandbox**:执行前校验本次 run 是否被授权调这个工具(最小实现 = 工具的 `risk` 字段 + 一张 `allowed` 集合)。
- **Retry/backoff + 幂等**:瞬时失败按退避重试 N 次;有副作用的工具用 `args 的 hash` 当幂等 key,保证重复调用只生效一次。
- **Tracer + budget**:记录每步的工具 / 耗时 / 伪 token / 伪 cost,维护 `max_steps + max_cost`,超了由 Terminator 停。**和 Memory 是两回事**:trace 给你看,memory 给 agent 看。

### 6) LLM 接缝(本 demo 的核心设计)

- **Brain.decide(SECTION 8)**:整个项目**唯一**需要改的地方,如果你要接真实 LLM。
  现在它调用 `Policy` 规则做决策;入参 `ctx` 已经是「拼好的 prompt 材料」。
  接 LLM = 把 `ctx` 序列化成 messages 发出去,把返回的 JSON 解析成 `Action`。**控制环、生产边界、记忆一行都不用动。**

---

## 六、五个「自己动手验证」的点

跑完 `python agent.py`,你可以亲眼确认这些(对应学习目标):

1. **每个组件都活着**:trace 里上面那张标签表的每个标签都至少出现一次——没出现就说明那个组件是死的。
2. **高风险路径真的过了审批**:`write_note` 前必有 `[APPROVE]`,且有一条 `[GUARD] note 文本 37>30 截断改写` 的可见改写。
3. **重试 + 幂等**:`write_note` 第 1 次瞬时失败 → backoff → 第 2 次成功;`[幂等演示]` 里同一个写操作提交两次,副作用次数 `1 → 1` 不变。
4. **跨进程持久化**:连跑两次,第二次 `[BOOT]` 显示读到了上次写进 `memory.json` 的内容。
5. **回归测试隔离**:`evaluate()` 打印 4 个用例的通过率,每个用例用全新 agent + 临时内存(不落盘),互不污染。

---

## 七、接下来往哪走

想加工具、想接真实 LLM、想把语义召回换成 embedding 向量检索——这三个**扩展点**和「为什么这么分层」的完整心智模型,都在 **[`ARCHITECTURE.md`](ARCHITECTURE.md)**。

一句话收尾:**读懂 `agent.py` 里 `run()` 的 ①~⑪ 十一步,你就读懂了「一个 agent 由哪些组件构成、谁调用谁」。** 剩下的全是实现细节。
