# -*- coding: utf-8 -*-
"""
最小但完整的「多工具 Agent」—— 学习「一个 agent 由哪些组件构成、谁调用谁」。
对标「最小 Spring Boot」:Spring 用 配置+Controller+Service+DAO+单测 跑通后端;
本文件用 数据平面+控制环+上下文+分层记忆+生产边界 跑通一个 agent(LLM 用确定性规则代替)。

一、调用链(顺着 run() 看清整条链路)
run(goal)
  ├─ IntentParser.parse ──► 结构化意图            (Controller)
  ├─ Planner.plan ────────► 有序 steps            (控制环·计划)
  └─ while not Terminator.should_stop(state):      (控制环·出口 = agent≠pipeline)
        ① step   = plan.next() / Planner.replan
        ② ctx    = ContextBuilder.build ─► LTM.semantic_recall   (上下文·语义召回)
        ③ action = Brain.decide(ctx)    ─► Policy 规则           (★ LLM 唯一接缝)
        ④ Guardrails.check_input        (生产边界:合法但危险?拦/改)
        ⑤ Approval.request  if HIGH     (生产边界:human-in-the-loop)
        ⑥ Executor.run = _check_permission ─► idempotency ─► retry/backoff ─► Router ─► Tool(DAO)
        ⑦ Guardrails.check_output       (生产边界:输出过滤/脱敏)
        ⑧ Reflector.review ─► continue|retry|replan|done   (自评驱动循环)
        ⑨ State.update                  (working memory)
        ⑩ LTM.remember                  (long-term + semantic memory)
        ⑪ Tracer.record                 (per-step trace + budget)
  return final_answer + Tracer.summary()

二、组件 → 类/函数 → 「像 Spring 里的什么」(给有后端背景的人架桥)
  IntentParser    .parse              Controller(解析入参)
  Policy          .choose             Service(选工具、拼参)
  Tools           tool_lookup/...     DAO(数据访问 / 副作用)
  Router          .dispatch           DispatcherServlet(按名分发)
  ErrorHandler    error_handler       @ExceptionHandler(崩了→结构化)
  Planner         .plan / .replan     (agent 特有:把目标拆成步骤)
  Terminator      .should_stop        (循环出口)
  Reflector       .review             (自评,把单发变成 loop)
  ContextBuilder  .build              组装「给大脑看的请求体」
  WorkingMemory   State               HttpSession / 请求作用域
  LongTermMemory  LongTermMemory      DB / Repository(持久层)
  SemanticMemory  LTM.semantic_recall 全文 / 向量检索
  Guardrails      .check_*            参数校验 + 输出脱敏(≠异常处理)
  Approval        .request            审批流(human-in-the-loop)
  Permissions     Executor._check_permission   Spring Security(鉴权)
  Executor        .run                事务模板(鉴权 / 重试 / 幂等)
  Tracer          Tracer              AOP 日志 / 监控(≠业务数据)
  Brain           .decide             ★ LLM 接缝(现在是规则)
运行:python agent.py(零依赖)。连跑两次可看到 long-term memory 跨进程持久化。
"""
import os, re, json, time, hashlib
from dataclasses import dataclass, field
from typing import Any, Callable

# ===== SECTION 0 · 公共工具 / 常量 / 异常 =====
def log(tag: str, msg: str = "") -> None:
    """打印一行带标签的调试日志。

    作用:
    - 统一输出格式,比如 [PLAN]、[DECIDE]、[TRACE]。
    - 你调试时可以按这些标签观察 Agent 当前走到哪个组件。
    """
    print(f"{tag:<11}{msg}")

def fmt(x: Any) -> str:  # 数字美化:500000.0 → 500000
    """把数字格式化得更适合展示。

    例如 500000.0 会显示成 500000,避免答案里出现多余的小数点。
    非整数或非数字则原样转成字符串。
    """
    return str(int(x)) if isinstance(x, float) and x.is_integer() else str(x)

def summarize(v: Any, n: int = 24) -> str:  # 最小「截断/摘要」:进上下文/trace 前截短
    """把任意值压缩成短摘要。

    用在上下文和 trace 里,防止很长的结果把日志刷屏。
    n 表示最多保留多少个字符。
    """
    s = str(v)
    return s if len(s) <= n else s[:n] + "…"

_TOK = re.compile(r"[a-zA-Z0-9_]+|[一-鿿]")  # 英文词 + 单个汉字
def tokenize(s: str) -> list:
    """把一句文本切成简单 token。

    这里是 demo 版分词:
    - 英文/数字/下划线会组成一个词,如 btc_price。
    - 中文按单字切分。
    semantic_recall 会用这些 token 做相关性匹配。
    """
    return _TOK.findall(s.lower())

def overlap(a: set, b: set) -> int:
    """计算两个 token 集合有多少共同元素。

    返回值越大,说明两段文本越相关;这是 semantic_recall 的简化打分方式。
    """
    return len(a & b)

HIGH, LOW = "HIGH", "LOW"                     # 工具风险等级:权限/审批/guardrail 按它分流
KV_FACTS = {"btc_price": 50000, "eth_price": 3000, "gold_price": 2000}  # 数值型事实库
FALLBACK_FACT = "btc_price"                   # replan 时的兜底事实

class BoundaryBlock(Exception): pass          # 生产边界拦截(确定性,重试无意义)
class GuardrailBlock(BoundaryBlock): pass
class ApprovalDenied(BoundaryBlock): pass
class PermissionDenied(BoundaryBlock): pass
class TransientError(Exception): pass         # 瞬时故障,可被 retry/backoff 救回

# ===== SECTION 1 · 数据结构(贯穿全程的值对象)=====
@dataclass
class Tool:        # description 即 tool schema 核心:给大脑「选工具」用,不只是 name
    name: str; description: str; risk: str; fn: Callable

@dataclass
class Action:      # 大脑的决策:调哪个工具、传什么参、风险多高、为什么
    tool: str; args: dict; risk: str; why: str = ""

@dataclass
class Result:      # 工具结果。ok=False 时 error 说明「崩了」,由 ErrorHandler 产出
    ok: bool; value: Any = None; error: str = ""

@dataclass
class Step:        # 计划里的一步。intent 决定 Policy 选哪个工具;hint 是静态参数提示
    name: str; intent: str; hint: dict

@dataclass
class HistoryItem:  # working memory 的一条历史。action 可能为 None(决策前就被拦截)
    step: Step; action: Any; result: Result; verdict: str

@dataclass
class Context:     # ContextBuilder 拼好的「决策上下文」= 真实场景要发给 LLM 的 prompt 材料
    step: Step; state: Any; tools: dict; system: str
    working: list  # recency:working memory 最近 N 条
    recalled: list  # retrieval:从 long-term 语义召回的相关条目
    schemas: list  # tool schema:[(name, description, risk)]

@dataclass
class State:
    """=== working memory(三种记忆之一)=== 当前 run 的便签纸,纯内存,run 结束即弃。"""
    goal: str                                # 用户这次给 agent 的原始目标,例如「查 btc_price 并写入笔记」
    steps: list                              # Planner 拆出来的步骤列表,例如「查事实 → 算公式 → 记笔记」
    cursor: int = 0                          # 当前执行到 steps 的第几个位置,相当于步骤指针
    scratch: dict = field(default_factory=dict)   # 中间结果 {intent: value},供后续步骤注入 args
    history: list = field(default_factory=list)    # 本次 run 已执行过的步骤历史,用于回看和构造上下文
    failures: int = 0                        # 连续失败次数,超过 max_failures 后 Terminator 会停机
    steps_taken: int = 0                     # 已经执行了多少步,Tracer 每记录一步就会累加
    cost: int = 0                            # 本次 run 的累计伪成本/伪 token,用于演示预算控制
    done: bool = False                       # 任务是否已经完成,Reflector 返回 done 时会置为 True
    replan_pending: bool = False             # 是否需要重新规划,为 True 时下一轮会走 Planner.replan
    used_fallback: bool = False              # 是否已经用过兜底查询,避免 replan 一直反复兜底
    stop_reason: str = ""                    # 停止原因,例如 goal 达成、超步数、超预算、失败过多
    max_steps: int = 8                       # 最大允许执行步数,budget 上限,超了由 Terminator 停
    max_cost: int = 500                      # 最大允许累计成本,budget 上限,超了由 Terminator 停
    max_failures: int = 3                    # 最大允许连续失败次数,超了由 Terminator 停

    @classmethod
    def new(cls, goal, steps, cfg):
        """创建一次运行用的 State。

        goal 是用户目标,steps 是 Planner 拆出来的步骤。
        cfg 可以覆盖 max_steps/max_cost/max_failures 这些预算和容错参数。
        """
        s = cls(goal=goal, steps=list(steps))
        s.max_steps, s.max_cost, s.max_failures = cfg.get("max_steps", 8), cfg.get("max_cost", 500), cfg.get("max_failures", 3)
        return s

    def next_step(self):
        """取当前应该执行的 Step。

        如果 replan_pending=True,这里故意返回 None,让 Agent.run 走 Planner.replan。
        否则按 cursor 指针从 steps 里取下一步。
        """
        if self.replan_pending: return None              # 强制走 `or Planner.replan` 分支
        return self.steps[self.cursor] if self.cursor < len(self.steps) else None

    def update(self, step, action, result, verdict):
        """⑨ 写 working memory,并按自评结果推进/回退游标。

        每执行完一步都会调用这里:
        - history 记录这一步发生了什么。
        - scratch 保存成功结果,给后续步骤拼参数用。
        - failures 记录连续失败次数。
        - cursor/replan_pending/done 决定下一轮循环怎么走。
        """
        self.history.append(HistoryItem(step, action, result, verdict))
        if result.ok:
            self.scratch[step.intent] = result.value     # ← 本步输出存便签,供下一步注入 args
            self.failures = 0
        else:
            self.failures += 1
        if verdict == "continue": self.cursor += 1
        elif verdict == "done": self.cursor += 1; self.done = True
        elif verdict == "retry": pass                    # 不前进:下一轮重做本步
        elif verdict == "replan": self.replan_pending = True

    def recent(self, k):
        """返回当前 run 里最近 k 条执行历史。

        这是 working memory 的 recency,只看本次运行内刚发生的事情。
        """
        return self.history[-k:]        # recency(≠ semantic_recall)

    def last(self):
        """返回当前 run 的最后一条历史。

        Planner.replan 会用它判断刚才失败的是不是 lookup 步骤。
        """
        return self.history[-1] if self.history else None

# ===== SECTION 2 · TOOLS(DAO · 纯函数/副作用 · 出错就抛,交上层)=====
# lookup、calculator 是纯函数;write_note 的「副作用」是写持久层,故定义在 LongTermMemory 上(SECTION 6)。
def tool_lookup(key: str):                 # 查数值型事实(只读、低风险)
    """根据 key 查询一个数值型事实。

    这是最简单的只读工具,相当于从一个小数据库 KV_FACTS 里取值。
    如果 key 不存在,故意抛异常,交给 Executor/error_handler/replan 处理。
    """
    if key not in KV_FACTS: raise KeyError(f"未知事实 key={key}")
    return KV_FACTS[key]

def tool_calculator(a, op, b):             # 四则运算(纯函数、低风险);出错就抛(如除零)
    """执行四则运算。

    a 是上一步查到的值,b 是倍数或另一个操作数。
    op 支持 mul/add/sub/div。
    这是纯函数工具:不读写文件,不产生副作用。
    """
    a, b = float(a), float(b)
    if op == "mul": return a * b
    if op == "add": return a + b
    if op == "sub": return a - b
    if op == "div":
        if b == 0: raise ZeroDivisionError("除数为 0")
        return a / b
    raise ValueError(f"未知运算符 {op}")

# ===== SECTION 3 · DATA PLANE(Controller / Service / Router / ErrorHandler)=====
class IntentParser:
    """Controller:原始文本 → 结构化意图。真实系统可上 NLU/LLM,这里用正则规则。"""
    @staticmethod
    def parse(goal: str) -> dict:
        """把用户输入的自然语言 goal 解析成结构化 intent。

        例子:
        "查 btc_price 的当前价格,按 10 倍估算,写进笔记 portfolio"
        会变成:
        {"key": "btc_price", "op": "mul", "factor": 10.0, "note_key": "portfolio"}
        """
        key = next((k for k in KV_FACTS if k in goal), None)     # 先匹配已知事实 key
        if key is None:
            m = re.search(r"查\s*([a-z_]+)", goal)               # 兜底:抓 "查 xxx"(可能是未知 key)
            key = m.group(1) if m else "unknown"
        fm = re.search(r"(\d+(?:\.\d+)?)", goal)                 # 第一个数字 = 倍数
        nm = re.search(r"笔记\s*([a-zA-Z0-9_]+)", goal)          # "笔记 xxx" = 笔记 key
        intent = {"key": key, "op": "mul",
                  "factor": float(fm.group(1)) if fm else 1.0,
                  "note_key": nm.group(1) if nm else "note"}
        log("[INTENT]", str(intent))
        return intent

class Policy:
    """Service:(意图 step + 状态 scratch)→ 选哪个工具、传什么参(把上下文注入 args)。
    这是 Brain.decide 当前的「规则大脑」;换成真 LLM 时这套规则被 LLM 取代。"""
    @staticmethod
    def choose(step, state, tools) -> Action:
        """根据当前 Step 和已有状态,决定下一次要调用哪个工具。

        这里做三件事:
        - lookup 步:决定调用 lookup 工具。
        - calc 步:从 state.scratch 取上一步结果,拼 calculator 参数。
        - note 步:从 state.scratch 取计算结果,拼 write_note 参数。
        """
        if step.intent == "lookup":
            return Action("lookup", {"key": step.hint["key"]}, tools["lookup"].risk, "查数值事实")
        if step.intent == "calc":
            a = state.scratch.get("lookup")                      # ← 注入上一步 lookup 的输出
            return Action("calculator", {"a": a, "op": step.hint["op"], "b": step.hint["b"]},
                          tools["calculator"].risk, "对查到的值套用公式")
        if step.intent == "note":
            v = state.scratch.get("calc")                        # ← 注入上一步 calc 的输出
            text = f"{step.hint['key']} = {fmt(v)} (来自 {state.goal[:12]}…)"
            return Action("write_note", {"key": step.hint["key"], "text": text},
                          tools["write_note"].risk, "把结果落盘成笔记")
        raise ValueError(f"未知 step.intent: {step.intent}")

class Router:
    """按名字指派工具(路由表)。注意:路由表只认 name;『选哪个工具』是大脑用 schema 决定的。"""
    @staticmethod
    def dispatch(action: Action, tools: dict):
        """真正调用工具函数。

        action.tool 是工具名,tools 是工具注册表。
        找到工具后把 action.args 展开成函数参数执行。
        """
        log("[ROUTE]", f"指派 → {action.tool}")
        tool = tools.get(action.tool)
        if tool is None: raise KeyError(f"路由失败,无此工具:{action.tool}")
        return tool.fn(**action.args)     # 调真实工具(DAO);出错就抛,交上层

def error_handler(exc: Exception) -> Result:
    """把工具抛出的异常转成统一 Result。

    类似后端里的 @ExceptionHandler:
    - 它不判断业务该不该重试。
    - 它只负责把异常包装成 Result(ok=False,error=...)。
    后续由 Reflector.review 决定 retry/replan/done。
    """
    log("[EXEC]", f"ErrorHandler 捕获:{type(exc).__name__}: {exc}")
    return Result(False, error=f"{type(exc).__name__}: {exc}")

# ===== SECTION 4 · 控制环(Layer 1 · 让它配叫 agent 的核心)=====
class Planner:
    """goal → 有序 steps;支持 replan(state):出错时改道。"""
    @staticmethod
    def plan(intent: dict) -> list:
        """把结构化 intent 拆成固定的三步计划。

        这个 demo 的计划永远是:
        1. 查事实 lookup
        2. 算公式 calc
        3. 记笔记 note
        真实 Agent 里这里通常会换成 LLM Planner。
        """
        # 【LLM 接缝】真实 planner 读 goal + tool schema 自由分解;这里固定拆成「查→算→记」三步。
        return [Step("查事实", "lookup", {"key": intent["key"]}),
                Step("算公式", "calc", {"op": intent["op"], "b": intent["factor"]}),
                Step("记笔记", "note", {"key": intent["note_key"]})]
    @staticmethod
    def replan(state) -> Any:
        """失败后重新规划。

        目前只处理一种情况:
        - 如果 lookup 失败,并且还没用过兜底 key,
          就把查询步骤改成 FALLBACK_FACT,再接上后面的 calc/note。
        返回新的 Step 表示继续执行;返回 None 表示无法恢复。
        """
        state.replan_pending = False
        last = state.last()
        if last and last.step.intent == "lookup" and not last.result.ok and not state.used_fallback:
            state.used_fallback = True
            log("[PLAN]", f"replan:事实查询失败,回退到 {FALLBACK_FACT} 重试")
            state.steps = [Step("回退查询", "lookup", {"key": FALLBACK_FACT})] + state.steps[state.cursor + 1:]
            state.cursor = 0
            return state.steps[0]
        return None                       # 无法恢复 → 循环收尾

class Terminator:
    """should_stop:goal 达成 / 超 max_steps / 超 budget / 连续失败超阈值 → 任一即停。"""
    @staticmethod
    def should_stop(state) -> bool:
        """判断 Agent 主循环是否应该停止。

        停止条件包括:
        - state.done=True,说明任务完成或确定性结束。
        - steps_taken 超过最大步数。
        - cost 超过预算。
        - 连续失败次数超过阈值。
        同时会把停止原因写到 state.stop_reason。
        """
        why = ("goal 达成 / 收尾" if state.done else
               "超 max_steps" if state.steps_taken >= state.max_steps else
               "超预算 budget" if state.cost >= state.max_cost else
               "连续失败超阈值" if state.failures >= state.max_failures else None)
        if why: state.stop_reason = why
        return bool(why)

class Reflector:
    """review → continue|retry|replan|done。把 single-shot 变成真正 loop 的关键:每步自评。"""
    @staticmethod
    def review(step, result, state) -> str:
        """根据本步执行结果决定下一轮怎么走。

        返回值含义:
        - continue:本步成功,进入下一步。
        - retry:本步失败但还能重试,下一轮仍执行本步。
        - replan:查询失败时换一条计划继续。
        - done:任务完成,或失败已无法恢复。
        """
        if not result.ok:
            if result.error.startswith("BOUNDARY"): return "done"          # 确定性拦截,重试无意义
            if step.intent == "lookup" and not state.used_fallback: return "replan"  # 查询失败→换路
            if state.failures + 1 < state.max_failures: return "retry"     # 还有余量→重做本步
            return "done"                                                  # 放弃(失败收尾)
        return "done" if state.cursor >= len(state.steps) - 1 else "continue"

# ===== SECTION 5 · 上下文(Layer 2)=====
class ContextBuilder:
    """build:把进决策的东西拼起来 = system + working(recency) + recalled(retrieval) + tool schema。"""
    SYSTEM = "你是一个多工具 agent,按计划调用工具完成 goal,每次只输出一个 Action。"
    @staticmethod
    def build(step, state, ltm, tools) -> Context:
        """构造 Brain.decide 需要看的上下文。

        真实 LLM Agent 会把这些内容拼成 messages/prompt:
        - system:系统指令。
        - working:当前 run 最近几步的短期记忆。
        - recalled:长期记忆里按相关性召回的条目。
        - schemas:工具说明和风险等级。
        """
        working = [f"{h.step.name}:{summarize(h.result.value)}" for h in state.recent(2)]  # recency
        recalled = ltm.semantic_recall(f"{step.name} {step.hint}", 2)                       # retrieval
        schemas = [(t.name, t.description, t.risk) for t in tools.values()]                 # tool schema
        log("[CTX]", f"working={len(working)}(recency) recalled={len(recalled)}(semantic_recall) schemas={len(schemas)}")
        return Context(step, state, tools, ContextBuilder.SYSTEM, working, recalled, schemas)

# ===== SECTION 6 · 分层记忆(Layer 3 · 三种,职责不同)=====
# working memory = State(SECTION 1);long-term + semantic memory = 下面这个类。
class LongTermMemory:
    """跨 run 持久化(写 memory.json),并提供两种取回:recent(recency) 与 semantic_recall(retrieval)。"""
    def __init__(self, path):
        """初始化长期记忆。

        path 是持久化文件路径:
        - path=None 表示只放内存,不会写文件。
        - path="memory.json" 表示跨进程保存笔记和情节记忆。
        """
        self.path = path        # None = 仅内存(给 evaluate 用,不落盘,互不污染)
        self.notes = {}         # 领域笔记 key→text(覆盖写)= write_note 工具的真实副作用落点
        self.episodes = []      # 情节记忆:追加,供语义召回
        self.write_calls = 0    # 副作用计数器:用来证明「幂等只生效一次」
        self._load()
    def _load(self):
        """从 memory.json 读取长期记忆。

        如果 path 不存在或 path=None,就保持空记忆。
        """
        if self.path and os.path.exists(self.path):
            d = json.load(open(self.path, encoding="utf-8"))
            self.notes, self.episodes = d.get("notes", {}), d.get("episodes", [])
    def _save(self):
        """把长期记忆写回 memory.json。

        notes 是业务笔记,episodes 是 Agent 执行过的情节记录。
        """
        if self.path:
            json.dump({"notes": self.notes, "episodes": self.episodes},
                      open(self.path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    def write_note(self, key, text):     # 高风险工具 write_note 的真实副作用:写领域笔记并落盘
        """写入一条业务笔记。

        这是 write_note 工具真正产生副作用的地方:
        - self.notes[key] 会被覆盖写入。
        - 如果有 path,会落盘到 memory.json。
        - write_calls 用来证明幂等逻辑是否避免了重复副作用。
        """
        self.write_calls += 1
        self.notes[key] = text
        self._save()
        return f"已写入笔记[{key}]"
    def remember(self, step, action, result):  # ⑩ 把「这步做了什么」记进情节记忆(供未来召回),与领域笔记两码事
        """记录一条 Agent 执行情节。

        它和 write_note 不一样:
        - write_note 是业务结果,比如 portfolio 笔记。
        - remember 是 Agent 自己的运行记忆,供后续 semantic_recall 使用。
        """
        self.episodes.append({"step": step.name, "tool": action.tool,
                              "value": summarize(result.value), "ok": result.ok})
        self._save()
    def recent(self, k):
        """返回长期记忆里最近 k 条情节。

        这是按时间顺序取最近记录,不做语义匹配。
        """
        return self.episodes[-k:]   # recency:最近 k 条(按时间)

    def semantic_recall(self, query, k):             # retrieval:token 重叠打分取 top-k(生产换 embedding 向量检索)
        """从长期记忆里按相关性召回 k 条情节。

        这里用 token 重叠数量做 demo 版相关性分数。
        真实系统一般会用 embedding 向量检索或全文检索。
        """
        q = set(tokenize(query))                      # 它与 recent 是两个方法,证明 retrieval ≠ recency
        scored = [(overlap(q, set(tokenize(json.dumps(e, ensure_ascii=False)))), e) for e in self.episodes]
        scored = sorted([se for se in scored if se[0] > 0], key=lambda se: se[0], reverse=True)
        return [e for _, e in scored[:k]]

# ===== SECTION 7 · 生产边界(Layer 4 · demo→prod 分水岭)=====
class Guardrails:
    """校验「合法但危险」——与 try/except(只管崩没崩)分开。
    check_input:参数范围/白名单、对高危 key 拦截、超长文本改写;check_output:输出脱敏。"""
    NOTE_KEY_OK = re.compile(r"^[a-z0-9_]+$")
    FORBIDDEN_KEYS = {"system", "passwd", "root"}    # 禁止写入的高危笔记 key
    MAX_NOTE_LEN = 30
    @staticmethod
    def check_input(action: Action) -> Action:
        """执行工具前的安全检查。

        这里处理“代码没崩,但业务上危险”的情况:
        - calculator 的输入不能为空、不能过大。
        - write_note 的 key 不能是 system/root/passwd 等敏感名。
        - write_note 的文本太长会被截断改写。
        """
        if action.tool == "calculator":              # 参数范围白名单(防「合法但危险」的天文数字)
            for v in (action.args.get("a"), action.args.get("b")):
                if v is None or abs(float(v)) > 1e9:
                    raise GuardrailBlock("calculator 操作数为空或超出安全范围")
        if action.tool == "write_note":
            key, text = action.args["key"], action.args["text"]
            if key in Guardrails.FORBIDDEN_KEYS or not Guardrails.NOTE_KEY_OK.match(key):
                raise GuardrailBlock(f"禁止写入笔记 key={key}")          # 拦截(block)
            if len(text) > Guardrails.MAX_NOTE_LEN:                      # 改写(rewrite·截断)
                action.args["text"] = text[: Guardrails.MAX_NOTE_LEN] + "…"
                log("[GUARD]", f"note 文本 {len(text)}>{Guardrails.MAX_NOTE_LEN},截断改写")
        log("[GUARD]", f"check_input 通过:{action.tool}")
        return action
    @staticmethod
    def check_output(result: Result) -> Result:
        """执行工具后的输出检查。

        当前 demo 只做简单脱敏:
        如果输出里出现 secret=xxx/token=xxx/key=xxx,就替换成 ***。
        """
        if result.ok and isinstance(result.value, str):
            red = re.sub(r"(secret|token|key)=\S+", r"\1=***", result.value)   # 输出脱敏
            if red != result.value:
                log("[GUARD]", "check_output 命中敏感串,已打码")
                result.value = red
        log("[GUARD]", "check_output 通过")
        return result

class Approval:
    """human-in-the-loop:高风险工具调用前必过此门;低风险工具不过门。"""
    def __init__(self, auto=True):
        """初始化审批器。

        auto=True 表示 demo 自动批准高风险动作。
        auto=False 时遇到高风险工具会抛 ApprovalDenied。
        """
        self.auto = auto   # 可注入的 auto-approve 策略

    def request(self, action: Action):
        """请求批准一次高风险动作。

        当前只有 write_note 是 HIGH 风险,所以写笔记前会经过这里。
        真实系统可以在这里接人工确认、审批流或权限系统。
        """
        # 真实场景:此处应 input("批准吗?") 或回调审批系统,阻塞等人。demo 用自动放行。
        log("[APPROVE]", f"申请高风险工具 {action.tool} → {'通过' if self.auto else '拒绝'} "
                         f"(auto={self.auto};生产环境此处阻塞等人审)")
        if not self.auto: raise ApprovalDenied(f"{action.tool} 未获批准")

class Executor:
    """⑥ 执行边界:鉴权 → 幂等 → retry/backoff → Router → Tool。
    把生产三件套(权限/重试/幂等)收在一处,像 Spring 的事务模板。"""
    def __init__(self, tools, allowed, flaky=None, max_retries=3):
        """初始化执行器。

        tools 是工具注册表。
        allowed 是本次允许调用的工具白名单。
        flaky 用来模拟瞬时故障,例如 {"write_note": 1} 表示第一次写笔记必失败。
        max_retries 是瞬时失败最多重试次数。
        """
        self.tools = tools
        self.allowed = set(allowed)     # 权限/sandbox:本 run 被授权调用的工具集合
        self.flaky = dict(flaky or {})  # 注入式 flaky:{tool: 还要瞬时失败几次}
        self.idem = {}                  # 幂等缓存 idem_key→Result(同 args 不再产生副作用)
        self.max_retries = max_retries
    def _check_permission(self, action):
        """检查当前 action 是否有权限调用目标工具。

        如果工具不在 allowed 白名单里,直接抛 PermissionDenied。
        """
        if action.tool not in self.allowed: raise PermissionDenied(f"未授权调用工具 {action.tool}")
        log("[PERM]", f"{action.tool} risk={action.risk} 授权通过")
    @staticmethod
    def _idem_key(action):
        """为一次工具调用生成幂等 key。

        同一个工具 + 同一组参数会生成同一个 key。
        这样重复提交 write_note 时可以直接返回缓存,不重复写文件。
        """
        raw = action.tool + "|" + json.dumps(action.args, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    def _inject_flaky(self, action):
        """按配置模拟一次瞬时故障。

        这是为了演示 retry/backoff:
        write_note 第一次会在真正写入前失败,第二次重试才成功。
        """
        n = self.flaky.get(action.tool, 0)
        if n > 0:                       # 在「副作用之前」抛,确保重试不会双写
            self.flaky[action.tool] = n - 1
            raise TransientError(f"注入的瞬时故障(还剩 {n - 1} 次)")
    def run(self, action: Action) -> Result:
        """执行一次工具调用,并包住生产级执行边界。

        顺序非常重要:
        1. 权限检查。
        2. 幂等检查,重复请求直接返回缓存。
        3. retry/backoff,只重试 TransientError。
        4. Router.dispatch 真正调用工具。
        5. 普通异常交给 error_handler 变成 Result。
        """
        self._check_permission(action)                     # 1) 鉴权
        key = self._idem_key(action)
        if key in self.idem:                               # 2) 幂等:命中则跳过副作用,返回缓存
            log("[EXEC]", f"幂等命中 key={key},跳过副作用,返回缓存")
            return self.idem[key]
        for attempt in range(1, self.max_retries + 1):     # 3) retry/backoff
            try:
                self._inject_flaky(action)                 #    模拟瞬时故障
                value = Router.dispatch(action, self.tools)#    Router → tool(DAO)
                result = Result(True, value=value)
                self.idem[key] = result                    #    成功才写幂等缓存
                log("[EXEC]", f"{action.tool} 第{attempt}次成功 → {summarize(value)}")
                return result
            except TransientError as e:
                wait = round(0.05 * attempt, 3)
                log("[EXEC]", f"{action.tool} 第{attempt}次瞬时失败({e}),backoff {wait}s 后重试")
                time.sleep(wait)
            except Exception as e:                         # 非瞬时错误 → 交给 ErrorHandler
                return error_handler(e)
        return error_handler(TransientError("重试耗尽"))

class Tracer:
    """per-step trace + budget。与 Memory 是两回事:trace 给 ops/debug,memory 给 agent 决策。"""
    def __init__(self):
        """初始化本次运行的 trace 收集器。

        rows 保存每一步摘要,t0 用来计算总耗时。
        """
        self.rows = []
        self.t0 = time.perf_counter()
    def record(self, step, action, result, verdict, state):
        """记录一步执行结果,并更新伪 token/cost 预算。

        这不是业务记忆,而是给调试、观测、回归分析看的运行日志。
        """
        tokens = (len(str(action.args if action else "")) + len(str(result.value))) // 4 + 5
        state.steps_taken += 1
        state.cost += tokens            # ← budget 计数写回 state,供 Terminator 检查(伪 cost=伪 token)
        self.rows.append({"#": state.steps_taken, "step": step.name,
                          "tool": action.tool if action else "-", "ok": result.ok,
                          "verdict": verdict, "tok": tokens})
        log("[TRACE]", f"#{state.steps_taken} {step.name} tool={action.tool if action else '-'} "
                       f"ok={result.ok} verdict={verdict} tok={tokens} costΣ={state.cost}/{state.max_cost}")
    def summary(self, state, reason):
        """生成整次运行的汇总文本。

        包括每一步调用了哪个工具、是否成功、消耗多少伪 token、
        总步数/总 cost/耗时/停止原因。
        """
        dt = (time.perf_counter() - self.t0) * 1000
        lines = [f"  #{r['#']} {r['step']:<6} tool={r['tool']:<11} ok={r['ok']!s:<5} "
                 f"verdict={r['verdict']:<8} tok={r['tok']}" for r in self.rows]
        return ("\n===== RUN SUMMARY =====\n" + "\n".join(lines) +
                f"\n步数={state.steps_taken}/{state.max_steps}  cost={state.cost}/{state.max_cost}"
                f"  耗时={dt:.0f}ms  停因={reason}\n" + "=" * 40)

# ===== SECTION 8 · BRAIN(★ 换成真实 LLM 的唯一接缝)=====
class Brain:
    """★★★ 把规则大脑换成真实 LLM 的唯一接缝 ★★★
    入参 ctx 已是「拼好的 prompt 材料」(system + working + recalled + tool schemas)。
    真实实现 = 把 ctx 序列化成 messages 发给 LLM,把返回的 JSON 解析成 Action 返回。
    现在用确定性规则(Policy)代替:不联网、可复现,方便学习与测试。"""
    @staticmethod
    def decide(ctx: Context) -> Action:
        """根据上下文决定下一次 Action。

        当前实现只是调用 Policy.choose。
        将来接真实 LLM 时,应该在这里把 ctx 组装成 messages,
        再把模型输出解析成 Action。
        """
        action = Policy.choose(ctx.step, ctx.state, ctx.tools)   # ← 规则大脑(将来由 LLM 取代)
        log("[DECIDE]", f"(Policy 规则) {action.tool}({action.args}) risk={action.risk} — {action.why}")
        return action

# ===== SECTION 9 · AGENT(把所有组件 wire 起来 + 控制环 run)=====
class Agent:
    def __init__(self, mem_path="memory.json", auto_approve=True, cfg=None):
        """组装一个 Agent 实例。

        这里相当于依赖注入:
        - LongTermMemory:长期记忆/持久化。
        - tools:工具注册表。
        - Executor:统一执行工具,负责权限/重试/幂等。
        - Approval:高风险动作审批。
        - Tracer:运行日志和预算统计。
        """
        self.ltm = LongTermMemory(mem_path)                         # long-term / semantic memory
        self.tools = self._build_tools()                            # 工具注册表(含 schema)
        self.executor = Executor(self.tools, allowed=self.tools.keys(),
                                 flaky={"write_note": 1})           # 注入:write_note 第一次必失败
        self.approval = Approval(auto=auto_approve)
        self.tracer = Tracer()
        self.cfg = cfg or {}
        self.state = None
    def _build_tools(self) -> dict:
        """注册 Agent 能用的工具。

        返回的 dict 是工具名到 Tool 对象的映射。
        Tool 里包含:
        - name:工具名。
        - description:给 Brain/LLM 看的工具说明。
        - risk:风险等级,决定是否需要审批。
        - fn:真正要执行的 Python 函数。
        """
        # 每个工具自带 description(tool schema)+ risk(分流)。write_note 绑定到本 agent 的 ltm。
        return {
            "lookup": Tool("lookup", "按 key 查数值型事实(只读,低风险)", LOW, lambda key: tool_lookup(key)),
            "calculator": Tool("calculator", "四则运算 a op b(纯函数,低风险)", LOW,
                               lambda a, op, b: tool_calculator(a, op, b)),
            "write_note": Tool("write_note", "把文本写入持久笔记(有副作用,高风险)", HIGH,
                               lambda key, text: self.ltm.write_note(key, text)),
        }
    def run(self, goal: str) -> dict:
        """Agent 的主流程入口。

        这是你调试时最应该 Step Into 的函数。
        它把一个用户目标 goal 跑成完整闭环:
        parse → plan → loop(step/context/decide/guard/execute/reflect/memory/trace) → finish。
        返回值里包含 ok、answer 和最终 state。
        """
        log("\n[GOAL]", goal)
        intent = IntentParser.parse(goal)                  # Controller:文本 → 意图
        plan = Planner.plan(intent)                        # 控制环:意图 → 有序 steps
        log("[PLAN]", " → ".join(s.name for s in plan))
        state = State.new(goal, plan, self.cfg)            # working memory
        self.state = state
        while not Terminator.should_stop(state):           # ← agent 的出口(≠ pipeline)
            step = state.next_step() or Planner.replan(state)        # ① 现在做哪一步
            if step is None: state.done = True; break                # 无步可做 → 收尾
            action = None
            try:
                ctx = ContextBuilder.build(step, state, self.ltm, self.tools)  # ② 拼决策上下文
                action = Brain.decide(ctx)                            # ③ 选工具 + 定参(LLM 接缝)
                action = Guardrails.check_input(action)               # ④ 执行前:合法但危险?拦/改
                if action.risk == HIGH: self.approval.request(action) # ⑤ 高风险 → 过人审门
                result = self.executor.run(action)                    # ⑥ Router→Tool(权限/重试/幂等)
                result = Guardrails.check_output(result)              # ⑦ 输出过滤
            except BoundaryBlock as e:                                # 生产边界拦截 → 记为失败
                result = Result(False, error=f"BOUNDARY:{type(e).__name__}: {e}")
                log("[GUARD]", f"边界拦截,终止本步:{e}")
            verdict = Reflector.review(step, result, state)           # ⑧ 自评 → 循环走向
            log("[REFLECT]", f"{step.name} → {verdict}")
            state.update(step, action, result, verdict)               # ⑨ 写 working memory
            if action and result.ok:
                self.ltm.remember(step, action, result)               # ⑩ 写 long-term / semantic
                log("[MEM]", f"记入情节记忆:{step.name}")
            self.tracer.record(step, action, result, verdict, state)  # ⑪ per-step trace + budget
        return self._finish(state)
    def _finish(self, state) -> dict:
        """收尾并生成最终答案。

        这里会:
        - 确保 stop_reason 已写入。
        - 从 state.scratch 取计算结果和写笔记结果。
        - 打印 trace summary。
        - 返回结构化输出给调用方。
        """
        Terminator.should_stop(state)                      # 让 stop_reason 落定
        v = state.scratch.get("calc")
        ans = (f"计算结果={fmt(v)},{state.scratch.get('note', '(未写入)')}"
               if v is not None else "未能完成(详见 trace)")
        log("[ANSWER]", ans)
        print(self.tracer.summary(state, state.stop_reason))
        return {"ok": "note" in state.scratch, "answer": ans, "state": state}   # note 步成功=任务达成

# ===== SECTION 10 · EVALUATE + 幂等演示 =====
def demo_idempotency(agent: Agent):
    """演示幂等:同一个写笔记 action 重复提交不会重复产生副作用。

    它会从刚才的 agent.state.history 里找到成功的 write_note action,
    再调用一次 agent.executor.run(note)。
    如果幂等生效,write_calls 不会增加。
    """
    note = next((h.action for h in reversed(agent.state.history)
                 if h.action and h.action.tool == "write_note" and h.result.ok), None)
    if not note: return
    before = agent.ltm.write_calls
    print(f"\n[幂等演示] 再次提交同一个 write_note(key={note.args['key']}) ……")
    agent.executor.run(note)
    print(f"[幂等演示] write_note 真实副作用次数:{before} → {agent.ltm.write_calls} (应保持不变)")

def evaluate():
    """跑一批内置测试 case,粗略验证 Agent 行为是否符合预期。

    覆盖场景:
    - happy:正常查、算、记。
    - replan:未知 key 触发重规划。
    - guard-block:危险笔记 key 被 Guardrails 拦截。
    - budget:步数预算太小导致提前停止。
    """
    print("\n" + "#" * 60 + "\n##### EVALUATE(批量回归)\n" + "#" * 60)
    cases = [
        {"name": "happy", "goal": "查 btc_price 价格 按 10 倍估算 写进笔记 portfolio", "cfg": {}, "want_ok": True},
        {"name": "replan", "goal": "查 doge_price 价格 按 2 倍估算 写进笔记 doge", "cfg": {}, "want_ok": True},      # 未知事实→replan
        {"name": "guard-block", "goal": "查 eth_price 价格 按 3 倍估算 写进笔记 system", "cfg": {}, "want_ok": False},  # key=system 被拦
        {"name": "budget", "goal": "查 btc_price 价格 按 5 倍估算 写进笔记 b", "cfg": {"max_steps": 1}, "want_ok": False},  # 预算 1 步→提前停
    ]
    passed = 0
    for c in cases:
        print(f"\n----- CASE: {c['name']} -----")
        out = Agent(mem_path=None, auto_approve=True, cfg=c["cfg"]).run(c["goal"])  # 临时内存,互不污染
        ok = (out["ok"] == c["want_ok"]); passed += ok
        log("[CASE]", f"{c['name']:<11} 期望ok={c['want_ok']} 实际ok={out['ok']} → {'PASS' if ok else 'FAIL'}")
    print(f"\n##### 通过率:{passed}/{len(cases)} = {100 * passed // len(cases)}%")

# ===== SECTION 11 · __main__ =====
if __name__ == "__main__":
    print("=" * 60 + "\n最小 Agent 演示 —— 看着 loop 走过每个组件\n" + "=" * 60)
    # 跨进程持久化证据:启动时先看 memory.json 里有没有上次留下的痕迹
    boot = LongTermMemory("memory.json")
    tag = "(首次运行为空)" if not boot.episodes else "(来自上一次进程运行)"
    log("[BOOT]", f"从 memory.json 读到 {len(boot.episodes)} 条情节、{len(boot.notes)} 条笔记 {tag}")
    # 1) 跑一遍完整 goal:点亮控制环 + 上下文 + 记忆 + 全部生产边界
    agent = Agent(mem_path="memory.json", auto_approve=True)
    agent.run("查 btc_price 的当前价格, 按 10 倍估算, 把结果写进笔记 portfolio")
    # 2) 幂等演示:同一个高风险写操作再来一次,副作用不重复生效
    demo_idempotency(agent)
    # 3) 批量回归:覆盖 replan / guardrail 拦截 / budget 终止 等分支
    evaluate()
    print("\n提示:再运行一次 `python agent.py`,开头 [BOOT] 行会显示上次写入,演示 long-term 跨进程持久化。")
