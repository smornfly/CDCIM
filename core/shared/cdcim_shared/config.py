"""集中配置：唯一的环境变量入口（编码规范 PY-09）。

三条纪律：

1. 业务代码**禁止** ``os.getenv``；一切配置经注入的 :class:`Settings` 读取。
2. 必填项缺失在**启动期**失败并打印键名，而不是在第一次写库时才炸。
3. 密码用 ``SecretStr`` 承载，:meth:`Settings.safe_summary` 输出脱敏后的连接信息；
   任何日志、健康检查、异常栈都不得打印含密码的连接串（安全架构 SEC-C-07）。

命名：环境变量统一 ``CDCIM_`` 前缀；为兼容仓库根 ``.env.example`` 里已有的
``TDENGINE_*`` / ``POSTGRES_*``，少数字段接受别名（见各 ``Field`` 的 ``validation_alias``）。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]

_MB: Final[int] = 1024 * 1024


class Settings(BaseSettings):
    """全部可配置项。默认值面向单机原型（8 GB 内存、单副本）。"""

    model_config = SettingsConfigDict(
        env_prefix="CDCIM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    # ---------------- 运行环境 ----------------
    env: str = "dev"
    service_name: str = "cdcim"
    log_level: str = "INFO"
    log_json: bool = True

    # ---------------- MQTT（边端接入） ----------------
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str | None = None
    mqtt_password: SecretStr | None = None
    mqtt_client_id: str = "cdcim-collector"
    #: 通配订阅：``cdcim/<site_code>/<device_code>/telemetry``。
    #: 只订阅遥测上行主题，控制下行主题不在采集侧订阅（单向数据流）。
    mqtt_topic_pattern: str = "cdcim/+/+/telemetry"
    mqtt_qos: int = 1
    mqtt_keepalive_s: int = 30
    mqtt_connect_timeout_s: float = 10.0

    # ---------------- 事件总线（Kafka 协议 / Redpanda） ----------------
    kafka_bootstrap_servers: str = "127.0.0.1:9092"
    # aiokafka 只接受整数 0/1/-1 或字符串 "all"；字符串 "1" 会直接抛 Invalid ACKS parameter。
    # 生产改为 -1（等价 all）。
    kafka_acks: int = 1
    kafka_linger_ms: int = 5
    kafka_compression: str = "zstd"
    kafka_max_message_bytes: int = 4 * 1024

    # ---------------- TDengine（时序库） ----------------
    # 走 taospy 的 REST 客户端（6041），避免容器内安装 native 客户端库；
    # 与部署侧 ``PORT_TDENGINE_REST`` 对齐。
    tdengine_host: str = "127.0.0.1"
    tdengine_rest_port: int = 6041
    tdengine_user: str = Field(
        default="root",
        validation_alias=AliasChoices("CDCIM_TDENGINE_USER", "TDENGINE_USER"),
    )
    tdengine_password: SecretStr = Field(
        default=SecretStr("taosdata"),
        validation_alias=AliasChoices("CDCIM_TDENGINE_PASSWORD", "TDENGINE_PASSWORD"),
    )
    tdengine_database: str = "dcim"
    tdengine_downsample_db: str = "dcim_1m"
    #: 单条 SQL 的超时（秒）。缺省值必须显式，禁止无限等待（编码规范 §2.4）。
    tdengine_timeout_s: float = 10.0
    #: 为 true 时不连库、只打印将要执行的 SQL（本地无 TDengine 冒烟用）。
    tdengine_dry_run: bool = False

    # ---------------- PostgreSQL（台账 / 告警） ----------------
    pg_host: str = "127.0.0.1"
    pg_port: int = 5432
    pg_database: str = Field(
        default="cdcim", validation_alias=AliasChoices("CDCIM_PG_DATABASE", "POSTGRES_DB")
    )
    pg_user: str = Field(
        default="cdcim", validation_alias=AliasChoices("CDCIM_PG_USER", "POSTGRES_USER")
    )
    pg_password: SecretStr = Field(
        default=SecretStr("cdcim_local_dev"),
        validation_alias=AliasChoices("CDCIM_PG_PASSWORD", "POSTGRES_PASSWORD"),
    )
    pg_pool_min_size: int = 1
    pg_pool_max_size: int = 10
    pg_command_timeout_s: float = 5.0

    # ---------------- Redis（窗口状态 / 会话 / 幂等） ----------------
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        validation_alias=AliasChoices("CDCIM_REDIS_URL", "REDIS_URL"),
    )
    redis_timeout_s: float = 2.0
    redis_session_prefix: str = "cdcim:sess:"
    redis_window_prefix: str = "cdcim:win:"

    # ---------------- 采样与攒批（进程视图 §3.2、契约 §6） ----------------
    sample_interval_s: int = 5          # 快变 5 s / 慢变 60 s，取快变
    write_batch_max_rows: int = 500
    write_batch_max_delay_ms: int = 200
    consumer_fetch_max_records: int = 500
    consumer_poll_timeout_ms: int = 200

    # ---------------- 降采样窗口 ----------------
    window_size_s: int = 60
    watermark_delay_s: int = 60         # 迟到阈值；超此值转 telemetry.late
    agg_version: int = 1

    # ---------------- 断网本地缓冲（D-41） ----------------
    buffer_path: Path = Path("/var/lib/cdcim/buffer/collector-buffer.db")
    buffer_quota_mb: int = 150
    buffer_high_watermark: float = 0.80     # 80% 打日志告警
    buffer_replay_batch: int = 200
    buffer_replay_rate_per_s: int = 1440    # 契约 §2：≤ 1 440 条·秒⁻¹
    buffer_sync_interval_ms: int = 1000

    # ---------------- API 服务 ----------------
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_history_max_span_days: int = 180    # 错误码 CDCIM-REQ-003 的区间上限
    api_raw_max_span_days: int = 7          # 原始粒度单次查询上限
    #: ``placeholder`` = P4 骨架的演示令牌；``session`` = P5 接真实会话（未实现则启动即失败）
    auth_mode: str = "placeholder"
    auth_session_ttl_min: int = 30

    # ---------------- 派生值 ----------------
    @property
    def buffer_quota_bytes(self) -> int:
        return self.buffer_quota_mb * _MB

    @property
    def buffer_high_watermark_bytes(self) -> int:
        return int(self.buffer_quota_bytes * self.buffer_high_watermark)

    def pg_dsn(self) -> str:
        """asyncpg 连接串。**返回值含密码，禁止直接写日志**。"""
        return (
            f"postgresql://{self.pg_user}:{self.pg_password.get_secret_value()}"
            f"@{self.pg_host}:{self.pg_port}/{self.pg_database}"
        )

    def safe_summary(self) -> dict[str, object]:
        """可安全打印的配置快照：只保留主机、端口、库名与配额，不含任何凭据。"""
        return {
            "env": self.env,
            "mqtt": f"{self.mqtt_host}:{self.mqtt_port}",
            "kafka": self.kafka_bootstrap_servers,
            "tdengine": f"{self.tdengine_host}:{self.tdengine_rest_port}/{self.tdengine_database}",
            "pg": f"{self.pg_host}:{self.pg_port}/{self.pg_database}",
            "redis": _mask_url(self.redis_url),
            "buffer_quota_mb": self.buffer_quota_mb,
            "window_size_s": self.window_size_s,
            "auth_mode": self.auth_mode,
        }


def _mask_url(url: str) -> str:
    """``redis://:pass@host`` → ``redis://***@host``，用于日志脱敏。"""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host_part = rest.rpartition("@")
    return f"{scheme}://***@{host_part}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。``lru_cache`` 让测试可用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
