# PostGIS 空间数据服务

网页和脚本统一通过 `scripts/upload_data_package.py` 上传 `vector` 数据包文件夹或 ZIP；包根目录契约和网页入口见[统一数据包契约](data-package-contracts.md)。本页保留的 multipart 和 `upload_spatial.py` 示例描述底层兼容接口；新导入请使用统一数据包流程。

7110 在现有文件与兼容接口之外，提供 `/spatial` API。PostgreSQL 16 + PostGIS 3.5 运行在独立的 `deqing-postgis` 容器，仅加入 `deqing_default` 内网，不映射数据库端口。持久化卷为 `deqing_postgis_data`。现有 `garden` 版本数据和旧平台镜像仍按各自原有方式存储。

## 数据模型

- `spatial_dataset`：数据集名称、原始文件名、原始 `.prj` WKT/EPSG、DBF 编码、字段定义、要素数、WGS84 水平边界框、坐标维数、Z 范围、垂直 CRS/单位声明、位置审查记录及导入时间。
- `spatial_feature`：源记录序号、DBF 属性 `jsonb`、实际空间列 `geom geometry`，由约束保证 SRID 为 4326 且维数为 2 或 3。`geom` 有 GiST 索引，属性有 GIN 索引。
- `user_annotation`：个人标绘名称、`jsonb` 属性、`geom geometry(Geometry, 4326)`、创建/更新时间及所有者 UUID。所有者和几何都有索引。
- 原始文件组保存在 7110 容器卷的 `/workspace/data/spatial/sources/<数据集UUID>/`，便于追溯；查询使用 PostGIS 中的 geometry，不逐次解析 Shapefile。

导入时按 `.prj` 识别原始水平坐标系，经 `pyproj` 将 X/Y 转到 EPSG:4326。管理员必须提交位置依据并确认 CRS、轴顺序和转换后范围；服务把依据、确认角色和时间戳保存在 `spatial_dataset.location_review`。导入还会拒绝没有几何或含无效几何的数据。对于 PointZ、PolylineZ、PolygonZ、MultiPointZ，Z 原值保存在 PostGIS 三维 geometry 中，不参与水平投影；如果缺少垂直 CRS，服务会明确记录 `vertical_reference_status: "unknown"`，同时保留 Z 范围，但不会把 Z 解释成米制海拔或椭球高。可通过 `vertical_crs` 声明垂直坐标系、通过 `vertical_unit` 声明单位；这两个字段只记录来源信息，不会转换 Z。若 `.prj` 自身是含垂直分量的 compound CRS，则自动记录该垂直 CRS。

查询的 `bbox`、相交计算和 `bounds` 都只使用水平 X/Y（EPSG:4326），保持已有二维调用兼容；GeoJSON 响应会在三维数据上附带第三个坐标值。高程范围筛选尚未启用，因为不同数据的 Z 基准可能不同。若缺 `.prj`，需提供 `source_srid`。`.cpg` 可选；没有 `.cpg` 时默认 UTF-8，也可通过 `encoding` 指定。当前支持 2D/XYZ Point、Polyline、Polygon、MultiPoint；M-only 和 MultiPatch 类型会明确拒绝。Z 类型中可选的 M（measure）值不属于坐标维数，不导入为 Z，也不会改变保留下来的 XYZ。已有二维数据保留为 2D，不需要重新导入。

## 用户标绘

标绘是账户私有数据，与公共 Shapefile 数据集分开存储。所有请求都必须使用 `/api/Login/Login` 签发且仍有效的账户 JWT；静态 admin/reader 服务 Bearer 没有用户归属，不能访问该 API。普通用户和管理员都可对自己的标绘进行增删改查；管理员权限不包含读取其他用户标绘。

服务端从 JWT 对应的有效会话读取 `owner_user_id`，绝不接受请求体中的所有者字段。列表查询、单条读取、更新和删除均带 `owner_user_id = 当前用户` 条件；尝试访问别人的 UUID 会返回 404，不透露记录是否存在。每个账户的多次登录有独立会话令牌，登出只撤销发起请求的那个令牌。

| 方法与路径 | 用途 |
| --- | --- |
| `GET /spatial/annotations?bbox=minLon,minLat,maxLon,maxLat&limit=100&offset=0` | 只列出当前账户标绘，返回 GeoJSON FeatureCollection；`bbox` 可省略 |
| `POST /spatial/annotations/query` | JSON：提供 `bbox` 数组和/或 `geometry` GeoJSON Geometry，返回与范围相交的当前账户标绘 |
| `POST /spatial/annotations` | 新建标绘，JSON：`{"name":"点位","geometry":{"type":"Point","coordinates":[120.1,30.2]},"properties":{"color":"red"}}`；服务端生成 UUID 与 owner |
| `GET /spatial/annotations/{id}` | 读取当前账户的一条标绘 |
| `PATCH /spatial/annotations/{id}` | 更新 `name`、`geometry`、`properties` 中的一个或多个字段；不接受 owner |
| `DELETE /spatial/annotations/{id}` | 删除当前账户的一条标绘，成功返回 204 |

响应是 GeoJSON Feature；`properties.name` 为标绘名称，其余属性来自请求的 `properties`。列表分页每页最多 1000 条。不同账户即使知道对方标绘 UUID，也无法读取、更新或删除对方数据。

需要用任意多边形查标绘时，`POST /spatial/annotations/query` 接受 bbox 与 GeoJSON Geometry，也可同时提供两者（结果必须同时满足）。例如：

```json
{
  "geometry": {
    "type": "Polygon",
    "coordinates": [[[120.0,30.0],[120.2,30.0],[120.2,30.2],[120.0,30.2],[120.0,30.0]]]
  },
  "limit": 100,
  "offset": 0
}
```

## 鉴权与接口

导入只能由管理员执行：可用 `/workspace/data/credentials.json` 中的 `admin` Bearer，也可用内置 admin 登录后获得的 JWT。普通注册账号的 JWT 和 `reader` Bearer 仅可查询。所有接口可直接访问 `http://192.168.150.41:7110`，或通过前端代理访问 `http://192.168.150.41:7010/legacy-data`。

| 接口 | 用途 |
| --- | --- |
| `GET /spatial/healthz` | PostGIS 连接与版本 |
| `POST /spatial/datasets/import` | `multipart/form-data` 上传同名 `.shp/.shx/.dbf`，附 `.prj/.cpg`；必填 `location_review_confirmed=true` 和 `location_review_source`；返回新数据集元数据及位置审查记录 |
| `GET /spatial/datasets?limit=100&offset=0` | 分页列出数据集 |
| `GET /spatial/datasets/{id}` | 数据集元数据 |
| `GET /spatial/datasets/{id}/features?bbox=114,30,115,31&limit=100&offset=0` | 返回 GeoJSON FeatureCollection；`bbox` 可省略 |
| `POST /spatial/datasets/{id}/query` | JSON：可选 `bbox` 数组、`geometry` GeoJSON Geometry、`properties` 属性包含条件、`limit`、`offset`。空间条件为相交 |
| `GET /spatial/datasets/{id}/features/{feature_id}` | 返回单条 GeoJSON Feature |

列表每次最多返回 1000 条；响应包含 `has_more` 以便继续翻页。`geometry` 查询对象仅接受 GeoJSON Geometry，不接受 Feature 或 FeatureCollection。

导入示例（文件扩展名必须匹配，各文件基本名相同）：

7010 的 Nginx 请求体上限为 1050 MiB；7110 的 Shapefile 文件组总计上限默认 4 GiB。浏览器通过 Agent 会话上传的 ZIP 上限为 1 GiB，智能体会流式校验并转发；直接使用本接口时，超过 1050 MiB 的文件组应直连 7110。

```bash
curl -X POST 'http://192.168.150.41:7110/spatial/datasets/import' \
  -H "Authorization: Bearer $ADMIN_TOKEN" \
  -F 'name=示例矢量数据' \
  -F 'location_review_confirmed=true' \
  -F 'location_review_source=已核对项目坐标系说明文件与 Shapefile 范围' \
  -F 'shp=@sample.shp' -F 'shx=@sample.shx' -F 'dbf=@sample.dbf' \
  -F 'prj=@sample.prj' -F 'cpg=@sample.cpg'
```

旧的直接上传脚本调用示例如下；服务端仍保留该底层兼容接口。网页和新脚本请使用上方统一的数据包入口：

```bash
python scripts/upload_spatial.py /path/to/sample.shp \
  --token-file /path/to/credentials.json \
  --confirm-location-review \
  --location-review-source "已核对项目坐标系说明文件与 Shapefile 范围"
```

仅在 XYZ 数据且确实知道其垂直基准时提供 `vertical_crs`/`vertical_unit`。例如输入中的 Z 如果只是未注明基准的局部重建高度，应省略这两个参数；数据集会保留 Z 数值并标记垂直基准未知。空间查询仍是水平范围相交，返回几何的坐标为 `[longitude, latitude, z]`。

空间查询示例：

```bash
curl -H "Authorization: Bearer $READER_TOKEN" \
  'http://192.168.150.41:7110/spatial/datasets/<数据集UUID>/features?bbox=114,30,115,31&limit=100'
```

## 命令行脚本

项目中的 [`scripts/upload_data_package.py`](../scripts/upload_data_package.py) 与 [`scripts/read_spatial.py`](../scripts/read_spatial.py) 只需 Python 3.10+。矢量数据的 `package.json` 模板见 [`examples/data-packages/vector`](../examples/data-packages/vector/README.md)。两者默认访问 `http://192.168.150.41:7110`；也可用 `--base-url` 指向其他部署。上传较大的 `.dbf` 时应直连 7110，不经过 7010 的上传限制。

脚本接受 `--token-file` 指向纯文本令牌，或服务的 `credentials.json`。在有 SSH 权限的工作站上，可将凭据安全复制到本机（文件仅自己可读）：

```bash
umask 077
ssh 192.168.150.41 'docker exec deqing-data cat /workspace/data/credentials.json' > ~/.deqing-credentials.json
```

下面旧命令保留为兼容底层上传工具；新的数据入库请使用统一 `upload_data_package.py` 和标准 `vector` 数据包：

```bash
python3 scripts/upload_spatial.py /path/to/roads.shp --token-file ~/.deqing-credentials.json
python3 scripts/upload_spatial.py /path/to/elevation_points.shp --token-file ~/.deqing-credentials.json --vertical-crs EPSG:5703
python3 scripts/upload_spatial.py /home/thd/Downloads/shp --token-file ~/.deqing-credentials.json --skip-existing
```

目录模式识别目录第一层的每个 `.shp` 及同名 `.shx/.dbf`，附带上传 `.prj/.cpg`。若缺 `.prj`，传 `--source-srid 4326`。已存在同名数据集时默认报错；`--skip-existing` 仅在名称和记录数均匹配时跳过，便于中断后继续。它不校验文件内容变化，更新数据应使用新的数据集名称。上传成功会输出数据集 ID 和要素数。

列出数据集、查看元数据、按边界框分页读取或导出全部 GeoJSON：

```bash
python3 scripts/read_spatial.py --token-file ~/.deqing-credentials.json list
DATASET_ID='从 list 输出中选择的 UUID'
python3 scripts/read_spatial.py --token-file ~/.deqing-credentials.json get "$DATASET_ID"
python3 scripts/read_spatial.py --token-file ~/.deqing-credentials.json features "$DATASET_ID" --bbox 119.2,30.3,120.5,31.2 --limit 100
python3 scripts/read_spatial.py --token-file ~/.deqing-credentials.json features "$DATASET_ID" --all --limit 1000 --output features.geojson
```

不使用令牌文件时，上传脚本读取 `DEQING_ADMIN_TOKEN`，读取脚本读取 `DEQING_READER_TOKEN`；两者也接受通用的 `DEQING_TOKEN`。`--all` 按页获取并逐条写出 GeoJSON，不一次把全部要素放进内存。省略 `--all` 时只取 `--offset` 指定的一页。

## 部署与备份

数据库 Compose 文件是 [`deploy/compose.postgis.yaml`](../deploy/compose.postgis.yaml)。服务器上 `/home/hj/deqing-dataset-system/private/postgis.env` 保存 PostGIS 启动密码，`/workspace/data/postgis_dsn` 保存服务端 DSN；二者均仅所有者可读。`ensure_service.sh` 启动 7110 时传入 `DEQING_POSTGIS_DSN_FILE`。数据库不可用时，`/spatial` 返回 503，原有 7110 接口继续运行。

备份需同时覆盖 `deqing_postgis_data` 中的数据库和 `/workspace/data/spatial/sources` 原始文件。数据库可使用 `pg_dump -Fc -U deqing_spatial deqing_spatial` 制作逻辑备份。
