# 点云服务接口

点云接口前缀为 `/pointcloud`，使用当前 7110 服务的 Bearer JWT。普通注册用户只能读取已发布资源、分类、任务结果和文件；只有管理员可以登记资源、维护分类、创建版本、上传文件、登记分割任务、更新任务状态和发布版本。点云接口不接受客户端指定的用户归属。此规则与个人标绘的用户隔离权限分开。

网页和脚本将点云/模型作为统一 `deqing.data-package.v1` 数据包上传，见[统一数据包契约](data-package-contracts.md)。下文 `/pointcloud/.../files` 与 `/pointcloud/uploads/...` 是底层资源 API，保留供兼容客户端和服务端适配器使用；新入库请使用一个文件夹或 ZIP 的统一上传入口。

## 数据组织

- **资源**代表一个可持续演进的点云数据项。
- 新资源必须提供有效源 CRS、可信的 `spatial_extent`（WGS84 GeoJSON Geometry）和顶层 `location_review` 确认记录。没有可核验覆盖范围时不能登记或发布；单个相机点不能替代点云/瓦片的覆盖范围。
- `location_review` 必须声明 `confirmed: true`、与资源 CRS 相同的 `source_crs`、位置依据 `evidence`、范围计算方法 `extent_method`，以及固定的 `extent_coordinate_order: "longitude,latitude"`。服务端会保存审查字段、管理员角色和服务器时间戳；修改 CRS 或范围必须重新审查。
- **版本**是不可变的文件包，格式为 `ply` 或 `3dtiles`，用途为 `source` 或 `segmentation`。
- **分类体系**是版本化的类别定义，类别包含 `id`、`code`、`name`、十六进制 `color` 和说明。
- **任务**保存算法名称/版本、参数、状态、进度和输出版本关联。7110 管理任务记录，不在服务进程里执行语义分割算法；外部算法收到任务后处理文件，再由管理员身份把结果上传并发布。
- 文件实际保存在数据卷 `pointcloud/files/{resource_id}/{version_id}/`；分块临时文件放在 `pointcloud/.uploads/`。数据库记录文件清单、大小、MIME 类型和 SHA-256。源 PLY 保持算法返回的 PLY 字节不变。
- 3D Tiles 版本必须包含入口 `tileset.json`。发布时会解析其中的相对 URI，拒绝包外路径、外部 URL 和缺失文件。所有子文件通过同版本文件路径读取。

## 接口与角色

所有成功的 JSON 响应使用 UTF-8。写接口均需管理员 JWT；读接口需任何有效账户 JWT（管理员、普通用户）或配置的只读 Bearer。普通用户只能看到已发布版本；管理员还可以看到草稿和未完成任务。

| 方法与路径 | 角色 | 用途 |
| --- | --- | --- |
| `POST /pointcloud/resources` | admin | 创建资源 |
| `GET /pointcloud/spatial-resources?bbox=minLon,minLat,maxLon,maxLat` | reader/admin | 按登记的空间范围查找资源，返回版本入口和 manifest URL |
| `GET /pointcloud/resources?limit=100&offset=0` | reader/admin | 分页列出资源和已发布版本 |
| `GET /pointcloud/resources/{resource_id}` | reader/admin | 资源详情；reader 仅可查已发布资源 |
| `PATCH /pointcloud/resources/{resource_id}` | admin | 修改资源描述、SRS 或元数据 |
| `POST /pointcloud/resources/{resource_id}/taxonomies` | admin | 新建一版类别定义 |
| `GET /pointcloud/resources/{resource_id}/taxonomies` | reader/admin | 读取已发布资源的类别定义；admin 可读取草稿资源类别 |
| `POST /pointcloud/resources/{resource_id}/versions` | admin | 创建上传中的 PLY/3D Tiles 版本，并确认该版本与已审查范围对应 |
| `GET /pointcloud/resources/{resource_id}/versions` | reader/admin | 列出已发布版本 |
| `GET /pointcloud/versions/{version_id}` | reader/admin | 已发布版本详情与文件清单 |
| `POST /pointcloud/versions/{version_id}/files` | admin | 登记一个文件并取得可续传的 upload ID |
| `PUT /pointcloud/uploads/{upload_id}/chunks/{chunk_index}` | admin | 上传一个二进制分块 |
| `GET /pointcloud/uploads/{upload_id}` | admin | 查看上传状态和已接收分块 |
| `POST /pointcloud/uploads/{upload_id}/complete` | admin | 校验 SHA-256 并完成文件入库 |
| `POST /pointcloud/versions/{version_id}/finalize` | admin | 校验入口文件并发布不可变版本 |
| `GET /pointcloud/versions/{version_id}/manifest` | reader/admin | 读取已发布版本的清单 |
| `GET /pointcloud/versions/{version_id}/files/{relative_path}` | reader/admin | 获取已发布版本中的文件 |
| `POST /pointcloud/resources/{resource_id}/jobs` | admin | 登记一个外部处理任务 |
| `GET /pointcloud/resources/{resource_id}/jobs` | reader/admin | 列出任务；reader 仅看到有已发布输出的任务 |
| `GET /pointcloud/jobs/{job_id}` | reader/admin | 查询单个任务状态；reader 仅可查有已发布输出的任务 |
| `PATCH /pointcloud/jobs/{job_id}` | admin | 更新外部任务状态、进度或错误 |

创建资源时提交经审查的 `spatial_extent` 与位置确认：

```json
{
  "name": "FastGS 点云",
  "srs": "EPSG:4547",
  "metadata": {"content_kind":"pointcloud","spatial_extent_status":"declared_wgs84"},
  "location_review": {
    "confirmed": true,
    "source_crs": "EPSG:4547",
    "evidence": "已核对 tileset boundingVolume 与项目坐标系说明",
    "extent_method": "将 tileset 范围转换为 WGS84",
    "extent_coordinate_order": "longitude,latitude"
  },
  "spatial_extent": {
    "type": "Polygon",
    "coordinates": [[[114.35,30.52],[114.36,30.52],[114.36,30.54],[114.35,30.54],[114.35,30.52]]]
  }
}
```

服务端拒绝缺少范围、位置依据、确认记录或 CRS 不匹配的资源；文件上传完成后，发布接口还会复核资源范围和位置审查记录。修改 CRS/范围时必须重新提交完整位置审查；范围不能清空。

`spatial_extent` 必须是 WGS84 经度、纬度 Polygon 或 MultiPolygon。资源和每个文件版本都要分别提交位置审查；版本审查的 `source_crs` 必须与资源 CRS 一致。空间检索示例：

```http
GET /pointcloud/spatial-resources?bbox=114.35,30.52,114.36,30.54&limit=100
Authorization: Bearer <user-jwt>
```

响应为命中资源和可见版本，并附 `manifest_url`、`entrypoint_url`。该查询只判断**资源整体范围**是否相交，不过滤 PLY 内的点，也不剪裁 3D Tiles。未登记 `spatial_extent` 的资源不会出现在结果中；需按 AOI 返回点子集时，应先建立点坐标索引或空间分块。

上传限制可通过环境变量配置：默认单文件最大 24 GiB、单版本最大 100 GiB、单块最大 16 MiB、建议块大小 8 MiB。上传请求必须严格使用登记的分块边界。超限返回 `413`；非法请求通常返回 `422`；未授权为 `401`，普通用户访问写接口为 `403`，缺失资源/文件为 `404`，未完成上传或版本状态冲突为 `409`。

## 登记和上传流程

### 1. 创建资源和版本

```http
POST /pointcloud/resources
Authorization: Bearer <admin-jwt>
Content-Type: application/json

{"name":"北区点云","description":"原始点云及语义分割派生版本","srs":"EPSG:4547","metadata":{"source":"survey-2026","spatial_extent_status":"declared_wgs84"},"location_review":{"confirmed":true,"source_crs":"EPSG:4547","evidence":"已核对 tileset boundingVolume 与项目坐标系说明","extent_method":"将 tileset 范围转换为 WGS84","extent_coordinate_order":"longitude,latitude"},"spatial_extent":{"type":"Polygon","coordinates":[[[114.35,30.52],[114.36,30.52],[114.36,30.54],[114.35,30.54],[114.35,30.52]]]}}
```

响应的 `id` 用于创建版本：

```http
POST /pointcloud/resources/{resource_id}/versions
Authorization: Bearer <admin-jwt>
Content-Type: application/json

{"label":"source-2026-11","format":"ply","purpose":"source","entrypoint":"cloud.ply","metadata":{"coordinate_unit":"m"},"location_review":{"confirmed":true,"source_crs":"EPSG:4547","evidence":"已核对该版本文件与 tileset 范围记录","extent_method":"确认本版本数据覆盖在资源登记范围内","extent_coordinate_order":"longitude,latitude"}}
```

3D Tiles 版本将 `format` 设为 `3dtiles`，入口固定为 `tileset.json`。分割结果版本使用 `purpose:"segmentation"`，并提供同资源中已发布的 `source_version_id`、`taxonomy_id`；若关联外部任务，再提供 `job_id`，任务的输入来源与类别要一致。普通前端用户不能调用以上写接口。

### 2. 维护分类并创建分割任务

```json
POST /pointcloud/resources/{resource_id}/taxonomies
{"name":"地物类别 v1","classes":[
  {"id":1,"code":"building","name":"建筑物","color":"#d94841","description":"建筑点"},
  {"id":2,"code":"vegetation","name":"植被","color":"#48a868","description":"植被点"}
]}
```

```json
POST /pointcloud/resources/{resource_id}/jobs
{"source_version_id":"<published-source-version-uuid>","taxonomy_id":"<taxonomy-uuid>","algorithm":"segmentation-worker","algorithm_version":"1.0","parameters":{"voxel_size":0.1}}
```

任务创建只登记 `queued` 状态，不会自动启动算法。工作进程可以由部署方另行运行；处理期间用 `PATCH /pointcloud/jobs/{job_id}` 更新 `running` 和进度。输出版本发布成功后，服务会将关联任务更新为 `completed`、进度 100%。失败或取消可用管理员 PATCH 记录状态及 `error`。

### 3. 分块上传

先计算本地文件的 SHA-256，再登记路径、字节数和块大小：

```json
POST /pointcloud/versions/{version_id}/files
{"path":"cloud.ply","size_bytes":123456789,"sha256":"<64位小写SHA-256>","chunk_size":8388608}
```

响应包含 `upload_id`。每块以原始二进制发送，`chunk_index` 从 0 开始，`Content-Range` 必须准确匹配登记的边界：

```http
PUT /pointcloud/uploads/{upload_id}/chunks/0
Authorization: Bearer <admin-jwt>
Content-Type: application/octet-stream
Content-Range: bytes 0-8388607/123456789

<该范围内的原始文件字节>
```

最后一块可以小于 `chunk_size`。网络中断后，重新 PUT 相同分块内容可幂等成功；调用 `GET /pointcloud/uploads/{upload_id}` 获取已接收分块后，从缺失分块续传。所有块上传后调用 `POST /pointcloud/uploads/{upload_id}/complete`，服务会核对整体 SHA-256。

每个包内文件独立登记和上传。例如 3D Tiles 除 `tileset.json` 外，还要按其相对 URI 上传 `.pnts`、`.b3dm`、子 tileset JSON、纹理等所有引用文件。完成全部文件后调用 `POST /pointcloud/versions/{version_id}/finalize`；只有发布成功的版本会对普通用户可见。

已发布且以 `tileset.json` 为入口的版本会自动兼容旧前端资源目录和文件路由，前端可继续用 `POST /api/ArScene/GetSceneResourceByPage` 搜索、按旧场景接口关联，并通过相应的 Mesh/PointCload 路由加载。资源或版本的 `metadata.content_kind` 可明确指定 `mesh`、`model`、`terrain` 或 `pointcloud`；未指定时，只有纯 `.b3dm` 包或纯 `.pnts` 包会按文件类型自动归类。包含两类瓦片或仅含 `.glb` 的包不会猜测分类，因此不会被旧目录列出。旧接口将资源 UUID 映射到该资源最新发布的 3D Tiles 版本；原有 `/pointcloud/versions/{version_id}/files/...` 接口仍然可用。

## 前端读取

前端先获取已发布版本元数据，读取 `entrypoint`；对应 URL 为：

```text
GET /pointcloud/versions/{version_id}/files/{entrypoint}
```

3D Tiles 入口返回 JSON，子文件 URL 根据 tileset 中的相对 URI 继续请求同一个 `/files/` 路径。每个入口和子文件请求都必须携带 `Authorization: Bearer <user-jwt>`；跨域浏览器端可通过前端代理转发 JWT。现有 Cesium/3D Tiles loader 若不支持自定义请求头，需要配置其 request headers 或经同源代理访问。标准 PLY 可通过同一路径下载，响应为原始 PLY 字节；当前服务不执行 PLY→3D Tiles 转换，也不在浏览器中渲染 PLY。若沿用现有 3D Tiles 加载器，应由外部转换任务生成完整 3D Tiles 包并作为派生版本上传。

文件接口支持 Starlette 文件响应提供的下载/Range 行为；请求仍须认证。文件响应使用登记的 MIME 类型，未登记或未发布的文件不会被返回。

## 安全边界与当前范围

服务端以 `administrator` 依赖保护所有点云写接口，写入请求无法传入或伪造用户 ID。普通用户不能上传、修改资源元数据/类别、创建算法任务、更新状态、发布版本或删除数据库对象；用户私有标绘仍按现有账户 JWT 与账户 ID 隔离。管理员删除资源使用统一的 `DELETE /admin/resources/pointcloud/{resource_id}`；如版本/任务被其他资源引用，服务会拒绝删除。该服务不执行语义分割，也不把 PLY 转换成 3D Tiles 或直接在浏览器渲染 PLY。

关系表和索引在首次访问点云 API 时由服务自动创建于现有 PostGIS 数据库，文件放在服务数据卷。因此迁移时应同时备份 PostGIS 和 `DEQING_DATA_DIR/pointcloud/` 文件目录。
