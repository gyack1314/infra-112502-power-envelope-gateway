"""把 inspect JSON 快照渲染成调度台可读的本地文本。"""

from __future__ import annotations

from typing import Any, Dict, List


def _wait_code(item: Dict[str, Any]) -> str:
    wait = item.get("wait")
    if isinstance(wait, dict):
        return str(wait.get("code") or wait.get("reason") or "")
    return "" if wait is None else str(wait)


def render_demand(view: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(
        f"需求 {view['demand']}（设备 {view['device']}，支路 {view['branch']}，"
        f"计划 #{view['plan']}）"
    )
    lines.append(
        f"  状态={view['lifecycle']}  当前可开始={'是' if view['can_start'] else '否'}"
    )
    lines.append(
        f"  窗口=[{view['window']['earliest']}, {view['window']['latest']}] "
        f"时长={view['duration']}s 功率={view['min_power']}..{view['max_power']} "
        f"可中断={'是' if view['interruptible'] else '否（保证不可中断）'}"
    )
    if view.get("planned_start") is not None:
        lines.append(f"  拟定安排=[{view['planned_start']}, {view['planned_end']})")
    auth = view.get("authorization")
    if auth:
        lines.append(
            "  授权="
            f"#{auth['plan']}/{auth['controller']}/boot{auth['boot']} "
            f"通知功率={auth.get('notified_power', auth['authorized_power'])} "
            f"扣住={auth['hold_power']} 已确认={auth.get('confirmed_power')} "
            f"释放={auth.get('released')}"
        )
    meter = view.get("latest_branch_meter")
    if meter:
        lines.append(
            f"  最新支路计量={meter['power']} @observed_at={meter['observed_at']}"
        )
    if not view["can_start"]:
        reasons = view["startability"].get("reasons") or []
        text = "；".join(r.get("code", "?") for r in reasons) or _wait_code(view)
        lines.append(f"  等待原因={text}")
    return "\n".join(lines)


def render_site(view: Dict[str, Any]) -> str:
    lines: List[str] = []
    env = view.get("envelope")
    if env:
        lines.append(
            f"站点 {view['site']} @t={view['at']} 包络 r{env['revision']} "
            f"[{env['from']},{env['until']}) 站点上限={env['total']}"
        )
    else:
        lines.append(f"站点 {view['site']} @t={view['at']}（当前秒点无有效包络）")

    cap = view["site_capacity"]
    lines.append(
        "站点功率："
        f"拟定={cap.get('planned_power', 0)} "
        f"已通知={cap.get('notified_power', 0)} "
        f"扣住={cap['held_power']}/{cap['total_limit']} "
        f"剩余={cap['headroom']} "
        f"控制器已确认={cap.get('actual_confirmed_power', 0)}"
    )
    if cap.get("measured_power") is None:
        lines.append(f"站点实测总量=不可合成（{cap.get('measurement_warning')}）")
    else:
        lines.append(
            f"站点实测总量={cap['measured_power']} @observed_at={cap['measured_at']}"
        )

    lines.append("支路：")
    for name, branch in view.get("branches", {}).items():
        measured = branch.get("measured")
        measured_text = "无快照" if not measured else (
            f"实测{measured['power']}@t{measured['observed_at']}"
        )
        lines.append(
            f"  {name}: 上限={branch['limit']} 拟定={branch['planned_power']} "
            f"通知={branch['notified_power']} 扣住={branch['held_power']} "
            f"确认={branch['actual_confirmed_power']} 剩余={branch['headroom']} "
            f"{measured_text}"
        )

    held = view.get("held_capacity", {}).get("demands", [])
    if held:
        lines.append("仍扣住容量：")
        for item in held:
            lines.append(
                f"  {item['demand']}#{item['plan']}@{item['branch']} "
                f"hold={item['hold_power']} 降额目标={item['projected_power']}"
            )

    commitments = [c for c in view.get("commitments", []) if c.get("outstanding")]
    if commitments:
        lines.append("待满足连续承诺：")
        for c in commitments:
            interval = c.get("authorized_interval") or c.get("planned_interval")
            lines.append(
                f"  {c['demand']}#{c['plan']}@{c['branch']} min={c['min_power']} "
                f"不可中断={c['guaranteed_uninterruptible']} 区间={interval} "
                f"状态={c['lifecycle']}"
            )

    startable = view.get("startable", [])
    lines.append("可开始设备：" + ("无" if not startable else ""))
    for item in startable:
        lines.append(
            f"  {item['demand']}（{item['device']}@{item['branch']}） "
            f"计划#{item['plan']} 可授权={item['would_grant_power']} "
            f"控制器={item['controller']}/boot{item['boot']}"
        )

    waiting = view.get("waiting", [])
    if waiting:
        lines.append("等待中：")
        for item in waiting:
            lines.append(
                f"  {item['demand']}#{item['plan']}@{item['branch']} "
                f"状态={item['lifecycle']} 原因={_wait_code(item)}"
            )

    restrictions = view.get("restrictions", [])
    if restrictions:
        lines.append("受限处置/未解决事件：")
        for inc in restrictions:
            lines.append(
                f"  [{inc['id']}] {inc['kind']} {inc['scope']} "
                f"受影响={inc.get('affected_demands')} "
                f"上限={inc.get('limit')} 扣住={inc.get('held_power')} "
                f"不可消解={inc.get('unmitigable', '-')}"
            )
    return "\n".join(lines)


def render(result: Dict[str, Any]) -> str:
    if "sites" in result:
        blocks = [render(site) for site in result["sites"].values()]
        return "\n\n".join(blocks) if blocks else "（尚无站点）"
    if "window" in result and "can_start" in result:
        return render_demand(result)
    if "branches" in result and "site_capacity" in result:
        return render_site(result)
    return str(result)
