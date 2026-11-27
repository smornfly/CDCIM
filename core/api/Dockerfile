# 查询服务：多阶段构建，非 root 运行（编码规范 PY-10）
FROM python:3.11-slim AS builder
WORKDIR /build
COPY pyproject.toml README.md ./
COPY core/shared ./core/shared
RUN pip install --no-cache-dir --prefix=/install .[dev] uvicorn

FROM python:3.11-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/core/shared:/app/core/api \
    TZ=Asia/Shanghai
WORKDIR /app
COPY --from=builder /install /usr/local
COPY core/shared ./core/shared
COPY core/api ./core/api
RUN useradd --uid 10001 --create-home cdcim && chown -R cdcim:cdcim /app
USER cdcim
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)"
CMD ["python", "/app/core/api/main.py"]
