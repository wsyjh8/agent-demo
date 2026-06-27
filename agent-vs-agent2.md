# agent.py vs agent2.py —— 两个 agent 到底差在哪

这个仓库里有**两个**可运行的 agent,故意做成一对对照:

| | `agent.py` | `agent2.py` |
|---|---|---|
| 一句话 | 把「大脑」拆成一堆**显式确定性组件**的教学骨架 | **LLM-native** 的现代 agent:那些组件统统塌进「模型 + 一个循环」 |
| 大脑 | 规则(`Policy`),`Brain.decide` 是预留接缝 | 真·LLM(默认 `fake_llm` 可复现,可切 Anthropic / DeepSeek) |
| 目的 | 让你看清**「一个 agent 由哪些组件构成」** | 让你看清**「真实的 agent 实际长什么样」** |
| 决策怎么来 | 你写死的规则,按固定计划走 | 模型每轮自己决定调哪些工具 / 还是收尾 |
| 控制流 | `Planner` 先拆好步骤 → 显式 `run()` 循环 ①~⑪ | append-only messages → 反复调模型 → tool-calling 循环 |
| 运行 | `python agent.py` | `python agent2.py` |
| 行数 / 依赖 | ~810 行 / 零依赖 | ~960 行 / 零依赖(接真模型才惰性 import) |

> 建议顺序:**先读 `agent.py`**(它把每个认知组件起了名字、摆在明面上,方便你建立词汇表),**再读 `agent2.py`**(它告诉你:在真实世界里,那些组件其实都活在大模型里,你手写的是它周围的「生产外壳」)。

---

## 一、最根本的区别:认知组件「显式」还是「塌进 LLM」

`agent.py` 为了「**用规则代替大脑**」,把决策过程拆成一排能 grep 到的名字:
`IntentParser`(解析意图)、`Planner`(拆步骤)、`Policy`(选工具+拼参)、`Reflector`(自评下一步)、`Terminator`(判断停)。这是它的**教学价值**——让你知道一个 agent 的「脑子」里到底发生了哪几件事。

`agent2.py` 是真实的 2026 形态:**这些认知组件统统塌进大模型的单一 tool-calling 循环**。没有 `Planner`、没有 `Policy`、没有 `Reflector`——

```
messages 不断累积 → 反复调用模型 → 模型用 tool_use 给出决策 →
并行执行工具 → 把结果(含错误)回灌进 messages → 直到模型不再调用工具
```

模型读完上下文,自己决定「下一步查什么、算什么、还是直接回答」。你不再写决策逻辑,你只写**喂给它什么、它的输出怎么安全地执行**。

---

## 二、逐组件对照:agent.py 的组件,在 agent2.py 里去哪了?

这张表是这两个文件的精华。**左边每一个你在 `agent.py` 里看到的显式组件,在 `agent2.py` 里要么塌进了 LLM,要么升级成了更硬核的生产组件。**

| agent.py 的显式组件 | 在 agent2.py 里的归宿 |
|---|---|
| `IntentParser`(文本→结构化意图) | **塌进 LLM**:模型直接读 user message,无需先解析 |
| `Planner.plan`(预先拆好步骤) | **塌进 LLM**:没有预定计划,模型每轮临场决定下一步 |
| `Policy.choose`(选工具 + 拼参) | **塌进 LLM**:模型输出 `tool_use` 块(name + input) |
| `Brain.decide`(规则,接缝) | **→ 真·LLM**:`fake_llm` / `make_anthropic_llm` / `make_deepseek_llm` |
| `Reflector.review`(continue/retry/replan/done) | **塌进 LLM + errors-as-observations**:模型读工具结果(含错误)自己决定继续还是收尾 |
| `Planner.replan`(出错改道) | **errors-as-observations**:错误包成 `tool_result(is_error)` 回灌,模型自纠(见 CASE 2) |
| `Terminator.should_stop`(显式出口判断) | **自然出口**(模型不再调工具 `stop_reason=end_turn`)+ `max_steps` 安全阀 |
| `ContextBuilder.build`(每轮拼上下文) | **append-only `messages` 数组** + `compact_messages`(超 token 预算时清理旧结果) |
| working memory(`State`) | **`messages` 数组本身**(append-only,就是 agent 的全部记忆/上下文) |
| `Router.dispatch`(按名分发) | **`ToolProvider.dispatch`**(MCP 风格抽象,可换成远程 MCP server) |
| `ErrorHandler`(异常→结构化) | **errors-as-observations**(异常包成 `tool_result(is_error=True)` 喂回模型) |
| `Guardrails.check_*` | **`Guardrails`(更强)**:JSON-Schema 运行时校验 + 禁写白名单 + 截断 + **输出脱敏 + prompt 注入检测** |
| `Approval`(阻塞式 auto-approve) | **可持久化 HITL**:`checkpoint → 挂起 → resume`(取代会钉死进程的 `input()`,见 CASE 3) |
| `Permissions`(`_check_permission`) | **最小权限 `allowed`**:每个 agent 一套,子 agent 更窄 |
| `Executor`(retry + 幂等) | **`Executor`**:async retry/backoff + 幂等(只对有副作用的工具,resume 也不双写) |
| `Tracer`(伪 token / 伪 cost) | **`Tracer`(真账)**:真实/估算 usage + prompt 缓存计费 + OTel 风格 span + 成本估算 |

---

## 三、关键洞察:谁消失了,谁反而变强了

把上表竖着看,会发现一条很重要的规律:

- **消失(塌进 LLM)的,全是「认知/决策」类组件**:IntentParser、Planner、Policy、Reflector、Terminator 的目标判断。
  → 这些恰恰是 `agent.py` 存在的全部意义:它用规则把「大脑该想什么」演给你看。在真实 agent 里,**这些都是模型的活,不用你写**。

- **保留、而且变得更硬核的,全是「生产外壳」类组件**:Guardrails、Permissions、Executor、Tracer、Approval/HITL。
  → 因为**模型不可信**:它可能被 prompt 注入、可能乱调高风险工具、可能烧钱、可能重复写。这些边界**永远是 harness 的活,不能交给模型**。

> **这就是两个文件合起来要教你的终极一课:**
> 一个真实 agent =「**一个大模型 + 一个 tool-calling 循环**」(`agent.py` 里那一大堆认知组件)+「**一圈你必须亲手写的生产边界**」(两个文件里都显式存在的 Guardrails / Executor / Permissions / Tracer / Approval)。
> `agent.py` 用规则把**大脑的解剖图**画给你看;`agent2.py` 告诉你**大脑其实就是模型**,真正要你做工程的是它周围那圈外壳。

---

## 四、agent2.py 多出来的能力(agent.py 没有的)

这些是「现代主流 agent」相对教学骨架补齐的部分,每条都在 `python agent2.py` 的某个 CASE 里真实触发:

| 能力 | 说明 | 对应 demo |
|---|---|---|
| **规范消息格式** | Anthropic Messages content blocks(`text`/`tool_use`/`tool_result` 按 id 配对,system 独立)= 真实 API 形态 | 全程 |
| **并行多工具** | 同一回合的独立调用用 `asyncio.gather` 并发执行 | CASE 1 |
| **errors-as-observations** | 工具/权限/guardrail 失败都变成观察回灌,模型自纠(取代显式 replan) | CASE 2 |
| **可持久化 HITL** | 高风险调用前 checkpoint 落盘 → 进程可退出 → 人审后 `resume` 续跑 | CASE 3 |
| **prompt 注入防御** | 工具输出当「不可信外部数据」:脱敏 + 注入启发式 + 来源标注;system 明令不得执行 | CASE 4 |
| **orchestrator-worker** | `delegate` 工具把独立子任务派给**独立上下文窗口**的子 agent(更窄权限) | CASE 5 |
| **上下文压缩** | 超 token 预算时清理旧工具结果(context editing) | CASE 6 |
| **strict JSON-Schema 工具** | `input_schema + strict`,运行时再校验一次参数 | 全程 |
| **MCP 风格工具来源** | `ToolProvider` 抽象:加能力 = 接 provider,不改循环 | SECTION 0 |
| **真实可观测性** | 真实/估算 token usage + prompt 缓存计费模型 + OTel span + 成本估算 | CASE 1/5 汇总 |
| **多模型** | 默认 `fake_llm`;可切真实 Anthropic 或 DeepSeek(含 Anthropic↔OpenAI 格式双向翻译) | `build_brain` |

## 五、反过来,agent.py 演示得更全的部分

不是说 agent2 全面更优——为了聚焦 LLM-native 循环,agent2 在这两点上反而更简:

- **分层记忆**:`agent.py` 显式演示了三种记忆——working memory(`State`)、long-term(`memory.json`,**跨进程持久化**:连跑两次能读到上次)、semantic memory(`semantic_recall` 按 token 重叠召回)。`agent2.py` 的「记忆」主要是 append-only `messages` + checkpoint(给 HITL resume 用),**没有做语义召回 / memory.json**(真实里这块通常外接 RAG / 向量库)。
- **回归测试**:`agent.py` 有 `evaluate()` 打印通过率(每个用例全新 agent + 临时内存,互不污染);`agent2.py` 用 6 个 CASE 展示能力,没有 pass-rate。

---

## 六、各自怎么跑 / 怎么接真模型

**agent.py**
```bash
python agent.py          # 连跑两次可看到 long-term memory 跨进程持久化
```
接真 LLM:改 `Brain.decide` 一处——把拼好的 `ctx` 发给模型、把返回解析成 `Action`。

**agent2.py**
```bash
python agent2.py                      # 默认 fake_llm,零依赖、可复现
AGENT2_LLM=anthropic python agent2.py # 需 pip install anthropic + ANTHROPIC_API_KEY
AGENT2_LLM=deepseek  python agent2.py # 需 pip install openai + DEEPSEEK_API_KEY
```
接真 LLM:唯一替换点是 SECTION 2 的 `make_anthropic_llm`——**循环 / 工具 / 边界一行都不用改**(因为 `fake_llm` 的输入输出形态本就 = 真实 Anthropic Messages 形态)。

---

## 七、一句话总结

> **`agent.py` 教你 agent 的「解剖学」——把脑子里的每个零件拆开命名;`agent2.py` 给你 agent 的「实物」——那些零件其实都长在大模型里,你真正要焊的是它外面那圈生产边界。**
> 先用 `agent.py` 建立词汇,再用 `agent2.py` 看这些词在真实 LLM-native 循环里如何各归其位(或塌缩、或加固)。
