# 设备模拟器（CDCIM-P4-TOOL-01）

按 [需求规格说明书](../../docs/02-需求/02-需求规格说明书.md) 的采集口径生成 `telemetry.sample` 报文并发布到 MQTT：
默认 **300 台设备 / 2000 个测点**，快变 5 s、慢变 30 s，快变占比默认 20%。报文结构遵循
[消息主题与事件契约](../../docs/04-详细设计/08-消息主题与事件契约.md) TAB-API-12 / TAB-API-13。

> 口径声明：本工具只产生**负载**，不产生任何性能结论。压测数字只能在 P6 由
> [容量规划 §7](../../docs/03-架构/12-容量规划与性能模型.md) 的五个场景采集，且必须带环境标签。

---

## 1. 依赖与运行

```bash
pip install -r tools/simulator/requirements.txt   # 仅 paho-mqtt，版本已钉
python tools/simulator/main.py --help
```

只做本机节奏自检（不连 broker、不需要 Docker）：

```bash
python tools/simulator/main.py --dry-run --devices 5 --points 50 --duration 20
```

接入本机 Mosquitto（默认 `127.0.0.1:1883`）：

```bash
python tools/simulator/main.py --devices 300 --points 2000 --duration 600
```

容器方式（队列卷挂到 `/data`，容器内以 uid 10001 运行）：

```bash
docker build -t cdcim/simulator:0.1 tools/simulator
docker run --rm --network host -v cdcim-sim-data:/data cdcim/simulator:0.1 \
  --broker 127.0.0.1 --port 1883 --devices 300 --points 2000 --duration 600
```

## 2. 参数

| 参数 | 默认 | 说明 |
|---|---|---|
| `--devices` | 300 | 设备数，按 `UPS/AC/PDU/ENV/BAT/DOOR` 轮转命名（如 `UPS-0001`） |
| `--points` | 2000 | 测点总数，ID 从 1001 递增 |
| `--fast-ratio` | 0.2 | 快变占比；快变 5 s、慢变 30 s |
| `--rate` | 0 | 目标聚合速率 msg/s；**0 表示按设计节奏**。非 0 时等比压缩发送间隔 |
| `--duration` | 120 | 运行秒数，0 为不限期 |
| `--fault` | normal | `normal` / `step`（阶跃 +20%）/ `drift`（线性漂移）/ `drop`（断链） |
| `--drop-after` | 30 | `drop` 模式下断链开始时刻（秒） |
| `--drop-seconds` | 1800 | 断链时长，默认对应 FR-M1-06 的 30 min |
| `--replay-rate` | 480 | 补传速率 msg/s，对应「43 万条 15 min 补齐」的设计值 |
| `--queue-file` | `.simulator/edge-queue.jsonl` | 断链落盘队列 |
| `--queue-quota-mb` | 150 | 队列配额，对应 FR-M1-06 的 150 MB；超限计入 `dropped` |
| `--manifest` | 空 | 断链期间落盘记录清单，供 `scripts/verify_backfill.py` 逐条对账 |
| `--stats-out` | 空 | 运行汇总 JSON 输出路径 |
| `--only-point-id` | 0 | 只发指定测点（用于 smoke 单点验证） |
| `--fixed-value` | 无 | 固定值（用于 smoke 断言） |
| `--broker` / `--port` | env / 1883 | 也可用 `CDCIM_MQTT_HOST`、`CDCIM_MQTT_PORT` |
| `--topic-prefix` | `cdcim/telemetry` | 实际主题为 `{prefix}/{device_code}` |
| `--qos` | 1 | 0/1/2，默认 1（至少一次） |
| `--jitter` | 0.05 | 发送间隔抖动比例，模拟 300 台设备相位独立 |
| `--seed` / `--dry-run` | — | 复现实验 / 不连 broker |

### `--rate` 的语义必须说清

按设计节奏，默认参数的实际聚合速率是 `400/5 + 1600/30 ≈ 133 msg/s`（20% 快变），**不是 400 点·秒⁻¹**。
容量文档的 400 点·秒⁻¹ 是「2000 点全快变」的上界，见 [容量规划 §2.1](../../docs/03-架构/12-容量规划与性能模型.md)。
因此：

- 稳态场景（S1）**不要**传 `--rate`，让它按 5 s/30 s 自然出数；
- 峰值场景（S2）传 `--rate 1800`，此时发送间隔被压缩，**只代表压测负载，不代表真实采集周期**，报告中必须标注。

## 3. 统计输出

每 10 s 一行：

```
[stats] t=   30.0s sent=4002 rate=  133.40/s online=4002 replay=0 buffered=0 dropped=0 queue=0.0MB
```

| 字段 | 含义 |
|---|---|
| `sent` | 累计成功发布条数（在线 + 补传） |
| `rate` | 本统计窗口内的实际速率，不是目标值 |
| `online` / `replay` | 在线发布 / 补传发布条数 |
| `buffered` | 断链期间累计落盘条数 |
| `dropped` | 队列超配额被丢弃的条数（设计判据要求 = 0） |
| `queue` | 当前队列占用（MB），用于核对 80% 水位预警 |

结束时的 `[summary]` 同时写入 `--stats-out` 指定的 JSON，字段与上表一致。

## 4. 如何造一次断网（FR-M1-06 / QA-AVAIL-05）

### 方式一：模拟器内建断链（推荐，可控、可对账）

`scripts/verify_backfill.py` 已经把「启动 → 落盘 → 补传 → 对账」串成一条命令，一般不需要手写：

```bash
# 1) 先确认栈已就绪
python scripts/check_stack.py --base-url http://127.0.0.1:8080

# 2) 一条命令跑完；--drop-seconds 1800 是 FR-M1-06 的正式口径
python scripts/verify_backfill.py --base-url http://127.0.0.1:8080 \
  --point-id 1001 --drop-seconds 1800 --backfill-wait 900
```

需要手工控制每一步时，直接用模拟器：

```bash
# 前 30 s 正常，随后 1800 s 只落盘不发送，恢复后按 480/s 补传
python tools/simulator/main.py \
  --fault drop --drop-after 30 --drop-seconds 1800 \
  --queue-file .simulator/edge-queue.jsonl \
  --manifest .simulator/backfill-manifest.jsonl \
  --stats-out .simulator/backfill-stats.json \
  --duration 2100

# 断链期间观察 queue 单调上升且 < 150 MB；恢复后观察 replay 上升、queue 回落
# 只对账、不重跑：
python scripts/verify_backfill.py --base-url http://127.0.0.1:8080 \
  --skip-run --manifest .simulator/backfill-manifest.jsonl --point-id 1001
```

缩短演练（把 30 min 压到 2 min，用于回归，不代表 FR-M1-06 的正式口径）：

```bash
python tools/simulator/main.py --fault drop --drop-after 10 --drop-seconds 120 \
  --queue-file .simulator/edge-queue.jsonl --manifest .simulator/backfill-manifest.jsonl \
  --duration 300
```

### 方式二：真实网络隔离（更接近 QA-AVAIL-05 的「拔线」语义）

[物理视图 §2](../../docs/03-架构/07-物理视图.md) 已把采集网关单独挂在 `edge-net`，与 `core-net` 只经 Mosquitto 双归属桥接。因此可以真的把边端摘下网络：

```bash
docker network disconnect edge-net cdcim-collector-gateway   # 断网开始
docker network connect    edge-net cdcim-collector-gateway   # 断网结束
```

此时断链缓存由**采集网关**的边端队列承担（`cdcim-collector-queue` 卷），模拟器保持正常发布即可。
两种方式的区别：方式一验证「缓存→补传→对账」的全链路契约，方式二验证「采集进程在断网下的行为」。
正式验收建议**两种都做**，报告分开记录。

### 方式三：只停 broker（验证消费侧，不验证采集侧）

```bash
docker stop cdcim-mqtt && sleep 1800 && docker start cdcim-mqtt
```

这条路径下模拟器的 `publish` 会失败并计入 `dropped`——**它验证的是「模拟器丢数」，不是系统的补传能力**，
因此不能用于 FR-M1-06 的判据，只能用于观察重连行为。

## 5. 与设计文档的对齐与差异

| # | 项 | 本文做法 | 说明 |
|---|---|---|---|
| 1 | 目录位置 | `tools/simulator/` | [开发视图 §2.2](../../docs/03-架构/05-开发视图.md) 把协议模拟器归入 `tests/fixtures/`。P4 将其提为独立工具目录，便于单独构建镜像；差异登记于此，P5 若合并需同步开发视图 |
| 2 | MQTT 主题 | `cdcim/telemetry/{device_code}` | 设计文档只定义总线主题 `telemetry.raw`，未定义边端 MQTT 主题。此处为 P4 落地约定，**需与采集网关的订阅配置对齐**，属待确认项 |
| 3 | 补传主题 | 与在线同一 MQTT 主题，`replay=true`、`ts_source=replay` | 与 [消息契约 §2 说明 2](../../docs/04-详细设计/08-消息主题与事件契约.md) 一致：边端同一连接、同一 schema，分流发生在总线上 |
| 4 | 单机房速率 | 断链场景用默认 133 msg/s（全量） | 设计口径的单机房是 240 点·秒⁻¹。做 S3 时应把 `--points` 调到单机房规模（约 1200 点）或用 `--rate 240`，并在报告中写明所用口径 |
| 5 | 质量码 | 恒为 `good` | Bad/Stale 由采集侧在通信异常时产生，模拟器不伪造；质量码验证用 `--fault drop` + 真实断链观察 |
