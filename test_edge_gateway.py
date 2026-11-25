"""边缘网关核心行为测试；全部使用临时 JSON 状态和合成设备。"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from edge_gateway import Gateway, GatewayError
from edge_gateway.cli import main as cli_main
from edge_gateway.scenario import run_file

ROOT = Path(__file__).parent
EXAMPLES = ROOT / "examples"


def envelope(at=0, site="yard", revision=1, total=20, branches=None, until=30):
    return {
        "site": site,
        "revision": revision,
        "from": 0,
        "until": until,
        "total": total,
        "branches": branches or {"east": 10, "west": 10},
    }


def controller(cid="c-e", site="yard", boot=1, branches=None):
    return {"id": cid, "site": site, "boot": boot, "branches": branches or ["east"]}


def demand(did="d1", **overrides):
    record = {
        "demand": did,
        "site": "yard",
        "branch": "east",
        "device": f"device-{did}",
        "earliest": 0,
        "latest": 5,
        "duration": 5,
        "min": 3,
        "max": 3,
        "interruptible": True,
    }
    record.update(overrides)
    return record


class GatewayBehaviorTest(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "gateway-state.json"
        self.gw = Gateway(self.state_path)

    def seed_yard(self, total=20, east=10, west=10):
        self.gw.handle(0, "grid", envelope(total=total, branches={"east": east, "west": west}))
        self.gw.handle(0, "controller", controller("ce", branches=["east"]))
        self.gw.handle(0, "controller", controller("cw", branches=["west"]))

    def test_example_reduction_fixture_releases_only_after_device_confirmation(self):
        summary = run_file(
            EXAMPLES / "reduction-is-not-yet-capacity.jsonl",
            self.state_path,
            keep_state=True,
        )
        gw = summary["gateway"]

        site10 = next(
            r for r in summary["responses"]
            if r["at"] == 10 and r["kind"] == "inspect"
        )["result"]
        self.assertEqual(11, site10["site_capacity"]["held_power"])
        self.assertIsNone(site10["site_capacity"]["measured_power"])
        self.assertIn("秒点不同", site10["site_capacity"]["measurement_warning"])
        self.assertEqual([], site10["startable"])
        self.assertEqual("next", site10["waiting"][0]["demand"])

        recovery9 = next(r for r in summary["responses"] if r["kind"] == "restart")["recovery"]
        self.assertEqual([6, 5], [x["hold_power"] for x in recovery9["active_decisions"]])

        # 重启次数和决定已写入文件；最终状态包含后续 feed，因此历史秒点的
        # 现场快照应以当时 inspect 响应为准，而不是用未来回报回溯改写。
        reloaded = Gateway(self.state_path)
        self.assertEqual(1, reloaded.state["restart_count"])

        site12 = gw.inspect(at=12, site="mine")
        self.assertEqual(6, site12["site_capacity"]["measured_power"])
        self.assertEqual(11, site12["site_capacity"]["measured_at"])

        next_view = gw.inspect(at=13, demand="next")
        self.assertTrue(next_view["can_start"])
        self.assertEqual(2, next_view["startability"]["would_grant_power"])

        actions = [a["type"] for a in gw.state["sites"]["mine"]["actions"]]
        self.assertEqual(2, actions.count("authorize"))
        self.assertEqual(1, actions.count("gateway_recovered"))

    def test_independent_branch_fixture_blocks_only_shared_site_path(self):
        summary = run_file(
            EXAMPLES / "independent-branch-and-new-envelope.jsonl",
            self.state_path,
            keep_state=True,
        )
        gw = summary["gateway"]

        b6 = next(
            r for r in summary["responses"]
            if r["at"] == 6 and r["kind"] == "inspect"
        )["result"]
        self.assertTrue(b6["can_start"])

        begin8 = next(
            r for r in summary["responses"]
            if r["at"] == 8 and r["kind"] == "begin"
        )["result"]
        self.assertFalse(begin8["accepted"])
        self.assertEqual("waiting", begin8["status"])

        site = gw.inspect(at=9, site="yard")
        self.assertEqual(6, site["branches"]["east"]["held_power"])
        self.assertEqual(-3, site["branches"]["east"]["headroom"])
        self.assertEqual(0, site["branches"]["west"]["held_power"])
        self.assertEqual(7, site["branches"]["west"]["headroom"])
        self.assertEqual(["b"], [w["demand"] for w in site["waiting"]])
        self.assertEqual([], site["startable"])
        scopes = {(i["kind"], i["scope"]): i for i in site["restrictions"]}
        self.assertIn(("envelope_violation", "branch:yard:east"), scopes)
        self.assertTrue(scopes[("envelope_violation", "branch:yard:east")]["unmitigable"])
        self.assertIn(("commitment_shortfall", "site:yard"), scopes)

    def test_cannot_promise_two_overlapping_minimum_loads(self):
        self.seed_yard(total=10, east=10)
        first = self.gw.handle(0, "request", demand("first", min=6, max=6, latest=2, duration=10))
        self.assertTrue(first["arrangement"]["feasible"])

        second = self.gw.handle(
            0, "request", demand("second", min=5, max=5, latest=2, duration=10)
        )
        self.assertFalse(second["arrangement"]["feasible"])
        self.assertFalse(second["committed"])
        self.assertEqual("insufficient_minimum_capacity", second["wait"]["code"])

        # 放宽站点和支路总量后，11 个最低单位可以同时安排。
        self.gw.handle(1, "grid", envelope(revision=2, total=11, branches={"east": 11, "west": 10}))
        second = self.gw.inspect(demand="second", at=1)
        self.assertTrue(second["arrangement"]["feasible"])
        self.assertTrue(second["committed"])

    def test_only_some_devices_start_and_later_release_admits_next(self):
        self.seed_yard(total=8, east=8)
        for name in ("a", "b", "c"):
            self.gw.handle(0, "request", demand(name))
            result = self.gw.handle(0, "begin", {
                "demand": name, "id": "ce", "boot": 1, "plan": {"a": 1, "b": 2, "c": 3}[name]
            })
            if name == "c":
                self.assertFalse(result["accepted"])
            else:
                self.assertTrue(result["accepted"])

        site = self.gw.inspect(at=0, site="yard")
        self.assertEqual(["c"], [w["demand"] for w in site["waiting"]])
        self.assertEqual(6, site["held_capacity"]["site"])

        self.gw.handle(1, "feed", {
            "demand": "a", "id": "ce", "boot": 1, "plan": 1,
            "state": "self_power", "power": 0,
        })
        started = self.gw.handle(2, "begin", {
            "demand": "c", "id": "ce", "boot": 1, "plan": 3
        })
        self.assertTrue(started["accepted"])
        self.assertEqual(3, started["authorized_power"])

    def test_notified_reduction_keeps_old_hold_until_feed_confirmation(self):
        self.seed_yard(total=10, east=10)
        self.gw.handle(0, "request", demand("pump", min=2, max=6, interruptible=True))
        begun = self.gw.handle(0, "begin", {"demand": "pump", "id": "ce", "boot": 1, "plan": 1})
        self.assertEqual(6, begun["hold_power"])

        self.gw.handle(1, "grid", envelope(
            revision=2, total=4, branches={"east": 10, "west": 10}
        ))
        d = self.gw.state["sites"]["yard"]["demands"]["pump"]
        self.assertEqual(6, d["authorization"]["hold_power"])
        self.assertEqual(4, d["authorization"]["pending_reduction"]["target"])

        blocked = self.gw.handle(1, "request", demand("tiny", min=1, max=1, latest=2))
        self.assertFalse(blocked["can_start"])

        feed = self.gw.handle(2, "feed", {
            "demand": "pump", "id": "ce", "boot": 1, "plan": 1,
            "state": "reduced", "power": 4,
        })
        self.assertEqual(4, feed["hold_power"])
        tiny_begin = self.gw.handle(2, "begin", {
            "demand": "tiny", "id": "ce", "boot": 1, "plan": 2
        })
        # 总量仍为 4，pump 已占 4；tiny 依旧不能借不存在的余量启动。
        self.assertFalse(tiny_begin["accepted"])

    def test_meter_snapshots_require_same_observed_at_for_site_total(self):
        self.seed_yard(total=20)
        self.gw.handle(7, "meter", {
            "site": "yard", "branch": "east", "observed_at": 7, "power": 6
        })
        self.gw.handle(8, "meter", {
            "site": "yard", "branch": "west", "observed_at": 6, "power": 3
        })
        view = self.gw.inspect(at=8, site="yard")
        self.assertIsNone(view["measured"]["site_power"])
        self.assertEqual(6, view["measured"]["branches"]["east"]["power"])
        self.assertEqual(3, view["measured"]["branches"]["west"]["power"])

        self.gw.handle(12, "meter", {
            "site": "yard", "branch": "east", "observed_at": 11, "power": 6
        })
        self.gw.handle(12, "meter", {
            "site": "yard", "branch": "west", "observed_at": 11, "power": 0
        })
        view = self.gw.inspect(at=12, site="yard")
        self.assertEqual(6, view["measured"]["site_power"])
        self.assertEqual(11, view["measured"]["site_observed_at"])

    def test_restart_reloads_and_duplicate_begin_is_idempotent(self):
        self.seed_yard()
        self.gw.handle(0, "request", demand("d1"))
        begin_record = {"demand": "d1", "id": "ce", "boot": 1, "plan": 1}
        first = self.gw.handle(0, "begin", begin_record)
        self.assertTrue(first["accepted"])
        self.gw.prepare_restart(4)

        recovered = Gateway(self.state_path)
        recovery = recovered.record_recovery(4)
        self.assertEqual(1, recovery["restart_count"])
        self.assertEqual(3, recovery["active_decisions"][0]["hold_power"])

        duplicate = recovered.handle(4, "begin", begin_record)
        self.assertTrue(duplicate["idempotent_replay"])
        actions = recovered.state["sites"]["yard"]["actions"]
        self.assertEqual(1, sum(a["type"] == "authorize" for a in actions))

        reloaded = Gateway(self.state_path)
        self.assertEqual(1, reloaded.state["restart_count"])

    def test_controller_reboot_does_not_clear_hold_and_old_boot_is_rejected(self):
        self.seed_yard()
        self.gw.handle(0, "request", demand("d1"))
        self.gw.handle(0, "begin", {
            "demand": "d1", "id": "ce", "boot": 1, "plan": 1
        })
        self.gw.handle(1, "controller", controller("ce", boot=2))
        d = self.gw.state["sites"]["yard"]["demands"]["d1"]
        self.assertEqual(3, d["authorization"]["hold_power"])

        old_feed = self.gw.handle(2, "feed", {
            "demand": "d1", "id": "ce", "boot": 1, "plan": 1,
            "state": "self_power", "power": 0,
        })
        self.assertFalse(old_feed["applied"])
        self.assertEqual("stale_boot", old_feed["code"])
        self.assertEqual(3, d["authorization"]["hold_power"])

        new_feed = self.gw.handle(3, "feed", {
            "demand": "d1", "id": "ce", "boot": 2, "plan": 1,
            "state": "self_power", "power": 0,
        })
        self.assertTrue(new_feed["budget_released"])
        self.assertEqual(0, d["authorization"]["hold_power"])

    def test_pause_is_only_allowed_for_interruptible_demand(self):
        self.seed_yard()
        self.gw.handle(0, "request", demand("fixed", min=6, max=6, interruptible=False))
        with self.assertRaises(GatewayError) as ctx:
            self.gw.handle(1, "pause", {"demand": "fixed"})
        self.assertEqual("not_pausable", ctx.exception.code)

        self.gw.handle(0, "request", demand("flex", interruptible=True))
        self.gw.handle(0, "begin", {"demand": "flex", "id": "ce", "boot": 1, "plan": 2})
        paused = self.gw.handle(1, "pause", {"demand": "flex", "reason": "crew_request"})
        self.assertTrue(paused["paused"])
        d = self.gw.state["sites"]["yard"]["demands"]["flex"]
        self.assertEqual(3, d["authorization"]["hold_power"])
        self.assertEqual(0, d["authorization"]["pending_reduction"]["target"])

        self.gw.handle(2, "feed", {
            "demand": "flex", "id": "ce", "boot": 1, "plan": 2,
            "state": "self_power", "power": 0,
        })
        self.assertTrue(d["authorization"]["released"])

    def test_unrelated_site_is_independent(self):
        self.seed_yard(total=3, east=3)
        self.gw.handle(0, "request", demand("yard-load", min=3, max=3))
        self.gw.handle(0, "begin", {"demand": "yard-load", "id": "ce", "boot": 1, "plan": 1})

        self.gw.handle(0, "grid", envelope(
            site="other", revision=1, total=4, branches={"east": 4}
        ))
        self.gw.handle(0, "controller", controller(
            "co", site="other", branches=["east"]
        ))
        other = demand("other-load", site="other", min=4, max=4)
        self.gw.handle(0, "request", other)
        begun = self.gw.handle(0, "begin", {
            "demand": "other-load", "id": "co", "boot": 1, "plan": 1
        })
        self.assertTrue(begun["accepted"])

    def test_cli_project_entry_updates_and_inspects(self):
        state = self.state_path
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli_main([
                "--state", str(state), "--at", "0", "grid",
                "--site", "cli", "--revision", "1", "--from", "0", "--until", "20",
                "--total", "5", "--branch", "east=5",
            ]))
            self.assertEqual(0, cli_main([
                "--state", str(state), "--at", "0", "controller",
                "--id", "c", "--site", "cli", "--boot", "1", "--branches", "east",
            ]))
            self.assertEqual(0, cli_main([
                "--state", str(state), "--at", "0", "request",
                "--demand", "d", "--site", "cli", "--branch", "east", "--device", "dev",
                "--earliest", "0", "--latest", "2", "--duration", "4",
                "--min", "2", "--max", "4", "--interruptible",
            ]))
            self.assertEqual(0, cli_main([
                "--state", str(state), "--at", "0", "begin",
                "--demand", "d", "--controller", "c", "--boot", "1", "--plan", "1",
            ]))
        view = Gateway(state).inspect(at=0, site="cli")
        self.assertEqual(4, view["held_capacity"]["site"])
        self.assertEqual("authorized", view["commitments"][0]["lifecycle"])


if __name__ == "__main__":
    unittest.main()
