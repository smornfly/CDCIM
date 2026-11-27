FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin simulator \
    && mkdir -p /data && chown -R 10001:10001 /data /app

USER 10001

ENTRYPOINT ["python", "main.py"]
CMD ["--devices", "300", "--points", "2000", "--duration", "600", "--queue-file", "/data/edge-queue.jsonl"]
