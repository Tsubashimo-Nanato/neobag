# neobag

在实机 ROS 2 终端运行 `neobag`，用鼠标或键盘选择话题、填写保存位置与 bag 名称，点击 Record / Stop 完成录制。

## 一键安装

在 ROS 2 Linux 机器上运行，需要 Python 3.10+、venv/pip 和联网：

```bash
curl -fsSL https://raw.githubusercontent.com/Tsubashimo-Nanato/neobag/main/install.sh | bash
neobag
```

安装使用独立 Python 环境，不修改系统 Python 或 ROS 环境。运行前 source 需要的 ROS 2 和工作空间环境；也支持 PATH 中已有的 micromamba ROS 包装脚本。

锁定到指定提交／标签：

```bash
curl -fsSL https://raw.githubusercontent.com/Tsubashimo-Nanato/neobag/main/install.sh | bash -s -- --ref <commit-or-tag>
```

一键卸载：

```bash
curl -fsSL https://raw.githubusercontent.com/Tsubashimo-Nanato/neobag/main/uninstall.sh | bash
```

离线卸载已安装版本：`bash ~/.local/share/neobag/uninstall.sh`。卸载只移除 neobag 管理的安装目录与命令入口，保留源代码目录、录制的 bags 和验收证据。

指定默认保存位置、名称和存储格式：

```bash
neobag --output ~/bags --name test_drive_01
neobag --storage mcap
neobag --qos-overrides /path/to/qos.yaml
neobag --doctor
```

程序不会自动开始录制。默认保存目录为 `~/bags`，名称使用当前本地时间；同名目录存在时拒绝覆盖。SQLite3 为默认格式，MCAP 需要当前 ROS 环境安装相应插件。

## 操作

- 话题列表每 5 秒刷新，包含隐藏话题。显示 ROS 图中的话题与消息类型；只有订阅者的空话题也可能出现，这不等于正在收到消息。
- 点击行或按空格勾选；Tab 切换控件。搜索支持话题名和消息类型。
- 类别依据消息类型和名称识别：Camera、Point clouds、Laser、IMU、Odometry、Transforms、Navigation、Control、Diagnostics、Other。
- **All / None** 操作所有已发现话题，包含被搜索条件隐藏的话题。
- **Category + / Category −** 操作下拉菜单所选类别的所有话题，不受搜索影响。选择 All categories 时等同全选 / 全不选。
- 切换搜索或分类保留勾选；刷新发现话题消失时会移除该话题的勾选并提示。
- 输出目录和 bag 名称是两个独立字段。录制期间锁定所选话题和保存位置；本次录制只使用启动时勾选的名称，后续新话题不会自动加入。
- **Record** 开始，**Stop** 请求 rosbag 保存并关闭；出现 **Saved** 后完成。关闭菜单也会先请求停止并等待保存。

| 快捷键 | 操作 |
| --- | --- |
| Ctrl+R | 刷新话题 |
| Ctrl+S | 开始录制 |
| Ctrl+T | 停止并保存 |
| Ctrl+L | 录制日志；Esc 返回 |
| Ctrl+Q / Ctrl+C | 退出；录制中先停止并等待 |

支持 80×24 及更大的终端，建议 120×36。界面使用英文短标签，避免远程终端的中文字宽差异。

## 资源与告警

CPU、RAM、GPU 和输出盘可用空间约每秒更新。CPU 首次采样显示 N/A，第二次开始计算；无法读取的 GPU 显示 N/A，不会以 0% 伪装正常。

支持 NVIDIA `nvidia-smi`、Jetson GPU sysfs 和 AMD `gpu_busy_percent`；多 GPU 使用所选后端可读 GPU 中最高的占用率。已验证 AMD Linux 与 NVIDIA Windows 遥测；Jetson 支持有单元测试但未经 Jetson 硬件验证。

- CPU / RAM / GPU 连续 3 次达到 90% 时提示资源紧张，录制频率可能下降或发生丢消息。
- 输出盘可用空间少于 5 GiB 或占用达到 95% 时立即告警。
- 告警是资源压力的风险提示，**不是实测 bag 丢帧率、消息频率或硬件降频**。不会自动改变车辆、节点或录制频率。

Stop 只向本次启动的录制进程组发送 SIGINT，等待 rosbag 写完数据和 `metadata.yaml`；超过 30 秒仍未结束时提示继续等待，不强杀录制器。ROS 报错可从 Ctrl+L 日志查看。突然断电、SIGKILL、驱动或文件系统故障仍可能使 bag 不完整。

## 从源码安装

要求 Python 3.10+、pip/venv、已安装 `ros2 bag` 的 ROS 2 环境，安装依赖时需要联网。

```bash
cd /path/to/neobag
bash install.sh
source /opt/ros/jazzy/setup.bash  # 按实际版本调整
source /path/to/workspace/install/setup.bash  # 使用自定义消息时
neobag
```

安装使用 `~/.local/share/neobag` 中的独立版本目录与用户级 `~/.local/bin/neobag`，不需要 sudo。现有非 neobag 启动脚本会被保留并报错。若命令不在 PATH 中，可以直接运行 `~/.local/bin/neobag`。

neobag 调用当前 PATH 中的 `ros2` 并继承 `ROS_DOMAIN_ID`、RMW 和 DDS 配置，因此需要在能够发现目标车辆话题的同一环境中运行。默认使用 rosbag 自适应 QoS；`--qos-overrides` 可传入 ROS 官方格式的 YAML，按话题设置接收队列等策略。未列出的话题继续自适应。

高带宽录制中，即使整机 CPU 不高，短暂的 DDS／回调积压也可能造成消息丢失。完整 bag 的默认回放首次缺失 1 条消息，仅加深 IMU 接收队列的复测缺失 7 条；接收队列无法找回发布端历史中已被移除的消息。详见 [完整验收报告](docs/acceptance.md)。

[回放测试配置](examples/acceptance-small-message-qos.yaml) 同时用于回放端和录制端，为已检查大小的 50 个小消息话题设置 depth 1000，大型图像队列保持自适应。这是特定 bag 的测试配置；在其他设备上应先用 `ros2 topic info --verbose /topic` 确认发布端策略并修改话题名。neobag 的 QoS 参数只控制自己的订阅，不能改变真实传感器发布端的发送队列。

**QoS 覆盖文件会替换该话题的自适应策略。** 不要只写 depth 并假设可靠性仍会自动匹配；应明确 `history`、`depth`、`reliability`、`durability`，并按需要保留其他策略。可靠订阅与 best-effort 发布端不兼容。增加队列会使用更多内存，也不保证任意负载下零丢失。格式和行为见 [ROS 2 QoS override 文档](https://docs.ros.org/en/jazzy/How-To-Guides/Overriding-QoS-Policies-For-Recording-And-Playback.html)。

## 开发与验证

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest -q
```

Windows 可执行单元测试和模拟界面测试；实际录制以 Linux 为目标。

显式运行真实 ROS 集成检查，使用空闲的独立 DDS domain：

```bash
ROS_DOMAIN_ID=217 .venv/bin/python scripts/live_smoke.py
```

该检查只发布 `/neobag_test/text` 测试字符串，用真实 ROS 发现与录制器，通过 Textual Pilot 点击开始/停止，验证数据库消息、metadata 和 `ros2 bag info`，将 bag 与界面 SVG 留在 `evidence/`。这是隔离的合成流测试，不代表车辆摄像头或高带宽多传感器录制已验证。

完整已有 bag 的回放与录制验收：

```bash
python scripts/replay_acceptance.py /path/to/source_bag /path/to/new_result --domain 219 --qos-overrides /path/to/verified-qos.yaml --playback-qos-overrides /path/to/verified-qos.yaml
python scripts/replay_acceptance.py /path/to/source_bag /path/to/pressure_result --domain 220 --cpu-pressure --qos-overrides /path/to/verified-qos.yaml --playback-qos-overrides /path/to/verified-qos.yaml
python scripts/compare_bags.py /path/to/source_bag /path/to/new_result/recorded --output comparison.json
```

回放验收限定 localhost 和指定的独立 ROS domain；开始前检查现有图，暂停回放直到录制器订阅完成，回放结束后等待尾部数据再停止。两个 QoS 参数均可省略，分别控制录制和回放，实际使用的文件及 SHA256 会保存在证据目录。CPU 压力选项会在本机启动低优先级、恒定内存、25 秒自限时的负载。需要安装原 bag 的全部消息类型。

比较工具逐话题、类型统计消息数与序列化消息 SHA256 的重复次数，检查缺失、额外及内容差异；接收时间和跨话题顺序不要求相等。ROS 自身生成的 `/rosout`、`/parameter_events` 额外消息也会报告，不会静默忽略。完整回放结果和边界见 [验收报告](docs/acceptance.md)。

仓库不包含用户原始 bags、机器地址或原始测试日志。许可证沿用仓库的 [GPL-3.0](LICENSE)。
