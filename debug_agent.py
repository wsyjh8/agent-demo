# -*- coding: utf-8 -*-
"""Small pdb entrypoint for stepping through one Agent.run call.

Run with:
    python -m pdb debug_agent.py

The sibling .pdbrc file sets breakpoints inside agent.py automatically.
"""

import debug_agent2


def main() -> None:
    # 旧的 debug_agent.py 入口现在统一委托给 debug_agent2.py,
    # 这样按 F5 或运行当前文件时默认调试 agent2 逻辑。
    debug_agent2.main()


if __name__ == "__main__":
    main()



    
