"""启动节点 agent。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from node.dsh_agent import main

if __name__ == "__main__":
    main()
