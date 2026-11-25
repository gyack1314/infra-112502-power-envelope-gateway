# 工地供电边缘网关的降额编排与计量确认

由供电代理发布逻辑计量单位的上限，边缘控制器仿真执行分配。示例无需真实硬件，power 使用整数单位。

## 输入消息

每个 JSONL 文件是一段独立输入记录。每行包括整数秒点 at、消息名称 kind 与对象 record。at 代表中间件收到消息的时刻，同一秒内按行序提交。业务时间和对应回执引用写在 record 中。

- **grid**　站点 site 的 revision 及有效区间 [from, until)。total 为站点总上限，branches 映射支路上限。不同 revision 重叠时以公布的当前 revision 为准。
- **controller**　登记控制器 id、site、boot 和 branches。 控制器的已生效取电变化以 feed 为事实，独立 meter 作为区域观测，不能只凭控制器新代次将旧用量清零。
- **request**　设备申请，通过 demand、site、branch 和 device 标识。earliest/latest 是可开工窗口，duration 是承诺的连续秒数，min/max 是功率单位，interruptible 表示可被暂停。
- **begin**　执行端请求启动此前被告知可开始的 demand，附 id、boot 和 plan。计划号为该站点从 1 起的身份。
- **feed**　同一控制器对 demand 上报实际取电调整，state 可为 reduced、self_power、drawing 或 unknown。plan 仅用于回查对应授权。
- **meter**　指定支路在 observed_at 的实测 power。不同支路相同 observed_at 才构成同时点合成。
- **inspect**　查看 site 或 demand。restart 模拟边缘网关退出重启。
- **restart**　模拟边缘网关重启，控制器继续保持此前实际状态。

## 示例

- reduction-is-not-yet-capacity.jsonl
- independent-branch-and-new-envelope.jsonl

## 读取演练资料

运行环境为 Python 3.11，只使用标准库。在根目录运行以下命令可核对文件格式及读取接口。

    python3 scenario_reader.py examples
    python3 -m unittest discover -v

资料读取器会列出各文件包含的消息和时间范围。演练记录只描述各参与方交给系统的输入。

## 网关入口

核心实现在 `edge_gateway/gateway.py`，只依赖标准库；`gateway.py` 是根目录命令入口。所有功率、时间和序号都是本地合成整数，不连接真实配电或充电设备。

回放演练并把完整 JSON 结果打印到标准输出：

```bash
python3 gateway.py scenario examples/reduction-is-not-yet-capacity.jsonl
python3 gateway.py scenario examples/independent-branch-and-new-envelope.jsonl
```

需要在回放后继续用同一状态查询时，显式指定状态文件：

```bash
python3 gateway.py scenario examples/independent-branch-and-new-envelope.jsonl \
  --state /tmp/yard-state.json
python3 gateway.py --state /tmp/yard-state.json --at 9 inspect --site yard --text
```

区域端、现场仿真端和调度台也可以分别调用：

```bash
STATE=/tmp/site.json
rm -f "$STATE"

# 区域端：发布包络、登记控制器、提出需求、暂停可中断需求
python3 gateway.py --state "$STATE" --at 0 grid --site yard --revision 1 \
  --from 0 --until 25 --total 20 --branch east=7 --branch west=7
python3 gateway.py --state "$STATE" --at 0 controller --id e \
  --site yard --boot 1 --branches east
python3 gateway.py --state "$STATE" --at 1 request --demand a --site yard \
  --branch east --device loader --earliest 1 --latest 3 --duration 12 \
  --min 6 --max 6 --not-interruptible
python3 gateway.py --state "$STATE" --at 1 request --demand flex --site yard \
  --branch west --device truck --earliest 1 --latest 9 --duration 8 \
  --min 3 --max 5 --interruptible
python3 gateway.py --state "$STATE" --at 2 pause --demand flex --reason crew

# 现场仿真端：请求授权、回报实际调整、上传计量快照
python3 gateway.py --state "$STATE" --at 2 begin --demand a \
  --controller e --boot 1 --plan 1
python3 gateway.py --state "$STATE" --at 5 feed --demand a \
  --controller e --boot 1 --plan 1 --state unknown
python3 gateway.py --state "$STATE" --at 12 meter --site yard \
  --branch east --observed-at 11 --power 6

# 调度台：JSON 或中文文本看板
python3 gateway.py --state "$STATE" --at 9 inspect --site yard
python3 gateway.py --state "$STATE" --at 9 inspect --demand a --text

# 重启后从同一 JSON 状态恢复；重复同一 begin 会按幂等决定处理
python3 gateway.py --state "$STATE" --at 20 restart
```

完整的 self-power 释放、跨秒点计量和等待窗口重估，请直接回放上面的两个示例。

也可以在 Python 中直接使用：

```python
from edge_gateway import Gateway

gw = Gateway("/tmp/site.json")
gw.handle(0, "grid", {...})
gw.handle(2, "begin", {...})
print(gw.inspect(at=9, site="yard"))
```

## 状态与安全规则

- **拟定负载**：请求找到 `[start, start+duration)` 的可行安排后，只把最低连续功率计入同时段承诺；站点总上限和支路上限都会检查。
- **已通知/扣住容量**：授权功率、降额目标和暂停通知会持久化。已通知降额不会立即释放 `hold_power`；未收到控制器的 `reduced` 或零外部功率 `self_power` 确认前，原占用继续计入上级站点包络。
- **实际负载**：`feed` 保存控制器确认值，`meter` 保存独立观测。计量快照按 `observed_at` 对齐；只有所有相关支路具有同一 `observed_at` 时才合成站点瞬时总量，否则站点实测总量为 `null` 并说明原因。
- **支路隔离**：某支路超限时生成 `branch:<site>:<branch>` 事件，只阻断共享该支路约束的新取电；其他支路仍受站点总上限检查，但不会被无关事件全场封锁。
- **受限处置**：新包络使当前扣住容量超限或使已接受承诺无法安排时，会产生活动事件并列出受影响需求。可中断设备会收到降功率到最小值或暂停到零的持久通知；不可中断承诺不会被正常新增需求移动或削减。
- **重启恢复**：状态原子写入 JSON。重启后保留授权、计划号、扣住容量、控制器代次、计量和事件；重复 `begin` 同一未释放计划返回幂等结果，不会新增冲突授权。控制器新 `boot` 不会把旧用量清零，旧代次的 `begin`/`feed` 会被拒绝且不应用。

测试：

```bash
python3 -m unittest discover -v
```

