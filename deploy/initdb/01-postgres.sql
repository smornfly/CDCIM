-- ============================================================
-- CDCIM PostgreSQL 16 初始化脚本
-- 来源：docs/04-详细设计/01-数据库设计说明书.md §3.3 可执行 DDL（原文照搬，未改写语义）
-- 字段命名：按《数据字典初稿》§4（字段名/类型/默认值以字典为唯一口径）
-- 分区策略：按《数据库设计说明书》§5 / §6（6 张月分区 + 1 张年分区，见文件末 DO 块）
-- 目标版本：PostgreSQL 16（语法要求 PG 13+，见文件末「待验证」清单）
--
-- 执行方式：
--   a) 自动：docker compose 首次启动时由 postgres 镜像的
--      /docker-entrypoint-initdb.d/01-postgres.sql 执行（仅空数据卷时触发一次）
--   b) 手工：psql -v ON_ERROR_STOP=1 -U cdcim -d cdcim -f deploy/initdb/01-postgres.sql
--      注意：脚本整体不可重入（无 IF NOT EXISTS 包裹的 CREATE TABLE），
--      重复执行会报表已存在。这是刻意的：迁移用 V<时间戳>__*.sql，初始化只做一次。
--
-- 本脚本不含 CREATE DATABASE：库由镜像环境变量 POSTGRES_DB 创建，
-- 角色由 POSTGRES_USER 创建，口令由 POSTGRES_PASSWORD 指定（见 .env.example §2.1）。
-- ============================================================

-- ============================================================
-- 0. schema 与公共函数
-- ============================================================
CREATE SCHEMA IF NOT EXISTS cdcim;
ALTER DATABASE cdcim SET timezone = 'UTC';
SET search_path TO cdcim, public;

-- updated_at 统一由触发器维护（字典 §4.1「updated_at 触发器维护」）
CREATE OR REPLACE FUNCTION cdcim.fn_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END
$fn$;

-- ============================================================
-- 1. 权限域：user_account / role / permission / user_role / role_permission
-- ============================================================
CREATE TABLE cdcim.user_account (
    user_id              bigint        GENERATED ALWAYS AS IDENTITY,
    username             varchar(64)   NOT NULL,
    display_name         varchar(64)   NOT NULL,
    employee_no          varchar(32),
    dept                 varchar(64),
    email                varchar(128),
    phone                varchar(32),
    password_hash        varchar(128)  NOT NULL,
    password_updated_at  timestamptz(3) NOT NULL DEFAULT now(),
    mfa_enabled          boolean       NOT NULL DEFAULT false,
    mfa_secret_ref       varchar(64),
    account_status       varchar(16)   NOT NULL DEFAULT 'active',
    failed_login_count   smallint      NOT NULL DEFAULT 0,
    locked_until         timestamptz(3),
    last_login_at        timestamptz(3),
    last_login_ip        inet,
    idp_source           varchar(16)   NOT NULL DEFAULT 'local',
    created_at           timestamptz(3) NOT NULL DEFAULT now(),
    updated_at           timestamptz(3) NOT NULL DEFAULT now(),
    updated_by           bigint,
    row_version          integer       NOT NULL DEFAULT 1,
    CONSTRAINT pk_user_account PRIMARY KEY (user_id),
    CONSTRAINT uk_user_account_name UNIQUE (username),
    CONSTRAINT ck_user_account_status CHECK (account_status IN ('active','locked','disabled')),
    CONSTRAINT ck_user_account_idp    CHECK (idp_source IN ('local','ldap','oidc')),
    CONSTRAINT ck_user_account_fail   CHECK (failed_login_count BETWEEN 0 AND 10),
    CONSTRAINT ck_user_account_ver    CHECK (row_version >= 1),
    CONSTRAINT fk_user_account_updated_by FOREIGN KEY (updated_by)
        REFERENCES cdcim.user_account (user_id) ON DELETE RESTRICT
);
-- 工号唯一：仅在有值时约束（字典 §4.16 部分唯一索引）
CREATE UNIQUE INDEX uk_user_account_emp ON cdcim.user_account (employee_no)
    WHERE employee_no IS NOT NULL;
CREATE INDEX idx_user_account_status ON cdcim.user_account (account_status);
CREATE INDEX idx_user_account_dept   ON cdcim.user_account (dept);

CREATE TABLE cdcim.role (
    role_id          bigint       GENERATED ALWAYS AS IDENTITY,
    role_code        varchar(32)  NOT NULL,
    role_name        varchar(64)  NOT NULL,
    description      varchar(256),
    role_level       smallint     NOT NULL DEFAULT 3,
    data_scope       varchar(16)  NOT NULL DEFAULT 'all',
    max_sessions     smallint     NOT NULL DEFAULT 3,
    is_builtin       boolean      NOT NULL DEFAULT false,
    separation_group varchar(32),
    role_status      varchar(16)  NOT NULL DEFAULT 'enabled',
    created_by       bigint,
    created_at       timestamptz(3) NOT NULL DEFAULT now(),
    updated_at       timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_role PRIMARY KEY (role_id),
    CONSTRAINT uk_role_code UNIQUE (role_code),
    CONSTRAINT ck_role_level  CHECK (role_level IN (1,2,3)),
    CONSTRAINT ck_role_scope  CHECK (data_scope IN ('all','room')),
    CONSTRAINT ck_role_status CHECK (role_status IN ('enabled','disabled')),
    CONSTRAINT ck_role_session CHECK (max_sessions BETWEEN 1 AND 10),
    CONSTRAINT fk_role_created_by FOREIGN KEY (created_by)
        REFERENCES cdcim.user_account (user_id) ON DELETE RESTRICT
);
CREATE INDEX idx_role_status ON cdcim.role (role_status);

CREATE TABLE cdcim.permission (
    perm_id              bigint       GENERATED ALWAYS AS IDENTITY,
    perm_code            varchar(64)  NOT NULL,
    perm_name            varchar(64)  NOT NULL,
    module               varchar(16)  NOT NULL,
    resource_type        varchar(32)  NOT NULL,
    action_type          varchar(16)  NOT NULL,
    is_sensitive         boolean      NOT NULL DEFAULT false,
    require_dual_control boolean      NOT NULL DEFAULT false,
    perm_desc            varchar(256),
    perm_status          varchar(16)  NOT NULL DEFAULT 'enabled',
    created_at           timestamptz(3) NOT NULL DEFAULT now(),
    updated_at           timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_permission PRIMARY KEY (perm_id),
    CONSTRAINT uk_permission_code UNIQUE (perm_code),
    CONSTRAINT ck_permission_module CHECK (module IN ('M1','M2','M3','M4','M5','M6')),
    CONSTRAINT ck_permission_action CHECK (action_type IN ('read','write','delete','export','control','approve')),
    CONSTRAINT ck_permission_status CHECK (perm_status IN ('enabled','disabled'))
);
CREATE INDEX idx_permission_module    ON cdcim.permission (module, action_type);
CREATE INDEX idx_permission_sensitive ON cdcim.permission (is_sensitive) WHERE is_sensitive;

CREATE TABLE cdcim.user_role (
    user_id      bigint       NOT NULL,
    role_id      bigint       NOT NULL,
    granted_by   bigint       NOT NULL,
    granted_at   timestamptz(3) NOT NULL DEFAULT now(),
    expire_at    timestamptz(3),
    grant_source varchar(16)  NOT NULL DEFAULT 'local',
    is_active    boolean      NOT NULL DEFAULT true,
    CONSTRAINT pk_user_role PRIMARY KEY (user_id, role_id),
    CONSTRAINT ck_user_role_source CHECK (grant_source IN ('local','idp')),
    CONSTRAINT ck_user_role_expire CHECK (expire_at IS NULL OR expire_at > granted_at),
    CONSTRAINT fk_user_role_user FOREIGN KEY (user_id)
        REFERENCES cdcim.user_account (user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_user_role_role FOREIGN KEY (role_id)
        REFERENCES cdcim.role (role_id) ON DELETE RESTRICT
);
CREATE INDEX idx_user_role_role ON cdcim.user_role (role_id);

CREATE TABLE cdcim.role_permission (
    role_id    bigint       NOT NULL,
    perm_id    bigint       NOT NULL,
    granted_by bigint       NOT NULL,
    granted_at timestamptz(3) NOT NULL DEFAULT now(),
    effect     varchar(8)   NOT NULL DEFAULT 'grant',
    is_active  boolean      NOT NULL DEFAULT true,
    CONSTRAINT pk_role_permission PRIMARY KEY (role_id, perm_id),
    CONSTRAINT ck_role_permission_effect CHECK (effect IN ('grant','deny')),
    CONSTRAINT fk_role_permission_role FOREIGN KEY (role_id)
        REFERENCES cdcim.role (role_id) ON DELETE RESTRICT,
    CONSTRAINT fk_role_permission_perm FOREIGN KEY (perm_id)
        REFERENCES cdcim.permission (perm_id) ON DELETE RESTRICT
);
CREATE INDEX idx_role_permission_perm ON cdcim.role_permission (perm_id);

-- ============================================================
-- 2. 资产域：space_node / device / alarm_rule / point_def / topology_link
-- ============================================================
CREATE TABLE cdcim.space_node (
    node_id      bigint       GENERATED ALWAYS AS IDENTITY,
    node_code    varchar(64)  NOT NULL,
    node_name    varchar(128) NOT NULL,
    node_type    varchar(16)  NOT NULL,
    parent_node_id bigint,
    room_id      bigint       NOT NULL,
    node_level   smallint     NOT NULL,
    seq_no       integer,
    pos_x        numeric(8,3),
    pos_y        numeric(8,3),
    pos_z        numeric(8,3),
    capacity_u   smallint,
    capacity_kw  numeric(8,2),
    used_u       smallint     DEFAULT 0,
    used_kw      numeric(8,2) DEFAULT 0,
    node_status  varchar(16)  NOT NULL DEFAULT 'normal',
    created_at   timestamptz(3) NOT NULL DEFAULT now(),
    updated_at   timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_space_node PRIMARY KEY (node_id),
    CONSTRAINT uk_space_node_code UNIQUE (node_code),
    CONSTRAINT ck_space_node_type  CHECK (node_type IN ('room','row','rack','u_slot','zone')),
    CONSTRAINT ck_space_node_level CHECK (node_level BETWEEN 1 AND 4),
    CONSTRAINT ck_space_node_status CHECK (node_status IN ('normal','maintenance','disabled')),
    CONSTRAINT ck_space_node_u CHECK (capacity_u IS NULL OR used_u BETWEEN 0 AND capacity_u),
    CONSTRAINT ck_space_node_kw CHECK (capacity_kw IS NULL OR used_kw BETWEEN 0 AND capacity_kw),
    -- room 必须是第一层；其余层必有父节点（字典 §4.3「层级不可跳」由应用层 + 本约束共同保证）
    CONSTRAINT ck_space_node_root CHECK (
        (node_type = 'room' AND parent_node_id IS NULL AND node_level = 1)
        OR (node_type <> 'room' AND parent_node_id IS NOT NULL AND node_level > 1)),
    CONSTRAINT fk_space_node_parent FOREIGN KEY (parent_node_id)
        REFERENCES cdcim.space_node (node_id) ON DELETE RESTRICT,
    CONSTRAINT fk_space_node_room FOREIGN KEY (room_id)
        REFERENCES cdcim.space_node (node_id) ON DELETE RESTRICT
);
CREATE INDEX idx_space_node_parent    ON cdcim.space_node (parent_node_id, seq_no);
CREATE INDEX idx_space_node_room_type ON cdcim.space_node (room_id, node_type);

CREATE TABLE cdcim.device (
    device_id        bigint       GENERATED ALWAYS AS IDENTITY,
    device_code      varchar(64)  NOT NULL,
    device_name      varchar(128) NOT NULL,
    device_type      varchar(32)  NOT NULL,
    vendor           varchar(64),
    model            varchar(64),
    serial_no        varchar(64),
    space_node_id    bigint       NOT NULL,
    parent_device_id bigint,
    protocol_type    varchar(16)  NOT NULL,
    host             varchar(64)  NOT NULL,
    port             integer,
    unit_id          smallint,
    credential_ref   varchar(64),
    protocol_params  jsonb        DEFAULT '{}'::jsonb,
    rated_power_kw   numeric(8,2),
    install_date     date,
    lifecycle_status varchar(16)  NOT NULL DEFAULT 'in_service',
    critical_level   smallint     NOT NULL DEFAULT 3,
    online_status    varchar(16)  NOT NULL DEFAULT 'unknown',
    last_seen_at     timestamptz(3),
    retire_at        timestamptz(3),
    updated_at       timestamptz(3) NOT NULL DEFAULT now(),
    updated_by       bigint,
    row_version      integer      NOT NULL DEFAULT 1,
    CONSTRAINT pk_device PRIMARY KEY (device_id),
    CONSTRAINT uk_device_code UNIQUE (device_code),
    CONSTRAINT ck_device_type CHECK (device_type IN
        ('ups','pdu','battery','precision_ac','th_sensor','leak','smoke','door_ctrl','camera','gateway')),
    CONSTRAINT ck_device_protocol CHECK (protocol_type IN
        ('modbus_tcp','snmp_v2c','snmp_v3','mqtt','http_pull')),
    CONSTRAINT ck_device_lifecycle CHECK (lifecycle_status IN ('in_service','maintenance','retired','planned')),
    CONSTRAINT ck_device_online CHECK (online_status IN ('online','offline','unknown')),
    CONSTRAINT ck_device_critical CHECK (critical_level IN (1,2,3)),
    CONSTRAINT ck_device_port CHECK (port IS NULL OR port BETWEEN 1 AND 65535),
    CONSTRAINT ck_device_unit CHECK (unit_id IS NULL OR unit_id BETWEEN 1 AND 247),
    CONSTRAINT ck_device_ver CHECK (row_version >= 1),
    -- 自引用不得指向自身（A→A）；A→B→A 的环由应用层保存时检测（字典 §4.2）
    CONSTRAINT ck_device_no_self CHECK (parent_device_id IS NULL OR parent_device_id <> device_id),
    CONSTRAINT fk_device_space FOREIGN KEY (space_node_id)
        REFERENCES cdcim.space_node (node_id) ON DELETE RESTRICT,
    CONSTRAINT fk_device_parent FOREIGN KEY (parent_device_id)
        REFERENCES cdcim.device (device_id) ON DELETE RESTRICT,
    CONSTRAINT fk_device_updated_by FOREIGN KEY (updated_by)
        REFERENCES cdcim.user_account (user_id) ON DELETE RESTRICT
);
CREATE INDEX idx_device_space       ON cdcim.device (space_node_id);
CREATE INDEX idx_device_parent      ON cdcim.device (parent_device_id);
CREATE INDEX idx_device_type_status ON cdcim.device (device_type, lifecycle_status);
CREATE INDEX idx_device_online      ON cdcim.device (online_status) WHERE online_status <> 'online';

CREATE TABLE cdcim.alarm_rule (
    rule_id                bigint       GENERATED ALWAYS AS IDENTITY,
    rule_code              varchar(64)  NOT NULL,
    rule_name              varchar(128) NOT NULL,
    rule_type              varchar(16)  NOT NULL,
    target_type            varchar(16)  NOT NULL,
    target_filter          jsonb        NOT NULL DEFAULT '{}'::jsonb,
    condition_expr         text,
    threshold_value        numeric(18,6),
    threshold_value2       numeric(18,6),
    compare_op             varchar(8)   DEFAULT 'gt',
    duration_s             integer      DEFAULT 0,
    rate_window_s          integer,
    rate_threshold         numeric(18,6),
    alarm_level            varchar(8)   NOT NULL,
    alarm_type             varchar(32)  NOT NULL,
    suppress_window_s      integer      NOT NULL DEFAULT 300,
    parent_suppress_enabled boolean     NOT NULL DEFAULT true,
    notify_channels        varchar[]    NOT NULL DEFAULT '{screen,im}',
    escalate_interval_s    integer      NOT NULL DEFAULT 300,
    escalate_max_times     smallint     NOT NULL DEFAULT 3,
    effective_scope        jsonb        NOT NULL DEFAULT '{"rooms":"all"}'::jsonb,
    rule_version           integer      NOT NULL DEFAULT 1,
    rule_status            varchar(16)  NOT NULL DEFAULT 'draft',
    effective_from         timestamptz(3) NOT NULL DEFAULT now(),
    created_by             bigint       NOT NULL,
    created_at             timestamptz(3) NOT NULL DEFAULT now(),
    updated_by             bigint,
    updated_at             timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_alarm_rule PRIMARY KEY (rule_id),
    CONSTRAINT uk_alarm_rule UNIQUE (rule_code, rule_version),
    CONSTRAINT ck_alarm_rule_type   CHECK (rule_type IN ('threshold','duration','rate','communication')),
    CONSTRAINT ck_alarm_rule_target CHECK (target_type IN ('device','point','group')),
    CONSTRAINT ck_alarm_rule_level  CHECK (alarm_level IN ('critical','major','minor')),
    CONSTRAINT ck_alarm_rule_atype  CHECK (alarm_type IN
        ('threshold','duration','rate','communication','linkage_failure','system','data_delay','forecast')),
    CONSTRAINT ck_alarm_rule_cmp    CHECK (compare_op IN ('gt','ge','lt','le','between','outside')),
    CONSTRAINT ck_alarm_rule_status CHECK (rule_status IN ('draft','gray','enabled','disabled','archived')),
    CONSTRAINT ck_alarm_rule_suppress CHECK (suppress_window_s BETWEEN 60 AND 3600),
    CONSTRAINT ck_alarm_rule_escalate CHECK (escalate_interval_s >= 60),
    CONSTRAINT ck_alarm_rule_escalate_max CHECK (escalate_max_times BETWEEN 0 AND 5),
    CONSTRAINT ck_alarm_rule_rate_window CHECK (rate_window_s IS NULL OR rate_window_s BETWEEN 60 AND 3600),
    CONSTRAINT ck_alarm_rule_duration CHECK (duration_s IS NULL OR duration_s >= 0),
    CONSTRAINT ck_alarm_rule_ver CHECK (rule_version >= 1),
    CONSTRAINT fk_alarm_rule_created_by FOREIGN KEY (created_by)
        REFERENCES cdcim.user_account (user_id) ON DELETE RESTRICT,
    CONSTRAINT fk_alarm_rule_updated_by FOREIGN KEY (updated_by)
        REFERENCES cdcim.user_account (user_id) ON DELETE RESTRICT
);
CREATE INDEX idx_alarm_rule_status ON cdcim.alarm_rule (rule_status, target_type);
CREATE INDEX gin_alarm_rule_target ON cdcim.alarm_rule USING gin (target_filter jsonb_path_ops);
CREATE INDEX idx_alarm_rule_level  ON cdcim.alarm_rule (alarm_level);

CREATE TABLE cdcim.point_def (
    point_id          bigint        GENERATED ALWAYS AS IDENTITY,
    point_code        varchar(64)   NOT NULL,
    device_id         bigint        NOT NULL,
    point_name        varchar(128)  NOT NULL,
    point_type        varchar(16)   NOT NULL,
    data_type         varchar(16)   NOT NULL DEFAULT 'uint16',
    unit              varchar(16),
    register_addr     varchar(64)   NOT NULL,
    function_code     smallint,
    scale_factor      numeric(12,6) NOT NULL DEFAULT 1.000000,
    offset_value      numeric(12,6) NOT NULL DEFAULT 0.000000,
    deadband          numeric(12,6) NOT NULL DEFAULT 0.005000,
    sample_interval_s smallint      NOT NULL DEFAULT 5,
    range_min         numeric(18,6),
    range_max         numeric(18,6),
    critical_flag     boolean       NOT NULL DEFAULT false,
    default_rule_id   bigint,
    point_status      varchar(16)   NOT NULL DEFAULT 'enabled',
    remark            varchar(256),
    created_at        timestamptz(3) NOT NULL DEFAULT now(),
    updated_at        timestamptz(3) NOT NULL DEFAULT now(),
    updated_by        bigint,
    CONSTRAINT pk_point_def PRIMARY KEY (point_id),
    CONSTRAINT uk_point_def_code UNIQUE (point_code),
    CONSTRAINT ck_point_def_ptype CHECK (point_type IN ('analog','discrete','accumulate','text')),
    CONSTRAINT ck_point_def_dtype CHECK (data_type IN
        ('uint16','int16','uint32','int32','float32','bool','string')),
    CONSTRAINT ck_point_def_status CHECK (point_status IN ('enabled','disabled','reserved')),
    CONSTRAINT ck_point_def_interval CHECK (sample_interval_s IN (5,30)),
    CONSTRAINT ck_point_def_scale CHECK (scale_factor > 0),
    CONSTRAINT ck_point_def_deadband CHECK (deadband >= 0),
    CONSTRAINT ck_point_def_fc CHECK (function_code IS NULL OR function_code IN (1,2,3,4)),
    CONSTRAINT ck_point_def_range CHECK (range_min IS NULL OR range_max IS NULL OR range_max > range_min),
    CONSTRAINT fk_point_def_device FOREIGN KEY (device_id)
        REFERENCES cdcim.device (device_id) ON DELETE RESTRICT,
    CONSTRAINT fk_point_def_rule FOREIGN KEY (default_rule_id)
        REFERENCES cdcim.alarm_rule (rule_id) ON DELETE RESTRICT,
    CONSTRAINT fk_point_def_updated_by FOREIGN KEY (updated_by)
        REFERENCES cdcim.user_account (user_id) ON DELETE RESTRICT
);
CREATE INDEX idx_point_def_device   ON cdcim.point_def (device_id, point_type);
CREATE INDEX idx_point_def_rule     ON cdcim.point_def (default_rule_id);
CREATE INDEX idx_point_def_critical ON cdcim.point_def (critical_flag) WHERE critical_flag;

CREATE TABLE cdcim.topology_link (
    link_id          bigint       GENERATED ALWAYS AS IDENTITY,
    link_code        varchar(64)  NOT NULL,
    link_type        varchar(16)  NOT NULL,
    src_node_type    varchar(16)  NOT NULL,
    src_node_id      bigint       NOT NULL,
    dst_node_type    varchar(16)  NOT NULL,
    dst_node_id      bigint       NOT NULL,
    direction        varchar(8)   NOT NULL DEFAULT 'down',
    criticality      smallint     NOT NULL DEFAULT 2,
    redundancy_group varchar(32),
    eff_from         timestamptz(3) NOT NULL DEFAULT now(),
    eff_to           timestamptz(3),
    is_current       boolean      NOT NULL DEFAULT true,
    created_at       timestamptz(3) NOT NULL DEFAULT now(),
    updated_at       timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_topology_link PRIMARY KEY (link_id),
    CONSTRAINT uk_topology_link UNIQUE
        (link_type, src_node_type, src_node_id, dst_node_type, dst_node_id, eff_from),
    CONSTRAINT ck_topology_link_type CHECK (link_type IN ('power','network','cooling','containment','data')),
    CONSTRAINT ck_topology_link_src  CHECK (src_node_type IN ('space','device')),
    CONSTRAINT ck_topology_link_dst  CHECK (dst_node_type IN ('space','device')),
    CONSTRAINT ck_topology_link_dir  CHECK (direction IN ('down','up','bi')),
    CONSTRAINT ck_topology_link_crit CHECK (criticality BETWEEN 1 AND 3),
    CONSTRAINT ck_topology_link_eff  CHECK (eff_to IS NULL OR eff_to > eff_from),
    CONSTRAINT ck_topology_link_self CHECK (NOT (src_node_type = dst_node_type AND src_node_id = dst_node_id))
);
CREATE INDEX idx_topology_link_src  ON cdcim.topology_link (src_node_type, src_node_id) WHERE is_current;
CREATE INDEX idx_topology_link_dst  ON cdcim.topology_link (dst_node_type, dst_node_id) WHERE is_current;
CREATE INDEX idx_topology_link_type ON cdcim.topology_link (link_type, is_current);

-- ============================================================
-- 3. 告警域：raw_alarm / alarm_event / linkage_record / ticket_link
--    四张表均按月 RANGE 分区；分区表唯一键必须含分区键（D-3），故均退化
-- ============================================================
CREATE TABLE cdcim.raw_alarm (
    raw_alarm_id       bigint       GENERATED ALWAYS AS IDENTITY,
    alarm_fingerprint  varchar(64)  NOT NULL,
    event_id           bigint,
    rule_id            bigint       NOT NULL,
    rule_version       integer      NOT NULL,
    device_id          bigint       NOT NULL,
    point_id           bigint,
    alarm_level        varchar(8)   NOT NULL,
    alarm_type         varchar(32)  NOT NULL,
    trigger_value      numeric(18,6),
    threshold_value    numeric(18,6),
    quality_code       smallint     NOT NULL DEFAULT 0,
    ts_event           timestamptz(3) NOT NULL,
    ts_detect          timestamptz(3) NOT NULL DEFAULT now(),
    window_start       timestamptz(3) NOT NULL,
    is_suppressed      boolean      NOT NULL DEFAULT false,
    suppress_type      varchar(16)  NOT NULL DEFAULT 'none',
    suppress_reason    varchar(128),
    is_derived         boolean      NOT NULL DEFAULT false,
    trace_id           varchar(32),
    raw_payload        jsonb,
    created_at         timestamptz(3) NOT NULL DEFAULT now(),
    is_archived        boolean      NOT NULL DEFAULT false,
    retention_until    timestamptz(3) NOT NULL,
    CONSTRAINT pk_raw_alarm PRIMARY KEY (raw_alarm_id),
    CONSTRAINT ck_raw_alarm_level CHECK (alarm_level IN ('critical','major','minor')),
    CONSTRAINT ck_raw_alarm_type  CHECK (alarm_type IN
        ('threshold','duration','rate','communication','linkage_failure','system','data_delay','forecast')),
    CONSTRAINT ck_raw_alarm_suppress CHECK (suppress_type IN ('none','self','parent_child')),
    CONSTRAINT ck_raw_alarm_quality  CHECK (quality_code BETWEEN 0 AND 3),
    CONSTRAINT ck_raw_alarm_time     CHECK (ts_detect >= ts_event),
    CONSTRAINT ck_raw_alarm_rulever  CHECK (rule_version >= 1)
) PARTITION BY RANGE (ts_detect);

-- D-3：唯一键必须含分区键，故退化为 (alarm_fingerprint, ts_detect)（决策 D-22）
CREATE UNIQUE INDEX uk_raw_alarm_fingerprint ON cdcim.raw_alarm (alarm_fingerprint, ts_detect);
CREATE INDEX idx_raw_alarm_dev_ts   ON cdcim.raw_alarm (device_id, ts_detect DESC);
CREATE INDEX idx_raw_alarm_event    ON cdcim.raw_alarm (event_id);
CREATE INDEX idx_raw_alarm_level_ts ON cdcim.raw_alarm (alarm_level, ts_detect DESC);
CREATE INDEX idx_raw_alarm_rule     ON cdcim.raw_alarm (rule_id, ts_detect DESC);

CREATE TABLE cdcim.alarm_event (
    event_id           bigint       GENERATED ALWAYS AS IDENTITY,
    event_code         varchar(32)  NOT NULL,
    event_title        varchar(128) NOT NULL,
    alarm_level        varchar(8)   NOT NULL,
    alarm_type         varchar(32)  NOT NULL,
    root_device_id     bigint,
    root_point_id      bigint,
    impact_scope       jsonb,
    event_status       varchar(16)  NOT NULL DEFAULT 'raised',
    first_occur_at     timestamptz(3) NOT NULL,
    last_occur_at      timestamptz(3) NOT NULL,
    recover_at         timestamptz(3),
    close_at           timestamptz(3),
    raw_count          integer      NOT NULL DEFAULT 0,
    suppressed_count   integer      NOT NULL DEFAULT 0,
    ack_by             bigint,
    ack_at             timestamptz(3),
    process_by         bigint,
    process_at         timestamptz(3),
    close_by           bigint,
    close_note         varchar(256),
    ticket_id          varchar(64),
    ai_suggest_status  varchar(16)  NOT NULL DEFAULT 'pending',
    ai_suggest_payload jsonb,
    ai_suggest_at      timestamptz(3),
    ai_feedback        varchar(8),
    suppress_expire_at timestamptz(3),
    created_at         timestamptz(3) NOT NULL DEFAULT now(),
    updated_at         timestamptz(3) NOT NULL DEFAULT now(),
    row_version        integer      NOT NULL DEFAULT 1,
    CONSTRAINT pk_alarm_event PRIMARY KEY (event_id),
    CONSTRAINT ck_alarm_event_level  CHECK (alarm_level IN ('critical','major','minor')),
    CONSTRAINT ck_alarm_event_type   CHECK (alarm_type IN
        ('threshold','duration','rate','communication','linkage_failure','system','data_delay','forecast')),
    CONSTRAINT ck_alarm_event_status CHECK (event_status IN
        ('raised','acknowledged','processing','recovered','closed')),
    CONSTRAINT ck_alarm_event_ai     CHECK (ai_suggest_status IN
        ('pending','ready','timeout','unavailable','filtered')),
    CONSTRAINT ck_alarm_event_fb     CHECK (ai_feedback IS NULL OR ai_feedback IN ('useful','useless')),
    CONSTRAINT ck_alarm_event_time   CHECK (last_occur_at >= first_occur_at),
    CONSTRAINT ck_alarm_event_recover CHECK (recover_at IS NULL OR recover_at >= first_occur_at),
    CONSTRAINT ck_alarm_event_close  CHECK (close_at IS NULL OR close_at >= first_occur_at),
    CONSTRAINT ck_alarm_event_count  CHECK (raw_count >= 0 AND suppressed_count >= 0),
    CONSTRAINT ck_alarm_event_ver    CHECK (row_version >= 1)
) PARTITION BY RANGE (first_occur_at);

-- D-3：event_code 已含日期（ALM-yyyymmdd-nnnn），加分区键后语义不变
CREATE UNIQUE INDEX uk_alarm_event_code ON cdcim.alarm_event (event_code, first_occur_at);
CREATE INDEX idx_alarm_event_status ON cdcim.alarm_event (event_status, alarm_level, last_occur_at DESC);
CREATE INDEX idx_alarm_event_root   ON cdcim.alarm_event (root_device_id);
CREATE INDEX idx_alarm_event_first  ON cdcim.alarm_event (first_occur_at DESC);
CREATE INDEX idx_alarm_event_ticket ON cdcim.alarm_event (ticket_id);

CREATE TABLE cdcim.linkage_record (
    linkage_id         bigint       GENERATED ALWAYS AS IDENTITY,
    request_id         varchar(64)  NOT NULL,
    event_id           bigint       NOT NULL,
    policy_id          bigint       NOT NULL,
    trigger_type       varchar(16)  NOT NULL,
    action_type        varchar(24)  NOT NULL,
    target_type        varchar(16)  NOT NULL,
    target_id          varchar(64)  NOT NULL,
    action_params      jsonb        DEFAULT '{}'::jsonb,
    exec_status        varchar(24)  NOT NULL DEFAULT 'pending_confirm',
    confirm_required   boolean      NOT NULL DEFAULT true,
    confirm_by         bigint,
    confirm_at         timestamptz(3),
    review_by          bigint,
    review_at          timestamptz(3),
    execute_at         timestamptz(3),
    result_code        varchar(32),
    result_message     varchar(256),
    retry_count        smallint     NOT NULL DEFAULT 0,
    execute_latency_ms integer,
    readback_status    varchar(32),
    readback_at        timestamptz(3),
    compensate_of      bigint,
    trace_id           varchar(32),
    created_at         timestamptz(3) NOT NULL DEFAULT now(),
    updated_at         timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_linkage_record PRIMARY KEY (linkage_id),
    CONSTRAINT ck_linkage_record_trigger CHECK (trigger_type IN ('auto_suggest','manual')),
    CONSTRAINT ck_linkage_record_action  CHECK (action_type IN
        ('door_hold_open','door_restore','camera_snapshot','camera_stream','video_wall')),
    CONSTRAINT ck_linkage_record_target  CHECK (target_type IN ('door','camera','nvr')),
    CONSTRAINT ck_linkage_record_status  CHECK (exec_status IN
        ('pending_confirm','confirmed','pending_review','rejected','expired','executing','success','failed','unknown')),
    CONSTRAINT ck_linkage_record_retry   CHECK (retry_count BETWEEN 0 AND 3),
    CONSTRAINT ck_linkage_record_latency CHECK (execute_latency_ms IS NULL OR execute_latency_ms >= 0),
    CONSTRAINT ck_linkage_record_confirm_required CHECK (confirm_required),  -- 恒 true，禁死自动执行（FR-M4-02）
    -- 提交人自审在库层直接拒绝（字典 §4.11）
    CONSTRAINT ck_linkage_record_review  CHECK (review_by IS NULL OR review_by <> confirm_by)
) PARTITION BY RANGE (created_at);

CREATE UNIQUE INDEX uk_linkage_record_request ON cdcim.linkage_record (request_id, created_at);
CREATE INDEX idx_linkage_record_event  ON cdcim.linkage_record (event_id);
CREATE INDEX idx_linkage_record_status ON cdcim.linkage_record (exec_status, created_at DESC);
CREATE INDEX idx_linkage_record_target ON cdcim.linkage_record (target_type, target_id, created_at DESC);

CREATE TABLE cdcim.ticket_link (
    link_id         bigint       GENERATED ALWAYS AS IDENTITY,
    event_id        bigint       NOT NULL,
    ticket_id       varchar(64)  NOT NULL,
    ticket_system   varchar(32)  NOT NULL DEFAULT 'itsm',
    ticket_type     varchar(32),
    priority        varchar(8),
    sync_status     varchar(16)  NOT NULL DEFAULT 'created',
    created_at      timestamptz(3) NOT NULL DEFAULT now(),
    last_sync_at    timestamptz(3),
    close_at        timestamptz(3),
    retry_count     smallint     NOT NULL DEFAULT 0,
    failure_reason  varchar(256),
    attachment_ref  jsonb,
    operator        bigint,
    updated_at      timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_ticket_link PRIMARY KEY (link_id),
    CONSTRAINT ck_ticket_link_sync  CHECK (sync_status IN ('created','sync_failed','synced','closed')),
    CONSTRAINT ck_ticket_link_prio  CHECK (priority IS NULL OR priority IN ('P1','P2','P3','P4')),
    CONSTRAINT ck_ticket_link_retry CHECK (retry_count BETWEEN 0 AND 3)
) PARTITION BY RANGE (created_at);

CREATE UNIQUE INDEX uk_ticket_link_event ON cdcim.ticket_link (event_id, ticket_id, created_at);
CREATE INDEX idx_ticket_link_sync   ON cdcim.ticket_link (sync_status, created_at);
CREATE INDEX idx_ticket_link_ticket ON cdcim.ticket_link (ticket_id);

-- ============================================================
-- 4. 审计 / 旁路 / 知识 / 能效
--    audit_log 不建外键（事件日志类，字典 §2.3）；actor_id 为快照值
-- ============================================================
CREATE TABLE cdcim.audit_log (
    audit_id         bigint       GENERATED ALWAYS AS IDENTITY,
    audit_no         varchar(32)  NOT NULL,
    operation_type   varchar(32)  NOT NULL,
    operation_result varchar(16)  NOT NULL,
    risk_level       varchar(8)   NOT NULL DEFAULT 'low',
    actor_id         bigint       NOT NULL,
    actor_name       varchar(64)  NOT NULL,
    actor_role       varchar(32)  NOT NULL,
    actor_ip         inet         NOT NULL,
    session_id       varchar(64),
    target_type      varchar(32)  NOT NULL,
    target_id        varchar(64),
    target_name      varchar(128),
    action_detail    jsonb,
    before_value     jsonb,
    after_value      jsonb,
    request_uri      varchar(256),
    trace_id         varchar(32),
    prev_hash        varchar(64),
    record_hash      varchar(64)  NOT NULL,
    created_at       timestamptz(3) NOT NULL DEFAULT now(),
    retention_until  timestamptz(3) NOT NULL,
    CONSTRAINT pk_audit_log PRIMARY KEY (audit_id),
    CONSTRAINT ck_audit_log_op CHECK (operation_type IN
        ('login','logout','create','update','delete','query','export','control',
         'rule_change','permission_change','config_change','ack','close')),
    CONSTRAINT ck_audit_log_result CHECK (operation_result IN ('success','failed','denied')),
    CONSTRAINT ck_audit_log_risk   CHECK (risk_level IN ('low','medium','high')),
    CONSTRAINT ck_audit_log_target CHECK (target_type IN
        ('device','point','alarm','rule','user','permission','linkage','knowledge','system'))
) PARTITION BY RANGE (created_at);

CREATE UNIQUE INDEX uk_audit_log_no ON cdcim.audit_log (audit_no, created_at);
CREATE INDEX brin_audit_log_created ON cdcim.audit_log USING brin (created_at) WITH (pages_per_range = 32);
CREATE INDEX idx_audit_log_actor  ON cdcim.audit_log (actor_id, created_at DESC);
CREATE INDEX idx_audit_log_type   ON cdcim.audit_log (operation_type, created_at DESC);
CREATE INDEX idx_audit_log_target ON cdcim.audit_log (target_type, target_id);
CREATE INDEX idx_audit_log_result ON cdcim.audit_log (operation_result) WHERE operation_result <> 'success';

CREATE TABLE cdcim.telemetry_late_bypass (
    bypass_id       bigint        GENERATED ALWAYS AS IDENTITY,
    device_id       bigint        NOT NULL,
    point_id        bigint        NOT NULL,
    ts              timestamptz(3) NOT NULL,
    val_double      numeric(18,6),
    quality         smallint      NOT NULL DEFAULT 2,
    late_seconds    integer       NOT NULL,
    original_window timestamptz(3) NOT NULL,
    received_at     timestamptz(3) NOT NULL DEFAULT now(),
    source_batch    varchar(32),
    trace_id        varchar(32),
    process_status  varchar(16)   NOT NULL DEFAULT 'pending',
    handled_at      timestamptz(3),
    remark          varchar(128),
    created_at      timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_telemetry_late_bypass PRIMARY KEY (bypass_id),
    CONSTRAINT ck_late_bypass_quality CHECK (quality BETWEEN 0 AND 3),
    CONSTRAINT ck_late_bypass_status  CHECK (process_status IN ('pending','merged','ignored')),
    CONSTRAINT ck_late_bypass_late    CHECK (late_seconds > 60),
    CONSTRAINT ck_late_bypass_recv    CHECK (received_at >= ts)
) PARTITION BY RANGE (ts);

CREATE UNIQUE INDEX uk_late_bypass ON cdcim.telemetry_late_bypass (device_id, point_id, ts);
CREATE INDEX idx_late_bypass_dev_ts ON cdcim.telemetry_late_bypass (device_id, ts DESC);
CREATE INDEX idx_late_bypass_status ON cdcim.telemetry_late_bypass (process_status)
    WHERE process_status = 'pending';

CREATE TABLE cdcim.knowledge_item (
    kb_id             bigint       GENERATED ALWAYS AS IDENTITY,
    kb_code           varchar(64)  NOT NULL,
    title             varchar(256) NOT NULL,
    doc_type          varchar(24)  NOT NULL,
    source_uri        varchar(512),
    source_version    varchar(32),
    content_text      text         NOT NULL,
    content_hash      varchar(64)  NOT NULL,
    tags              varchar[]    DEFAULT '{}',
    device_type_scope varchar(32),
    fault_type_scope  varchar(32),
    chunk_count       integer      NOT NULL DEFAULT 0,
    embedding_model   varchar(32),
    vector_collection varchar(32)  NOT NULL DEFAULT 'kb_docs',
    validity          varchar(16)  NOT NULL DEFAULT 'valid',
    effective_from    date,
    deprecated_at     timestamptz(3),
    author            varchar(64),
    reviewer          varchar(64),
    ingest_status     varchar(16)  NOT NULL DEFAULT 'pending',
    created_at        timestamptz(3) NOT NULL DEFAULT now(),
    updated_at        timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_knowledge_item PRIMARY KEY (kb_id),
    CONSTRAINT uk_knowledge_item_code UNIQUE (kb_code),
    CONSTRAINT uk_knowledge_item_hash UNIQUE (content_hash),
    CONSTRAINT ck_knowledge_item_type CHECK (doc_type IN
        ('manual','vendor_doc','ticket_case','sop','regulation')),
    CONSTRAINT ck_knowledge_item_valid CHECK (validity IN ('valid','deprecated','draft')),
    CONSTRAINT ck_knowledge_item_ingest CHECK (ingest_status IN ('pending','embedded','failed')),
    CONSTRAINT ck_knowledge_item_chunk CHECK (chunk_count >= 0)
);
CREATE INDEX gin_knowledge_item_tags ON cdcim.knowledge_item USING gin (tags);
CREATE INDEX idx_knowledge_item_scope ON cdcim.knowledge_item (device_type_scope, fault_type_scope, validity);
CREATE INDEX idx_knowledge_item_status ON cdcim.knowledge_item (ingest_status)
    WHERE ingest_status <> 'embedded';

CREATE TABLE cdcim.energy_metric_daily (
    metric_id        bigint        GENERATED ALWAYS AS IDENTITY,
    stat_date        date          NOT NULL,
    room_id          bigint        NOT NULL,
    metric_type      varchar(24)   NOT NULL,
    metric_value     numeric(12,4) NOT NULL,
    unit             varchar(16)   NOT NULL,
    sample_count     integer       NOT NULL DEFAULT 0,
    valid_count      integer       NOT NULL DEFAULT 0,
    confidence       numeric(5,4),
    calc_method      varchar(64),
    forecast_30d     numeric(12,4),
    forecast_slope   numeric(12,6),
    capacity_warn_at date,
    data_quality     varchar(16)   NOT NULL DEFAULT 'trusted',
    calc_at          timestamptz(3) NOT NULL DEFAULT now(),
    calc_version     integer       NOT NULL DEFAULT 1,
    created_at       timestamptz(3) NOT NULL DEFAULT now(),
    updated_at       timestamptz(3) NOT NULL DEFAULT now(),
    CONSTRAINT pk_energy_metric_daily PRIMARY KEY (metric_id),
    CONSTRAINT ck_energy_metric_type CHECK (metric_type IN
        ('pue','cooling_efficiency','load_rate','capacity_remain','it_power','total_power')),
    CONSTRAINT ck_energy_metric_quality CHECK (data_quality IN ('trusted','incomplete')),
    CONSTRAINT ck_energy_metric_counts CHECK (sample_count >= 0 AND valid_count BETWEEN 0 AND sample_count),
    CONSTRAINT ck_energy_metric_conf CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    CONSTRAINT ck_energy_metric_ver  CHECK (calc_version >= 1),
    CONSTRAINT fk_energy_metric_room FOREIGN KEY (room_id)
        REFERENCES cdcim.space_node (node_id) ON DELETE RESTRICT
) PARTITION BY RANGE (stat_date);

CREATE UNIQUE INDEX uk_energy_metric ON cdcim.energy_metric_daily
    (room_id, stat_date, metric_type, calc_version);
CREATE INDEX idx_energy_metric_room_date ON cdcim.energy_metric_daily (room_id, stat_date DESC);
CREATE INDEX idx_energy_metric_type      ON cdcim.energy_metric_daily (metric_type, stat_date DESC);

-- ============================================================
-- 5. 分区创建（§5 / §6.1）
--    7 张分区表：6 张月分区（raw_alarm / alarm_event / linkage_record /
--    ticket_link / audit_log / telemetry_late_bypass）+ 1 张年分区（energy_metric_daily）
--    先建 DEFAULT 兜底，再预建区间；上界之外的新分区须先 DETACH DEFAULT（§6.2）
-- ============================================================
CREATE TABLE cdcim.p_raw_alarm_default PARTITION OF cdcim.raw_alarm DEFAULT;
CREATE TABLE cdcim.p_alarm_event_default PARTITION OF cdcim.alarm_event DEFAULT;
CREATE TABLE cdcim.p_linkage_record_default PARTITION OF cdcim.linkage_record DEFAULT;
CREATE TABLE cdcim.p_ticket_link_default PARTITION OF cdcim.ticket_link DEFAULT;
CREATE TABLE cdcim.p_audit_log_default PARTITION OF cdcim.audit_log DEFAULT;
CREATE TABLE cdcim.p_late_bypass_default PARTITION OF cdcim.telemetry_late_bypass DEFAULT;
CREATE TABLE cdcim.p_energy_metric_daily_default PARTITION OF cdcim.energy_metric_daily DEFAULT;

DO $do$
DECLARE
    m timestamptz;
    y date;
BEGIN
    -- 6 张月分区表：预建 14 个月（覆盖在线留存 + 缓冲）
    FOR m IN SELECT generate_series(timestamptz '2026-11-01 00:00+00',
                                    timestamptz '2027-11-01 00:00+00',
                                    interval '1 month') LOOP
        EXECUTE format('CREATE TABLE IF NOT EXISTS cdcim.p_raw_alarm_%s '
                       'PARTITION OF cdcim.raw_alarm FOR VALUES FROM (%L) TO (%L)',
                       to_char(m AT TIME ZONE 'UTC','YYYYMM'), m, m + interval '1 month');
        EXECUTE format('CREATE TABLE IF NOT EXISTS cdcim.p_alarm_event_%s '
                       'PARTITION OF cdcim.alarm_event FOR VALUES FROM (%L) TO (%L)',
                       to_char(m AT TIME ZONE 'UTC','YYYYMM'), m, m + interval '1 month');
        EXECUTE format('CREATE TABLE IF NOT EXISTS cdcim.p_linkage_record_%s '
                       'PARTITION OF cdcim.linkage_record FOR VALUES FROM (%L) TO (%L)',
                       to_char(m AT TIME ZONE 'UTC','YYYYMM'), m, m + interval '1 month');
        EXECUTE format('CREATE TABLE IF NOT EXISTS cdcim.p_ticket_link_%s '
                       'PARTITION OF cdcim.ticket_link FOR VALUES FROM (%L) TO (%L)',
                       to_char(m AT TIME ZONE 'UTC','YYYYMM'), m, m + interval '1 month');
        EXECUTE format('CREATE TABLE IF NOT EXISTS cdcim.p_late_bypass_%s '
                       'PARTITION OF cdcim.telemetry_late_bypass FOR VALUES FROM (%L) TO (%L)',
                       to_char(m AT TIME ZONE 'UTC','YYYYMM'), m, m + interval '1 month');
    END LOOP;

    -- 审计表按月、预建 17 个月（16 个月滚动 + 1 个月缓冲）
    FOR m IN SELECT generate_series(timestamptz '2026-11-01 00:00+00',
                                    timestamptz '2028-03-01 00:00+00',
                                    interval '1 month') LOOP
        EXECUTE format('CREATE TABLE IF NOT EXISTS cdcim.p_audit_log_%s '
                       'PARTITION OF cdcim.audit_log FOR VALUES FROM (%L) TO (%L)',
                       to_char(m AT TIME ZONE 'UTC','YYYYMM'), m, m + interval '1 month');
    END LOOP;

    -- 能效按年、预建 6 年
    FOR y IN SELECT generate_series(date '2026-01-01', date '2032-01-01', interval '1 year')::date LOOP
        EXECUTE format('CREATE TABLE IF NOT EXISTS cdcim.p_energy_metric_daily_%s '
                       'PARTITION OF cdcim.energy_metric_daily FOR VALUES FROM (%L) TO (%L)',
                       to_char(y,'YYYY'), y, (y + interval '1 year')::date);
    END LOOP;
END
$do$;

-- ============================================================
-- 6. updated_at 触发器（13 张带 updated_at 的表）
-- ============================================================
DO $do$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['point_def','device','space_node','topology_link','alarm_rule',
                             'alarm_event','linkage_record','ticket_link','knowledge_item',
                             'user_account','role','permission','energy_metric_daily'] LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS tg_%s_touch ON cdcim.%I', t, t);
        EXECUTE format('CREATE TRIGGER tg_%s_touch BEFORE UPDATE ON cdcim.%I '
                       'FOR EACH ROW EXECUTE FUNCTION cdcim.fn_touch_updated_at()', t, t);
    END LOOP;
END
$do$;

-- ============================================================
-- 7. 审计表只追加：行级触发器 + 语句级 TRUNCATE 拦截 + 权限回收
-- ============================================================
CREATE OR REPLACE FUNCTION cdcim.fn_audit_log_append_only() RETURNS trigger
LANGUAGE plpgsql AS $fn$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only, % denied', TG_OP USING ERRCODE = '42501';
END
$fn$;

CREATE TRIGGER tg_audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON cdcim.audit_log
    FOR EACH ROW EXECUTE FUNCTION cdcim.fn_audit_log_append_only();

CREATE TRIGGER tg_audit_log_no_truncate
    BEFORE TRUNCATE ON cdcim.audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION cdcim.fn_audit_log_append_only();

-- 业务账号只授予 INSERT / SELECT（角色名以实际部署为准）
-- REVOKE UPDATE, DELETE, TRUNCATE ON cdcim.audit_log FROM cdcim_app;
-- GRANT  INSERT, SELECT          ON cdcim.audit_log TO   cdcim_app;

-- ============================================================
-- 待验证清单（首次建库时逐条确认，均为版本兼容问题，不影响表结构）
--   1. 分区表上的 GENERATED ALWAYS AS IDENTITY —— 需 PG 11+；
--      若目标实例拒绝，改写为独立 CREATE SEQUENCE + DEFAULT nextval(...)
--   2. 分区表上的 BEFORE ... FOR EACH ROW 触发器 —— 需 PG 13+；
--      本脚本在 PG 16 上应可用，未实测
--   3. BEFORE TRUNCATE FOR EACH STATEMENT 不随分区继承（审计表各月分区需单独建）
--   4. shared_preload_libraries=pg_stat_statements 需镜像内已安装该扩展（官方镜像已含）
-- ============================================================
