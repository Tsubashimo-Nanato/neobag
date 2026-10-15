> 本文是 0.1 历史验收记录。0.2 的默认录制配置和发布端不变复测见 [最新验收报告](acceptance-v0.2.md)。

# ROS 2 实机回放验收 · 2026-10-15

**结论：选择、录制、正常停止、资源监测及压力告警已验证。配置回放端和接收端队列后，正常负载的全量比对零缺失；100% CPU 压力轮缺失 190 条消息，消息完整性验收未通过。neobag 不承诺任意负载下无丢失。**

机器地址、登录信息、原始 bag 和原始日志不上传仓库。以下数字来自实际回放和逐条比较；机器可读摘要见 [acceptance-summary.json](acceptance-summary.json)。完整证据保留在本地与测试端的 `evidence/acceptance/`。

## 数据与环境

- 原始 SQLite bag：21,411,135,488 bytes，122.833087120 秒，176,950 条消息。
- metadata 中有 81 个话题，其中 67 个非空；66 个非系统非空话题共 171,622 条消息，另有原始 `/rosout` 5,328 条。
- 原始 DB SHA256：`27e0fd1c6986468cd5afbd988eee78e52cff52935566bb1f87de589db835d604`。
- 四轮回放结束后再次计算 DB SHA256，结果不变；domain 219 / 220 的节点列表均已清空。
- Fedora 44、ROS 2 Jazzy、`rmw_fastrtps_cpp`、12 个逻辑 CPU、Python 3.14.8。自定义 `can_msgs` / `vectornav_msgs` 使用测试目录内的 overlay。
- 使用独立 domain 219 / 220，限制 localhost，开始前检查图和节点。没有启动真实车辆控制节点。
- 真实 ROS、真实录制进程和主机遥测；界面通过 Textual Pilot 点击操作并导出 SVG。这部分是 headless 终端界面渲染，并非人工操作桌面的录像。另有真实 Linux PTY 键盘及 SIGINT 保存检查。

## 完整结果

每轮均以 **1 倍速**回放完整数据，通过菜单 All 选择图中的 82 个话题（包含播放器生成的话题），点击 Record / Stop。开始回放前验证播放器处于暂停状态并等待订阅建立；播放结束后等待 5 秒，再正常停止、写入 metadata。

| 轮次 | 配置 | 新 bag 消息数 | 缺失 | 额外 | 消息完整性 |
| --- | --- | ---: | ---: | ---: | --- |
| `baseline_1x` | 默认 rosbag QoS | 177,040 | 1 | 91 | 未通过 |
| `baseline_qos_1x` | 仅 4 个 IMU 接收队列 depth 1000 | 177,040 | 7 | 97 | 未通过 |
| `baseline_buffered_1x` | 50 个小消息话题，发送与接收 depth 1000 | 177,143 | **0** | 193 | **通过** |
| `pressure_buffered_1x` | 同上，增加 25 秒 CPU 压力 | 176,953 | **190** | 193 | **未通过** |

所有额外消息都来自 `/rosout`，是本轮运行产生的诊断日志；它们被单独计数，没有掩盖原始日志的缺失检查。正常负载通过轮中，每个非系统话题的类型、消息数和序列化载荷多重集完全一致。四轮均正常退出、生成 metadata，清理过程没有错误。

首轮缺失位于第 81.106762718 秒的 `/aiformula_sensing/zed_node/imu/data_raw`，前后及首尾消息均匹配，排除了启动／停止边界。日志报告传输层缺失 1 条。只加深 IMU 接收队列仍出现 7 条传输层缺失，因此该尝试不能称为修复。

## 队列配置与边界

[最终配置](../examples/acceptance-small-message-qos.yaml) 用于**回放端和录制端**。对源 bag 的每个载荷检查长度后，选择了 50 个非空小消息话题：实际最大载荷 820 bytes，各 depth 1000；17 个大型图像话题保持默认队列。按各话题最大载荷估算，每端队列载荷合计上限约 13.16 MB；这不是实测 RSS，DDS 元数据、复制与分配还会占用内存。

配置明确指定可靠性与持久性；`/rosout` 和 `/tf_static` 保留 transient-local，其余为 volatile。原 metadata 缺少 QoS 的话题使用该播放器的 reliable / volatile 默认值。两个实际 YAML 文件及各自 SHA256 均保存在每轮证据中；摘要 SHA 针对测试文件原始字节（CRLF 换行），仓库按 LF 保存同一 YAML 内容。早期失败的 [IMU-only 配置](../examples/acceptance-imu-qos.yaml) 仅供复现实验，不是最终推荐配置。

Jazzy 回放会将普通记录配置的历史队列恢复为默认历史；可靠传输不等于无限保留所有已发送消息。发送历史满后，接收端更深的队列无法找回已经被发送端移除的样本。这与本次现象相符，但现有日志不足以证明每条丢失的具体内部原因。参见 [ROS QoS 实现](https://github.com/ros2/rosbag2/blob/jazzy/rosbag2_storage/src/rosbag2_storage/qos.cpp) 和 [FastDDS writer history 实现](https://github.com/eProsima/Fast-DDS/blob/v2.14.5/src/cpp/fastdds/publisher/DataWriterHistory.cpp)。

neobag 的 `--qos-overrides` 只控制自己的接收端。真实传感器的发布端、DDS、磁盘、CPU 和消息体大小仍决定录制能力；本次配置回放通过不能推导为默认设置或实车环境必然无丢失。

## 资源告警

- 正常通过轮：CPU 峰值 21.5%，RAM 峰值 48.9%。
- 压力轮：12 个低优先级、恒定内存、25 秒自限时 CPU 工作进程；CPU 实测达到 100%，RAM 峰值 49.5%。
- 验收同时要求：工作期间连续 3 次有效 CPU 采样 ≥90%；监测器发出告警；界面实际显示告警并保存可核验的 SVG。
- 工作结束后，验证有效 CPU 采样降至 90% 以下、界面告警消失，并保存恢复 SVG。该轮告警截图 CPU 100%，恢复截图 CPU 25%。四项告警／恢复检查全部通过。
- 压力轮实际缺失 190 条（原始总量约 0.1074%），涉及图像、IMU、CAN 和 TF 等话题。逐话题数量见 JSON 摘要；录制器也报告 transport-layer lost 190。
- GPU / RAM 数据是真实遥测，本轮没有专门施加 GPU 或 RAM 压力；不能据此声称这两类硬件压力场景已实测。风险告警也不等于硬件降频测量或实时丢消息计数。

压力脚本保留失败退出码，未把告警触发当作消息完整性通过。

## 比较方法与复现

`scripts/compare_bags.py` 以只读 SQLite 访问各 split 文件，逐话题、类型统计序列化载荷 SHA256 的重复次数，同时检查缺失和额外消息。重复载荷不会仅因去重而漏检。接收时间和跨话题顺序不要求相等，因为回放后会产生新的接收时间；这不是时序等价或频率精度测试。

```bash
python scripts/replay_acceptance.py /path/to/source /path/to/new-baseline \
  --domain 219 \
  --qos-overrides examples/acceptance-small-message-qos.yaml \
  --playback-qos-overrides examples/acceptance-small-message-qos.yaml

python scripts/replay_acceptance.py /path/to/source /path/to/new-pressure \
  --domain 220 --cpu-pressure \
  --qos-overrides examples/acceptance-small-message-qos.yaml \
  --playback-qos-overrides examples/acceptance-small-message-qos.yaml
```

正常轮数据完整性通过；压力轮在本机返回非零，原因是上述 190 条缺失。使用空闲 domain、新输出目录，以及包含所有原始消息类型的 ROS 环境。

测试套件：Windows 205 passed / 8 skipped；测试端 Linux 213 passed。跳过项涉及 POSIX 进程／文件系统行为，在 Linux 上执行。覆盖分类选择、界面交互、正常停止、子进程清理、资源告警、比较工具和安装／卸载边界。

首次 GitHub CI 发现 Python 3.10 信号枚举字符串的测试兼容性问题，以及关闭界面时定时回调访问已卸载控件的竞态；已修正断言并增加退出保护及回归测试。完整 bag 回放针对首发提交 `3f96522` 的录制逻辑；这次退出保护没有修改录制器、QoS 参数、消息比较逻辑或资源阈值。

GitHub 首发提交 `3f96522` 已在测试端完成固定提交的一键安装 → 卸载 → 重装。核对了启动器、安装标记和全部 6 个模块的文件 SHA256；源码、原 bag 和所有新录 bags 的大小与修改时间均保留。实际安装入口通过 `--doctor` 以及 Linux PTY 120×36 的 Tab/Tab/Enter 全选、Ctrl+S 录制、外部 SIGINT 保存检查，产生 8 条诊断消息且正常退出。远端保持已安装状态。
