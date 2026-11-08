# 旧前端与 7110 兼容范围

7010 前端的 `SYSTEM_REQUEST_URL` 使用同源 `/legacy-data`，由 Nginx 转发到 7110。旧平台已取得的数据保存在 `/workspace/legacy/out` 和 `/workspace/data/legacy-mirror`。7110 运行时只查这两处本地资产，不登录或访问旧数据服务。

2026-11-02 核对并补采：资源目录 370 条、数据集 16 条、资源类型 24 条；23,599 个已知非空 FastDFS 键均已在本地，合计 109,852,086,999 B。新增了 8 组倾斜影像的 23,554 条 ARLayer 图片索引，原始照片继续使用既有 FastDFS 文件缓存。保存了前端需要的场景、模型、TMS 根文件和视频方向信息响应，并按 3D Tiles 的相对路径采集地形子文件。镜像索引的条数和占用会随断点续传任务继续增长，不能把资源目录完整等同于所有文件完整。

## 认证与用户数据

| 接口 | 行为 |
| --- | --- |
| `POST /api/Login/Login` | `Password` 提交明文密码的 MD5；返回 3 天 HS256 JWT，`AuthInfo` 保持 JSON 字符串 |
| `POST /api/Login/Register`、`POST /api/Register/Register` | 公开注册普通只读账号；请求不能指定角色或权限码 |
| `POST /api/Login/Logout` | 撤销当前 Bearer JWT；重复登出返回成功 |
| `GET /api/User/GetByToken` | 返回有效会话对应的本地用户；无效、已登出或停用账号返回 401 |

每次登录生成独立 `LoginRecordId` 与令牌。`legacy_session` 仅保存令牌 SHA-256 摘要、用户 ID、到期时间和撤销时间；登出只撤销当前令牌。服务不使用会话 Cookie，前端应清除本地 Token。静态 `credentials.json` Bearer 是服务级凭据，不绑定个人账号。

内置管理员及服务管理员 Bearer 可执行公共数据集和 PostGIS Shapefile 的写入操作。普通用户可读公共数据并读写自己的标绘。`/spatial/annotations` 从账户 JWT 确定所有者，每条查询和修改均限定该用户；服务级 Bearer 不能读取个人标绘。详见[空间接口说明](spatial-postgis.md#用户标绘)。

## 本地兼容数据

| 接口 | 本地来源与缺失行为 |
| --- | --- |
| `POST /api/cim/CIMScene/GetCIMSceneResourceByPage` | `/workspace/legacy/out/resources` 的资源快照，支持分页和主要过滤 |
| 图层大类、资源类型、数据集下拉接口 | 本地字典文件；资源类型可读取已保存的对应请求响应 |
| `POST /api/Resource/GetByPage` | 优先本地精简资源快照；不存在时读取已保存响应或从完整资源快照推导 |
| `POST /api/ArScene/GetByPage` | 只读取 7110 场景库，按筛选条件分页；旧接口镜像场景不再列出 |
| `POST /api/ArScene/Add`、`GET /api/ArScene/GetByNameAndId/{id}` | 本地场景创建与名称冲突校验；创建的场景具有可读取的基础配置和空资源树 |
| `GET /api/ArScene/GetDetail/{id}`、`GetSceneConfig/{id}`、`GET /api/cim/CIMScene/GetCIMSceneConfig/{id}` | 只读取 7110 场景库；旧镜像 ID 返回 404 |
| `GET /api/ArScene/GetResourceTreeForScene/{id}`、`GET /api/cim/CIMScene/GetResourceTreeForCIMScene/{id}` | 只从 7110 场景、资源关联与个人标绘生成树；空场景不借用旧缓存模板 |
| `POST /api/cim/CIMScene/CIMSceneResourceRelevance`、`POST /api/ArScene/ArSceneResourceRelevance`、`AddCIMSceneResourceByCustomType`、`/api/ArScene/AddArSceneResourceByCustomType`；`DELETE /api/cim/CIMScene/DeleteCIMSceneTreeLayer/{sceneId}/{resourceId}`、`/api/ArScene/DeleteTreeLayer/{sceneId}/{resourceId}` | 本地场景资源关联/移除；资源 ID 必须存在于本地资源目录，关系保存在数据卷数据库 |
| `/api/ArSceneTrees/Add`、`Edit`、`DeleteBySceneIdAndTreesId` | 自定义资源树目录的新增、改名和删除；目录及其关联数据本地持久化 |
| `GET /api/DownLoad/GetFileStream?<裸FastDFS键>` | 先查本地资源目录，再查已保存的镜像对象；键不存在时返回 204 |
| `GET /api/ARLayer/GetNumARNote/{资源ID}`、`FetchARNote?arlayerUrl=...&startPos=...&num=...` | 本地 ARLayer 索引库；8 个倾斜影像资源的图片 UUID、相机信息和分页顺序与远端一致 |
| `GET /api/ARLayer/GetImageFileByImageId/{图片UUID}` | 将图片 UUID 映射到本地 FastDFS 原图；文件缺失时返回 204 |
| `GET /api/Mesh/GetMesh3DTilesFile/{资源ID}/{相对路径}` | 已保存的地形 tileset、JSON 和 GLB 原始响应；已发布的版本化 3D Tiles 也会按 Mesh 资源 ID 映射到相同路由 |
| `GET /api/PointCload/GetPointCloud3DTilesFile/{资源ID}/{相对路径}` | 点云根索引及持续补采的子 JSON/GLB；可按资源 ID 读取已发布的版本化点云 3D Tiles；旧前端 SDK 的控制器确实拼作 `PointCload` |
| `POST /spatial/resource-subsets`、`GET /spatial/resource-subsets/{子资源ID}` | 管理员按 WGS84 Polygon/MultiPolygon 创建并查询 `intersecting_tiles` 虚拟子资源；派生 ID 加入资源目录，旧 Mesh/PointCload 路由动态筛选 tileset JSON 并复用源瓦片文件 |
| `GET /spatial/rasters/{资源ID}/tiles?bbox=...&zoom=...` | 从本地 TMS tilemap 空间参考计算 bbox 命中的瓦片，并标记每张瓦片是否已镜像 |
| `GET /api/Raster/GetTmsFile/{资源ID}/{层级}/{列}/{行}.png` | 已保存的 22 个 TMS 根 XML 及持续补采的非空 PNG 瓦片；未缓存路径仍返回 404 |
| `GET /api/Video/GetDirectionalInfoList/...`、`GetInitDirectionalInfoList/...`、`FetchVideoDirectionalInfo/...` | 已保存的方向视频元数据和方向信息；原始 MP4 文件尚未确认可下载 |
| `GET /api/Video/GetLiveMediaDirectionalInfo/{资源ID}` | 通过本地镜像读取实时视频方向信息；未保存时返回 404 |
| `GET /api/Raster/GetTSMCTBFile/{资源ID}/...`、`GET /api/VectorTiles/GetFile?{资源ID}/...`、`GET /api/EntityModel/GetGlbFile/{资源ID}/...` | 为旧 Cesium 加载器开放匿名本地镜像读取；仅返回完全匹配的已缓存字节，缺失时返回 404 |
| `GET /geoserver/...` | 提供 GeoServer URL 的本地镜像入口；目前只命中已缓存的完整 URL/查询串响应，不是动态 WMS/WMTS/WFS 服务 |
| 其他 `Get*`、`Query*`、`Check*` 旧读取动作 | 仅返回本地镜像中完全匹配的方法、路径和请求体；未保存时返回 404 |

场景写入仍使用旧前端字段和响应包装：新建请求为 `{"Name":"场景名","Remark":"描述","SceneTreeType":1,"SceneEngineType":2}`；名称校验响应的 `Data` 是“该名称已被其他场景占用”的布尔值。按类型关联接口接收数组，每项至少包含 `ResourceId`、`ArSceneId`、`Type`、`Label`、`MetaData`；自定义目录关联请求为 `{"SceneTreesId":"目录UUID","SceneResourceList":[...]}`。关联成功的 `Data` 返回更新后的场景树，`GetResourceByCIMScene/{sceneId}` 返回已关联资源 ID 数组。移除接口按场景 ID 和资源 ID 定位关系，不会删除资源库中的资源文件。

兼容写入动作不会转发。文件接口查询串中的完整裸路径就是键，不能写为 `?key=...`。旧前端 SDK 通过无认证的图片、3D Tiles、TMS 与视频方向读取路径加载图层，这些已确认在远端公开的路径在 7110 上也可匿名读取。用户和标绘的写入仍由本地账户权限约束。

标绘兼容路由已连接到现有 PostGIS `user_annotation` 存储：`POST /api/ArLabel/Add`、`POST /api/ArLabel/AddByForm`、`PUT /api/ArLabel/Edit`、`POST /api/ArLabel/EditByForm`、`DELETE /api/ArLabel/Delete/{场景ID}/{标绘ID}`，以及名称校验、附件列表和附件读取路由。JWT 确定记录所有者；读取场景树时只把当前账户自己的标绘合并到 `PlottingGroup`。旧访客/分享场景树镜像入口返回 404；新的场景分享能力尚需单独实现。上传附件保存在 7110 数据卷的 `spatial/annotation-files`。

场景兼容写入要求账户 JWT，且仅管理员可创建场景、关联/移除资源及增删改自定义目录；普通用户仍只能读取场景和资源。场景及其资源关系保存在 `/workspace/data/legacy_scenes.sqlite3`（文件权限 `0600`），不修改只读的远端镜像数据库。场景对已登录用户共享可读，创建者 ID 仅用于审计；这与账户私有标绘隔离规则不同。按类型资源树从场景关联构建分组，自定义树目录及其资源关联在同一 SQLite 文件中持久化。旧镜像场景对象留在数据卷归档，不参与场景接口读取。

旧前端 `POST /api/ArScene/GetSceneResourceByPage` 作为别名调用当前资源快照分页实现，与现有 `/api/cim/CIMScene/GetCIMSceneResourceByPage` 共用同一份资源数据。对有功能冲突的登录、资源目录、FastDFS 文件和空间/点云 API，仍由当前显式实现优先处理；通用缓存入口只处理未被显式路由接管的 GET/POST 读取请求，不转发写操作。

版本化 `/pointcloud` 中已发布、入口为 `tileset.json` 的 3D Tiles 包会动态合并到旧资源目录。带 `.b3dm` 内容且未声明 `content_kind` 的版本按 `Type=1000012`（Mesh/Terrain3dtiles）登记；带 `.pnts` 内容的版本按 `Type=13000006`（PointCloud3Dtiles）登记。也可在资源或版本 `metadata` 中显式设置 `content_kind`（`mesh`、`model`、`terrain` 或 `pointcloud`），以避免只凭瓦片文件扩展名推断。旧目录使用稳定的 pointcloud `resource_id`，场景关联仍按旧协议完成；文件请求由 Mesh 或 PointCload 路由映射到该资源最新发布版本的已登记资产。瓦片相对 URI 中的 `./` 与重复斜杠会安全规范化，`..` 路径仍会拒绝。此桥接将旧前端平移字段 `SRSOrigin` 默认置零，因为地理定位由 tileset transform 承担；源投影原点单独保存在 `SourceSRSOrigin`，只有明确提供 `legacy_srs_origin` 时才作为旧前端平移。条目在资源/场景请求时刷新，最长约 60 秒出现；未发布版本、入口文件布局不兼容的版本和无法判定内容类型的版本不会进入旧目录。

区域 3D Tiles 子资源仅支持目录类型 `1000012`（旧前端 `Terrain3dtiles`/Mesh 路由）和 `13000006`（`PointCloud3Dtiles`/PointCload 路由）。子集数据只保存源 ID、源指纹、AOI 和派生资源目录行，不复制瓦片二进制；浏览器读取派生入口后，7110 递归过滤子 tileset JSON，并将命中二进制请求映射回源资源镜像。该筛选依据瓦片包围体，边界瓦片可能含范围外几何；严格裁切、隐式瓦片和外部内容 URL 尚不支持。实现和请求示例见[区域 3D Tiles 子资源接口](spatial-resource-subsets.md)。

镜像索引 `/workspace/data/legacy-mirror/mirror.sqlite3` 记录方法、路径、规范化 JSON 请求、响应类型、大小、SHA-256 和抓取时间；`objects/` 保存原始字节。管理员可用 `GET /admin/legacy-cache` 查看索引摘要。本地镜像损坏或数据库不可用时返回 503；普通缺失不会触发网络请求。

`GetMesh3DTilesFile` 已证实可以读取地形子 JSON/GLB；其 JSON 链接约 41.5 万个相对路径，现由 `remote-data/mirror_mesh_remaining.py` 分批通过 Wi-Fi 下载、经有线 LAN 导入 7110。旧前端对点云使用 `PointCload/GetPointCloud3DTilesFile`，该路径的子 JSON/GLB 已证实可读。10 个点云根中虽然只有 7 组不同内容，但同一组内的 GLB 字节可以不同，所以 10 个点云资源都独立采集与存储；地形资源也按 ID 分别保存，避免推断性的字节复用。TMS 子瓦片按资源和坐标有些是非空 PNG、有些是空响应，`remote-data/mirror_tms_remaining.py` 只保存非空瓦片。新地形资源 `68e31d19-5204-4904-b2d6-ac26a411fadd` 连根入口都未给出。视频方向信息含 MP4 的 FastDFS 键，原片通过 Wi-Fi 请求 90 秒仍未收到响应字节。

### 参考前端的替换边界

`../remote-data/agent-web-master/public/config/index.js` 将主 API 配为 `SYSTEM_REQUEST_URL`（远端 16761），另将 `BASE_API_GEOSERVER` 配为独立的 GeoServer（远端 29292）。把 `SYSTEM_REQUEST_URL` 指向 7110 后，旧前端会从 7110 读取已镜像的目录、元数据、FastDFS 文件、地形/点云 3D Tiles、TMS 和视频方向信息。已从 7110 匿名验证地形 `tileset.json`、点云根 JSON 与 TMS 根 XML 均返回 200；来自 `http://192.168.150.41:7010` 的 7110 CORS 预检也返回 200，允许 `Authorization` 和 `Content-Type` 请求头。

当前资源快照有 370 条：地形 3D Tiles 13 条、点云 10 条、TMS 22 条、航空倾斜 8 条、WMS 236 条、WMTS 51 条、WFS 25 条、定向动态视频 5 条。WMS/WMTS 通过独立 GeoServer 加载；WFS 元数据中的 `DataUrl` 指向内网 `192.168.2.2:8080`。因此，只改 `SYSTEM_REQUEST_URL` 能替换主数据 API，但不能让依赖 GeoServer 或内网 WFS 的图层自动转到 7110。参考 SDK 还包含 `VectorTiles/GetFile` 和 `Raster/GetTSMCTBFile` 加载器；虽然 7110 已为这类路径增加缓存读取入口，这两种资源类型不在当前 370 条资源快照中，未缓存时仍无法提供数据。`EntityModel/GetGlbFile` 和实时视频路径也遵循相同的本地缓存规则。

2026-11-02 对远端 GeoServer 的轻量检查：通用 WMS `GetCapabilities` 返回 200（318,951 B）；通用 WMTS capabilities 返回 400，报错指出 GWC 配置与 GeoServer 图层不同步；按一个 WMTS 资源 UUID 请求其 workspace 路径返回 404。也就是说，这个独立依赖目前可提供 WMS 能力文档，但 WMTS 访问有实测故障。

要做到页面全链路离线替换，还需为实际使用的 GeoServer 图层提供本地 WMTS/WMS 服务，并解决内网 WFS 数据源；前端的 `BASE_API_GEOSERVER` 需要指向这项本地服务。历史探测与采集过程见 `remote-data/REMOTE_ACCESS_SPEC.md` 和 `remote-data/DATA_SURVEY.md`。

## 补采任务

双网工作站上启用了三个可续传的用户级 systemd 服务：`deqing-mesh-mirror.service`、`deqing-pointcloud-mirror.service` 和 `deqing-tms-mirror.service`。它们分别下载地形子文件、点云子文件和非空 TMS 瓦片，分批导入 7110。用户级 systemd 已开启 linger，退出登录后任务仍可运行。查看进度：

```bash
systemctl --user status deqing-mesh-mirror.service deqing-pointcloud-mirror.service deqing-tms-mirror.service
journalctl --user -u deqing-mesh-mirror.service -u deqing-pointcloud-mirror.service -u deqing-tms-mirror.service -f
```

任务状态保存在 `remote-data/out/transfer-20261102/` 下各类的 `mesh-cache`、`pointcloud-cache`、`tms-cache` SQLite 清单；地形和点云任务在开批次前通过 Wi-Fi 检查远端根文件，连接不可用时每 60 秒重试；无 HTTP 响应的传输错误会重新入队，HTTP 级空瓦片则保留为失败记录。已完成的文件不会重复下载。服务器缓存只接受大小、SHA-256 校验通过的对象；7110 不执行远端回源。
