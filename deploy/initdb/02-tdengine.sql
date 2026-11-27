-- ============================================================
-- CDCIM TDengine 3.x 初始化脚本
-- 来源：docs/04-详细设计/01-数据库设计说明书.md §4.1、§4.2
-- 字段命名：按《数据字典初稿》§4（st_metric 11 列 + 5 TAG 为唯一口径）
-- 分区/保留：按《数据库设计说明书》§4.1（KEEP 是库级参数，故拆 4 库，D-2）
--
-- 执行方式（TDengine 镜像没有 /docker-entrypoint-initdb.d 机制，必须手工执行）：
--   docker exec -i cdcim-tsdb taos < deploy/initdb/02-tdengine.sql
--   或 make init-db（Makefile 内已封装）
--
-- 本脚本只建「库 + 超级表」，不预建 8000 张子表：
--   子表由程序在台账新增测点时懒建，命名规则 m_<point_id>（见文件末模板）。
--   理由：预建 2000 张原始子表 + 6000 张降采样子表会把初始化脚本变成数据导入，
--   且 point_id 来自 PG 的自增主键，SQL 阶段还拿不到（数据字典 D-1 修订）。
-- ============================================================

-- ============================================================
-- 1. 四个库：KEEP / DURATION 不同，无法共存于一个库（D-2）
--    dcim    原始明细   DURATION 10d  KEEP 190d（在线 180 天 + 缓冲）
--    dcim_1m 1 分钟降采样 DURATION 30d  KEEP 1095d（3 年）
--    dcim_5m 5 分钟降采样 DURATION 90d  KEEP 1095d（3 年）
--    dcim_1h 1 小时降采样 DURATION 365d KEEP 1825d（5 年）
-- ============================================================
CREATE DATABASE IF NOT EXISTS dcim
    PRECISION 'ms'          -- 原始采样时刻即毫秒，不做任何偏移（D-1 修订）
    BUFFER 96 PAGES 128 PAGESIZE 4
    CACHEMODEL 'both' CACHESIZE 16   -- 同时缓存 last_row 与 last_value
    COMP 2 DURATION 10d KEEP 190d
    MAXROWS 4096 MINROWS 100
    REPLICA 1 VGROUPS 2 WAL_LEVEL 1;

CREATE DATABASE IF NOT EXISTS dcim_1m
    PRECISION 'ms' BUFFER 64 PAGES 128 PAGESIZE 4
    CACHEMODEL 'both' CACHESIZE 8
    COMP 2 DURATION 30d KEEP 1095d
    MAXROWS 4096 MINROWS 100
    REPLICA 1 VGROUPS 2 WAL_LEVEL 1;

CREATE DATABASE IF NOT EXISTS dcim_5m
    PRECISION 'ms' BUFFER 32 PAGES 64 PAGESIZE 4
    COMP 2 DURATION 90d KEEP 1095d
    MAXROWS 4096 MINROWS 100
    REPLICA 1 VGROUPS 1 WAL_LEVEL 1;

CREATE DATABASE IF NOT EXISTS dcim_1h
    PRECISION 'ms' BUFFER 32 PAGES 64 PAGESIZE 4
    COMP 2 DURATION 365d KEEP 1825d
    MAXROWS 4096 MINROWS 100
    REPLICA 1 VGROUPS 1 WAL_LEVEL 1;

-- ============================================================
-- 2. 超级表
--    子表粒度 = 测点：一测点一子表（m_<point_id>），
--    子表内 ts 即该测点采样时刻，同设备多测点同毫秒不冲突（D-1）
-- ============================================================

-- 2.1 原始明细：11 列 + 5 TAG
CREATE STABLE IF NOT EXISTS dcim.st_metric (
    ts           TIMESTAMP,
    point_id     BIGINT,
    val_double   DOUBLE,
    val_int      BIGINT,
    val_bool     BOOL,
    quality      TINYINT,
    ts_source    TINYINT,
    raw_val      DOUBLE,
    ingest_ts    TIMESTAMP,
    source_batch VARCHAR(32),
    is_late      TINYINT
) TAGS (
    device_id     BIGINT,
    room_id       INT,
    rack_id       BIGINT,
    device_type   VARCHAR(24),
    critical_flag TINYINT
);

-- 2.2 1 分钟降采样：11 列 + 4 TAG；子表名与原始表同名（m_<point_id>），靠库区分粒度
CREATE STABLE IF NOT EXISTS dcim_1m.st_metric_1m (
    ts           TIMESTAMP,
    point_id     BIGINT,
    val_avg      DOUBLE,
    val_max      DOUBLE,
    val_min      DOUBLE,
    val_first    DOUBLE,
    val_last     DOUBLE,
    sample_count INT,
    valid_count  INT,
    quality_flag TINYINT,
    agg_version  INT
) TAGS (
    device_id   BIGINT,
    room_id     INT,
    device_type VARCHAR(24),
    point_type  VARCHAR(16)
);

-- 2.3 5 分钟降采样：与 1m 同构（含首末值）
CREATE STABLE IF NOT EXISTS dcim_5m.st_metric_5m (
    ts           TIMESTAMP,
    point_id     BIGINT,
    val_avg      DOUBLE,
    val_max      DOUBLE,
    val_min      DOUBLE,
    val_first    DOUBLE,
    val_last     DOUBLE,
    sample_count INT,
    valid_count  INT,
    quality_flag TINYINT,
    agg_version  INT
) TAGS (
    device_id   BIGINT,
    room_id     INT,
    device_type VARCHAR(24),
    point_type  VARCHAR(16)
);

-- 2.4 1 小时降采样：8 列（去掉 val_first / val_last / sample_count）
CREATE STABLE IF NOT EXISTS dcim_1h.st_metric_1h (
    ts           TIMESTAMP,
    point_id     BIGINT,
    val_avg      DOUBLE,
    val_max      DOUBLE,
    val_min      DOUBLE,
    valid_count  INT,
    quality_flag TINYINT,
    agg_version  INT
) TAGS (
    device_id   BIGINT,
    room_id     INT,
    device_type VARCHAR(24),
    point_type  VARCHAR(16)
);

-- ============================================================
-- 3. 子表建表模板（不要在此处执行批量建表，仅供程序实现对照）
-- ------------------------------------------------------------
-- 规则：一测点一子表，子表名 m_<point_id>；4 个库各一张（原始 + 3 档降采样）。
-- 建表时机：台账新增测点时由 stream-compute 调 ensure_child_table(point_id)，
--          共 4 条 CREATE TABLE IF NOT EXISTS（幂等，重复调用无副作用）。
-- 停用测点：只置 point_status='disabled' 并停写，不删表（删表即丢历史）。
--
-- -- 原始（dcim 库，5 个 TAG）
-- CREATE TABLE IF NOT EXISTS dcim.m_7001 USING dcim.st_metric
--     TAGS (1001, 1, 30012, 'ups', 1);   -- device_id / room_id / rack_id / device_type / critical_flag
--
-- -- 1 分钟（dcim_1m 库，4 个 TAG）
-- CREATE TABLE IF NOT EXISTS dcim_1m.m_7001 USING dcim_1m.st_metric_1m
--     TAGS (1001, 1, 'ups', 'analog');
--
-- -- 5 分钟（dcim_5m 库）
-- CREATE TABLE IF NOT EXISTS dcim_5m.m_7001 USING dcim_5m.st_metric_5m
--     TAGS (1001, 1, 'ups', 'analog');
--
-- -- 1 小时（dcim_1h 库）
-- CREATE TABLE IF NOT EXISTS dcim_1h.m_7001 USING dcim_1h.st_metric_1h
--     TAGS (1001, 1, 'ups', 'analog');
--
-- 批量写入模板（自动建表 + 写 TAG，同一测点多行走一条 INSERT）：
-- INSERT INTO dcim.m_7001 USING dcim.st_metric
--     TAGS (1001, 1, 30012, 'ups', 1)
-- VALUES
--     ('2026-11-27 10:00:03.000', 7001, 221.35, NULL, NULL, 0, 0, 22135, '2026-11-27 10:00:03.180', 'b20261127-1000', 0);
--
-- 写入约定（D-1 修订，必须由 stream-compute 实现）：
--   1. ts 即原始采样时刻，禁止任何偏移（FR-M1-06 要求补传偏差 0）
--   2. point_id 列冗余保存并校验与 TAG 一致；过滤靠子表定位，不靠该列
--   3. ts_source=2(replay) 走「同 ts 覆盖」，与在线写入口径一致
-- ============================================================

-- ============================================================
-- 待验证清单（首次建库时执行 SHOW DATABASES / DESCRIBE 核对）
--   1. BUFFER ... PAGES ... PAGESIZE 与 CACHEMODEL 'both' 的写法在 3.3.x 上的
--      参数名兼容性（3.0 与 3.3 的 BUFFER 语义有调整）—— 若报错，先去掉
--      BUFFER/PAGES/PAGESIZE 三项按默认值建库，其余参数保留
--   2. VGROUPS 2 在两个 vnode 的容器上需足够磁盘；单机 75 GB 下 2 个 vgroup 可行
--   3. KEEP 190d 的最大值受 DURATION 约束（KEEP 必须 ≥ DURATION × 2）——
--      dcim 库 DURATION 10d / KEEP 190d 满足
--   4. 脚本整体幂等（全部 IF NOT EXISTS），可重复执行
-- ============================================================
