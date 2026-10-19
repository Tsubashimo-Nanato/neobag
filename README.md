# neobag

在实机 ROS 2 终端运行 `neobag`，用鼠标或键盘选择话题、填写保存位置与 bag 名称，点击 Record / Stop 完成录制。

0.2.1 修复 Foxy 录制缓存和停止状态兼容问题，话题改为启动扫描一次、后续手动刷新。实现与车辆验证见 [Foxy 0.2.1 说明](docs/foxy-v0.2.1.md)。

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

离线卸载已安装版本：`bash ~/.local/share/neobag/uninstall.sh`。卸载只移除 neobag 管理的安装目录与命令入口，保留源代码目录、录制的 bags、话题配置和验收证据。

指定默认保存位置、名称和存储格式：

```bash
neobag --output ~/bags --name test_drive_01
neobag --storage mcap
neobag --qos-overrides /path/to/qos.yaml
neobag --doctor
```

程序不会自动开始录制。默认保存目录为 `~/bags`，名称使用当前本地时间；同名目录存在时拒绝覆盖。默认优先使用当前 ROS 环境中已安装的 MCAP 插件，否则使用 SQLite3；可用 `--storage sqlite3` 或 `--storage mcap` 明确指定。

## 操作

- 启动时扫描一次话题，之后点击 Refresh 或按 Ctrl+R 手动刷新，包含隐藏话题。资源占用仍每秒更新。显示 ROS 图中的话题与消息类型；只有订阅者的空话题也可能出现，这不等于正在收到消息。
- 点击行或按空格勾选；Tab 切换控件。搜索支持话题名和消息类型。
- 类别依据消息类型和名称识别：Camera、Point clouds、Laser、IMU、Odometry、Transforms、Navigation、Control、Diagnostics、Other。
- **All / None** 操作所有已发现话题，包含被搜索条件隐藏的话题。
- **Category + / Category −** 操作下拉菜单所选类别的所有话题，不受搜索影响。选择 All categories 时等同全选 / 全不选。
- 切换搜索或分类保留勾选；未在最近一次扫描发现的已选话题保留勾选，显示在 Offline 分类中，可单独取消；同时显示 offline 数量，录制时等待其发布者出现。None 也会清除离线选择。
- 输出目录和 bag 名称是两个独立字段。录制期间锁定所选话题和保存位置；本次录制只使用启动时勾选的名称，后续新话题不会自动加入。
- **Record** 开始，**Stop** 请求 rosbag 保存并关闭；出现 **Saved** 后完成。关闭菜单也会先请求停止并等待保存。

| 快捷键 | 操作 |
| --- | --- |
| Ctrl+R | 刷新话题 |
| Ctrl+S | 开始录制 |
| Ctrl+T | 停止并保存 |
| Ctrl+L | 录制日志；Esc 返回 |
| Ctrl+O | 话题配置；Esc 返回 |
| Ctrl+Q / Ctrl+C | 退出；录制中先停止并等待 |

支持 80×24 及更大的终端，建议 120×36。界面使用英文短标签，避免远程终端的中文字宽差异。

## 资源与告警

CPU、RAM、GPU 和输出盘可用空间约每秒更新。CPU 首次采样显示 N/A，第二次开始计算；无法读取的 GPU 显示 N/A，不会以 0% 伪装正常。

支持 NVIDIA `nvidia-smi`、Jetson GPU sysfs 和 AMD `gpu_busy_percent`；多 GPU 使用所选后端可读 GPU 中最高的占用率。已验证 AMD Linux 与 NVIDIA Windows 遥测；Jetson 支持有单元测试但未经 Jetson 硬件验证。

- CPU / RAM / GPU 连续 3 次达到 90% 时提示资源紧张，录制频率可能下降或发生丢消息。
- 输出盘可用空间少于 5 GiB 或占用达到 95% 时立即告警。
- 告警是资源压力的风险提示，**不是实测 bag 丢帧率、消息频率或硬件降频**。不会自动改变车辆、节点或录制频率。

Stop 只向本次启动的录制进程组发送 SIGINT，等待 rosbag 写完数据和 `metadata.yaml`；超过 30 秒仍未结束时提示继续等待，不强杀录制器。ROS 报错可从 Ctrl+L 日志查看。突然断电、SIGKILL、驱动或文件系统故障仍可能使 bag 不完整。

SQLite3 保存后在后台只读核对数据库实际消息数与 metadata 声明的总数和话题计数，完成前保持 Stopping；不一致时报告失败。Foxy 的 ros2cli 在正常响应本次 Stop 后可能返回 2，只有正常停止流程且 SQLite 校验通过才显示 Saved；无请求的异常退出仍为失败。这些检查不证明发布端所有消息均已收到。

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

## 默认录制可靠性（0.2）

同一 176,950 条消息 bag 的实机正常复测零缺失；满 CPU 压力两次分别缺失 0 和 8 条。默认可靠性有所改善，**尚未达到稳定零丢失**；完整成功与失败结果均保留在验收报告中。

neobag 调用当前 PATH 中的 `ros2`，继承当前 ROS domain、网络和工作空间环境。以下优化只作用于本次录制进程，不修改传感器、发布端、系统设置或 ROS 安装：

- **存储**：自动优先使用已安装的 MCAP，保留普通 MCAP 的分块、索引与 CRC；不使用关闭校验的 fastwrite 预设。未安装插件则使用 SQLite3。
- **接收实现**：未显式设置 `RMW_IMPLEMENTATION` 或 Fast DDS 配置文件时，Linux 上优先使用已安装的 Cyclone DDS。已有设置优先；插件不存在则继承 ROS 默认实现。`--recorder-rmw inherit` 可始终继承，或明确指定 `cyclonedds` / `fastrtps`。
- **接收调度**：neobag 自动选择 Cyclone DDS 且没有用户 `CYCLONEDDS_URI` 时，为自己的进程启用异步交付、最多 1024 条交付队列、请求 4 MiB socket 接收缓冲，并缩短 ACK/NACK 重复等待至 1 ms。操作系统仍可限制 socket 大小，不修改 sysctl；用户已有 Cyclone 配置保持原样。交付队列按消息条数限制，大消息可能额外占用内存。
- **写入缓存**：开始前检查实际 `ros2 bag record --help` 中缓存选项的单位。明确使用字节的实现按总内存和当前可用内存计算，最高每个缓冲区 512 MiB；现代 ROS 双缓冲默认总上限为 1 GiB，且不超过当前可用内存的一半或总内存的八分之一。`--cache-mib 128` 可明确设置大小，`--cache-mib 0` 禁用缓存。Foxy 0.3.11 按消息条数解释该选项，不能传 MiB 数值：本程序对此以及无法确认单位的实现使用 `--max-cache-size 0` 直接写盘，即使指定了非零 MiB 也不会启用旧缓存。
- **小消息队列**：开始前使用 ROS 自身的 Python 检查已选话题的发布者。对 IMU、里程计、CAN、VectorNav、TF、常见标量和控制消息等使用 depth 1000；按实际发布者匹配 reliability / durability。混合可靠性使用 best-effort，只有全部 transient-local 发布者才请求 transient-local。图像、点云、地图和未知类型保留 ROS 自适应策略，避免深队列占用大量内存。
- 自动 QoS 文件保留在 bag 内的 `neobag-receiver-qos.yaml`。探测不支持当前 ROS 包装方式或失败时，会在界面明确警告并回退到 ROS 自适应 QoS。`--native-qos` 可禁用小消息缓冲；显式 `--qos-overrides file.yaml` 优先于自动策略。

支持读取 sourced 系统 ROS Python 和安装前缀内的 conda/micromamba Python，不向 UI 虚拟环境安装 rclpy。自动队列基于消息类型而非实测字节大小；TF、日志等可变数组/字符串仍可能增长，RAM 告警继续有效。启动后新出现的发布者或策略变化可能需要重新开始录制。

界面顶部及 Ctrl+L 日志显示实际存储、缓存、RMW 和缓冲话题数。若 ROS 日志报告 transport 消息丢失，界面持续显示 LOST 和不完整提示；Jazzy 可能到 Stop 才输出累计数。没有报告不代表绝对零丢失，需要原始消息或序号才能完整验证。

`--qos-overrides` 会替换列出话题的完整自适应配置。手写文件时明确 `history`、`depth`、`reliability`、`durability`；可靠订阅与 best-effort 发布端不兼容。参见 [ROS QoS 覆盖文档](https://docs.ros.org/en/jazzy/How-To-Guides/Overriding-QoS-Policies-For-Recording-And-Playback.html) 与 [MCAP 存储选项](https://github.com/ros2/rosbag2/blob/jazzy/rosbag2_storage_mcap/README.md)。

0.1 的失败与早期两端队列实验保留在 [历史验收报告](docs/acceptance.md)。0.2 的发布端不变测试见 [新版验收报告](docs/acceptance-v0.2.md)。测试通过仅证明该机器、该 bag 和该压力条件，不保证任意负载或 best-effort 网络下零丢失。

## 保存多套话题配置

点击 **Profiles**（Ctrl+O），输入名称后点击 **Save / update**，即可保存当前勾选。相同名称会更新原配置；不同名称会另存一套。选择已有配置并点击 **Load**，返回主界面修改勾选，再打开 Profiles 保存即可。

初始提供 **Left raw + VectorNav**，匹配 `*/left_raw/*` 和 `*/vectornav/*`。首次手动勾选或点击 All / None 后由你的选择接管；保存时存储当前选中的确切话题名称。可建立例如“左相机与惯导”“仅 VectorNav”“全部传感器”等配置。下次启动恢复最后保存或加载的配置，不会自动开始录制。

配置位置为 `~/.config/neobag/profiles.json`，支持绝对路径 `XDG_CONFIG_HOME`。原子保存、保留离线话题，坏文件不会被自动覆盖。配置独立于安装目录，升级和卸载都会保留。

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

该检查只发布 `/neobag_test/text` 测试字符串，用真实 ROS 发现与录制器，明确使用 SQLite3，通过 Textual Pilot 点击开始/停止，验证数据库消息、metadata 和 `ros2 bag info`，将 bag 与界面 SVG 留在 `evidence/`。这是隔离的合成流测试，不代表车辆摄像头或高带宽多传感器录制已验证。

完整已有 bag 的回放与录制验收：

```bash
python scripts/replay_acceptance.py /path/to/source_bag /path/to/new_result --domain 219
python scripts/replay_acceptance.py /path/to/source_bag /path/to/pressure_result --domain 220 --cpu-pressure
python scripts/compare_bags.py /path/to/source_bag /path/to/new_result/recorded --output comparison.json
```

回放验收限定 localhost 和指定的独立 ROS domain；开始前检查现有图，暂停回放直到录制器订阅完成，回放结束后等待尾部数据再停止。上述命令不传入手写 QoS，不修改发布端配置。保留的可选 `--qos-overrides` / `--playback-qos-overrides` 仅用于明确的对照实验；使用时会记录文件和 SHA256。CPU 压力选项会在本机启动低优先级、恒定内存、25 秒自限时的负载。需要安装原 bag 的全部消息类型。

比较工具逐话题、类型统计消息数与序列化消息 SHA256 的重复次数，检查缺失、额外及内容差异；接收时间和跨话题顺序不要求相等。ROS 自身生成的 `/rosout`、`/parameter_events` 额外消息也会报告，不会静默忽略。完整回放结果和边界见 [0.2 验收报告](docs/acceptance-v0.2.md)。

仓库不包含用户原始 bags、机器地址或原始测试日志。许可证沿用仓库的 [GPL-3.0](LICENSE)。
