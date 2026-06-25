# -*- coding: utf-8 -*-
"""
Small pdb entrypoint for stepping through one Agent.run call in agent2.py.

Run with:
    python -m pdb debug_agent2.py
"""

import os
import asyncio

from agent2 import Agent, build_brain

# 让这个调试入口【默认接真实 DeepSeek】(前提:已设 DEEPSEEK_API_KEY)。
# 用 setdefault —— launch.json / 命令行里显式设的 AGENT2_LLM 仍然优先,
# 想调回离线 fake 时,在该调试配置的 env 里写 "AGENT2_LLM": "fake" 即可覆盖。
if os.getenv("DEEPSEEK_API_KEY"):
    os.environ.setdefault("AGENT2_LLM", "deepseek")
    os.environ.setdefault("AGENT2_MODEL", "deepseek-v4-flash")

# 这个目标 fake_llm 和真实模型都能处理(便于在两种大脑下逐步调试)。
# 接了真实模型(AGENT2_LLM=deepseek)后,也可以换成自由形式的目标,例如
# "查 btc_price 的当前价格,按 10 倍估算,把结果写进笔记 portfolio"。
GOAL = "查 btc_price 和 eth_price,把两者之和写进笔记 portfolio"


def main() -> None:
    print("=" * 60 + "\nAgent2 debug run —— 单次目标，方便逐步调试\n" + "=" * 60)
    llm, label = build_brain()          # 本文件默认接 DeepSeek(有 key 时);可被 launch.json 的 AGENT2_LLM 覆盖
    print(f"[DEBUG] brain={label}  goal={GOAL}")

    agent = Agent(name="debug", llm=llm, model_label=label, suspend_high=False)
    result = asyncio.run(agent.run(GOAL))

    print("\n[RESULT]", result.status)
    print("[ANSWER]", result.answer)
    if result.pending:
        print("[PENDING]", result.pending)


if __name__ == "__main__":
    import sys
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
