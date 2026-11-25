"""项目命令行入口：区域端、仿真端和调度台共用一个持久状态文件。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from .gateway import Gateway, GatewayError


def _branch_pairs(values: Optional[List[str]]) -> Dict[str, int]:
    branches: Dict[str, int] = {}
    for value in values or []:
        if "=" not in value:
            raise argparse.ArgumentTypeError("--branch 需要形如 east=7")
        name, raw = value.split("=", 1)
        try:
            branches[name] = int(raw)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"支路限制不是整数：{value}") from exc
    return branches


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gateway",
        description="纯本地合成的工地供电边缘网关；所有功率均为模拟整数单位。",
    )
    parser.add_argument("--state", default=None,
                        help="持久状态 JSON 路径（命令默认 ./.edge-gateway-state.json；scenario 默认临时文件）")
    parser.add_argument("--at", type=int, default=None,
                        help="消息秒点；命令行单次调用默认使用当前 Unix 秒")
    sub = parser.add_subparsers(dest="command", required=True)

    p_scenario = sub.add_parser("scenario", help="回放一个或多个 JSONL 演练文件")
    p_scenario.add_argument("files", nargs="+", type=Path)
    p_scenario.add_argument("--state", type=Path, default=argparse.SUPPRESS,
                            help="持久状态 JSON；默认使用临时文件")
    p_scenario.add_argument("--keep-state", action="store_true")

    p_grid = sub.add_parser("grid", aliases=["envelope", "update-envelope"],
                            help="区域端发布/更新分时段供电包络")
    p_grid.add_argument("--site", required=True)
    p_grid.add_argument("--revision", type=int, required=True)
    p_grid.add_argument("--from", dest="start", type=int, required=True)
    p_grid.add_argument("--until", type=int, required=True)
    p_grid.add_argument("--total", type=int, required=True)
    p_grid.add_argument("--branch", action="append", help="形如 east=7，可重复")

    p_controller = sub.add_parser("controller", help="登记现场控制器")
    p_controller.add_argument("--id", required=True)
    p_controller.add_argument("--site", required=True)
    p_controller.add_argument("--boot", type=int, required=True)
    p_controller.add_argument("--branches", required=True, help="逗号分隔支路")

    p_request = sub.add_parser("request", aliases=["demand", "new-demand"],
                               help="区域/运营终端提出新需求")
    p_request.add_argument("--demand", required=True)
    p_request.add_argument("--site", required=True)
    p_request.add_argument("--branch", required=True)
    p_request.add_argument("--device", required=True)
    p_request.add_argument("--earliest", type=int, required=True)
    p_request.add_argument("--latest", type=int, required=True)
    p_request.add_argument("--duration", type=int, required=True)
    p_request.add_argument("--min", dest="min_power", type=int, required=True)
    p_request.add_argument("--max", dest="max_power", type=int, required=True)
    p_request.add_argument("--interruptible", dest="interruptible",
                           action="store_true", default=False)
    p_request.add_argument("--not-interruptible", dest="interruptible", action="store_false")

    p_begin = sub.add_parser("begin", help="现场仿真端请求开始授权")
    p_begin.add_argument("--demand", required=True)
    p_begin.add_argument("--controller", required=True)
    p_begin.add_argument("--boot", type=int, required=True)
    p_begin.add_argument("--plan", type=int, required=True)

    p_feed = sub.add_parser("feed", help="现场仿真端回报实际调整")
    p_feed.add_argument("--demand", required=True)
    p_feed.add_argument("--controller", required=True)
    p_feed.add_argument("--boot", type=int, required=True)
    p_feed.add_argument("--plan", type=int, required=True)
    p_feed.add_argument("--state", required=True,
                        choices=["drawing", "reduced", "self_power", "unknown"])
    p_feed.add_argument("--power", type=int, default=None)

    p_meter = sub.add_parser("meter", help="录入独立计量快照")
    p_meter.add_argument("--site", required=True)
    p_meter.add_argument("--branch", required=True)
    p_meter.add_argument("--observed-at", type=int, required=True)
    p_meter.add_argument("--power", type=int, required=True)

    p_pause = sub.add_parser("pause", aliases=["pause-demand"],
                             help="区域端暂停可中断需求")
    p_pause.add_argument("--demand", required=True)
    p_pause.add_argument("--reason", default="regional_pause")

    p_resume = sub.add_parser("resume", help="区域端请求恢复；恢复前重新校验容量")
    p_resume.add_argument("--demand", required=True)

    p_inspect = sub.add_parser("inspect", aliases=["status", "show"],
                               help="调度台查看站点或需求")
    p_inspect.add_argument("--site")
    p_inspect.add_argument("--demand")
    p_inspect.add_argument("--text", action="store_true", help="输出中文文本看板而不是 JSON")

    sub.add_parser("restart", help="模拟网关重启并从 --state 恢复")
    return parser


def _record_from_args(args: argparse.Namespace) -> tuple[str, Dict[str, Any]]:
    command = args.command
    if command == "grid":
        return "grid", {
            "site": args.site,
            "revision": args.revision,
            "from": args.start,
            "until": args.until,
            "total": args.total,
            "branches": _branch_pairs(args.branch),
        }
    if command == "controller":
        return "controller", {
            "id": args.id,
            "site": args.site,
            "boot": args.boot,
            "branches": [x.strip() for x in args.branches.split(",") if x.strip()],
        }
    if command == "request":
        return "request", {
            "demand": args.demand,
            "site": args.site,
            "branch": args.branch,
            "device": args.device,
            "earliest": args.earliest,
            "latest": args.latest,
            "duration": args.duration,
            "min": args.min_power,
            "max": args.max_power,
            "interruptible": bool(args.interruptible),
        }
    if command == "begin":
        return "begin", {
            "demand": args.demand,
            "id": args.controller,
            "boot": args.boot,
            "plan": args.plan,
        }
    if command == "feed":
        record = {
            "demand": args.demand,
            "id": args.controller,
            "boot": args.boot,
            "plan": args.plan,
            "state": args.state,
        }
        if args.power is not None:
            record["power"] = args.power
        return "feed", record
    if command == "meter":
        return "meter", {
            "site": args.site,
            "branch": args.branch,
            "observed_at": args.observed_at,
            "power": args.power,
        }
    if command in {"pause", "resume"}:
        record = {"demand": args.demand}
        if command == "pause":
            record["reason"] = args.reason
        return command, record
    if command == "inspect":
        return "inspect", {"site": args.site, "demand": args.demand}
    raise ValueError(f"未映射命令 {command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # 便捷形式：``python gateway.py path/to/trace.jsonl``。
    if raw_argv and raw_argv[0].endswith(".jsonl") and Path(raw_argv[0]).exists():
        raw_argv.insert(0, "scenario")

    parser = build_parser()
    args = parser.parse_args(raw_argv)
    aliases = {
        "envelope": "grid",
        "update-envelope": "grid",
        "new-demand": "request",
        "demand": "request",
        "pause-demand": "pause",
        "status": "inspect",
        "show": "inspect",
    }
    args.command = aliases.get(args.command, args.command)

    if args.command == "scenario":
        from .scenario import main as scenario_main
        scenario_argv: List[str] = [str(p) for p in args.files]
        if args.state:
            scenario_argv += ["--state", str(args.state)]
        if args.keep_state:
            scenario_argv.append("--keep-state")
        return scenario_main(scenario_argv)

    at = args.at if args.at is not None else int(time.time())
    gateway = Gateway(args.state or ".edge-gateway-state.json")

    if args.command == "restart":
        gateway = Gateway(args.state or ".edge-gateway-state.json")
        result = gateway.record_recovery(at)
        _print(result)
        return 0

    kind, record = _record_from_args(args)
    try:
        result = gateway.handle(at, kind, record)
    except GatewayError as error:
        _print(error.to_dict())
        return 2
    if args.command == "inspect" and getattr(args, "text", False):
        from .console import render
        print(render(result))
    else:
        _print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
