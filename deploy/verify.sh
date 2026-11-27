#!/usr/bin/env bash
# CDCIM P4 一键验证（在 WSL / Ubuntu 24.04 中执行）
#   bash deploy/verify.sh quick         # 只验启动 + 健康 + 冒烟（约 2~3 分钟）
#   bash deploy/verify.sh full 60       # 再做断网补传，60 秒断链冒烟（不是 30 min 验收）
#   bash deploy/verify.sh full 1800     # 正式补传验收（约 35 分钟）
set -Eeuo pipefail

MODE="${1:-quick}"
DROP_SECONDS="${2:-60}"
cd "$(dirname "$0")"

LOG="verify-$(date +%Y%m%d-%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

step() { printf '\n===== %s =====\n' "$1"; }
fail() { printf '[FAIL] %s\n' "$1" >&2; exit 1; }

step 0 "环境检查"
for cmd in docker make python3; do
  command -v "$cmd" >/dev/null 2>&1 || fail "缺少 $cmd，请先安装（Ubuntu: apt install make python3；Docker 参考官方文档）"
done
docker info >/dev/null 2>&1 || fail "Docker daemon 未运行：sudo service docker start 或打开 Docker Desktop 集成"
docker compose version || fail "需要 Docker Compose v2（docker compose，不是 docker-compose）"
printf '[OK] %s\n' "$(docker --version)"

step 1 "准备 .env"
if [ ! -f .env ]; then
  cp .env.example .env
  printf '[WARN] 已由 .env.example 生成 .env，请编辑其中的 CHANGE_ME_* 密码后重新执行本脚本。\n'
  exit 2
fi
if grep -q 'CHANGE_ME' .env; then
  fail '.env 中仍有 CHANGE_ME_* 占位密码，请先填写真实密码'
fi
printf '[OK] .env 已就绪（未回显内容）\n'

step 2 "构建与配置校验"
make init-mqtt
make build
make config

step 3 "启动容器"
make up

step 4 "等待健康（最长 120 秒）"
for i in $(seq 1 40); do
  bad="$(docker compose --env-file .env ps --format '{{.Service}} {{.Status}}' | grep -Ec 'unhealthy|exited|dead' || true)"
  if [ "${bad:-0}" -eq 0 ]; then break; fi
  printf '  仍有 %s 个容器未就绪，等待 3 秒（%s/40）\n' "$bad" "$i"
  sleep 3
done
docker compose --env-file .env ps
if docker compose --env-file .env ps --format '{{.Status}}' | grep -Eq 'unhealthy|exited|dead'; then
  fail '容器未全部就绪，先看上面列表，再用 make logs <服务名> 查日志'
fi
printf '[OK] 容器全部就绪\n'

step 5 "初始化 topic 与时序库"
make init-topics
make init-db

PORT="$(grep -E '^PORT_WEB=' .env | head -1 | cut -d= -f2)"
PORT="${PORT:-8080}"
export CDCIM_BASE_URL="http://127.0.0.1:${PORT}"
printf '访问地址：%s\n' "$CDCIM_BASE_URL"

cd ..
step 6 "健康与冒烟"
python3 scripts/check_stack.py --mode all
python3 scripts/smoke_e2e.py --point-id 7001

if [ "$MODE" = "full" ]; then
  step 7 "断网补传（断链 ${DROP_SECONDS} 秒）"
  python3 scripts/verify_backfill.py --drop-seconds "$DROP_SECONDS"
else
  printf '\n[跳过] 断网补传。需要时执行：bash deploy/verify.sh full 1800\n'
fi

step 8 "完成"
printf '全部执行完毕。日志：deploy/%s\n' "$LOG"
printf '请把本日志贴回对话；观测表数值需人工填入 docs/05-实施/03-数据链路验证报告.md\n'
