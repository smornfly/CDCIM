# syntax=docker/dockerfile:1
# 采集网关镜像：MQTT 订阅 + Kafka 生产 + 本地缓冲盘。
# 多阶段：builder 里装依赖，runtime 只带 site-packages 与代码，镜像不含编译工具链。
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
# 公共库经 PYTHONPATH 挂载，不装成 wheel：三个镜像共享同一份源码，改代码不必重建依赖层
# aiokafka 的 zstd 支持由 cramjam 提供；缺失时 producer 构造即 RuntimeError
RUN pip install --prefix=/install \
        "aiokafka>=0.10" \
        "paho-mqtt>=2.0" \
        "pydantic>=2.6" \
        "pydantic-settings>=2.2" \
        "structlog>=24.1" \
        "cramjam>=2.8" \
        "orjson>=3.9"

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/shared \
    CDCIM_SERVICE_NAME=collector-gateway \
    CDCIM_BUFFER_PATH=/var/lib/cdcim/buffer/collector-buffer.db

# 非 root：UID 固定为 10001，便于宿主卷目录 chown 一次即可
RUN groupadd --gid 10001 cdcim \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin cdcim \
    && mkdir -p /var/lib/cdcim/buffer /app \
    && chown -R 10001:10001 /var/lib/cdcim

COPY --from=builder /install /usr/local
COPY --chown=10001:10001 core/shared /app/shared
COPY --chown=10001:10001 core/collector /app/collector

USER 10001:10001
WORKDIR /app

# 采集网关无 HTTP 端口，健康检查靠进程存活 + 日志；依赖可达性由 /readyz（api 服务）统一暴露
STOPSIGNAL SIGTERM
CMD ["python", "-u", "/app/collector/main.py"]
