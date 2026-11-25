"""边缘网关核心：拟定计划、授权、扣住预算、计量确认与重启恢复。

设计上严格区分三类功率：

``planned``
    请求被接受时安排的最低连续功率，只用于避免未来时段重复承诺。

``notified`` / ``hold``
    已下发授权或降额通知后，在现场确认前继续保守扣住的容量。

``actual``
    控制器 feed 与独立 meter 给出的已实现/实测功率。

状态全部是 JSON 可序列化的普通字典，便于原子写入本地状态文件。
"""

from __future__ import annotations

import copy
import functools
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, Iterable, List, Optional, Tuple


class GatewayError(ValueError):
    """输入契约或当前状态不允许该操作。"""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": False, "error": self.code, "message": self.message, "details": self.details}


def _persist(method):
    """让直接调用公开命令方法也会原子落盘；handle 再保存一次无副作用。"""

    @functools.wraps(method)
    def wrapper(self: "Gateway", *args: Any, **kwargs: Any) -> Any:
        try:
            result = method(self, *args, **kwargs)
        except BaseException:
            self.save()
            raise
        self.save()
        return result

    return wrapper


def _new_state() -> Dict[str, Any]:
    return {
        "schema": 1,
        "last_at": 0,
        "next_action_seq": 1,
        "restart_count": 0,
        "global_actions": [],
        "sites": {},
    }


def _new_site() -> Dict[str, Any]:
    return {
        "next_plan": 1,
        "envelopes": [],
        "controllers": {},
        "demands": {},
        "observations": [],
        "incidents": [],
        "actions": [],
    }


class Gateway:
    """协调多个站点、控制器、需求、包络和计量快照的本地网关。"""

    # ------------------------------------------------------------------
    # 构造、持久化和入口分发
    # ------------------------------------------------------------------
    def __init__(self, state_path: Optional[os.PathLike[str] | str] = None,
                 state: Optional[Dict[str, Any]] = None):
        self.state_path = Path(state_path) if state_path is not None else None
        if state is not None:
            self.state = state
        elif self.state_path is not None and self.state_path.exists():
            self.state = self._read_state(self.state_path)
        else:
            self.state = _new_state()
        if self.state_path is not None:
            self.save()

    @staticmethod
    def _read_state(path: Path) -> Dict[str, Any]:
        with path.open("r", encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or "sites" not in data:
            raise GatewayError("bad_state", "状态文件不是有效的网关状态")
        data.setdefault("schema", 1)
        data.setdefault("last_at", 0)
        data.setdefault("next_action_seq", 1)
        data.setdefault("restart_count", 0)
        data.setdefault("global_actions", [])
        return data

    def save(self) -> None:
        if self.state_path is None:
            return
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.state, stream, ensure_ascii=False, indent=2, sort_keys=False)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except BaseException:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
            raise

    def simulate_restart(self, at: Optional[int] = None) -> Tuple["Gateway", Dict[str, Any]]:
        """用深拷贝模拟进程退出后从持久状态恢复。"""
        self.prepare_restart(at)
        restored = Gateway(state_path=self.state_path, state=copy.deepcopy(self.state)
                           if self.state_path is None else None)
        recovery = restored.record_recovery(at)
        return restored, recovery

    def prepare_restart(self, at: Optional[int] = None) -> Dict[str, Any]:
        if at is not None:
            self._require_int_value(at, "at", minimum=0)
            self.state["last_at"] = at
        action = self._global_action(at, "gateway_stopping")
        for ss in self.state["sites"].values():
            ss["actions"].append(action)
        self.save()
        return action

    def record_recovery(self, at: Optional[int] = None) -> Dict[str, Any]:
        if at is not None:
            self._require_int_value(at, "at", minimum=0)
            self.state["last_at"] = at
        self.state["restart_count"] += 1
        active: List[Dict[str, Any]] = []
        for site_id, ss in self.state["sites"].items():
            self._reconcile_site(site_id, at if at is not None else self.state.get("last_at", 0))
            for did, d in ss["demands"].items():
                auth = d.get("authorization")
                if auth and not auth.get("released"):
                    active.append({
                        "site": site_id,
                        "demand": did,
                        "plan": auth.get("plan", d.get("plan")),
                        "controller": auth.get("controller"),
                        "boot": auth.get("boot"),
                        "hold_power": auth.get("hold_power"),
                        "state": d.get("lifecycle"),
                    })
        recovery = {
            "restarted": True,
            "restart_count": self.state["restart_count"],
            "at": at,
            "active_decisions": active,
            "duplicate_authorizations_issued": 0,
        }
        recovery_action = self._global_action(
            at, "gateway_recovered",
            restart_count=self.state["restart_count"],
            active_decisions=active,
            duplicate_authorizations_issued=0,
        )
        for ss in self.state["sites"].values():
            ss["actions"].append(recovery_action)
        self.save()
        return recovery

    def handle_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """处理 ``{"at": ..., "kind": ..., "record": ...}`` 形式的消息。"""
        if not isinstance(event, dict):
            raise GatewayError("bad_event", "事件必须是对象")
        return self.handle(event["at"], event["kind"], event["record"])

    def handle(self, at: int, kind: str, record: Dict[str, Any]) -> Dict[str, Any]:
        """处理一条输入消息；``kind`` 与 examples/message_types.json 对齐。"""
        self._require_int_value(at, "at", minimum=0)
        if not isinstance(kind, str) or not kind:
            raise GatewayError("bad_kind", "kind 必须是非空字符串")
        if not isinstance(record, dict):
            raise GatewayError("bad_record", "record 必须是对象")

        self.state["last_at"] = at
        handler = {
            "grid": self.publish_envelope,
            "controller": self.register_controller,
            "request": self.submit_demand,
            "begin": self.begin_demand,
            "feed": self.report_feed,
            "meter": self.report_meter,
            "pause": self.pause_demand,
            "resume": self.resume_demand,
            "inspect": lambda a, r: self.inspect(
                at=a, site=r.get("site"), demand=r.get("demand")
            ),
        }.get(kind)
        if handler is None:
            raise GatewayError("unknown_kind", f"未支持的消息名称：{kind}")
        result = handler(at, record)
        if kind != "inspect":
            self.save()
        return result

    # ------------------------------------------------------------------
    # 校验和小工具
    # ------------------------------------------------------------------
    @staticmethod
    def _require_int_value(value: Any, key: str, *, minimum: Optional[int] = None,
                           maximum: Optional[int] = None) -> int:
        if type(value) is not int:
            raise GatewayError("bad_integer", f"{key} 必须是整数")
        if minimum is not None and value < minimum:
            raise GatewayError("out_of_range", f"{key} 不能小于 {minimum}")
        if maximum is not None and value > maximum:
            raise GatewayError("out_of_range", f"{key} 不能大于 {maximum}")
        return value

    @classmethod
    def _require_int(cls, record: Dict[str, Any], key: str, *, required: bool = True,
                     minimum: Optional[int] = None) -> Optional[int]:
        if key not in record:
            if required:
                raise GatewayError("missing_field", f"缺少字段 {key}")
            return None
        return cls._require_int_value(record[key], key, minimum=minimum)

    @staticmethod
    def _require_str(record: Dict[str, Any], key: str) -> str:
        value = record.get(key)
        if not isinstance(value, str) or not value:
            raise GatewayError("bad_string", f"{key} 必须是非空字符串")
        return value

    def _site(self, site_id: str, *, create: bool = False) -> Dict[str, Any]:
        if create and site_id not in self.state["sites"]:
            self.state["sites"][site_id] = _new_site()
        try:
            return self.state["sites"][site_id]
        except KeyError:
            raise GatewayError("unknown_site", f"未登记站点：{site_id}") from None

    def _find_demand(self, demand_id: str) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        for site_id, ss in self.state["sites"].items():
            if demand_id in ss["demands"]:
                return site_id, ss, ss["demands"][demand_id]
        raise GatewayError("unknown_demand", f"未找到需求：{demand_id}")

    def _global_action(self, at: Optional[int], action_type: str, **payload: Any) -> Dict[str, Any]:
        action = {
            "seq": self.state["next_action_seq"],
            "at": at,
            "type": action_type,
            **payload,
        }
        self.state["next_action_seq"] += 1
        self.state.setdefault("global_actions", []).append(action)
        return action

    def _action(self, site_id: str, ss: Dict[str, Any], at: Optional[int],
                action_type: str, **payload: Any) -> Dict[str, Any]:
        action = self._global_action(at, action_type, site=site_id, **payload)
        ss["actions"].append(action)
        return action

    @staticmethod
    def _scope_for(site_id: str, scope_type: str, branch: Optional[str] = None) -> str:
        if scope_type == "site":
            return f"site:{site_id}"
        return f"branch:{site_id}:{branch}"

    @staticmethod
    def _in_scope(d: Dict[str, Any], scope: Tuple[str, Optional[str], Optional[str]]) -> bool:
        scope_type, _site_id, branch = scope
        if scope_type == "site":
            return True
        return d.get("branch") == branch

    # ------------------------------------------------------------------
    # 输入：包络、控制器
    # ------------------------------------------------------------------
    @_persist
    def publish_envelope(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        site_id = self._require_str(record, "site")
        revision = self._require_int(record, "revision", minimum=1)
        start = self._require_int(record, "from", minimum=0)
        until = self._require_int(record, "until", minimum=1)
        if start >= until:
            raise GatewayError("bad_interval", "包络要求 from < until")
        total = self._require_int(record, "total", minimum=0)
        branches = record.get("branches")
        if not isinstance(branches, dict) or not branches:
            raise GatewayError("bad_branches", "branches 必须是非空对象")
        clean_branches: Dict[str, int] = {}
        for name, limit in branches.items():
            if not isinstance(name, str) or not name:
                raise GatewayError("bad_branch", "支路名必须是非空字符串")
            clean_branches[name] = self._require_int_value(limit, f"branches.{name}", minimum=0)

        ss = self._site(site_id, create=True)
        for env in ss["envelopes"]:
            if env["revision"] == revision:
                if (env["from"], env["until"], env["total"], env["branches"]) == (
                    start, until, total, clean_branches
                ):
                    return {"ok": True, "duplicate": True, "site": site_id, "revision": revision}
                raise GatewayError("revision_conflict", f"revision {revision} 已以不同内容发布")

        env = {
            "site": site_id,
            "revision": revision,
            "received_at": at,
            "from": start,
            "until": until,
            "total": total,
            "branches": clean_branches,
        }
        ss["envelopes"].append(env)
        action = self._action(site_id, ss, at, "envelope_published",
                              revision=revision, **{k: env[k] for k in
                                                    ("from", "until", "total", "branches")})
        self._reconcile_site(site_id, at)
        return {"ok": True, "site": site_id, "revision": revision, "action": action}

    @_persist
    def register_controller(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        controller_id = self._require_str(record, "id")
        site_id = self._require_str(record, "site")
        boot = self._require_int(record, "boot", minimum=0)
        branches = record.get("branches")
        if not isinstance(branches, list) or not branches or not all(
            isinstance(x, str) and x for x in branches
        ):
            raise GatewayError("bad_branches", "控制器 branches 必须是非空字符串数组")
        if len(set(branches)) != len(branches):
            raise GatewayError("bad_branches", "控制器 branches 不能重复")

        ss = self._site(site_id, create=True)
        for branch in branches:
            for cid, other in ss["controllers"].items():
                if cid != controller_id and branch in other.get("branches", []):
                    raise GatewayError("branch_conflict",
                                       f"支路 {branch} 已由控制器 {cid} 负责")

        existing = ss["controllers"].get(controller_id)
        if existing is None:
            controller = {
                "id": controller_id,
                "site": site_id,
                "boot": boot,
                "branches": list(branches),
                "registered_at": at,
                "boot_history": [],
            }
            ss["controllers"][controller_id] = controller
            action = self._action(site_id, ss, at, "controller_registered",
                                   controller=controller_id, boot=boot, branches=list(branches))
            return {"ok": True, "controller": controller_id, "boot": boot, "action": action}

        old_boot = existing.get("boot")
        existing["branches"] = list(branches)
        if old_boot != boot:
            existing.setdefault("boot_history", []).append({"boot": old_boot, "at": at})
            existing["boot"] = boot
            # 明确不清零任何 demand.authorization.hold_power：旧代次下可能仍在取电。
            action = self._action(
                site_id, ss, at, "controller_reboot_detected",
                controller=controller_id, old_boot=old_boot, boot=boot,
                note="旧用量和未确认释放继续保留",
            )
            return {"ok": True, "controller": controller_id, "boot": boot,
                    "reboot": True, "action": action}

        action = self._action(site_id, ss, at, "controller_reregistered",
                              controller=controller_id, boot=boot, branches=list(branches))
        return {"ok": True, "controller": controller_id, "boot": boot, "action": action}

    def _controller_for_branch(self, ss: Dict[str, Any], branch: str) -> Optional[Dict[str, Any]]:
        for controller in ss["controllers"].values():
            if branch in controller.get("branches", []):
                return controller
        return None

    # ------------------------------------------------------------------
    # 包络选择与容量计算
    # ------------------------------------------------------------------
    def _envelope_at(self, ss: Dict[str, Any], t: int) -> Optional[Dict[str, Any]]:
        best: Optional[Dict[str, Any]] = None
        best_key: Optional[Tuple[int, int]] = None
        for env in ss["envelopes"]:
            if env["from"] <= t < env["until"]:
                key = (int(env["revision"]), int(env["received_at"]))
                if best is None or best_key is None or key > best_key:
                    best = env
                    best_key = key
        return best

    @staticmethod
    def _demand_load_at(d: Dict[str, Any], t: int) -> int:
        auth = d.get("authorization")
        if auth:
            # 已授权后以保守 hold 为准；已释放授权不再回退到拟定计划，
            # 否则会把同一需求重复计算一次。
            if not auth.get("released"):
                return max(0, int(auth.get("hold_power", 0)))
            return 0
        start = d.get("planned_start")
        end = d.get("planned_end")
        if start is not None and end is not None and start <= t < end:
            arrangement = d.get("arrangement") or {}
            if arrangement.get("feasible"):
                return max(0, int(d.get("min_power", 0)))
        return 0

    def _available_power(
        self, site_id: str, ss: Dict[str, Any], start: int, end: int, branch: str,
        exclude_id: Optional[str] = None,
    ) -> Tuple[Optional[int], Optional[Dict[str, Any]]]:
        """返回区间内所有时刻、站点和该支路约束下的最小剩余功率。"""
        if end <= start:
            return None, {"code": "bad_interval", "at": start}

        points = {start, end}
        for env in ss["envelopes"]:
            for value in (env["from"], env["until"]):
                if start < value < end:
                    points.add(value)
        for did, d in ss["demands"].items():
            if did == exclude_id:
                continue
            auth = d.get("authorization")
            if auth and not auth.get("released"):
                # 未确认释放的 hold 是保守的持续可能占用，不需要区间端点。
                continue
            p_start = d.get("planned_start")
            p_end = d.get("planned_end")
            if p_start is not None and p_end is not None:
                left = max(start, p_start)
                right = min(end, p_end)
                if left < right:
                    points.add(left)
                    points.add(right)

        ordered = sorted(points)
        minimum: Optional[int] = None
        tight: Optional[Dict[str, Any]] = None

        for left, right in zip(ordered, ordered[1:]):
            if left >= end:
                break
            env = self._envelope_at(ss, left)
            if env is None:
                return None, {"code": "no_envelope", "at": left,
                              "scope": self._scope_for(site_id or "?", "site")}
            branch_limit = env["branches"].get(branch)
            if branch_limit is None:
                return None, {"code": "no_branch_limit", "at": left, "branch": branch,
                              "scope": self._scope_for(site_id or "?", "branch", branch)}

            site_load = 0
            branch_load = 0
            contributors: List[Dict[str, Any]] = []
            for did, d in ss["demands"].items():
                if did == exclude_id:
                    continue
                power = self._demand_load_at(d, left)
                if power <= 0:
                    continue
                site_load += power
                contributors.append({"demand": did, "power": power})
                if d.get("branch") == branch:
                    branch_load += power

            site_slack = int(env["total"]) - site_load
            branch_slack = int(branch_limit) - branch_load
            segment_min = min(site_slack, branch_slack)
            candidate_tight = {
                "code": "capacity_exceeded" if segment_min < 0 else "tight_capacity",
                "at": left,
                "segment_until": right,
                "envelope_revision": env["revision"],
                "site_limit": env["total"],
                "site_load": site_load,
                "site_slack": site_slack,
                "branch": branch,
                "branch_limit": branch_limit,
                "branch_load": branch_load,
                "branch_slack": branch_slack,
                "contributors": contributors,
                "scope": (self._scope_for(site_id, "site")
                          if site_slack <= branch_slack
                          else self._scope_for(site_id, "branch", branch)),
            }
            if minimum is None or segment_min < minimum:
                minimum = segment_min
                tight = candidate_tight

        return minimum, tight

    def _find_arrangement(
        self, site_id: str, ss: Dict[str, Any], d: Dict[str, Any], at: int
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        earliest = max(int(d["earliest"]), at)
        latest = int(d["latest"])
        if earliest > latest:
            return None, {
                "code": "window_closed",
                "at": at,
                "earliest": d["earliest"],
                "latest": latest,
            }

        last_error: Optional[Dict[str, Any]] = None
        for start in range(earliest, latest + 1):
            end = start + int(d["duration"])
            slack, tight = self._available_power(
                site_id, ss, start, end, d["branch"], exclude_id=d["id"]
            )
            if slack is None:
                last_error = tight
                continue
            if slack >= int(d["min_power"]):
                return {
                    "feasible": True,
                    "start": start,
                    "end": end,
                    "minimum_available_power": slack,
                    "checked_at": at,
                }, None
            last_error = dict(tight or {})
            last_error.update({
                "code": "insufficient_minimum_capacity",
                "candidate_start": start,
                "candidate_end": end,
                "required_min": d["min_power"],
                "available_power": slack,
                "window_earliest": d["earliest"],
                "window_latest": latest,
            })
        if last_error is None:
            last_error = {"code": "no_candidate_start", "earliest": earliest, "latest": latest}
        return None, last_error

    # ------------------------------------------------------------------
    # 输入：需求
    # ------------------------------------------------------------------
    @_persist
    def submit_demand(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        demand_id = self._require_str(record, "demand")
        site_id = self._require_str(record, "site")
        branch = self._require_str(record, "branch")
        device = self._require_str(record, "device")
        earliest = self._require_int(record, "earliest", minimum=0)
        latest = self._require_int(record, "latest", minimum=0)
        duration = self._require_int(record, "duration", minimum=1)
        min_power = self._require_int(record, "min", minimum=0)
        max_power = self._require_int(record, "max", minimum=0)
        interruptible = record.get("interruptible")
        if not isinstance(interruptible, bool):
            raise GatewayError("bad_bool", "interruptible 必须是布尔值")
        if earliest > latest:
            raise GatewayError("bad_window", "earliest 不能晚于 latest")
        if min_power > max_power:
            raise GatewayError("bad_power_range", "min 不能大于 max")

        ss = self._site(site_id, create=True)
        if branch not in (self._envelope_at(ss, at) or {}).get("branches", {}) and ss["envelopes"]:
            # 包络可能尚未覆盖当前秒，但支路名应能在已发布包络中找到。
            known_branches = set()
            for env in ss["envelopes"]:
                known_branches.update(env["branches"])
            if branch not in known_branches:
                raise GatewayError("unknown_branch", f"包络中没有支路：{branch}")

        if demand_id in ss["demands"]:
            old = ss["demands"][demand_id]
            same = all(old.get(k) == v for k, v in {
                "site": site_id, "branch": branch, "device": device,
                "earliest": earliest, "latest": latest, "duration": duration,
                "min_power": min_power, "max_power": max_power,
                "interruptible": interruptible,
            }.items())
            if same:
                return {"ok": True, "duplicate": True, "demand": demand_id, "plan": old["plan"]}
            raise GatewayError("demand_conflict", f"demand {demand_id} 已以不同契约存在")

        plan = int(ss["next_plan"])
        ss["next_plan"] += 1
        d = {
            "id": demand_id,
            "site": site_id,
            "branch": branch,
            "device": device,
            "earliest": earliest,
            "latest": latest,
            "duration": duration,
            "min_power": min_power,
            "max_power": max_power,
            "interruptible": interruptible,
            "plan": plan,
            "lifecycle": "recorded",
            "committed": False,
            "regional_paused": False,
            "planned_start": None,
            "planned_end": None,
            "arrangement": None,
            "wait": None,
            "authorization": None,
            "feeds": [],
            "begin_attempts": 0,
            "created_at": at,
        }
        ss["demands"][demand_id] = d
        self._action(site_id, ss, at, "demand_received", demand=demand_id, plan=plan,
                     branch=branch, min=min_power, max=max_power)
        self._replan_demand(site_id, ss, d, at)
        return self.demand_view(site_id, ss, d, at=at)

    def _replan_demand(self, site_id: str, ss: Dict[str, Any], d: Dict[str, Any],
                       at: int) -> Optional[Dict[str, Any]]:
        # 已授权决定不能被后台调度悄悄移动；意外降额走事件/受限处置。
        auth = d.get("authorization")
        if auth and not auth.get("released"):
            return d.get("wait")
        if d.get("regional_paused"):
            d["lifecycle"] = "paused"
            d["wait"] = {"code": "regional_pause", "at": at,
                         "message": "区域端要求暂停，恢复前不得开始"}
            return d["wait"]
        if d.get("lifecycle") == "self_power":
            return None

        arrangement, error = self._find_arrangement(site_id, ss, d, at)
        if arrangement is None:
            d["arrangement"] = {"feasible": False, "checked_at": at, "error": error}
            d["planned_start"] = None
            d["planned_end"] = None
            d["lifecycle"] = "waiting"
            d["wait"] = error or {"code": "no_feasible_arrangement", "at": at}
            if d.get("committed"):
                d["wait"]["committed_promise_at_risk"] = True
            return d["wait"]

        first_accept = not d.get("committed")
        d["arrangement"] = arrangement
        d["planned_start"] = arrangement["start"]
        d["planned_end"] = arrangement["end"]
        d["committed"] = True
        if d.get("lifecycle") not in ("waiting", "authorized", "drawing", "reduced", "paused"):
            d["lifecycle"] = "accepted"

        startability = self._startability(site_id, ss, d, at)
        if startability["can_start"]:
            d["wait"] = None
            if d.get("lifecycle") in ("recorded", "waiting", "accepted"):
                d["lifecycle"] = "accepted"
        else:
            reason = (startability.get("reasons") or [{}])[0]
            d["wait"] = {
                "code": reason.get("code", "not_startable"),
                "at": at,
                "message": reason.get("message"),
                "details": reason,
            }
            # 窗口未到叫 accepted；窗口已开但容量不足叫 waiting。
            if reason.get("code") in ("before_window",):
                d["lifecycle"] = "accepted"
            else:
                d["lifecycle"] = "waiting"
        if first_accept:
            self._action(site_id, ss, at, "demand_accepted", demand=d["id"], plan=d["plan"],
                         planned_start=d["planned_start"], planned_end=d["planned_end"])
        return d["wait"]

    # ------------------------------------------------------------------
    # 可开始性与 begin 授权
    # ------------------------------------------------------------------
    def _startability(self, site_id: str, ss: Dict[str, Any], d: Dict[str, Any],
                      at: int) -> Dict[str, Any]:
        reasons: List[Dict[str, Any]] = []
        auth = d.get("authorization")
        if auth and not auth.get("released"):
            reasons.append({"code": "already_authorized", "plan": auth.get("plan")})
        if d.get("lifecycle") == "self_power" or (auth and auth.get("released")):
            reasons.append({"code": "external_budget_released",
                            "message": "该需求已确认结束外部取电"})
        if d.get("regional_paused"):
            reasons.append({"code": "regional_paused"})

        if at < int(d["earliest"]):
            reasons.append({"code": "before_window", "earliest": d["earliest"], "at": at})
        if at > int(d["latest"]):
            reasons.append({"code": "window_closed", "latest": d["latest"], "at": at})

        controller = self._controller_for_branch(ss, d["branch"])
        if controller is None:
            reasons.append({"code": "no_responsible_controller", "branch": d["branch"]})

        slack: Optional[int] = None
        tight: Optional[Dict[str, Any]] = None
        if int(d["earliest"]) <= at <= int(d["latest"]):
            slack, tight = self._available_power(
                site_id, ss, at, at + int(d["duration"]), d["branch"], exclude_id=d["id"]
            )
            if slack is None:
                reasons.append(tight or {"code": "no_capacity_information"})
            elif slack < int(d["min_power"]):
                reasons.append({
                    "code": "insufficient_capacity",
                    "required_min": d["min_power"],
                    "available_power": slack,
                    "tight": tight,
                })

        can_start = not reasons
        grant = None
        if can_start and slack is not None:
            grant = min(int(d["max_power"]), slack)
        return {
            "can_start": can_start,
            "at": at,
            "would_grant_power": grant,
            "available_power": slack,
            "required_min": d["min_power"],
            "controller": controller["id"] if controller else None,
            "current_boot": controller["boot"] if controller else None,
            "reasons": reasons,
        }

    @_persist
    def begin_demand(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        demand_id = self._require_str(record, "demand")
        controller_id = self._require_str(record, "id")
        boot = self._require_int(record, "boot", minimum=0)
        plan = self._require_int(record, "plan", minimum=1)

        site_id, ss, d = self._find_demand(demand_id)
        controller = ss["controllers"].get(controller_id)
        if controller is None:
            raise GatewayError("unknown_controller", f"未登记控制器：{controller_id}")
        if controller.get("site") != site_id:
            raise GatewayError("wrong_site", "控制器不属于该站点")
        if d["branch"] not in controller.get("branches", []):
            self._action(site_id, ss, at, "authorization_rejected_branch",
                         demand=demand_id, controller=controller_id, plan=plan,
                         branch=d["branch"])
            raise GatewayError("controller_branch_forbidden",
                               f"控制器 {controller_id} 不负责支路 {d['branch']}")
        if boot != controller.get("boot"):
            self._action(site_id, ss, at, "authorization_rejected_stale_boot",
                         demand=demand_id, controller=controller_id,
                         record_boot=boot, current_boot=controller.get("boot"))
            raise GatewayError("stale_boot", "boot 不是控制器当前代次；旧用量不会被清零")
        if plan != d.get("plan"):
            self._action(site_id, ss, at, "authorization_rejected_plan",
                         demand=demand_id, controller=controller_id,
                         record_plan=plan, current_plan=d.get("plan"))
            raise GatewayError("plan_mismatch",
                               f"plan 应为 {d.get('plan')}，收到 {plan}")

        auth = d.get("authorization")
        if auth and not auth.get("released"):
            if (auth.get("plan") == plan and auth.get("controller") == controller_id
                    and auth.get("boot") == boot):
                return {"ok": True, "accepted": True, "idempotent_replay": True,
                        "demand": demand_id, "plan": plan, "authorization": auth}
            raise GatewayError("conflicting_authorization", "已有未释放的冲突授权")

        d["begin_attempts"] = int(d.get("begin_attempts", 0)) + 1
        startability = self._startability(site_id, ss, d, at)
        if not startability["can_start"]:
            reason = (startability.get("reasons") or [{"code": "not_startable"}])[0]
            wait = {"at": at, **reason, "startability": startability}
            d["wait"] = wait
            d["lifecycle"] = "waiting"
            action = self._action(site_id, ss, at, "begin_deferred", demand=demand_id,
                                   plan=plan, controller=controller_id, reason=wait)
            return {"ok": True, "accepted": False, "status": "waiting",
                    "demand": demand_id, "plan": plan, "wait": wait, "action": action}

        grant = int(startability["would_grant_power"])
        auth = {
            "plan": plan,
            "controller": controller_id,
            "boot": boot,
            "branch": d["branch"],
            "authorized_power": grant,
            "notified_power": grant,
            "original_authorized_power": grant,
            "min_power": d["min_power"],
            "hold_power": grant,
            "confirmed_power": None,
            "effective_state": "authorized_waiting_feed",
            "start": at,
            "end": at + int(d["duration"]),
            "authorized_at": at,
            "released": False,
            "released_at": None,
            "release_reason": None,
            "pending_reduction": None,
        }
        d["authorization"] = auth
        d["lifecycle"] = "authorized"
        d["wait"] = None
        action = self._action(
            site_id, ss, at, "authorize", demand=demand_id, plan=plan,
            controller=controller_id, boot=boot, branch=d["branch"],
            power=grant, hold_power=grant, start=auth["start"], end=auth["end"],
        )
        return {"ok": True, "accepted": True, "demand": demand_id, "plan": plan,
                "authorized_power": grant, "hold_power": grant,
                "authorization": auth, "action": action}

    @_persist
    def start_ready(self, at: int, site_id: str) -> Dict[str, Any]:
        """按计划顺序授权所有当前可开始设备；其余保留等待原因。"""
        ss = self._site(site_id)
        started: List[Dict[str, Any]] = []
        waiting: List[Dict[str, Any]] = []
        for did in sorted(ss["demands"], key=lambda x: ss["demands"][x].get("plan", 0)):
            d = ss["demands"][did]
            auth = d.get("authorization")
            if auth and not auth.get("released"):
                continue
            status = self._startability(site_id, ss, d, at)
            if status["can_start"]:
                record = {"demand": did, "id": self._controller_for_branch(ss, d["branch"])["id"],
                          "boot": self._controller_for_branch(ss, d["branch"])["boot"],
                          "plan": d["plan"]}
                result = self.begin_demand(at, record)
                started.append(result)
            else:
                waiting.append({"demand": did, "plan": d["plan"], "startability": status})
        self.save()
        return {"started": started, "waiting": waiting}

    # ------------------------------------------------------------------
    # 输入：feed、meter、暂停
    # ------------------------------------------------------------------
    @_persist
    def report_feed(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        demand_id = self._require_str(record, "demand")
        controller_id = self._require_str(record, "id")
        boot = self._require_int(record, "boot", minimum=0)
        plan = self._require_int(record, "plan", minimum=1)
        state = self._require_str(record, "state")
        if state not in {"drawing", "reduced", "self_power", "unknown"}:
            raise GatewayError("bad_feed_state", "state 必须是 drawing/reduced/self_power/unknown")
        power = self._require_int(record, "power", required=False, minimum=0)
        if state in {"drawing", "reduced"} and power is None:
            raise GatewayError("missing_field", f"state={state} 的 feed 必须带整数 power")
        if state == "self_power" and power is None:
            # 状态本身可表达“已切到自带电源并结束外部取电”；按外部功率 0 处理。
            power = 0

        site_id, ss, d = self._find_demand(demand_id)
        controller = ss["controllers"].get(controller_id)
        feed = {
            "at": at,
            "demand": demand_id,
            "controller": controller_id,
            "boot": boot,
            "plan": plan,
            "state": state,
            "power": power,
        }
        d.setdefault("feeds", []).append(feed)
        d["last_feed"] = feed
        feed_payload = {key: value for key, value in feed.items() if key != "at"}

        if controller is None:
            raise GatewayError("unknown_controller", f"未登记控制器：{controller_id}")
        if d["branch"] not in controller.get("branches", []):
            result = {"ok": True, "applied": False, "code": "controller_branch_forbidden"}
            self._action(site_id, ss, at, "feed_rejected_branch", **feed_payload)
            return result
        if boot != controller.get("boot"):
            result = {"ok": True, "applied": False, "code": "stale_boot",
                      "current_boot": controller.get("boot"),
                      "note": "未应用该回报，也未清零旧占用"}
            self._action(site_id, ss, at, "feed_rejected_stale_boot", **feed_payload)
            return result
        auth = d.get("authorization")
        if auth is None or plan != d.get("plan") or plan != auth.get("plan"):
            result = {"ok": True, "applied": False, "code": "plan_mismatch_or_not_authorized",
                      "current_plan": d.get("plan")}
            self._action(site_id, ss, at, "feed_rejected_plan", **feed_payload)
            return result

        old_hold = int(auth.get("hold_power", 0))
        applied: Dict[str, Any] = {"old_hold_power": old_hold}

        if state == "unknown":
            auth["effective_state"] = "unknown"
            auth["confirmed_power"] = None
            applied.update(applied=True, hold_power=old_hold,
                           note="控制器状态未知，继续保守扣住原容量")
        elif state == "drawing":
            auth["confirmed_power"] = power
            auth["effective_state"] = "drawing"
            if power > old_hold:
                # 回报显示超过原扣住值（可能是越权/突变）；按已实现高值保守入账。
                auth["hold_power"] = power
            d["lifecycle"] = "drawing"
            applied.update(applied=True, hold_power=auth["hold_power"], confirmed_power=power)
        elif state == "reduced":
            auth["confirmed_power"] = power
            auth["hold_power"] = power
            auth["notified_power"] = power
            auth["authorized_power"] = power
            auth["effective_state"] = "reduced" if power > 0 else "pause_confirmed"
            if power == 0:
                d["lifecycle"] = "paused"
            else:
                d["lifecycle"] = "reduced"
            pending = auth.get("pending_reduction")
            if pending and power <= int(pending["target"]):
                auth["pending_reduction"] = None
            applied.update(applied=True, hold_power=power, confirmed_power=power,
                           released_external_budget=False)
            self._action(site_id, ss, at, "reduction_confirmed", demand=demand_id,
                         plan=plan, power=power, old_hold_power=old_hold)
        else:  # self_power
            auth["confirmed_power"] = power
            auth["effective_state"] = "self_power"
            if power == 0:
                auth["hold_power"] = 0
                auth["released"] = True
                auth["released_at"] = at
                auth["release_reason"] = "device_confirmed_end_of_external_draw"
                auth["pending_reduction"] = None
                d["lifecycle"] = "self_power"
                applied.update(applied=True, hold_power=0, budget_released=True)
                self._action(site_id, ss, at, "external_budget_released", demand=demand_id,
                             plan=plan, old_hold_power=old_hold,
                             reason=auth["release_reason"])
            else:
                # 报了 self_power 但仍有 external power；不能释放全部预算。
                auth["hold_power"] = power
                applied.update(applied=True, hold_power=power, budget_released=False,
                                note="self_power 回报仍带外部功率，未确认结束取电")

        self._reconcile_site(site_id, at)
        return {"ok": True, "demand": demand_id, **applied}

    @_persist
    def report_meter(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        site_id = self._require_str(record, "site")
        branch = self._require_str(record, "branch")
        observed_at = self._require_int(record, "observed_at", minimum=0)
        power = self._require_int(record, "power", minimum=0)
        ss = self._site(site_id, create=True)

        observation = {
            "seq": len(ss["observations"]) + 1,
            "site": site_id,
            "branch": branch,
            "observed_at": observed_at,
            "received_at": at,
            "power": power,
        }
        observation["analysis"] = self._analyze_meter(ss, observation)
        ss["observations"].append(observation)
        self._action(site_id, ss, at, "meter_received", branch=branch,
                     observed_at=observed_at, power=power,
                     previous_plan_draw=observation["analysis"].get("previous_plan_draw"),
                     unconfirmed_reduction=observation["analysis"].get("unconfirmed_reduction"))
        # 快照可能证明减量已发生；重新考虑原窗口中的等待需求。
        self._reconcile_site(site_id, at)
        return {"ok": True, "observation": observation,
                "analysis": observation["analysis"]}

    def _analyze_meter(self, ss: Dict[str, Any], obs: Dict[str, Any]) -> Dict[str, Any]:
        branch = obs["branch"]
        power = int(obs["power"])
        observed_at = int(obs["observed_at"])
        env = self._envelope_at(ss, observed_at)
        analysis: Dict[str, Any] = {
            "observed_at": observed_at,
            "envelope_revision": env["revision"] if env else None,
        }
        if env and branch in env.get("branches", {}):
            limit = int(env["branches"][branch])
            analysis["branch_limit_at_observed"] = limit
            analysis["over_branch_envelope"] = power > limit
        else:
            analysis["branch_limit_at_observed"] = None
            analysis["over_branch_envelope"] = None

        related: List[Dict[str, Any]] = []
        old_draw = False
        unconfirmed = False
        for did, d in ss["demands"].items():
            if d.get("branch") != branch:
                continue
            auth = d.get("authorization")
            if not auth or auth.get("released"):
                continue
            hold = int(auth.get("hold_power", 0))
            pending = auth.get("pending_reduction")
            target = int(pending["target"]) if pending else int(
                auth.get("confirmed_power") if auth.get("confirmed_power") is not None
                else auth.get("authorized_power", hold)
            )
            original = int(auth.get("original_authorized_power", auth.get("authorized_power", hold)))
            feed = d.get("last_feed")
            confirmed_by_feed = bool(
                feed and feed.get("state") in {"reduced", "self_power"}
                and type(feed.get("power")) is int and feed["power"] <= power
            )
            below_hold = power < hold
            is_unconfirmed = below_hold and not confirmed_by_feed
            is_old = power >= original and (
                pending is not None or analysis.get("over_branch_envelope") is True
            )
            old_draw = old_draw or is_old
            unconfirmed = unconfirmed or is_unconfirmed
            related.append({
                "demand": did,
                "plan": d.get("plan"),
                "hold_power": hold,
                "expected_target_power": target,
                "original_authorized_power": original,
                "observed_below_hold": below_hold,
                "unconfirmed_reduction": is_unconfirmed,
                "possible_previous_plan_draw": is_old,
            })
        analysis["demands"] = related
        analysis["previous_plan_draw"] = old_draw
        analysis["unconfirmed_reduction"] = unconfirmed
        analysis["do_not_sum_with_other_observed_at"] = True
        return analysis

    @_persist
    def pause_demand(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        demand_id = record.get("demand") or record.get("id")
        if not isinstance(demand_id, str) or not demand_id:
            raise GatewayError("missing_field", "pause 需要 demand")
        reason = record.get("reason", "regional_pause")
        site_id, ss, d = self._find_demand(demand_id)
        if not d.get("interruptible"):
            raise GatewayError("not_pausable",
                               f"demand {demand_id} 是不可中断承诺，不能正常暂停")
        d["regional_paused"] = True
        auth = d.get("authorization")
        if auth and not auth.get("released"):
            self._notify_reduction(site_id, ss, d, 0, at, str(reason))
            d["lifecycle"] = "pause_requested"
        else:
            d["lifecycle"] = "paused"
            d["wait"] = {"code": "regional_pause", "at": at, "reason": reason}
        action = self._action(site_id, ss, at, "regional_pause", demand=demand_id, reason=reason)
        self._reconcile_site(site_id, at)
        return {"ok": True, "demand": demand_id, "paused": True, "action": action}

    @_persist
    def resume_demand(self, at: int, record: Dict[str, Any]) -> Dict[str, Any]:
        demand_id = record.get("demand") or record.get("id")
        if not isinstance(demand_id, str):
            raise GatewayError("missing_field", "resume 需要 demand")
        site_id, ss, d = self._find_demand(demand_id)
        d["regional_paused"] = False
        auth = d.get("authorization")
        if auth and not auth.get("released") and int(auth.get("hold_power", 0)) == 0:
            # 恢复前必须按新包络重新检查；不自动恢复旧授权功率。
            check = self._startability(site_id, ss, d, at)
            if not check["can_start"]:
                d["lifecycle"] = "waiting"
                d["wait"] = {"code": "resume_blocked", "at": at, **check}
                self._reconcile_site(site_id, at)
                return {"ok": True, "resumed_request": True, "authorized": False,
                        "startability": check}
            grant = min(int(d["max_power"]), int(check["would_grant_power"]))
            auth["hold_power"] = grant
            auth["authorized_power"] = grant
            auth["pending_reduction"] = None
            d["lifecycle"] = "authorized"
            d["wait"] = None
            self._action(site_id, ss, at, "resume_authorized", demand=demand_id,
                         plan=d["plan"], power=grant)
        else:
            self._replan_demand(site_id, ss, d, at)
        self._action(site_id, ss, at, "regional_resume", demand=demand_id)
        return {"ok": True, "demand": demand_id, "resumed": True}

    def _notify_reduction(self, site_id: str, ss: Dict[str, Any], d: Dict[str, Any],
                          target: int, at: int, reason: str) -> Dict[str, Any]:
        auth = d.get("authorization")
        if not auth:
            raise GatewayError("not_authorized", "只有已授权需求需要降额通知")
        current_target = (auth.get("pending_reduction") or {}).get("target")
        if current_target is None:
            current_target = int(auth.get("hold_power", 0))
        target = max(0, int(target))
        auth["notified_power"] = target
        if auth.get("pending_reduction") and int(auth["pending_reduction"]["target"]) == target:
            return auth["pending_reduction"]
        if target >= int(current_target):
            pending = {"target": target, "requested_at": at, "reason": reason}
            auth["pending_reduction"] = pending
            return pending
        pending = {
            "target": target,
            "requested_at": at,
            "reason": reason,
            "previous_target": current_target,
            "hold_power_unchanged_until_confirmed": int(auth.get("hold_power", 0)),
        }
        auth["pending_reduction"] = pending
        self._action(
            site_id, ss, at, "notify_reduction",
            demand=d["id"], plan=d.get("plan"),
            controller=auth.get("controller"), boot=auth.get("boot"),
            target_power=target, notified_from_power=current_target,
            hold_power=int(auth.get("hold_power", 0)), reason=reason,
        )
        return pending

    # ------------------------------------------------------------------
    # 受限处置 / 事件
    # ------------------------------------------------------------------
    def _hold_load(self, ss: Dict[str, Any],
                   scope: Tuple[str, Optional[str], Optional[str]]) -> Tuple[int, List[Dict[str, Any]]]:
        total = 0
        items: List[Dict[str, Any]] = []
        for did, d in sorted(ss["demands"].items(), key=lambda kv: kv[1].get("plan", 0)):
            auth = d.get("authorization")
            if not auth or auth.get("released"):
                continue
            hold = max(0, int(auth.get("hold_power", 0)))
            if hold <= 0 or not self._in_scope(d, scope):
                continue
            total += hold
            pending = auth.get("pending_reduction")
            projected = int(pending["target"]) if pending else hold
            items.append({
                "demand": did,
                "plan": d.get("plan"),
                "branch": d.get("branch"),
                "hold_power": hold,
                "projected_power": projected,
                "min_power": d.get("min_power"),
                "interruptible": bool(d.get("interruptible")),
                "pending_reduction": pending,
            })
        return total, items

    def _balance_scope(self, site_id: str, ss: Dict[str, Any], at: int,
                       scope: Tuple[str, Optional[str], Optional[str]],
                       limit: int) -> Tuple[bool, List[str]]:
        notified: List[str] = []
        scope_id = self._scope_for(site_id, scope[0], scope[2])
        for _iteration in range(max(1, 2 * len(ss["demands"]) + 5)):
            projected_total = 0
            candidates: List[Dict[str, Any]] = []
            for did, d in ss["demands"].items():
                auth = d.get("authorization")
                if not auth or auth.get("released") or not self._in_scope(d, scope):
                    continue
                pending = auth.get("pending_reduction")
                power = int(pending["target"]) if pending else int(auth.get("hold_power", 0))
                projected_total += power
                if d.get("interruptible") and power > 0:
                    candidates.append(d)
            if projected_total <= limit:
                return True, notified
            if not candidates:
                return False, notified
            candidates.sort(key=lambda x: (
                -int(((x.get("authorization") or {}).get("pending_reduction") or {})
                     .get("target", (x.get("authorization") or {}).get("hold_power", 0))),
                int(x.get("plan", 0)),
                x["id"],
            ))
            d = candidates[0]
            auth = d["authorization"]
            pending = auth.get("pending_reduction")
            current = int(pending["target"]) if pending else int(auth.get("hold_power", 0))
            excess = projected_total - limit
            if current > int(d["min_power"]):
                target = max(int(d["min_power"]), current - excess)
            else:
                target = 0
            if target >= current:
                target = 0
            if target == current or target < 0:
                return False, notified
            self._notify_reduction(site_id, ss, d, target, at,
                                   f"emergency_balance:{scope_id}")
            notified.append(d["id"])
        return False, notified

    def _upsert_incident(self, ss: Dict[str, Any], at: int, kind: str, scope_id: str,
                         **fields: Any) -> Dict[str, Any]:
        for inc in reversed(ss["incidents"]):
            if inc.get("status") == "active" and inc.get("kind") == kind and inc.get("scope") == scope_id:
                inc["last_at"] = at
                inc.update(fields)
                return inc
        incident = {
            "id": f"I{len(ss['incidents']) + 1}",
            "kind": kind,
            "scope": scope_id,
            "site": scope_id.split(":", 2)[1] if scope_id.startswith(("site:", "branch:")) else None,
            "started_at": at,
            "last_at": at,
            "status": "active",
            **fields,
        }
        ss["incidents"].append(incident)
        return incident

    def _reconcile_site(self, site_id: str, at: Optional[int]) -> None:
        ss = self._site(site_id)
        current_at = int(at if at is not None else self.state.get("last_at", 0))

        env = self._envelope_at(ss, current_at)
        if env is None:
            self._upsert_incident(
                ss, current_at, "no_active_envelope", self._scope_for(site_id, "site"),
                message="当前秒点没有有效供电包络，禁止新授权",
            )
        else:
            for inc in ss["incidents"]:
                if inc.get("kind") == "no_active_envelope" and inc.get("status") == "active":
                    inc["status"] = "resolved"
                    inc["resolved_at"] = current_at

            site_scope = ("site", site_id, None)
            held, items = self._hold_load(ss, site_scope)
            sufficient, notified = self._balance_scope(
                site_id, ss, current_at, site_scope, int(env["total"])
            )
            projected = sum(i["projected_power"] for i in items)
            # balance 后重新计算 projected
            projected = 0
            for did, d in ss["demands"].items():
                auth = d.get("authorization")
                if auth and not auth.get("released"):
                    pending = auth.get("pending_reduction")
                    projected += int(pending["target"]) if pending else int(auth["hold_power"])
            if held > int(env["total"]):
                inc = self._upsert_incident(
                    ss, current_at, "envelope_violation", self._scope_for(site_id, "site"),
                    envelope_revision=env["revision"], limit=env["total"], held_power=held,
                    excess=held - int(env["total"]), projected_power=projected,
                    mitigation_sufficient=projected <= int(env["total"]),
                    unmitigable=projected > int(env["total"]),
                    affected_demands=[i["demand"] for i in items],
                    interruptible_actions=notified,
                    message="站点总上限受限：已保留未确认占用并安排可暂停设备降载",
                )
                self._action(site_id, ss, current_at, "restricted_handling",
                             incident=inc["id"], scope=inc["scope"],
                             affected_demands=inc["affected_demands"],
                             unmitigable=inc["unmitigable"])
            else:
                for inc in ss["incidents"]:
                    if (inc.get("kind") == "envelope_violation"
                            and inc.get("scope") == self._scope_for(site_id, "site")
                            and inc.get("status") == "active"):
                        inc["status"] = "resolved"
                        inc["resolved_at"] = current_at

            for branch, limit in env["branches"].items():
                scope = ("branch", site_id, branch)
                held, items = self._hold_load(ss, scope)
                sufficient, notified = self._balance_scope(
                    site_id, ss, current_at, scope, int(limit)
                )
                projected = 0
                for item in items:
                    d = ss["demands"][item["demand"]]
                    auth = d["authorization"]
                    pending = auth.get("pending_reduction")
                    projected += int(pending["target"]) if pending else int(auth["hold_power"])
                scope_id = self._scope_for(site_id, "branch", branch)
                if held > int(limit):
                    inc = self._upsert_incident(
                        ss, current_at, "envelope_violation", scope_id,
                        envelope_revision=env["revision"], limit=limit, held_power=held,
                        excess=held - int(limit), projected_power=projected,
                        mitigation_sufficient=projected <= int(limit),
                        unmitigable=projected > int(limit),
                        affected_demands=[i["demand"] for i in items],
                        interruptible_actions=notified,
                        message="支路上限受限：只阻断共享该支路约束的新增取电",
                    )
                    self._action(site_id, ss, current_at, "restricted_handling",
                                 incident=inc["id"], scope=scope_id,
                                 affected_demands=inc["affected_demands"],
                                 unmitigable=inc["unmitigable"])
                else:
                    for inc in ss["incidents"]:
                        if (inc.get("kind") == "envelope_violation"
                                and inc.get("scope") == scope_id
                                and inc.get("status") == "active"):
                            inc["status"] = "resolved"
                            inc["resolved_at"] = current_at

        # 重新考虑原等待窗口中的安排，并维护承诺无法兑现事件。
        failures: Dict[str, List[Dict[str, Any]]] = {}
        for did in sorted(ss["demands"], key=lambda x: ss["demands"][x].get("plan", 0)):
            d = ss["demands"][did]
            auth = d.get("authorization")
            if auth and not auth.get("released"):
                continue
            self._replan_demand(site_id, ss, d, current_at)
            wait = d.get("wait") or {}
            if d.get("committed") and d.get("planned_start") is None:
                scope_id = (wait.get("scope")
                            or self._scope_for(site_id, "site"))
                failures.setdefault(scope_id, []).append({
                    "demand": did,
                    "plan": d.get("plan"),
                    "branch": d.get("branch"),
                    "reason": wait.get("code"),
                    "details": wait,
                })

        active_shortfalls = [
            inc for inc in ss["incidents"]
            if inc.get("kind") == "commitment_shortfall" and inc.get("status") == "active"
        ]
        for inc in active_shortfalls:
            if inc["scope"] in failures:
                rows = failures.pop(inc["scope"])
                inc["last_at"] = current_at
                inc["commitments"] = rows
                inc["affected_demands"] = [r["demand"] for r in rows]
            else:
                inc["status"] = "resolved"
                inc["resolved_at"] = current_at
        for scope_id, rows in failures.items():
            inc = self._upsert_incident(
                ss, current_at, "commitment_shortfall", scope_id,
                commitments=rows, affected_demands=[r["demand"] for r in rows],
                message="新包络/未确认释放使已接受的连续承诺暂时无法安排",
            )
            self._action(site_id, ss, current_at, "restricted_handling",
                         incident=inc["id"], scope=scope_id,
                         affected_demands=inc["affected_demands"])

    # ------------------------------------------------------------------
    # 计量合成与 inspect
    # ------------------------------------------------------------------
    def _power_rollup(self, ss: Dict[str, Any], at: int) -> Dict[str, Dict[str, Any]]:
        """分别汇总拟定、已通知/扣住和已确认实际功率。"""
        rollup: Dict[str, Dict[str, Any]] = {}

        def row(branch: str) -> Dict[str, Any]:
            return rollup.setdefault(branch, {
                "planned_power": 0,
                "notified_power": 0,
                "held_power": 0,
                "actual_confirmed_power": 0,
                "actual_unknown_demands": [],
            })

        for d in ss["demands"].values():
            branch = d.get("branch")
            if not branch:
                continue
            item = row(branch)
            auth = d.get("authorization")
            if auth and not auth.get("released"):
                item["notified_power"] += int(auth.get(
                    "notified_power", auth.get("authorized_power", auth.get("hold_power", 0))
                ))
                item["held_power"] += int(auth.get("hold_power", 0))
                confirmed = auth.get("confirmed_power")
                if type(confirmed) is int:
                    item["actual_confirmed_power"] += max(0, confirmed)
                else:
                    item["actual_unknown_demands"].append(d["id"])
            elif self._demand_load_at(d, at) > 0:
                # 未授权且拟定区间覆盖查看秒点。
                item["planned_power"] += int(d.get("min_power", 0))
        return rollup

    def _measured_status(self, ss: Dict[str, Any]) -> Dict[str, Any]:
        latest_by_branch: Dict[str, Dict[str, Any]] = {}
        at_time: Dict[int, Dict[str, Dict[str, Any]]] = {}
        known_branches = set()
        for env in ss["envelopes"]:
            known_branches.update(env["branches"].keys())
        for c in ss["controllers"].values():
            known_branches.update(c.get("branches", []))

        for obs in ss["observations"]:
            branch = obs["branch"]
            known_branches.add(branch)
            old = latest_by_branch.get(branch)
            key = (int(obs["observed_at"]), int(obs["received_at"]), int(obs.get("seq", 0)))
            if old is None or key > (
                int(old["observed_at"]), int(old["received_at"]), int(old.get("seq", 0))
            ):
                latest_by_branch[branch] = obs
            at_time.setdefault(int(obs["observed_at"]), {})
            prior = at_time[int(obs["observed_at"])].get(branch)
            if prior is None or int(obs["received_at"]) >= int(prior["received_at"]):
                at_time[int(obs["observed_at"])][branch] = obs

        synchronized = None
        for observed_at in sorted(at_time, reverse=True):
            rows = at_time[observed_at]
            env = self._envelope_at(ss, observed_at)
            required = set(env["branches"].keys()) if env else set(known_branches)
            if required and set(rows.keys()) == required:
                synchronized = {
                    "observed_at": observed_at,
                    "power": sum(int(o["power"]) for o in rows.values()),
                    "branches": {b: int(o["power"]) for b, o in sorted(rows.items())},
                    "received_at": max(int(o["received_at"]) for o in rows.values()),
                }
                break

        branch_status = {}
        for branch in sorted(known_branches):
            obs = latest_by_branch.get(branch)
            branch_status[branch] = None if obs is None else {
                "power": obs["power"],
                "observed_at": obs["observed_at"],
                "received_at": obs["received_at"],
                "analysis": obs.get("analysis"),
            }

        if synchronized is not None:
            site_power: Optional[int] = synchronized["power"]
            site_at: Optional[int] = synchronized["observed_at"]
            warning = None
        else:
            site_power = None
            site_at = None
            times = {b: o["observed_at"] for b, o in latest_by_branch.items()}
            if len(set(times.values())) > 1:
                warning = "各支路计量秒点不同，不能拼成同一瞬时站点总量"
            elif not times:
                warning = "尚无计量快照"
            else:
                warning = "缺少同一 observed_at 的全部支路快照"

        return {
            "site_power": site_power,
            "site_observed_at": site_at,
            "synchronized_sample": synchronized,
            "warning": warning,
            "branches": branch_status,
        }

    def demand_view(self, site_id: str, ss: Dict[str, Any], d: Dict[str, Any],
                    at: Optional[int] = None) -> Dict[str, Any]:
        view_at = int(at if at is not None else self.state.get("last_at", 0))
        startability = self._startability(site_id, ss, d, view_at)
        meter = None
        branch_obs = [o for o in ss["observations"] if o["branch"] == d["branch"]]
        if branch_obs:
            meter = sorted(branch_obs, key=lambda o: (o["observed_at"], o["received_at"]))[-1]
        incidents = [
            inc for inc in ss["incidents"]
            if d["id"] in (inc.get("affected_demands") or [])
            or any(r.get("demand") == d["id"] for r in inc.get("commitments", []))
        ]
        return {
            "ok": True,
            "site": site_id,
            "demand": d["id"],
            "device": d["device"],
            "branch": d["branch"],
            "plan": d["plan"],
            "window": {"earliest": d["earliest"], "latest": d["latest"]},
            "duration": d["duration"],
            "min_power": d["min_power"],
            "max_power": d["max_power"],
            "interruptible": d["interruptible"],
            "lifecycle": d["lifecycle"],
            "accepted": bool(d["committed"]),
            "committed": d["committed"],
            "regional_paused": d["regional_paused"],
            "arrangement": d["arrangement"],
            "planned_start": d["planned_start"],
            "planned_end": d["planned_end"],
            "wait": d["wait"],
            "startability": startability,
            "can_start": startability["can_start"],
            "authorization": d["authorization"],
            "last_feed": d.get("last_feed"),
            "latest_branch_meter": None if meter is None else {
                "power": meter["power"],
                "observed_at": meter["observed_at"],
                "received_at": meter["received_at"],
                "analysis": meter.get("analysis"),
            },
            "incidents": incidents,
            "feeds": d.get("feeds", []),
        }

    def inspect(self, at: Optional[int] = None, site: Optional[str] = None,
                demand: Optional[str] = None) -> Dict[str, Any]:
        view_at = int(at if at is not None else self.state.get("last_at", 0))
        if demand is not None:
            site_id, ss, d = self._find_demand(demand)
            return self.demand_view(site_id, ss, d, at=view_at)

        if site is None:
            return {
                "ok": True,
                "at": view_at,
                "sites": {sid: self.inspect(at=view_at, site=sid) for sid in self.state["sites"]},
            }

        ss = self._site(site)
        env = self._envelope_at(ss, view_at)
        measured = self._measured_status(ss)

        rollup = self._power_rollup(ss, view_at)
        site_held, hold_items_all = self._hold_load(ss, ("site", site, None))
        branch_capacity: Dict[str, Any] = {}
        all_branches = set(rollup.keys())
        if env:
            all_branches.update(env["branches"].keys())
        all_branches.update(measured["branches"].keys())
        for controller in ss["controllers"].values():
            all_branches.update(controller.get("branches", []))

        for branch in sorted(all_branches):
            held, _items = self._hold_load(ss, ("branch", site, branch))
            numbers = rollup.get(branch, {
                "planned_power": 0,
                "notified_power": 0,
                "held_power": held,
                "actual_confirmed_power": 0,
                "actual_unknown_demands": [],
            })
            limit = int(env["branches"][branch]) if env and branch in env.get("branches", {}) else None
            branch_capacity[branch] = {
                "limit": limit,
                "planned_power": numbers["planned_power"],
                "notified_power": numbers["notified_power"],
                "held_power": held,
                "actual_confirmed_power": numbers["actual_confirmed_power"],
                "actual_unknown_demands": numbers["actual_unknown_demands"],
                "headroom": None if limit is None else limit - held,
                "measured": measured["branches"].get(branch),
                "controllers": [c["id"] for c in ss["controllers"].values()
                                if branch in c.get("branches", [])],
            }

        commitments = []
        waiting = []
        startable = []
        for did in sorted(ss["demands"], key=lambda x: ss["demands"][x].get("plan", 0)):
            d = ss["demands"][did]
            check = self._startability(site, ss, d, view_at)
            auth = d.get("authorization")
            outstanding = not (auth and auth.get("released")) and d.get("lifecycle") != "self_power"
            if d.get("committed") or auth:
                commitments.append({
                    "demand": did,
                    "device": d["device"],
                    "plan": d["plan"],
                    "branch": d["branch"],
                    "min_power": d["min_power"],
                    "max_power": d["max_power"],
                    "interruptible": d["interruptible"],
                    "guaranteed_uninterruptible": not d["interruptible"],
                    "window": {"earliest": d["earliest"], "latest": d["latest"]},
                    "planned_interval": None if d["planned_start"] is None else
                        [d["planned_start"], d["planned_end"]],
                    "authorized_interval": None if not auth else [auth["start"], auth["end"]],
                    "lifecycle": d["lifecycle"],
                    "outstanding": outstanding,
                    "hold_power": None if not auth else auth.get("hold_power"),
                    "wait": d.get("wait"),
                })
            if (not auth or auth.get("released")) and d.get("lifecycle") != "self_power":
                if check["can_start"]:
                    startable.append({
                        "demand": did,
                        "device": d["device"],
                        "branch": d["branch"],
                        "plan": d["plan"],
                        "would_grant_power": check["would_grant_power"],
                        "controller": check["controller"],
                        "boot": check["current_boot"],
                    })
                else:
                    reason = check["reasons"][0] if check["reasons"] else d.get("wait")
                    waiting.append({
                        "demand": did,
                        "device": d["device"],
                        "branch": d["branch"],
                        "plan": d["plan"],
                        "lifecycle": d["lifecycle"],
                        "wait": d.get("wait") or reason,
                        "startability": check,
                    })

        active_incidents = [i for i in ss["incidents"] if i.get("status") == "active"]
        site_planned = sum(row["planned_power"] for row in rollup.values())
        site_notified = sum(row["notified_power"] for row in rollup.values())
        site_actual = sum(row["actual_confirmed_power"] for row in rollup.values())
        site_unknown = [d for row in rollup.values() for d in row["actual_unknown_demands"]]
        return {
            "ok": True,
            "site": site,
            "at": view_at,
            "envelope": None if env is None else {
                "revision": env["revision"],
                "from": env["from"],
                "until": env["until"],
                "total": env["total"],
                "branches": env["branches"],
            },
            "controllers": list(ss["controllers"].values()),
            "branches": branch_capacity,
            "site_capacity": {
                "total_limit": None if env is None else env["total"],
                "planned_power": site_planned,
                "notified_power": site_notified,
                "held_power": site_held,
                "actual_confirmed_power": site_actual,
                "actual_unknown_demands": site_unknown,
                "headroom": None if env is None else env["total"] - site_held,
                "measured_power": measured["site_power"],
                "measured_at": measured["site_observed_at"],
                "measurement_warning": measured["warning"],
                "synchronized_sample": measured["synchronized_sample"],
            },
            "measured": measured,
            "held_capacity": {
                "site": site_held,
                "demands": [
                    {"demand": item["demand"], "plan": item["plan"], "branch": item["branch"],
                     "hold_power": item["hold_power"], "projected_power": item["projected_power"],
                     "interruptible": item["interruptible"]}
                    for item in hold_items_all
                ],
            },
            "commitments": commitments,
            "waiting": waiting,
            "startable": startable,
            "restrictions": active_incidents,
            "incidents": ss["incidents"],
            "restart_count": self.state.get("restart_count", 0),
            "recent_actions": ss["actions"][-20:],
            "action_count": len(ss["actions"]),
        }
