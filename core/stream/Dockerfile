# syntax=docker/dockerfile:1
# 流计算镜像：Kafka 消费 + TDengine 批写 + 60 s 降采样。
# 走 taosrest（HTTP 6041），镜像内不需要 TDengine native 客户端与 gcc。
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# aiokafka 的 zstd 压缩依赖 zstandard；缺失时 producer 构造即 RuntimeError
RUN pip install --prefix=/install \
        "aiokafka>=0.10" \
        "taospy>=2.7" \
        "pydantic>=2.6" \
        "pydantic-settings>=2.2" \
        "structlog>=24.1" \
        "zstandard>=0.22" \
        "orjson>=3.9"

FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/shared \
    CDCIM_SERVICE_NAME=stream-compute

RUN groupadd --gid 10001 cdcim \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin cdcim \
    && mkdir -p /app

COPY --from=builder /install /usr/local
COPY --chown=10001:10001 core/shared /app/shared
COPY --chown=10001:10001 core/stream /app/stream

USER 10001:10001
WORKDIR /app

STOPSIGNAL SIGTERM
CMD ["python", "-u", "/app/stream/main.py"]
