# 验证脚本（CDCIM-P4-TEST-02 / 03 / 04）

三个脚本只依赖 Python 标准库 + `requests`，全部支持 `--base-url` 与 `--timeout`，
失败时打印 `[FAIL]` 与可操作的 `[HINT]`。

```bash
pip install requests
export CDCIM_BASE_URL=http://127.0.0.1:8080      # 也可逐条用 --base-url 传入
```

| 脚本 | 用途 | 对应判据 |
|---|---|---|
| `check_stack.py` | 逐个探测容器：`/healthz` 与各 TCP 端口 | NFR-AVAIL-02（依赖可达） |
| `smoke_e2e.py` | 发一条模拟数据 → 等 5 s → 查最新值 → 断言一致，打印各段耗时 | 采集→入库→可查链路连通（NFR-PERF-01 的前置冒烟） |
| `verify_backfill.py` | 断链 → 落盘 → 恢复补传 → 逐条对账条数与时间戳偏差 | FR-M1-06、QA-AVAIL-05 |

## 1. 健康探测

```bash
python scripts/check_stack.py --base-url http://127.0.0.1:8080
python scripts/check_stack.py --mode http                  # 只看入口
python scripts/check_stack.py --only api,mqtt,redis --host 192.168.1.20
python scripts/check_stack.py --ports mqtt=11883 --json-out /tmp/stack.json
```

退出码：`0` 全部通过；`1` 有失败项；`2` 参数/依赖问题。

> 容器内端口默认只绑 `127.0.0.1`（[物理视图 §3 说明 2](../../docs/03-架构/07-物理视图.md)），
> 所以 TCP 探测应在目标机上执行，或显式 `--host` 指向目标机。远端失败通常是「未映射」而非「服务挂了」。

## 2. 端到端冒烟

```bash
python scripts/smoke_e2e.py --base-url http://127.0.0.1:8080 --point-id 1001 --value 42.5
```

流程与观测：

| 步骤 | 实现 |
|---|---|
| 发布 | 调 `tools/simulator/main.py --only-point-id … --fixed-value …` 发 1 条 `telemetry.sample` |
| 等待 | 默认 5 s（`--wait`） |
| 查询 | 先 `/api/v1/realtime/points?point_ids=`（接口 18），404 时回落 `/api/v1/points/{id}/latest` |
| 断言 | 值相等（浮点容差 1e-6） |

打印 `发布阶段耗时`、`查询接口耗时`、`发布→可查` 三段。

**一条纪律**：单条样本不能判定 `P99 ≤ 2 s`。分位指标只能在 P6 稳态压测（2000 测点 × 10 min）中得到，
本脚本给的是「链路通不通」，不是「性能达不达标」。

## 3. 断链补传

```bash
# 官方口径：断 30 min，恢复后 15 min 内补齐
python scripts/verify_backfill.py --base-url http://127.0.0.1:8080 \
  --point-id 1001 --drop-seconds 1800 --backfill-wait 900

# 演练口径（2 min，只验证机制，不作为 FR-M1-06 结论）
python scripts/verify_backfill.py --base-url http://127.0.0.1:8080 \
  --point-id 1001 --drop-seconds 120 --backfill-wait 120
```

脚本自己做四件事：

1. `/healthz` 预检；
2. 启动模拟器 `--fault drop`，断链期间报文落盘到 `.simulator/edge-queue.jsonl`，
   同时写出 `--manifest` 清单（每条含 `ts` / `point_id` / `device_code`）；
3. 恢复后每 5 s 轮询 `/api/v1/points/{id}/series`（接口 19），直到清单中的时间戳全部出现或超时；
4. 逐条比对：`缺失条数 = 0`、`时间戳偏差 = 0`、模拟器 `dropped = 0` 同时成立才算 PASS。

只核对既有结果（不重跑）：

```bash
python scripts/verify_backfill.py --base-url http://127.0.0.1:8080 \
  --skip-run --manifest .simulator/backfill-manifest.jsonl --point-id 1001
```

## 4. 与接口文档的路径对照

| 脚本里的调用 | 接口文档中的定义 | 备注 |
|---|---|---|
| `GET /api/v1/healthz` | 接口 47 | 匿名，仅内网 |
| `GET /api/v1/realtime/points?point_ids=` | 接口 18 | 批量实时值，单次 ≤ 50 点 |
| `GET /api/v1/points/{id}/latest` | **文档未定义** | 契约以 `/realtime/points` 为准；此路径仅作兼容回落，正式实现不应新增 |
| `GET /api/v1/points/{id}/series` | 接口 19 | 历史曲线，响应含 `route` 回显 |
| `GET /api/v1/alarm-events` | 接口 21 | 前端告警列表 |

路径以前缀 `/api/v1` 为默认值，可用 `--api-prefix` 覆盖。若 P5 最终实现与上表不符，
应改**实现**而不是改脚本——契约的唯一来源是 [接口文档](../../docs/04-详细设计/07-接口文档.md)。
