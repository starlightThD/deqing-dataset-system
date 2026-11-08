# 远程数据服务访问说明书（用于本地复刻 + 数据迁移）

> 目标：① 按这份文档在本地复刻一个"同样的数据服务"；② 远程恢复后把数据迁移进本地库。
> 本文件所有内容均来自对 `http://1jk412ww3207.vicp.fun:16761` 的实测（约 2.3 万次接口探测 + 369 个资源的元数据 + 8 套空三/索引文件解析），
> 标注「推断」的条目为合理外推，标注「实测」的为已验证。
> 配套文件：`local_schema.sql`（本地库 DDL）、`api_spec.json`（机器可读接口清单）、`DATA_SURVEY.md`（数据摸底与可获取性）。

> **2026-11-02 补采修订**：下文的“只有根文件键、不能取得 3D Tiles 子文件”是最初摸底结论，现已被前端实际调用路径更新。`GET /api/Mesh/GetMesh3DTilesFile/{资源ID}/{相对路径}` 可读取地形子 JSON/GLB；点云使用拼作 `PointCload` 的独立同名接口，也可读取子 JSON/GLB。`GET /api/Raster/GetTmsFile/{资源ID}/tilemapresource.xml` 可读取 22 个 TMS 根文件，后续坐标路径中的部分 PNG 也可下载；`GET /api/Video/GetInitDirectionalInfoList/{资源ID}` 含 MP4 的 FastDFS 键。TMS 无效坐标有时返回 200 空内容；MP4 文件流 90 秒无响应。资源目录已增至 370 条。以 [兼容范围](../docs/legacy-compatibility.md) 和 `DATA_SURVEY.md` 最新章节作为当前镜像状态；下文保留原始探测记录。

---

## 1. 拓扑与部署形态

| 地址 | 服务 | 技术栈 | 说明 |
|---|---|---|---|
| `http://1jk412ww3207.vicp.fun:16761` | 数据管理 API | .NET / Kestrel（`Server: Kestrel`），JWT HS256 | 我们唯一的数据入口 |
| `http://1jk412ww3207.vicp.fun:45119/geoserver/` | GeoServer（Jetty 10） | OGC 服务 | 与 369 个资源**无关**（只有 GeoServer 示例数据 + `xygis` 少量图层） |
| `http://1jk412ww3207.vicp.fun/` | 另一个前端 SPA（Vue + Cesium，「智能灾害感知协同平台」） | nginx 1.29.8 | 不连数据 API，`app-config.js` 只配了 GeoServer 地址 |
| 内网 `192.168.2.2:8080/geoserver/sctx/…` | GeoServer（平台内部） | — | wfs 资源的 `DataUrl` 指向这里，外网不可达 |
| 内网 `\\192.168.2.2\image\group1\…` | 文件存储共享（FastDFS `group1`） | — | 所有文件真实存放地，见 §6 |

- JWT 的 `iss` / `aud` 都是 `http://localhost:8031/` → 后端内网端口 8031，对外经花生壳（Oray）映射。
- 域名当前只有一个 A 记录（`175.27.226.195`，腾讯云），无 AAAA。
- **没有任何前端管理界面**：`/`、`/index.html`、`/swagger`、`/swagger/v1/swagger.json`、`/api-docs`、`/health` 全部 404。

---

## 2. 认证与令牌

### 2.1 登录

```
POST /api/Login/Login
Content-Type: application/json;charset=UTF-8

{"UserName": "admin", "Password": "<md5(明文密码) 的小写 32 位 hex>"}
```

响应（实测）：

```json
{
  "State": 0,
  "Message": "登录成功",
  "Data": {
    "Token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9....",
    "UserName": "admin",
    "RealName": "admin",
    "AuthCodes": ["BASEPLATFORM_DPS_ENGINE", "PLATFORM_MANAGE_ALL", "…"]
  }
}
```

- `State == 0` 表示成功；非 0 时 `Message` 给原因（账号/密码/权限）。
- 密码是**直接 MD5**（不是加盐、不是 bcrypt）：客户端提交 `md5(明文密码)` 的 32 位小写十六进制字符串。历史实测账号的具体密码不属于接口契约。

### 2.2 令牌（JWT）

```
header : {"alg":"HS256","typ":"JWT"}
payload: {
  "nbf": 1792565742,            // 生效时间
  "exp": 1792824942,            // = nbf + 259200s → 有效期 3 天
  "AuthInfo": "{\"UserId\":\"…\",\"LoginRecordId\":\"…\",\"TenantId\":\"…\",
                 \"ExpireDays\":3,\"Type\":1,\"CurrentToken\":null,\"Param\":null,
                 \"IsSuperAdmin\":false}",
  "iss": "http://localhost:8031/",
  "aud": "http://localhost:8031/"
}
```

注意 `AuthInfo` 是一个**被转义的 JSON 字符串**（不是对象），本地复刻时要保持同样形态，否则老客户端解析会挂。

### 2.3 鉴权头

```
Authorization: Bearer <Token>
```

- 缺/失效 → `401`；无权限资源 → `403`。
- 服务端支持多设备登录（`CurrentToken` 字段预留），实测同一账号可多次登录取不同 Token。

---

## 3. 接口规范（实测存在的全部接口）

> 通用约定：请求体为 JSON 时**必须**带 `Content-Type: application/json`（否则 `415 Unsupported Media Type`，ASP.NET 默认行为）；
> 响应统一 `application/json; charset=utf-8`；业务层错误用 `{"State":非0,"Message":"…"}` 表达，HTTP 状态仍是 200。

### 3.1 资源分页列表（核心，369 条全量）

```
POST /api/cim/CIMScene/GetCIMSceneResourceByPage
Authorization: Bearer <token>
Content-Type: application/json;charset=UTF-8

{
  "BigResourceType": [],      // int[] 按大类型过滤，空数组=不限
  "DataSetIds": [],           // string[] 按数据集过滤，空数组=不限
  "Type": 0,                  // int，0=全部；可传具体 Type 码（如 4000001）
  "Label": "",                // 标签模糊查询
  "StartTime": null,          // 数据时间范围（ISO8601 字符串）
  "EndTime": null,
  "PageIndex": 1,             // 从 1 开始
  "PageSize": 100             // 实测可到 100，未测上限；大于总数时返回全部
}
```

响应：

```json
{
  "Total": 369,
  "Rows": [ { /* 42 字段，见 §5.1 */ } ]
}
```

要点：
- 这是**唯一**能拿到资源完整信息（含 `MetaData`、`OriginalRootFileId`）的接口；分页取全 369 条用 `PageSize=100` 拉 4 页即可。
- 资源记录里的 `MetaData` 是**JSON 字符串**，需二次解析。
- 该接口**不返回**任何文件清单，只有"根文件键"。

### 3.2 文件流下载（唯一取数据的通道）

```
GET /api/DownLoad/GetFileStream?group1/M00/47/D3/wKgCAmpUQFaAdY2KAD0BIOQ3DkU131.jpg
Authorization: Bearer <token>
```

**极其特殊的约定（实测）**：路径是 `/api/DownLoad/GetFileStream`，而 **FastDFS 存储键整段放在查询串里，没有参数名**。
`?group1/M00/.../xxx.jpg` 就是全部——不要写成 `?path=` / `?fileId=`（都会 204）。

| 情况 | 响应 |
|---|---|
| 键存在 | `200`，body = 文件原始字节（无 Content-Disposition，`Accept-Encoding: identity` 才不会被压缩） |
| 键不存在 / 库内无记录 | `204 No Content`（body 空） |
| 偶发瞬时失败 | 也会返回 `204`，**重试 1~3 次即恢复**（实测约 1/60 概率） |
| 用 POST 调 | `405 Method Not Allowed`（只允许 GET） |

- 不支持"按资源列文件"、不支持相对路径、不支持目录、不接受 `resourceId`/`path` 组合（累计试过 100+ 种参数组合，全部 204/404）。
- 实测单连接吞吐 ≈ **4 MB/s**（8~12 并发无提升，瓶颈在服务端），单文件最大已下 20 MB（XML）。
- 客户端建议：校验首字节判断类型（JPEG `FF D8 FF`、PNG `89 50 4E 47`、SQLite `SQLite format 3\0`、JSON `{`）。

### 3.3 资源精简视图（114 条，无文件键）

```
POST /api/Resource/GetByPage
{"PageIndex": 1, "PageSize": 50}
```

响应行字段（与 3.1 不同）：

```json
{"TypeName":"Raster TMS切片","Id":"7d19af2f-…","Label":"2204dom_tms","Description":"",
 "Type":12000008,"UploadTime":"2026-07-13T14:33:42.592419","MetaData":null,
 "BusinessTag":"","EpsgId":4326,"TypeKey":"RasterTMS","Url":null,"Attribute":null,
 "IsLook":true,"IsSetting":true,"Proj4text":null}
```

用途：拿到 `TypeKey`（如 `RasterTMS`）这类"类型别名"，以及 `IsLook/IsSetting` 等展示开关。
注意它只覆盖 114 条（即 8 类中的 5 类，不含 WMS/3dtiles），**不是全量**。

### 3.4 其余接口（与数据无关）

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/User/GetByPage` | POST | 用户分页（实测 `Total=0`、`Rows=null`，需按同样 JSON body 调用） |
| `/api/Role/GetByPage` | POST | 角色分页 |
| `/api/Organization/GetByPage` | POST | 组织分页 |
| `/api/Layer/GetFile` | GET | 存在但**任何参数都返回 200 + 0 字节**，实际不可用（可能是给内网图层用的） |

---

## 4. 不存在的接口（探测结论，避免重复踩坑）

对 `/api/**` 做了系统枚举：**130+ 个控制器根名 × 200+ 个动作名 × GET/POST ≈ 2.3 万个组合**，仅命中 §3 列出的那几个。
明确不存在（全 404）的代表性路径：

```
/api/DownLoad/GetFileList          /api/DownLoad/GetResourceFileList     /api/DownLoad/DownloadResource
/api/DownLoad/GetFileTree          /api/DownLoad/GetResourceFiles        /api/DownLoad/Export
/api/cim/CIMScene/GetCIMSceneResourceById                                /api/cim/CIMScene/GetCIMSceneResourceFileList
/api/cim/CIMScene/GetTile          /api/cim/CIMScene/GetTileset          /api/cim/CIMScene/GetFileStream
/api/File/*  /api/Video/*  /api/Photo/*  /api/Model/*  /api/Tiles/*  /api/Tile/*  /api/DataSet/*  /api/Storage/*
/api/FastDFS/*  /api/DownloadCenter/*  /api/TileService/*  /api/Package/*  …（均为 404）
```

**结论：远端 API 的"读"能力只有三件事——列资源、按键取文件、读资源精简视图。没有任何"按资源枚举文件"的能力。**

---

## 5. 数据模型

### 5.1 资源（Resource）42 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `Id` | string(uuid) | 资源主键 |
| `Label` | string | 标签/图层名，如 `2204dom_tms`、`倾斜影像2` |
| `Description` | string/null | |
| `Type` | int | 资源类型码，见 §5.2 |
| `TypeName` | string | 类型中文名 |
| `BigResourceType` | int | 大类型（1 地形 / 2 矢量 / 4 倾斜 / 10 视频 / 12 栅格服务 / 13 点云） |
| `DataSetId` | string(uuid) | 所属数据集 |
| `Name` | string | 项目/场景名，如 `2204武大数据` |
| `MetaData` | string | **JSON 字符串**，按类型不同（见 §5.3） |
| `OriginalRootFileId` | string/null | 根文件 FastDFS 键（`.db3` 8 个 / `.json` 21 个） |
| `Range` | string/null | WKT：**实测为"相机中心（或模型）的 XY 外接矩形"**，不是地面覆盖；坐标系随资源 SRS（可能是 4547，也可能是 ENU 米制） |
| `EpsgId` | int | 声明坐标系，**不可靠**（ENU 资源也会写 4326）→ 一律以根文件内的 SRS 为准 |
| `SrsId` / `SrsDefinition` / `RangeWktBySRSOrigin` | string/null | 元数据字段，实测多为 null |
| `FileCount` | int | 该资源包含的文件数（倾斜=照片数） |
| `FileSize` | int/null | 字节数（服务类资源为 null） |
| `UploadTime` / `DataProductionTime` | string | ISO8601 |
| `DataUrl` | string/null | 仅 wfs：内网 GeoServer WFS 地址 |
| `IsUploadCompleted` / `IsDeleted` / `IsDisabled` / `IsPhysicalDeleted` / `IsChecked` / `IsGeneric` / `IsModifyData` / `IsEdgeMatch` / `IsOtherSolutionTaskResource` | bool | 状态位 |
| `ResourceHandleState` | int | 实测 0（340 条）/ 5（29 条，均为 wfs） |
| `ResourceHandleStateName` | string/null | |
| `ResourceRequestStatisticsNum` | int | 访问计数（全 0） |
| `BusinessTag` / `GuideMap` / `WfsDataUrl` / `LastDeletedTime` / `RangeWkt` | null | 实测恒 null |
| `CreateById` / `EditById` / `ApplicationId` / `TenantId` / `num` | string/int | 创建人、租户、递增编号（`num` 唯一，7~873） |

### 5.2 资源类型码（实测全表）

| Type | TypeName | BigResourceType | 数量 | TypeKey | 可获取性 |
|---|---|---|---|---|---|
| `4000001` | 航空倾斜 | 4 | 8 | — | **完整可拉** |
| `1000012` | 地形3dtiles | 1 | 12 | — | 仅根 tileset |
| `13000006` | 点云3dtiles | 13 | 10 | — | 仅根 tileset |
| `12000008` | Raster TMS切片 | 12 | 22 | `RasterTMS` | 不可拉 |
| `10000004` | 定向动态视频 | 10 | 5 | — | 仅首帧快照 |
| `12000004` | 栅格数据WMS服务 | 12 | 236 | — | 仅元数据 |
| `12000005` | 栅格数据WMTS服务 | 12 | 51 | — | 仅元数据 |
| `2000004` | wfs | 2 | 25 | — | 仅元数据（DataUrl 内网） |

合计 369 个资源、11 个数据集、标称 30,173 个文件、276.96 GiB。

### 5.3 各类型 `MetaData` 的 JSON 键

公共尾部（除 WMS/TMS 外几乎都有）：
`SRSOrigin[3]`、`LocationPoint[3]`、`OffsetOrigin[3]`、`boundingsphere[4]`（或 `Boundingsphere`）、`BoundingBox[6]`、
`IsSharePath`、`ShareRootPath`、`ShareUserName`、`SharePassword`、`IsRangeSelectedDownload`、`IsMigrate`、`FastTrackerAddress`。

| 类型 | 专有键 |
|---|---|
| 航空倾斜 | `XmlFileId`（空三 XML 的 FastDFS 键）、`IsXmlMatrix` |
| 地形3dtiles | `BoundingBoxDb3FileId`（瓦片包围盒索引的键，可能为 null）、`IsExistTopTile`、`ServerUrl` |
| 点云3dtiles | `IsExistTrajectoryFile`、`PointDensity`、`MapScale`、`PointCloud3dtilesFormat`、`ServerUrl`、`BoundingBoxDb3FileId` |
| Raster TMS | `Resolution`、`Xmin/Xmax/Ymin/Ymax`、`MininumLevel`、`MaxinumLevel`、`Srid`、`RootFile`（`tilemapresource.xml`）、`TileFormat{Width,Height,Extension,MimeType}`、`OriginalResourceType`（12000001）、`Scale`、`ServiceUrl`（恒 null） |
| WMS | `IsUseDefaultWorkSpace`、`Format`（`image/png`）——**只有这两个键** |
| WMTS | `Resolution`、`Scale`、`Xmin/Xmax/Ymin/Ymax`、`ServerUrl`（null）、`LayerName`（null）、`IsGeneratePyramid`、`IsMergeResource`、`IsCreateLayerGroup`、`IsUseDefaultWorkSpace`、`Format` |
| wfs | `Resolution`、`Scale`、`Xmin/Xmax/Ymin/Ymax`、`ServerUrl`（null）、`LayerName`（null）、`IsGeneratePyramid` |
| 定向视频 | `videoInfos[]`：`{VideoName, Duration, FrameRate, Width, Height, FirstSnapshotFileId}`、`IsOrient`、`CenterPoint` |

### 5.4 数据集（11 个）

`202309武大数据`(159)、`4.16演示`(139)、`whu-遥感+建模+点云+正射`(33)、`0129`(16)、`2204武大数据`(5)、
`202205武大数据`(5)、`202305武大数据`(5)、`202511武大数据`(4)、`4.8测试视频`(1)、`武汉大学-2`(1)、`WHU`(1)。

---

## 6. 文件存储模型（复刻的关键）

### 6.1 键格式 = FastDFS 路径

```
group1/M00/47/D3/wKgCAmpUQFaAdY2KAD0BIOQ3DkU131.jpg
└─┬──┘ └┬┘ └┬┘ └┬┘ └────────┬───────────────────┘
 group  M00  分目录(2 级)     文件名 = base64(时间戳+大小+CRC+随机) + 扩展名
```

- 键**不可枚举、不可推算**（含上传时间戳、文件大小、CRC、随机串），只能从"索引文件"里读出来。
- 最初据此认为无法取得 `3dtiles` 分块和 `TMS` 切片；2026-11-02 根据旧前端实际调用路径发现了按资源 ID 加相对路径读取的专用接口，见文件顶部修订说明。

### 6.2 真实存放位置（从 DB3 的 UNC 路径得知）

DB3 的 `Photo.PhotoFile` 里存的是：

```
\\192.168.2.2\image\group1\M00\47\D3\wKgCAmpUQFaAdY2KAD0BIOQ3DkU131.jpg
```

→ 去掉前缀两段（`\\192.168.2.2\image`）即得 FastDFS 键。即 **`192.168.2.2` 上的 `image` 共享 = FastDFS `group1` 存储目录**。
复刻时：把 `image/` 目录整体挂到本地对象存储或文件系统，键 → 相对路径直接映射（本地就是 `image/group1/M00/47/D3/xxx.jpg`）。

### 6.3 目录结构（实测，来自 3dtiles 的 content.uri）

```
group1/M00/…/<tileset>.json                      ← 资源根（OriginalRootFileId）
  Data/Tile_+000_+011/Tile_+000_+011.json        ← 3dtiles 分块 json（地形/模型）
  Tile_+000_+011/Tile_+000_+011.json             ← 点云分块（无 Data/ 前缀）
  <timestamp>/<timestamp>.json                    ← 另一种点云分块命名
  Model/Model.json  /  result_16_20_10/...json   ← 其他变体
  Tile_N/Tile_N.json                              ← 地形数据变体
```

倾斜影像、视频、XML 都是扁平地放在某个 `M00/xx/xx/` 目录里，没有语义目录。

---

## 7. 根文件格式

### 7.1 倾斜影像索引：SQLite DB3（8 个，每资源一个）

文件头 `SQLite format 3\0`。表结构（实测，全部表都要建，客户端可能只读其中几张）：

```sql
CREATE TABLE PhotoGroup(Id INTEGER PRIMARY KEY, GroupName TEXT, PhotoType TEXT, Description TEXT);
CREATE TABLE Photo(
  Id INTEGER PRIMARY KEY, PhotoFile TEXT, GroupId INTEGER,
  RowNo INTEGER, ColNo INTEGER, Width REAL, Height REAL,
  FocalLength REAL, PixelSize REAL, Principal_X REAL, Principal_Y REAL,
  Distotion_K1 REAL, Distotion_K2 REAL, Distotion_K3 REAL, Distotion_K4 REAL,
  Distotion_P1 REAL, Distotion_P2 REAL,
  X REAL, Y REAL, Z REAL, Phi REAL, Omega REAL, Kappa REAL,
  RotationSys BIT, RPC TEXT, ModelMatrix TEXT, ProjectMatrix TEXT,
  OriFlag INTEGER, IsCCXmlData BOOL);
CREATE TABLE PhotoSite(Id INTEGER PRIMARY KEY, X REAL, Y REAL, Z REAL, PhotoList TEXT, SiteRela TEXT);
CREATE TABLE PointGroup(Id INTEGER PRIMARY KEY, GroupName TEXT, PointGroupType TEXT);
CREATE TABLE Point3D(Id INTEGER PRIMARY KEY, PointName TEXT, PointGroupId BIGINT, X REAL, Y REAL, Z REAL, Name TEXT, DEF1 TEXT, DEF2 TEXT, DEF3 TEXT);
CREATE TABLE Point2D(Id INTEGER PRIMARY KEY, PhotoId INTEGER, Point3DId INTEGER, MarkType INTEGER, X REAL, Y REAL, IndexInPhoto INTEGER, DEF1 TEXT, DEF2 TEXT, DEF3 TEXT);
CREATE TABLE LineGroup(Id INTEGER PRIMARY KEY, GroupName TEXT);
CREATE TABLE Line3D(Id INTEGER PRIMARY KEY, LineGroupId INTEGER, Start_X REAL, Start_Y REAL, Start_Z REAL, End_X REAL, End_Y REAL, End_Z REAL);
CREATE TABLE Line2D(Id INTEGER, PhotoId INTEGER, Line3DId INTEGER, Start_X REAL, Start_Y REAL, End_X REAL, End_Y REAL);
CREATE INDEX Index_Point2D ON Point2D(PhotoId, IndexInPhoto, Point3DId);
```

实测填充情况（8 个资源一致）：`Photo` 全有（数量 = `FileCount`）、`PhotoGroup` 5 行（`Photogroup_1_1`…，`PhotoType='Aerial'`），
`PhotoSite`/`Point*`/`Line*` **全空**（0 行）。所以有效信息都在 `Photo` + `PhotoGroup`。
（`Index_Point2D` 在部分库里缺失，建表时给不给都不影响读取。）

### 7.2 瓦片包围盒索引（3 个资源才有）

```sql
CREATE TABLE BoundingBox(Name TEXT PRIMARY KEY, Path TEXT,
  Xmin REAL, Ymin REAL, Zmin REAL, Xmax REAL, Ymax REAL, Zmax REAL);
```

`Name` = 瓦片名（`Tile_+000_+011` / `Tile_000000`），`Path` = **生产机本地路径**（`I:\…\Tile_+000_+011.osgb` / `E:\…\Tile_000000.osgb`）——
只能用来知道"有哪些瓦片、瓦片在哪、包围盒多大"，**不能用来下载**（不是 FastDFS 键）。

### 7.3 3dtiles 根文件 `tileset.json`（21 个）

```json
{
  "asset": {"version": "1.1", "gltfUpAxis": "Z"},
  "geometricError": 764.86,
  "root": {
    "boundingVolume": {"sphere": [x, y, z, radius]},          // 或 {"box": [16 个数]}
    "geometricError": 764.86,
    "transform": [16 个 double，列主序，ECEF],                 // 实测平移量 = 模型原点，椭球高 0
    "children": [
      {"boundingVolume": {"box": [...16]}, "geometricError": 27.71,
       "content": {"uri": "Data/Tile_+000_+011/Tile_+000_+011.json"}}
    ]
  }
}
```

- 分块 `uri` 全部是**相对路径**，没有绝对 URL、没有 `url`/`basePath` 字段 → 本地复刻时必须自己实现"相对路径 → 存储键"的映射表（`file_object.relative_path`）。
- `transform` 的平移量换成经纬度可与倾斜影像范围交叉校验（实测 2204 数据集两者相差约 20 m，属正常）。

### 7.4 空三文件：ContextCapture `BlocksExchange` XML（8 个）

```xml
<?xml version="1.0" encoding="utf-8"?>
<BlocksExchange version="3.2">
  <SpatialReferenceSystems>
    <SRS><Id>0</Id><Name>WGS 84 (EPSG:4326)</Name><Definition>EPSG:4326</Definition></SRS>
    <SRS><Id>1</Id><Name>CGCS2000 / 3-degree Gauss-Kruger CM 114E (EPSG:4547)</Name><Definition>EPSG:4547</Definition></SRS>
  </SpatialReferenceSystems>
  <Block>
    <Name>Block_1 - AT</Name>
    <Description>Result of aerotriangulation of Block_1 (2026-Apr-12 13:45:02)</Description>
    <SRSId>1</SRSId>                       <!-- 下面的坐标都用这个 SRS -->
    <Photogroups>
      <Photogroup>
        <Name>Photogroup 1</Name>
        <ImageDimensions><Width>6000</Width><Height>4000</Height></ImageDimensions>
        <CameraModelType>Perspective</CameraModelType>
        <CameraModelBand>Visible</CameraModelBand>
        <FocalLength>35.7152985170768</FocalLength>   <!-- mm -->
        <SensorSize>23.5</SensorSize>                  <!-- mm（对应宽向） -->
        <CameraOrientation>XRightYDown</CameraOrientation>
        <PrincipalPoint><x>2903.48785808107</x><y>1953.10216035523</y></PrincipalPoint>
        <AspectRatio>1</AspectRatio><Skew>0</Skew>
        <Model>ILCE-6000</Model>
        <Photo>
          <Id>0</Id>
          <ImagePath>\\192.168.2.2\image\group1\M00\47\D3\…131.jpg</ImagePath>
          <Component>1</Component>
          <Pose>
            <Rotation><M_00>…</M_00>…<M_22>…</M_22></Rotation>
            <Center><x>91.76</x><y>-354.14</y><z>124.47</z></Center>
            <MetaData><GPS>…</GPS></MetaData>
          </Pose>
        </Photo>
        … 1936 张/组 …
      </Photogroup>
      … 共 5 个 Photogroup …
    </Photogroups>
  </Block>
</BlocksExchange>
```

SRS 实测出现三种：
| 定义 | 含义 | 处理方式 |
|---|---|---|
| `EPSG:4547` | CGCS2000 / 3° 高斯-克吕格 CM **114°E**（k0=1, FE=500000） | 横轴墨卡托反算到经纬度 |
| `ENU:30.52956,114.3557` | 站心坐标，原点纬度,经度（米制，z=天向） | ENU → ECEF → 大地坐标 |
| `EPSG:4326` / `EPSG:4326+5773` | 仅出现于 SRS 清单里，作为基准 | 直接用 |

---

## 8. 坐标、位姿与几何约定（复刻必备）

1. **旋转矩阵约定**：`Rotation` 的 9 个数按 `M_00..M_22` 存，**列向量 = 相机轴在世界系中的方向**：
   `世界方向 = M · 相机方向`，相机系为 `x 右, y 下, z 前`（与 `CameraOrientation=XRightYDown` 一致）。
   验证方法：取各 Photogroup 第一张的第三列（相机光轴），下视组应接近 `(0,0,-1)`——实测 25 mm 组为 `(+0.01,-0.24,-0.97)`（俯角 76°）✅，
   35 mm 斜视组为俯角 31°~56° ✅。
2. **内参换算**：`f_px = FocalLength(mm) × Width(px) / SensorSize(mm)`。
   例：`35.715 × 6000 / 23.5 = 9118.7 px`，与 DB3 里的 `FocalLength=9118.8` 完全一致 ✅（可用来校验 XML 与 DB3 是否同一套相机）。
3. **像点 → 地面（用于区域筛选）**：
   `d_cam = (px − ppx, py − ppy, f_px)` → `d_world = M · d_cam` → 与地面平面 `z = z_ground` 求交
   `t = (z_ground − Cz)/d_world.z`，`(gx, gy) = (Cx + t·dwx, Cy + t·dwy)`。
   斜视相机上部像素射线可能在地平线以上，需按"最大水平距离 = k × AGL"截断（脚本里 k=8）。
4. **相对航高 AGL**：`AGL = 相机 Z − 地面 Z`。平台**不提供地面高程**，只能外部给定或估算；
   默认 `AGL=120 m` 时，AoI 覆盖范围误差与该值成线性关系。实测相机 Z：4547 资源 143~190 m，ENU 资源 120~180 m。
5. **Gauss-Krüger 反算**（无 pyproj 时的替代）：Snyder 级数，椭球 CGCS2000（a=6378137, 1/f=298.257222101），
   中央经线 114°，FE=500000，FN=0，k0=1。自检：中央经线上 E 恒为 500000.000；30°N 子午线弧长 3,320,113.4 m（标准值 ≈3,320,116 m，差 2.6 m 来自椭球扁率，正确）。
   已实现在 `geo.py`（`tm_inverse` / `tm_forward` / `enu_to_geodetic` / `crs_from_xml_srs`）。
6. **`Range` 字段语义（易踩坑）**：实测等于"相机中心 XY 的外接矩形"，不是地面覆盖范围。
   所以按 `Range` 做区域筛选会漏边（真实覆盖比它大 1 个像幅）；正确做法是用 §8.3 逐张算地面覆盖。

---

## 9. 可获取性矩阵与缺口清单（原始探测快照）

以下表格记录最初探测结论；2026-11-02 后 3D Tiles、TMS 和视频方向接口的补充发现见文件顶部修订说明及 `DATA_SURVEY.md` 第 11 节。

| 类型 | 数量 | 标称体积 | 能拿到什么 | 拿不到的部分 | 缺口根因 |
|---|---|---|---|---|---|
| 航空倾斜 | 8 | 102.16 GiB | DB3 索引、全部原始影像（23,554 张）、空三 XML | — | — |
| 地形/点云 3dtiles | 22 | 163.04 GiB | 根 `tileset.json`（21 个） | 全部分块 `json/b3dm/pnts` | 无"相对路径 → 存储键"接口 |
| Raster TMS | 22 | 3.23 GiB | 元数据（方案、级别、范围） | 全部切片 | `OriginalRootFileId` 为空 + 无切片服务地址 |
| 定向视频 | 5 | 8.53 GiB | 5 张 4K 首帧 PNG | MP4 原片 | `videoInfos` 只暴露快照键 |
| WMS/WMTS/wfs | 312 | — | 服务元数据（图层名/范围/EPSG/分辨率） | 栅格/矢量数据 | `ServerUrl`/`LayerName` 为 null；wfs 的 `DataUrl` 指向内网 |

**要让本地复刻"完整"，必须向平台方要到其中一项：**
1. 文件存储本体访问权（`\\192.168.2.2\image` 只读 / FastDFS Tracker 直连）——一次解决全部 4 类；
2. 或补齐接口：`按资源列文件（相对路径 + 键）`、`TMS 根文件键`、`视频文件键`；
3. 或开放内网 GeoServer（`192.168.2.2:8080`）与 3D Tiles 服务端口。

---

## 10. 本地复刻方案

### 10.1 最小可用的"同构服务"

按 §3 实现 3 个接口即可让现有客户端（`read_data.py` / `mirror.py`）零改造指向本地：

| 本地路由 | 语义 | 数据来源（`local_schema.sql`） |
|---|---|---|
| `POST /api/Login/Login` | 校验 `md5(密码)`，签发同结构 JWT（HS256，3 天） | `app_user`（`password_md5` 直接存 MD5，兼容老客户端） |
| `POST /api/cim/CIMScene/GetCIMSceneResourceByPage` | 分页返回 42 字段（`MetaData` 需序列化成**字符串**） | `resource` + `resource_type` |
| `GET /api/DownLoad/GetFileStream?<key>` | **把整段查询串当键**，返回文件字节；找不到返回 204 | `file_object`（`key` → 本地路径） |
| （建议自建）`POST /api/resource/{id}/files` | 补上远端缺的"按资源列文件"能力 | `file_object`（`resource_id` + `relative_path`） |

要点：
- 响应头保持 `Content-Type: application/json; charset=utf-8`；`GetFileStream` 支持 `Accept-Encoding: identity`。
- 错误约定照抄远端：业务错误 `{"State":1,"Message":"…"}`；取流失败用 `204`。
- 目录即存储：本地把 `image/`（`group1/…` 相对路径）作为静态根，`file_object.local_path` 指向它，
  这样迁移完成后目录结构与远端**完全一致**，将来反向导出也方便。

### 10.2 推荐落库流程（本地）

1. 建库：`sqlite3 local.db < local_schema.sql`（或翻译到 MySQL/PostgreSQL）。
2. 灌字典：`resource_type` 已内置 8 条实测编码；`tenant`/`app_user` 按需插入。
3. 灌资源：调 §3.1 拉 369 条 → 写 `resource`（`MetaData` 原样塞 `metadata_json`）+ `dataset`。
4. 灌根文件：按 `OriginalRootFileId` 下载 → 写 `file_object(kind=incline_index|tileset_root)` + `resource_srs`。
5. 解析展开：
   - `.db3` → `photo_group` / `photo`（29 个字段全搬）
   - `.xml` → `camera_group` / `photo_pose` / `resource_srs`
   - `tileset.json` → `tileset_node`（含 `content_uri` 相对路径；将来拿到键就能回填 `content_file`）
6. 灌影像：按 `photo.file_key` 逐个下载 → 回填 `photo.status/bytes/sha256/local_path` + `file_object(kind=photo)`。
7. 区域筛选：`aoi` / `aoi_photo`（对应 `aoi.py` 的输出）。

### 10.3 复刻时需要"伪造"的部分（远端有、但我们拿不到）

| 远端能力 | 本地建议 |
|---|---|
| 3dtiles 分块服务（`Data/Tile_X/Tile_X.json` → b3dm/pnts） | 用平台给的 OSGB/原始模型自己重转一遍（ContextCapture / 开源的 3dtiles 转换），或直接从 `BoundingBox` 索引 + 生产路径推出瓦片名，再让平台方导出 |
| TMS/WMTS/WMS 切片服务 | 用本地 GeoServer 发布 GeoTIFF/正射影像，或把 DOM 转 TMS 目录（`tilemapresource.xml` + `{z}/{x}/{y}.png`） |
| 视频流 | 有原片后放本地 nginx/HLS |
| 鉴权/权限 | 复刻 JWT + `AuthCodes` 字符串数组即可，不必实现真实权限位 |

---

## 11. 数据迁移方案（远程恢复后）

```bash
# 1) 体检（1 次登录请求）
python3 healthcheck.py                      # 返回码 0 = 可用

# 2) 资源 + 根索引 + 影像清单 + 空三 XML（约 200 MB，含 8 个 XML 共 55 MB）
python3 mirror.py survey
python3 mirror.py assets

# 3) 去重（发现 3 个资源是同一航飞，省 2×674 张 ≈ 2.8 GiB）
python3 mirror.py dedupe

# 4) 区域筛选（给经纬度，只下需要的）
python3 aoi.py --bbox 114.3566,30.5410,114.3600,30.5440 --name project_area --agl 120

# 5) 断点续传式下载（已下的自动跳过；--workers 12 恒定约 4 MB/s）
python3 mirror.py images --aoi project_area --workers 12

# 6) 进度与校验
python3 mirror.py report
```

要点：
- 全部断点续传：`out/images/<resourceId>/<photoId>_<key>.jpg` 存在且非空即跳过；失败的照片在 `photo.status` 留 `http_204` 等标记，重跑 `mirror.py images` 会重试。
- 完整性：`photo.bytes` / `photo.sha256` 与远端文件一一对应；`out/catalog.db` 的 `PRAGMA integrity_check` 应为 ok。
- 迁移到本地服务库：把 `out/catalog.db` 的 `resource/photo/photo_group` 直接 `ATTACH` 后 `INSERT ... SELECT` 到 §10 的 `local.db`，文件按 `local_path` 搬到 `image/` 下即可。
- 若平台方开放了 `\\192.168.2.2\image`：用 rsync 按 `photo.file_key` 生成文件清单批量拉取，比走 HTTP 快一个数量级（HTTP 被限在 ~4 MB/s）。

---

## 12. 已知坑与经验值

| 坑 | 现象 | 对策 |
|---|---|---|
| 取流接口的键没有参数名 | 写成 `?key=…`/`?path=…` 一律 204 | 直接把键放在 `?` 后面 |
| 204 假失败 | 约 1/60 概率返回 204，立即重试即 200 | 指数退避重试 3~4 次 |
| 415 | POST 未带 `Content-Type: application/json` | 请求头补齐 |
| 405 | 用 POST 调 `GetFileStream` | 只能 GET |
| HTML 响应 | 网关（如花生壳）拦截时会返回 200 + HTML | 校验 `Content-Type` 与首字节，别把 HTML 当数据 |
| 响应压缩 | 未声明 `Accept-Encoding: identity` 可能被压缩 | 显式声明 |
| Token 有效期 | 3 天，`ExpireDays=3` | 长任务里遇 401 自动重登 |
| 分页 | `PageSize` 过大未测；`PageSize=0` 返回空 Rows 但 `Total` 正常 | 用 100 一批拉全 |
| `Range` 语义 | 是相机中心外接矩形，不是地面覆盖 | 用空三位姿逐张算覆盖 |
| `EpsgId` 不可信 | ENU 资源也写 4326 | 以 XML 的 `SRSId`/`Definition` 为准 |
| 吞吐 | 单连接 4 MB/s，加并发无效 | 全量 96 GiB ≈ 7~8 小时，分批做 |
| 出口 IP 限制 | 网关按访问者 IP 拦截（返回「网站访问受限」页，固定 10974 B） | `python3 ipcheck.py` 记录各网络可用性；长期靠管理员加白 |
| 本机代理 | 本机 Mihomo 把流量塞进 `table 2022`，`GetFileStream` 可能被代理改写 | 需要直连时用 `APILIB_BIND_IP=192.168.150.58`（绑定源地址 + DoH 取真实 IP） |

---

## 13. 本地已有资产（可直接用于复刻验证）

| 路径 | 内容 |
|---|---|
| `out/catalog.db` | 本地 SQLite：369 资源 + 11 数据集 + 8 资源类型 + 23,554 条影像清单（含全部位姿/内参）+ 选区 + 去重表 |
| `out/resources/resources_all.json` | 369 条资源完整记录（含解析后的 MetaData） |
| `out/indexes/<rid>/index.db3` | 8 个倾斜影像索引（23.4 MiB） |
| `out/indexes/<rid>/root.json` | 21 个 3dtiles 根 tileset |
| `out/assets/camera_xml/*.xml` | 8 个空三 XML（55 MiB，含相机内参与逐张位姿） |
| `out/assets/video_snapshot/*.png` | 5 张视频首帧（88 MB） |
| `out/assets/boundingbox_db3/*.db3` | 3 个瓦片包围盒索引 |
| `out/samples/*.jpg` | 16 张抽样影像（72 MB，用于确认数据真伪） |
| `out/aoi/<name>/` | 区域选区结果（`summary.json` / `footprints.geojson` / `preview.png` 卫星底图预览） |

工具脚本：

| 脚本 | 作用 |
|---|---|
| `read_data.py` | 原始样例（只读、不落盘） |
| `apilib.py` | 带 token 缓存 + 源地址绑定的极简客户端 |
| `discover.py` | 一次性摸底（根索引探测） |
| `mirror.py` | `survey` / `images` / `assets` / `dedupe` / `report` |
| `aoi.py` | 按经纬度筛选影像并出卫星底图预览 |
| `geo.py` | 纯标准库投影/ENU/WKT/多边形 |
| `healthcheck.py` | 五合一连通性体检 |
| `ipcheck.py` | 记录「出口 IP ↔ 可用性」，摸清拦截边界 |
| `probe.py` / `scan_api.py` | 接口枚举（复刻时可用来对照） |

---

## 14. 本地复刻服务已实现并验证（`local_service.py`）

只依赖标准库，读 `out/catalog.db` + 已落盘文件，对外提供与远端**完全同构**的 3 个接口，
可让 `read_data.py` / `mirror.py` 等原客户端**零改造**指向本地：

```bash
python3 local_service.py --port 18080 --password '<本地测试密码>'
# 另一个终端
python3 read_data.py --base-url http://127.0.0.1:18080 --username admin --page-size 3
python3 read_data.py --base-url http://127.0.0.1:18080 --username admin \
    --resource-id 4bb288ad-125f-4067-9b57-a19ba347efbf --page-size 3
```

实测结果（与远端输出逐字一致）：

| 调用 | 远端 | 本地复刻 |
|---|---|---|
| 资源列表 | `Total=369` | `Total=369` ✓ |
| 倾斜影像清单（4bb288ad） | `total=9680`，前 3 条 id/宽高/文件键 | 完全相同 ✓ |
| 读单张影像 | 5,586,504 B JPEG | 5,586,504 B JPEG ✓（需该张已下载） |
| 未下载的影像 | 204 | 204（语义一致，可用来验证断点续传） |

实现要点（与远端保持一致，便于将来双向迁移）：
- 登录同样用 **md5(明文)** 校验，签发同样结构的 HS256 JWT（3 天）；
- `MetaData` 同样以**字符串**返回，不做对象化；
- `GetFileStream` 同样把**整段查询串**当文件键；
- 找不到文件同样返回 `204`，POST 同样 `405`；
- 额外提供 `GET /health`（远端没有），返回资源数与可访问文件键数。

启动日志会打印：`资源 369 个 | 可访问文件键 104 个`（键数 = 已落盘的根索引/空三/快照/已下载影像）。

**下一步扩展建议**：在 `local_schema.sql` 的 `file_object` 上补一个 `POST /api/resource/{id}/files` 端点，
把远端缺失的"按资源列文件"能力补上——这样本地服务就严格优于远端（远端做不到的事本地能做到）。
