"""按时间顺序回放 JSONL 演练记录，并在 restart 时真正重新加载状态。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Any, Dict, Iterable, List, Optional

try:  # 兼容从仓库根运行与已安装包两种路径。
    from scenario_reader import load_events
except ImportError:  # pragma: no cover - 仅路径兜底
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scenario_reader import load_events

from .gateway import Gateway, GatewayError

SUPPORTED_KINDS = {
    "grid", "controller", "request", "begin", "feed", "meter", "inspect",
    "restart", "pause", "resume",
}


def run_file(path: Path, state_path: Optional[Path] = None, *,
             keep_state: bool = False) -> Dict[str, Any]:
    path = Path(path)
    temporary: Optional[Any] = None
    if state_path is None:
        temporary = tempfile.NamedTemporaryFile(prefix="edge-gateway-", suffix=".json", delete=False)
        state_path = Path(temporary.name)
        temporary.close()
    state_path = Path(state_path)
    if state_path.exists():
        state_path.unlink()

    events = list(load_events(path, SUPPORTED_KINDS))
    gateway = Gateway(state_path)
    responses: List[Dict[str, Any]] = []

    for event in events:
        at = int(event["at"])
        kind = str(event["kind"])
        record = dict(event["record"])
        if kind == "restart":
            stopping = gateway.prepare_restart(at)
            gateway = Gateway(state_path)
            recovery = gateway.record_recovery(at)
            responses.append({
                "at": at,
                "kind": kind,
                "ok": True,
                "stopping": stopping,
                "recovery": recovery,
            })
            continue

        try:
            result = gateway.handle(at, kind, record)
            responses.append({"at": at, "kind": kind, "ok": True, "result": result})
        except GatewayError as error:
            responses.append({"at": at, "kind": kind, "ok": False, "result": error.to_dict()})

    summary = {
        "file": str(path),
        "event_count": len(events),
        "state_path": str(state_path) if keep_state else None,
        "responses": responses,
    }
    if keep_state:
        gateway.save()
    elif temporary is not None:
        # 保留内存中的最终 gateway 供程序化调用；只删除临时文件。
        try:
            state_path.unlink()
        except FileNotFoundError:
            pass
    summary["gateway"] = gateway
    return summary


def response_json(summary: Dict[str, Any]) -> str:
    payload = {k: v for k, v in summary.items() if k != "gateway"}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: Optional[Iterable[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="回放边缘网关 JSONL 演练")
    parser.add_argument("files", nargs="+", type=Path)
    parser.add_argument("--state", type=Path, help="持久状态 JSON；默认使用临时文件")
    parser.add_argument("--keep-state", action="store_true", help="保留并覆盖 --state")
    args = parser.parse_args(list(argv) if argv is not None else None)

    for index, path in enumerate(args.files):
        state_path = args.state
        if state_path is not None and len(args.files) > 1:
            state_path = state_path.with_name(f"{state_path.stem}-{index}{state_path.suffix}")
        summary = run_file(path, state_path, keep_state=args.keep_state or args.state is not None)
        print(response_json(summary))
    return 0
