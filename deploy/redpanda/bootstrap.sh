#!/bin/sh
# ============================================================
# Redpanda 主题初始化（13 个主题，幂等）
# 依据：docs/04-详细设计/08-消息主题与事件契约.md 表 TAB-API-11
# 运行方式（两种都支持）：
#   1) 容器内：make init-topics  ->  docker compose exec -T broker sh -s < redpanda/bootstrap.sh
#   2) 宿主上：REDPANDA_BROKERS=127.0.0.1:9092 sh deploy/redpanda/bootstrap.sh
#
# 幂等策略：先取现有主题清单，存在则跳过创建，只做分区数核对。
# 分区数不一致时「只告警不修改」——Kafka/Redpanda 改分区数会改变
# hash(key) % N 的落点，同一设备的历史与新数据将分属不同分区（契约 §5.3）。
# ============================================================
set -eu

# 容器内执行时 rpk 默认就能发现本机 broker（compose 健康检查也是这么做的）；
# 只有从宿主机执行才需要显式给 REDPANDA_BROKERS。
if [ -n "${REDPANDA_BROKERS:-}" ]; then
    RPK_BROKER_ARGS="--brokers ${REDPANDA_BROKERS}"
else
    RPK_BROKER_ARGS=""
fi
RPK="${RPK_BIN:-rpk}"

log() {
    printf '[bootstrap] %s\n' "$1"
}

# ---------- 1. 等待 broker 就绪 ----------
# 最多等 120 s（12 次 × 10 s）。首次启动需建数据目录与元数据，40~60 s 属正常。
i=0
while [ "$i" -lt 12 ]; do
    if "$RPK" cluster health $RPK_BROKER_ARGS 2>/dev/null | grep -q "Healthy:.*true"; then
        log "broker 就绪：${REDPANDA_BROKERS:-容器内本机}"
        break
    fi
    i=$((i + 1))
    log "等待 broker（$i/12）..."
    sleep 10
done

if [ "$i" -ge 12 ]; then
    log "错误：120 s 内 broker 未就绪。先看 docker compose logs broker"
    exit 1
fi

# ---------- 2. 取现有主题清单 ----------
# rpk topic list 默认输出三列：NAME  PARTITIONS  REPLICAS
LIST="$("$RPK" topic list $RPK_BROKER_ARGS)"

partitions_of() {
    echo "$LIST" | awk -v n="$1" '$1 == n {print $2; exit}'
}

# ---------- 3. 逐条创建（幂等） ----------
while IFS='|' read -r name parts retention_ms note; do
    [ -z "$name" ] && continue

    current="$(partitions_of "$name")"
    if [ -n "$current" ]; then
        log "已存在，跳过：$name（分区 $current）"
        continue
    fi

    log "创建：$name  分区=$parts  保留=${retention_ms}ms  # $note"
    if ! "$RPK" topic create "$name" \
        --partitions "$parts" \
        --replicas 1 \
        -c retention.ms="$retention_ms" \
        -c cleanup.policy=delete \
        -c segment.bytes=67108864; then
        log "警告：创建 $name 失败，稍后统一核对"
    fi
done <<'TOPICS'
telemetry.raw|1|21600000|在线遥测原始样本 6h，单分区是硬约束（契约 §5.1）
telemetry.replay|1|86400000|断网补传通道 24h，与 raw 同 schema 仅 replay=true
telemetry.late|3|604800000|迟到 >60s 的样本 7d
metric.window|6|86400000|1m/5m/1h 窗口关闭结果 24h
alarm.raw|3|604800000|每条原始告警（含被抑制）7d
alarm.events|3|604800000|告警事件状态变更 7d，同 event_id 必落同分区
linkage.commands|1|604800000|联动指令各阶段 7d，单分区保阶段有序
linkage.results|1|604800000|联动回执与回读 7d
ai.tasks|2|86400000|AI 建议任务 1d，可丢
ai.results|2|86400000|AI 建议结果 1d
meta.change|1|2592000000|台账/规则变更广播 30d
notify.outbox|3|259200000|通知事务性发件箱 3d
dlq.default|1|2592000000|死信统一收口 30d
TOPICS

# ---------- 4. 核对（重新取清单，包含新建的） ----------
LIST="$("$RPK" topic list $RPK_BROKER_ARGS)"
FAIL=0

while IFS='|' read -r name parts retention_ms note; do
    [ -z "$name" ] && continue
    current="$(partitions_of "$name")"
    if [ -z "$current" ]; then
        log "缺失：$name"
        FAIL=1
    elif [ "$current" != "$parts" ]; then
        log "分区数不一致：$name 期望 $parts 实际 $current（不自动修改，见契约 §5.3）"
        FAIL=1
    fi
done <<'TOPICS'
telemetry.raw|1
telemetry.replay|1
telemetry.late|3
metric.window|6
alarm.raw|3
alarm.events|3
linkage.commands|1
linkage.results|1
ai.tasks|2
ai.results|2
meta.change|1
notify.outbox|3
dlq.default|1
TOPICS

if [ "$FAIL" -ne 0 ]; then
    log "存在缺失或分区数不一致，请人工核对：rpk topic list"
    exit 1
fi

log "完成：13 个主题就绪"
log "关键核对：telemetry.raw 必须为 1 分区；扩分区须走契约 §5.3 的五步手术"
