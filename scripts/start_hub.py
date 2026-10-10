"""启动 DSH v1 中枢 gateway（单 worker）。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 让 hub/ 能 import shared
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hub.gateway.app import main


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start XVIS DSH v1 hub gateway")
    parser.add_argument(
        "--config",
        default="hub/config.yaml",
        help="Path to hub config YAML (default: hub/config.yaml)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    main(args.config)
