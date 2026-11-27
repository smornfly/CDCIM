#!/usr/bin/env python3
"""逐个探测 CDCIM 容器健康状态，输出表格；任一目标失败则非零退出。

HTTP 目标走 `/healthz`（接口 47），TCP 目标只判断端口可连通。
容器与端口以 docs/03-架构/07-物理视图.md TAB-ARCH-121 为准。
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from _common import (
    add_common_args,
    get_json,
    info,
    print_table,
    require_base_url,
    shorten,
    warn,
)

TCP_TARGETS = [
    ("mqtt", "MQTT 接入", 1883),
    ("redpanda", "事件总线", 9092),
    ("tdengine", "时序库", 6030),
    ("postgres", "关系库", 5432),
    ("redis", "缓存", 6379),
    ("qdrant", "向量库", 6333),
    ("prometheus", "指标", 9090),
    ("grafana", "看板", 3000),
]


@dataclass
class CheckResult:
    target: str
    kind: str
    address: str
    passed: bool
    elapsed_ms: float
    detail: str
    optional: bool = False


def parse_ports(raw: str) -> dict[str, int]:
    ports: dict[str, int] = {}
    for item in filter(None, (part.strip() for part in raw.split(","))):
        if "=" not in item:
            raise SystemExit(f"--ports 片段格式应为 name=port，收到 {item!r}")
        name, value = item.split("=", 1)
        ports[name.strip()] = int(value)
    return ports


def check_http(base_url: str, timeout: float, label: str, path: str) -> CheckResult:
    result = get_json(base_url, path, timeout)
    address = f"{base_url}{path}"
    if result.error:
        return CheckResult(label, "HTTP", address, False, result.elapsed_ms, result.error)
    ok = 200 <= result.status < 300
    detail = f"HTTP {result.status}"
    body = result.body
    if isinstance(body, dict):
        status = str(body.get("status", ""))
        version = str(body.get("version", ""))
        if status:
            detail += f" status={status}"
        if version:
            detail += f" version={version}"
        components = body.get("components")
        if isinstance(components, dict) and components:
            bad = [name for name, item in components.items()
                   if isinstance(item, dict) and item.get("reachable") is False]
            if bad:
                detail += f" 不可达={','.join(bad)}"
        if status == "degraded":
            detail += "（旁路降级，主链路可用）"
    return CheckResult(label, "HTTP", address, ok, result.elapsed_ms, detail)


def check_tcp(host: str, port: int, timeout: float, label: str, detail_label: str) -> CheckResult:
    address = f"{host}:{port}"
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=min(timeout, 3.0)):
            elapsed = (time.perf_counter() - started) * 1000
            return CheckResult(label, "TCP", address, True, elapsed, detail_label)
    except OSError as exc:
        elapsed = (time.perf_counter() - started) * 1000
        return CheckResult(label, "TCP", address, False, elapsed, f"{type(exc).__name__}: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CDCIM 容器健康探测")
    add_common_args(parser)
    parser.add_argument("--host", default="", help="TCP 探测的主机；默认取 --base-url 的 hostname")
    parser.add_argument("--mode", choices=("all", "http", "tcp"), default="all")
    parser.add_argument("--only", default="", help="只测这些目标，逗号分隔，如 api,mqtt,redis")
    parser.add_argument("--skip", default="", help="跳过这些目标，逗号分隔")
    parser.add_argument("--ports", default="", help="覆盖端口，如 mqtt=11883,postgres=15432")
    parser.add_argument("--json-out", default="", help="结果写入 JSON 文件")
    args = parser.parse_args(argv)

    base_url = require_base_url(args.base_url)
    host = args.host or urlparse(base_url).hostname or ""
    if not host:
        raise SystemExit("无法从 --base-url 解析主机名，请显式指定 --host")
    ports = parse_ports(args.ports)
    only = {item.strip() for item in args.only.split(",") if item.strip()}
    skip = {item.strip() for item in args.skip.split(",") if item.strip()}

    def selected(name: str) -> bool:
        if name in skip:
            return False
        return not only or name in only

    results: list[CheckResult] = []
    if args.mode in ("all", "http") and selected("api"):
        results.append(check_http(base_url, args.timeout, "api-service", "/healthz"))
    if args.mode in ("all", "http") and selected("web"):
        results.append(check_http(base_url, args.timeout, "反向代理", "/healthz"))

    if args.mode == "tcp":
        warn("仅做 TCP 探测时不校验 API 语义。",
             "完整验收请用 --mode all，让 /healthz 也参与判定")

    if args.mode in ("all", "tcp"):
        for name, label, default_port in TCP_TARGETS:
            if not selected(name):
                continue
            results.append(check_tcp(host, ports.get(name, default_port), args.timeout, name, label))

    if not results:
        raise SystemExit("没有选中任何探测目标，请检查 --only / --skip / --mode")

    rows = []
    for item in results:
        rows.append([
            item.target,
            item.kind,
            item.address,
            "通过" if item.passed else "失败",
            f"{item.elapsed_ms:.1f}",
            shorten(item.detail),
        ])
    print_table(["目标", "类型", "地址", "结果", "耗时(ms)", "说明"], rows)

    passed = sum(1 for item in results if item.passed)
    failed = [item for item in results if not item.passed]
    info(f"共 {len(results)} 项：通过 {passed}，失败 {len(failed)}")

    if failed:
        warn("局域网/容器内端口默认只绑 127.0.0.1（物理视图 §3 说明 2）。",
             "若在另一台机器上执行，请用 --host 指向目标机，或只在目标机上跑本脚本；"
             "真实设备接入时才需要放开 MQTT 绑定地址")
        for item in failed:
            print(f"  - {item.target} ({item.address}): {item.detail}", file=sys.stderr)

    if args.json_out:
        import json

        with open(args.json_out, "w", encoding="utf-8") as handle:
            json.dump([item.__dict__ for item in results], handle, ensure_ascii=False, indent=2)
        info(f"结果已写入 {args.json_out}")

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
