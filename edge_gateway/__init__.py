"""本地合成工地的边缘供电协调网关。

只使用 Python 标准库。核心对象 :class:`Gateway` 可直接嵌入测试或 CLI；
:func:`render_dashboard` 把 inspect 结果渲染成调度台文本。
"""

from .gateway import Gateway, GatewayError
from .console import render as render_dashboard

__all__ = ["Gateway", "GatewayError", "render_dashboard"]
