-- 本地复刻版数据库结构（与远程平台模型对齐）
-- 方言：SQLite（本地验证/开发）；迁移到 MySQL/PostgreSQL 时把 TEXT/INTEGER 换成等价类型即可。
-- 对应说明见 REMOTE_ACCESS_SPEC.md

PRAGMA foreign_keys = ON;

-- ============ 0. 账号与租户（对齐远端 JWT.AuthInfo） ============
CREATE TABLE IF NOT EXISTS tenant (
    id            TEXT PRIMARY KEY,              -- 远端 TenantId
    name          TEXT
);

CREATE TABLE IF NOT EXISTS app_user (
    id              TEXT PRIMARY KEY,            -- 远端 UserId
    tenant_id       TEXT REFERENCES tenant(id),
    username        TEXT UNIQUE NOT NULL,
    password_md5    TEXT NOT NULL,               -- 远端就是 md5(明文)，本地复刻沿用以便兼容客户端
    real_name       TEXT,
    is_super_admin  INTEGER NOT NULL DEFAULT 0,
    auth_codes      TEXT,                        -- JSON 数组，对应远端 AuthCodes
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- ============ 1. 数据集 / 资源类型 ============
CREATE TABLE IF NOT EXISTS dataset (
    id              TEXT PRIMARY KEY,
    name            TEXT,
    description     TEXT,
    resource_count  INTEGER DEFAULT 0
);

-- 资源类型字典（远端 Type / BigResourceType 编码，实测值）
CREATE TABLE IF NOT EXISTS resource_type (
    type_code       INTEGER PRIMARY KEY,
    type_name       TEXT NOT NULL,               -- 中文名，如「航空倾斜」
    big_type        INTEGER NOT NULL,            -- BigResourceType
    type_key        TEXT,                         -- /api/Resource/GetByPage 的 TypeKey，如 RasterTMS
    retrievable     TEXT NOT NULL,               -- full | partial | service | none
    note            TEXT
);

-- ============ 2. 资源（对齐远端 42 字段） ============
CREATE TABLE IF NOT EXISTS resource (
    id                      TEXT PRIMARY KEY,
    label                   TEXT,
    description             TEXT,
    type_code               INTEGER NOT NULL REFERENCES resource_type(type_code),
    type_name               TEXT,
    big_resource_type       INTEGER,
    dataset_id              TEXT REFERENCES dataset(id),
    tenant_id               TEXT REFERENCES tenant(id),
    name                    TEXT,                -- 项目/场景名，如「2204武大数据」
    business_tag            TEXT,
    metadata_json           TEXT,                -- 远端 MetaData（JSON 字符串）
    range_wkt               TEXT,                -- 远端 Range：注意是「相机中心/模型 XY 外接矩形」，非地面覆盖
    epsg_id                 INTEGER,             -- 远端字段不可靠，以 original_root_file 内的 SRS 为准
    srs_id                  TEXT,
    srs_definition          TEXT,
    range_wkt_by_srs_origin TEXT,
    original_root_file_id   TEXT,                -- FastDFS 键（.db3 / tileset.json）
    data_url                TEXT,                -- wfs 指向内网 GeoServer
    wfs_data_url            TEXT,
    guide_map               TEXT,
    file_size               INTEGER,             -- 字节
    file_count              INTEGER,
    upload_time             TEXT,
    data_production_time    TEXT,
    is_upload_completed     INTEGER DEFAULT 0,
    is_deleted              INTEGER DEFAULT 0,
    is_disabled             INTEGER DEFAULT 0,
    is_generic              INTEGER DEFAULT 0,
    is_checked              INTEGER DEFAULT 0,
    is_modify_data          INTEGER DEFAULT 0,
    is_edge_match           INTEGER DEFAULT 0,
    is_physical_deleted     INTEGER DEFAULT 0,
    is_other_solution_task_resource INTEGER DEFAULT 0,
    resource_handle_state   INTEGER,             -- 0 正常 / 5 见注释
    resource_handle_state_name TEXT,
    resource_request_statistics_num INTEGER DEFAULT 0,
    create_by_id            TEXT,
    edit_by_id              TEXT,
    application_id          TEXT,
    legacy_num              INTEGER,              -- 远端 num 字段（递增编号）
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_resource_type   ON resource(type_code);
CREATE INDEX IF NOT EXISTS idx_resource_dataset ON resource(dataset_id);
CREATE INDEX IF NOT EXISTS idx_resource_root   ON resource(original_root_file_id);

-- ============ 3. 文件对象（替代 FastDFS） ============
-- 本地用「存储键」抽象路径；kind 区分根文件/瓦片/影像/XML/快照等
CREATE TABLE IF NOT EXISTS file_object (
    key             TEXT PRIMARY KEY,            -- 形如 group1/M00/47/D3/wKgCAmpUQFaAdY2KAD0BIOQ3DkU131.jpg
    resource_id     TEXT REFERENCES resource(id),
    dataset_id      TEXT REFERENCES dataset(id),
    kind            TEXT NOT NULL,               -- incline_index|tileset_root|tile_json|tile_b3dm|tile_pnts|
                                                 -- photo|photo_xml|video_snapshot|video|tms_root|bbox_db3
    relative_path   TEXT,                        -- 3dtiles 内部相对路径（Data/Tile_+000_+011/...json）
    bytes           INTEGER,
    sha256          TEXT,
    content_type    TEXT,
    local_path      TEXT,                        -- 本地落盘相对路径
    status          TEXT DEFAULT 'pending',      -- pending|ok|missing|failed
    extra_json      TEXT,                        -- 任意附加信息（如瓦片层级、瓦片行列）
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_file_resource ON file_object(resource_id, kind);
CREATE INDEX IF NOT EXISTS idx_file_relpath  ON file_object(relative_path);

-- ============ 4. 倾斜影像（DB3 索引内容） ============
CREATE TABLE IF NOT EXISTS photo_group (
    resource_id     TEXT NOT NULL REFERENCES resource(id),
    group_id        INTEGER NOT NULL,            -- 远端 DB3 PhotoGroup.Id（1..5）
    group_name      TEXT,
    photo_type      TEXT,                        -- Aerial
    description     TEXT,
    PRIMARY KEY (resource_id, group_id)
);

CREATE TABLE IF NOT EXISTS photo (
    resource_id     TEXT NOT NULL REFERENCES resource(id),
    photo_id        INTEGER NOT NULL,            -- 远端 DB3 Photo.Id
    file_key        TEXT REFERENCES file_object(key),
    group_id        INTEGER,
    row_no          INTEGER,
    col_no          INTEGER,
    width           REAL,
    height          REAL,
    focal_length    REAL,                        -- 像素
    pixel_size      REAL,
    principal_x     REAL,
    principal_y     REAL,
    dist_k1 REAL, dist_k2 REAL, dist_k3 REAL, dist_k4 REAL, dist_p1 REAL, dist_p2 REAL,
    x REAL, y REAL, z REAL,                      -- 相机中心（资源 SRS 内，m）
    phi REAL, omega REAL, kappa REAL,
    rotation_sys    INTEGER,
    rpc             TEXT,
    model_matrix    TEXT,                        -- GL 视图矩阵（16 个数，分号分隔）
    project_matrix  TEXT,
    ori_flag        INTEGER,
    is_cc_xml_data  INTEGER,
    status          TEXT DEFAULT 'pending',
    bytes           INTEGER,
    sha256          TEXT,
    local_path      TEXT,
    PRIMARY KEY (resource_id, photo_id)
);
CREATE INDEX IF NOT EXISTS idx_photo_status ON photo(status);
CREATE INDEX IF NOT EXISTS idx_photo_group  ON photo(resource_id, group_id);

-- 空三位姿（来自 BlocksExchange XML；比 DB3 更权威，含四元/矩阵与组内参）
CREATE TABLE IF NOT EXISTS camera_group (
    resource_id     TEXT NOT NULL REFERENCES resource(id),
    group_id        INTEGER NOT NULL,
    width           INTEGER, height INTEGER,
    focal_mm        REAL, sensor_mm REAL, f_px REAL,
    principal_x     REAL, principal_y REAL,
    camera_model    TEXT,                        -- ILCE-6000 / PMA2635 ...
    camera_orientation TEXT,                     -- XRightYDown
    PRIMARY KEY (resource_id, group_id)
);

CREATE TABLE IF NOT EXISTS photo_pose (
    resource_id     TEXT NOT NULL REFERENCES resource(id),
    photo_id        INTEGER,
    group_id        INTEGER,
    file_key        TEXT,
    cx REAL, cy REAL, cz REAL,
    m00 REAL, m01 REAL, m02 REAL, m10 REAL, m11 REAL, m12 REAL, m20 REAL, m21 REAL, m22 REAL,
    PRIMARY KEY (resource_id, photo_id)
);

CREATE TABLE IF NOT EXISTS resource_srs (
    resource_id     TEXT PRIMARY KEY REFERENCES resource(id),
    srs_definition  TEXT,                        -- EPSG:4547 / EPSG:4326 / ENU:lat,lon
    epsg            INTEGER,
    enu_origin_lat  REAL,
    enu_origin_lon  REAL
);

-- ============ 5. 3dtiles / TMS ============
CREATE TABLE IF NOT EXISTS tileset_node (
    resource_id     TEXT NOT NULL REFERENCES resource(id),
    node_id         TEXT NOT NULL,               -- 相对路径或瓦片名
    parent_id       TEXT,
    level           INTEGER,
    geometric_error REAL,
    bv_type         TEXT,                        -- sphere | box | region
    bv_json         TEXT,                        -- 原始 boundingVolume
    transform_json  TEXT,
    content_uri     TEXT,                        -- 原始相对 uri
    content_file    TEXT REFERENCES file_object(key),
    PRIMARY KEY (resource_id, node_id)
);

CREATE TABLE IF NOT EXISTS tile_scheme (       -- TMS/WMTS 切片方案（供复刻瓦片服务）
    resource_id     TEXT PRIMARY KEY REFERENCES resource(id),
    srid            INTEGER, min_level INTEGER, max_level INTEGER,
    tile_size       INTEGER DEFAULT 256, extension TEXT, mime TEXT,
    xmin REAL, ymin REAL, xmax REAL, ymax REAL, resolution REAL,
    root_file       TEXT
);

-- ============ 6. 面/区筛选（AoI） ============
CREATE TABLE IF NOT EXISTS aoi (
    id          TEXT PRIMARY KEY,                -- 名称
    name        TEXT,
    polygon_wkt TEXT NOT NULL,                   -- WGS84
    bbox_json   TEXT,
    ground_z    REAL, agl REAL, buffer_m REAL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS aoi_photo (
    aoi_id          TEXT NOT NULL REFERENCES aoi(id),
    resource_id     TEXT NOT NULL,
    photo_id        INTEGER NOT NULL,
    group_id        INTEGER,
    footprint_wkt   TEXT,
    agl             REAL,
    PRIMARY KEY (aoi_id, resource_id, photo_id)
);

-- ============ 7. 运维 ============
CREATE TABLE IF NOT EXISTS download_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT,                                 -- remote|local-migration
    resource_id TEXT, kind TEXT, target TEXT,
    status TEXT, bytes INTEGER, detail TEXT
);

CREATE TABLE IF NOT EXISTS resource_duplicate (
    dup_id       TEXT PRIMARY KEY REFERENCES resource(id),
    canonical_id TEXT REFERENCES resource(id),
    matched_photos INTEGER,
    note TEXT
);

-- ============ 8. 初始字典数据（实测编码） ============
INSERT OR REPLACE INTO resource_type(type_code, type_name, big_type, type_key, retrievable, note) VALUES
 (4000001,  '航空倾斜',        4,  NULL,        'full',    'DB3 索引 + 全部原始影像 + 空三 XML 可完整下载'),
 (1000012,  '地形3dtiles',     1,  NULL,        'partial', '仅 tileset.json 根文件；分块 json/b3dm 无键映射'),
 (13000006, '点云3dtiles',     13, NULL,        'partial', '仅 tileset.json 根文件；分块 json/pnts 无键映射'),
 (12000008, 'Raster TMS切片',  12, 'RasterTMS', 'none',    'OriginalRootFileId 为空，无切片服务地址'),
 (10000004, '定向动态视频',     10, NULL,        'partial', '仅首帧快照 PNG；MP4 无文件键'),
 (12000004, '栅格数据WMS服务',  12, NULL,        'service', '仅服务元数据'),
 (12000005, '栅格数据WMTS服务', 12, NULL,        'service', '仅服务元数据'),
 (2000004,  'wfs',             2,  NULL,        'service', 'DataUrl 指向内网 GeoServer(192.168.2.2:8080)');
