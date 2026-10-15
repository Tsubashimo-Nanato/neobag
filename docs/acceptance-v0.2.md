# neobag 0.2 实机验收

日期：2026-10-15。目标：优化录制端默认可靠性，不改变发布端 QoS；增加可命名、保存、加载和更新的多套话题配置。

**结论：默认配置正常回放零缺失；相同 CPU 压力两次分别缺失 0 和 8 条，稳定零丢失验收未通过。** 保留更好的接收端默认配置与全部失败证据，不依赖修改发布者，也不把一次成功当成稳定无损。[机器可读汇总](acceptance-v0.2.json)。

## 数据与方法

沿用 0.1 的同一原始 bag：21,411,135,488 bytes，176,950 条消息，81 个元数据话题，其中 67 个有消息，时长 122.833087120 秒。源数据库 SHA256：`27e0fd1c6986468cd5afbd988eee78e52cff52935566bb1f87de589db835d604`，本轮再次校验一致。

目标机：Fedora 44、ROS 2 Jazzy、12 逻辑 CPU、约 14.18 GiB RAM、AMD GPU。完整回放使用 localhost-only 独立 domain 219；发布端始终使用原有 Fast DDS，不传入播放器 QoS override。回放速度 1×，暂停直到所有非空源话题的录制订阅创建完毕，结束后留 5 秒尾部时间，再由 UI Stop 正常保存。

压力条件与此前相同：回放约 15 秒后启动 12 个 nice 10、恒定内存、25 秒自限时 CPU worker。资源值来自实机；界面操作与 SVG 证据来自 Textual headless Pilot，不冒充实体屏幕截图。每次测试单独保存完整 bag、命令配置、ROS 日志、资源时间线、界面截图和比较结果。

比较 SQLite 原始数据与新录制的 MCAP，按话题、类型和序列化载荷 SHA256 比较完整多重集合，重复消息也按次数核对。MCAP 以流式方式读取并校验可用的 CRC。接收时间和跨话题顺序会改变，不作为相等条件。原始消息必须全部出现；额外系统日志单独报告，额外传感器消息不允许。

## 定位过程

以下结果全部保留，不只挑选成功运行。除特别注明的接收端手写队列文件外，发布者均未改变。

| 实验 | 负载 | 原始消息缺失 | 结果 |
| --- | --- | ---: | --- |
| Fast DDS + SQLite，手写 50 小话题 depth 1000 | 正常 | 0 | 完整性通过 |
| 同上 | CPU 压力 | 2,626 | 完整性失败 |
| Cyclone DDS + MCAP + 512 MiB 缓存，手写小话题队列 | CPU 压力 | 0 | 该次通过，不能证明稳定 |
| Cyclone DDS + MCAP + 512 MiB，ROS 自适应队列 | 正常 | 0 | 完整性通过 |
| 同上 | CPU 压力 | 38 | 完整性失败 |
| Cyclone DDS + MCAP + 自动小话题队列 | CPU 压力 | 94 | 完整性失败 |
| 再启用 Cyclone 异步交付与接收缓冲调整 | 正常 | 0 | 完整性通过 |
| 同上 | CPU 压力，第 1 次 | 0 | 完整性与告警通过 |
| 同上 | CPU 压力，第 2 次 | 8 | 完整性失败；告警通过 |
| Fast DDS + MCAP + 自动小话题队列 + 512 MiB 缓存 | CPU 压力 | 1,602 | 不选作该机器的默认接收路径 |

最终默认组合在正常和首次压力测试均录得 177,099 条，其中 176,950 条原始消息全部匹配，另外 149 条为新增 `/rosout`。重复压力测试录得 177,091 条，新增系统日志仍为 149 条，缺失 8 条原始消息；没有额外传感器消息。

首次压力 CPU 达到 100%，RAM 峰值 46.8%；压力约 3.68 秒后捕获到已渲染的告警，负载结束约 0.52 秒后捕获恢复。两次压力运行的持续 CPU、告警显示、告警恢复和截图检查均通过。

重复压力运行的 8 条缺失来自 joy、CAN vehicle_info、3 个 camera_info、left_cam_imu_transform 和 TF；原始时间分别约 22.71、30.47、35.13–35.18 秒，均位于压力区间。该次 ROS 日志没有 transport 累计丢失警告。**未报告丢失不能当作零丢失证明。**

这些对照表明，更深队列或单次零缺失不足以支持任意负载下可靠性保证。一次同条件重复也可能得到不同结果。

## 多套配置

- 真实暂停回放图上验证了 Save、Load、同名更新和多个名称共存，读取落盘 JSON 再核对话题集合。
- 内置 Left raw + VectorNav 在该 bag 图上选择 22 个话题（包括当前无数据但已发布的接口）。
- 单元与 UI 测试覆盖 80×24 / 120×36、离线选择、重启恢复、损坏文件保留、写入失败保留和名称/话题验证。
- 配置存放在 XDG 用户配置目录，独立于软件安装版本。安装升级与卸载不应删除配置或 bags。

## 软件验证

Windows：241 passed、8 项 POSIX 检查跳过；目标 Linux：249 passed。覆盖自动存储/RMW 选择、尊重用户 DDS 配置、双缓存预算、混合 QoS 发布者、临时文件生命周期、丢失计数保留，以及多配置与 Offline 单独取消。

另外在独立 domain 218 运行真实 SQLite3 合成流检查，录得 89 条测试字符串，开始/停止、metadata 和 `ros2 bag info` 均通过。这一检查验证 SQLite 分支，不能替代上面的完整多传感器回放结果。

## 可复现命令

```bash
python scripts/replay_acceptance.py /path/to/source_bag /path/to/normal_result --domain 219
python scripts/replay_acceptance.py /path/to/source_bag /path/to/pressure_result --domain 219 --cpu-pressure
python scripts/compare_bags.py /path/to/source_bag /path/to/pressure_result/recorded --output comparison.json
```

依赖安装：`python -m pip install '.[test]'`；原始 bag 的全部消息接口需要在 ROS 环境中可用。每次使用新的输出目录。工具不会修改源 bag。

本仓库只发布汇总与测试工具，不上传原始传感器 bag、机器地址、凭据或完整私有日志。[0.1 历史报告](acceptance.md) 保留此前发布端与接收端同时调队列的实验，不能用来证明本轮仅调整接收端的结果。
