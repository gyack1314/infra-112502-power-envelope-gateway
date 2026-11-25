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
