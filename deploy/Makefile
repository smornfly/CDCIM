# ============================================================
# CDCIM 部署快捷命令
# 用法：cd deploy && make help
# 前置：deploy/.env 已由 .env.example 复制并填好凭据；Docker Engine ≥ 24
# 本 Makefile 的所有目标均未在本机执行过（脚本随 P4 交付，首次执行即验证）
# ============================================================

SHELL      := /bin/bash
COMPOSE    := docker compose
PROJECT    := cdcim
ENV_FILE   := .env

.DEFAULT_GOAL := help
.PHONY: help up down restart ps logs logs-db init-mqtt init-topics init-db config reset backup

help: ## 显示可用命令
	@echo "CDCIM 部署命令："
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "默认启动 13 个容器；额外加 profile："
	@echo "  $(COMPOSE) --profile ai up -d       # 启用 AI 旁路（需先配 LLM_* 与 MASK_SALT）"
	@echo "  $(COMPOSE) --profile flink up -d    # 启用 Flink 替代实现"

up: init-mqtt ## 启动全部默认服务（先确保 mosquitto 口令文件存在）
	@test -f $(ENV_FILE) || { echo "缺少 deploy/.env：先 cp .env.example .env 并填凭据"; exit 1; }
	$(COMPOSE) --env-file $(ENV_FILE) up -d
	@echo "==> 等待健康检查（约 60~90 s，TDengine 首次初始化较慢）"
	@$(COMPOSE) ps
	@echo "==> 入口：http://localhost:$$(grep -E '^PORT_WEB=' $(ENV_FILE) | cut -d= -f2)"

down: ## 停止并移除容器（保留卷与数据）
	$(COMPOSE) --env-file $(ENV_FILE) down --remove-orphans

restart: ## 重启全部服务（不重建）
	$(COMPOSE) --env-file $(ENV_FILE) restart

ps: ## 查看容器状态与健康
	$(COMPOSE) --env-file $(ENV_FILE) ps

logs: ## 跟踪全部日志（最近 100 行）
	$(COMPOSE) --env-file $(ENV_FILE) logs -f --tail=100

logs-db: ## 只跟踪数据区日志（pg / tsdb / cache）
	$(COMPOSE) --env-file $(ENV_FILE) logs -f --tail=100 pg tsdb cache

build: ## 构建三个业务镜像（collector / stream / api）
	$(COMPOSE) build collector stream api

config: ## 校验 compose 语法与环境变量插值（不启动）
	$(COMPOSE) --env-file $(ENV_FILE) config -q && echo "compose 配置合法"

init-mqtt: ## 生成 mosquitto 口令文件（幂等：已存在则跳过）
	@if [ -f mosquitto/passwd ]; then \
		echo "==> mosquitto/passwd 已存在，跳过"; \
	else \
		test -f $(ENV_FILE) || { echo "缺少 deploy/.env"; exit 1; }; \
		MQTT_USER=$$(grep -E '^MQTT_USER=' $(ENV_FILE) | cut -d= -f2); \
		MQTT_PASSWORD=$$(grep -E '^MQTT_PASSWORD=' $(ENV_FILE) | cut -d= -f2); \
		echo "==> 生成 mosquitto/passwd（用户 $$MQTT_USER）"; \
		docker run --rm -v "$$(pwd)/mosquitto:/mosquitto/config" eclipse-mosquitto:2.0 \
			mosquitto_passwd -b -c /mosquitto/config/passwd "$$MQTT_USER" "$$MQTT_PASSWORD"; \
		echo "==> 完成；该文件含哈希，已在 .gitignore 排除"; \
	fi

init-topics: ## 创建 13 个 Kafka 主题（幂等）
	@echo "==> 初始化 Redpanda 主题（13 个）"
	docker compose exec -T broker sh -s < redpanda/bootstrap.sh

init-db: ## 初始化 TDengine 4 库 4 超级表（PG 由镜像首次启动自动执行）
	@echo "==> 初始化 TDengine（dcim / dcim_1m / dcim_5m / dcim_1h）"
	docker exec $(PROJECT)-tsdb taos -s "$$(cat initdb/02-tdengine.sql)"
	@echo "==> PostgreSQL 18 张表：首次启动时由 initdb/01-postgres.sql 自动建好"
	@echo "    若数据卷已存在过，需手工执行：docker exec -i $(PROJECT)-pg psql -U cdcim -d cdcim -v ON_ERROR_STOP=1 < initdb/01-postgres.sql"

backup: ## 备份不可重建的两个库（导出到 BACKUP_DIR）
	@mkdir -p "$${BACKUP_DIR:-./backup}"
	@echo "==> pg_dump（全库，custom 格式）"
	docker exec $(PROJECT)-pg pg_dump -U cdcim -d cdcim -Fc \
		> "$${BACKUP_DIR:-./backup}/cdcim_pg_$$(date +%Y%m%d_%H%M).dump"
	@echo "==> TDengine 数据目录打包（停写窗口内执行更安全）"
	docker run --rm -v $(PROJECT)_tdengine-data:/data:ro -v "$$(pwd)/$${BACKUP_DIR:-backup}:/backup" \
		alpine tar czf "/backup/cdcim_tsdb_$$(date +%Y%m%d_%H%M).tar.gz" -C /data .
	@echo "==> 完成；务必把备份再复制到 Windows 盘（单盘同宿主不算备份，见部署说明 §4）"

reset: ## 销毁性操作：删除全部容器与数据卷（需二次确认）
	@echo "============================================================"
	@echo " 危险：将删除以下命名卷中的全部数据，且不可恢复："
	@echo "   $(PROJECT)_tdengine-data  $(PROJECT)_pg-data  $(PROJECT)_collector-queue"
	@echo "   $(PROJECT)_redpanda-data  $(PROJECT)_qdrant-data  $(PROJECT)_redis-data ..."
	@echo "============================================================"
	@read -p "确认请输入 DELETE-ALL-DATA（其他任何输入即取消）： " ans; \
	if [ "$$ans" != "DELETE-ALL-DATA" ]; then \
		echo "已取消，未做任何改动"; exit 0; \
	fi; \
	read -p "第二次确认：是否已把 pg 与 tsdb 备份到 Windows 盘？(yes/no) " ans2; \
	if [ "$$ans2" != "yes" ]; then \
		echo "已取消。先执行 make backup"; exit 0; \
	fi; \
	echo "==> 执行 down -v"; \
	$(COMPOSE) --env-file $(ENV_FILE) down -v --remove-orphans; \
	echo "==> 卷已删除。重新启动前需重跑 make up && make init-db"
