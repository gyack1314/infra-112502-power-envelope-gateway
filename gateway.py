#!/usr/bin/env python3
"""项目根入口：等价于 ``python -m edge_gateway``。"""

import sys
from pathlib import Path

# 允许 ``python -I /path/to/gateway.py`` 时仍找到同目录的项目包。
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from edge_gateway import Gateway, GatewayError  # noqa: E402
from edge_gateway.cli import main  # noqa: E402

__all__ = ["Gateway", "GatewayError", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
