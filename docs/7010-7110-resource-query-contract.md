# 7010 与 7110 资源查询统一路径设计

> 这是 2026-11-07 从相邻 `agent-web` 项目复制的接口契约快照，供本仓库独立查阅。后续若两端变更，应同步核对两份文档。

## 目标与边界

7110 持有资源目录、版本、空间索引和文件目录；数据卷持有原始文件和瓦片字节。7010 只保存和使用场景图层定义、展示状态及渲染适配，不再把范围查询结果登记为新的资源。范围查询返回原资源 ID、固定的源版本和命中文件的相对路径；场景中的局部视图由 `scene_layer.id` 标识，仍引用原资源 ID。

空间筛选的含义按类型标明：POI/矢量为要素几何相交，ODM 照片可按相机中心或有依据的候选覆盖范围检索，视频按已发布组的产品足迹检索，栅格和 3D Tiles 按瓦片范围相交。瓦片命中是文件级筛选，不是像素或三维几何的精确裁切。没有空间范围、文件目录或版本依据时返回明确的 `index_unavailable` / `extent_unavailable`，不能退化成全量加载并声称已按范围筛选。

## 现有代码中的分叉

| 链路 | 现状 | 收敛点 |
| --- | --- | --- |
| 7110 `spatial_feature`、`oblique_image` | 已有逐要素或逐影像 PostGIS 几何和 GiST 索引；ODM 影像已有 `storage_key`、大小、SHA-256。 | 保留原表和原 ID，补统一查询响应；缺少校验值或覆盖依据的记录明确标状态。 |
| 7110 `pointcloud_resource` / `pointcloud_version` / `pointcloud_asset` | 有整项 `spatial_extent`、版本和文件清单，但 `pointcloud_asset` 没有逐瓦片几何与 Tileset 层级。 | 发布或回填时解析每个 Tileset JSON，登记瓦片节点、内容文件、世界变换和 WGS84 包围范围。 |
| 7110 `raster_collection` / `raster_epoch` 和旧 Raster TMS | 时序栅格只有期次范围；旧 TMS 查询从 `tilemapresource.xml` 计算候选，再检查镜像缓存。 | 登记每个实际存在瓦片的 `(z,x,y)`、路径、大小、SHA-256 和 WGS84 范围；查询只返回目录中已登记的文件。 |
| 7110 `legacy_subsets.py` / `legacy_oblique_subsets.py` | 创建目录中的派生资源 ID；SQLite 保存源指纹、范围和映射，文件字节仍复用源镜像。 | 只读保留旧 ID 的兼容读取；新写入改为原资源上的范围查询及 `scene_layer.scope_geometry`。 |
| 7110 `legacy.py:create_scene_layer` | `legacy_resource` 带范围时强制先有 `subset_resource_id`；ODM 和栅格集合拒绝范围。 | 按来源类型允许有索引支持的范围图层，服务核实命中和版本后返回渲染入口。 |
| 7010 资源树和 Agent | Agent 的 `load_scene_*_subset_from_plot` 创建子资源再关联；通用 `loadSingleResource` 仍从目录行生成旧 SDK 图层。 | Agent 保存查询定义，树节点使用 `layer_id`；局部 3D/栅格改用 7110 给出的范围渲染入口，源资源 ID 只用于检索和文件读取。 |

## 唯一身份和持久状态

| 字段 | 含义 | 可否随范围查询改变 |
| --- | --- | --- |
| `source_kind` + `source_resource_id` | 原目录资源、ODM 包、点云资源、栅格集合、视频组或矢量数据集的身份。 | 否。任何范围查询均不产生新的资源 ID。 |
| `source_version_id` | 已发布的不可变版本；旧镜像使用根清单及文件目录的内容指纹形成不可变版本键。 | 查询时必须确定。场景图层默认钉住版本；显式“升级到新版本”才更换。 |
| `relative_path` | 版本数据卷内的规范化 POSIX 相对路径。 | 是，随命中文件而变；不得包含绝对路径、`..`、外部 URL。 |
| `scene_layer_id` | 同一原资源在一个场景中的一个展示实例；可有不同目录、范围和显隐。 | 否。调整范围时更新图层定义并重新计算命中。 |
| `scope_geometry`、`scope_crs`、`selection_mode` | 场景要重放的查询定义。 | 可编辑；`scope_crs` 统一为 EPSG:4326。 |

同一原资源可在同一场景有多个图层，分别引用不同范围。场景树节点 ID 使用 `scene_layer_id`，目录和显隐仍由 `scene_layer` 持久化。查询结果集属于可重算的读取结果，不登记为资源；如果业务确需冻结文件列表，应另外设计“结果快照”并明确其版本和保留策略，不借用资源 ID。

当前 `scene_layer.source_kind=pointcloud_version` 时，`source_id` 实际是版本 ID。迁移到本契约后应改为点云**资源** ID，版本 ID 进入 `source_version_id`；迁移旧图层时从 `pointcloud_version.resource_id` 反查，保留原 `scene_layer.id`。旧目录桥接的已发布点云也使用同一个点云资源 ID，不能因为读取路由不同而生成第二个来源身份。

## PostGIS 文件目录与空间索引

新增统一的已发布文件目录和类型专属空间细表。以下为目标字段，实际迁移 SQL 应分阶段执行，不能在回填完成前把旧路径切断。

```text
resource_file
  source_kind, source_resource_id, source_version_id, relative_path  -- 复合唯一键
  storage_namespace, size_bytes, sha256, media_type, file_role
  geom_wgs84, extent_status, created_at
  raster_zoom, raster_x, raster_y, acquisition_date              -- 栅格可选

tileset_node
  source_kind, source_resource_id, source_version_id, document_path, node_path
  parent_document_path, parent_node_path, content_relative_path
  bounding_volume_json, world_transform_json, geom_wgs84
  geometric_error, refine, node_status
```

`resource_file.geom_wgs84` 用 GiST，`(source_kind, source_resource_id, source_version_id, relative_path)` 用 B-tree；栅格再对 `(source_resource_id, source_version_id, acquisition_date, raster_zoom, raster_x, raster_y)` 加唯一索引。`tileset_node.geom_wgs84` 用 GiST，父节点键和内容路径加 B-tree。图像、视频和栅格文件元数据可从各自现有表批量回填；原表仍保留相机、期次、编码等业务字段。矢量要素继续使用 `spatial_feature` 的几何索引，查询响应给 `feature_id` 与原始导入文件引用，不把每条要素伪装成单独文件。

入库事务顺序为：先将文件写到数据卷临时目录并验证大小、SHA-256 和内部引用；计算范围并登记目录与瓦片节点；提交 PostGIS 版本状态为 `published`；最后原子切换资源的当前版本。文件存在但索引失败时版本不可发布；索引存在但文件丢失时读取返回 `asset_unavailable` 并告警，不能静默换用其他版本。文件读取只从经目录登记的 `storage_namespace + relative_path` 定位，解析后再次检查仍在允许的数据卷目录内。

### 3D Tiles

逐个解析根及子 Tileset 的 `root`、`children`、`content` / `contents`、`boundingVolume`、`transform`、`refine` 和相对 URI。把继承后的世界变换应用到瓦片包围体，保留原始包围体和变换供重建清单。`box`、`sphere`、`region` 均转换为保守的 WGS84 检索几何；边界处允许多取瓦片，不允许漏取。外部 URL、隐式瓦片、缺少有效包围体及同一路径的冲突变换，先标为 `unsupported_source` 并阻止范围发布；不能靠整项资源范围假装有逐瓦片索引。未被任何内容直接引用的辅助文件仍登记在文件目录，但无空间范围。

查询选中与范围相交的内容节点和维持有效树结构所必需的祖先；保留原 `geometricError`、`refine`、变换和扩展。对未命中的内容移除 `content`，对仍有后代的空祖先保留节点。每份子 Tileset JSON 按同一范围递归过滤。返回的清单是请求时生成的 JSON，不写入资源目录，也不复制二进制；内容 URI 仍指向同一源版本的原始文件。服务必须核查每个返回 URI 对应目录项，不能通过 URL 请求到查询范围外的内容文件。

### Raster TMS 与时序栅格

入库时遍历实际存在的瓦片，根据 tilemap 或期次矩阵定义计算每个 `(z,x,y)` 的 WGS84 多边形，登记实际文件路径、大小和 SHA-256；保留集合/期次总范围供快速发现。范围查询在所有所需 zoom 上使用 `ST_Intersects`，返回已登记文件及瓦片坐标。场景加载器沿用 TMS 瓦片 URL 模板，但请求必须带 `scene_layer_id`，7110 按该图层范围和版本验证每个瓦片；范围外返回 404，避免浏览器按模板下载整集。无目录项时不能仅由矩阵坐标推测文件存在。

### ODM、旧倾斜影像、视频和矢量

- ODM 以现有 `oblique_image.id` 为影像记录 ID，响应保留原包 ID、版本和 `storage_key` 对应的相对路径。`camera_centers_inside` 与 `candidate_footprint_intersects` 是不同模式；无可靠地面覆盖时不得称为精确覆盖。
- 旧 `ImageAirImage` 从已有照片记录和数据卷/镜像回填文件目录；源照片缺少可验证相机位置的，要保留 `unlocated` 状态并说明查询可能不完整。
- 视频以已发布 `video_group` 和 `video_group_asset` 为目录，空间筛选依据资料组足迹或推定相机中心；目前没有逐帧轨迹，不把整段视频拆成空间瓦片。
- POI/矢量仍在 PostGIS 按要素几何检索；可在图层上保存属性条件和范围。查询定义随源版本重放，固定要素列表需独立快照语义。

## 7110 对外契约

统一查询端点使用类型无关的外壳和类型专属的 `selection_mode`，保持原有只读端点作为迁移期适配器：

```http
POST /spatial/resources/query
Authorization: Bearer <user JWT>
Content-Type: application/json

{
  "source_kind": "legacy_resource",
  "source_resource_id": "原资源 UUID",
  "source_version_id": "已发布版本键，可省略以使用当前版本",
  "geometry": {"type":"Polygon","coordinates":[...]},
  "crs": "EPSG:4326",
  "selection_mode": "intersecting_tiles",
  "limit": 200,
  "cursor": null
}
```

响应的每一项都明确 `item_kind`、`relative_path`、可读取的服务路径、几何依据和状态；结果按稳定复合键分页，禁止用 offset 扫描海量目录。返回 `source_resource_id` 与实际 `source_version_id`，不包含 `subset_resource_id`。`total_matches` 可为估计值，但 `has_more` 与下一页游标必须准确。无索引、版本不匹配、范围过大、文件缺失分别使用结构化错误码；`empty` 与 `index_unavailable` 不得混淆。

```json
{
  "source_kind": "legacy_resource",
  "source_resource_id": "原资源 UUID",
  "source_version_id": "版本键",
  "selection_mode": "intersecting_tiles",
  "items": [{
    "item_kind": "tile_content",
    "relative_path": "Data/Tile_0/0.b3dm",
    "file_path": "/spatial/resources/原资源 UUID/versions/版本键/files/Data/Tile_0/0.b3dm",
    "size_bytes": 12345,
    "sha256": "64 位十六进制校验值",
    "match_basis": "tileset_bounding_volume"
  }],
  "next_cursor": null,
  "precision_notice": "瓦片包围体相交，不执行几何裁切"
}
```

场景写入只创建/更新图层：`POST /api/scenes/{scene_id}/layers` 接受原资源 `source_id`、`source_version_id`、`scope_geometry`、`selection_mode`、目录与显隐。服务先按相同查询契约确认索引与命中，再保存图层；响应给 `layer_id` 和渲染描述。`GET /api/scenes/{scene_id}/layers` 重新打开时返回同一查询定义、固定版本和渲染描述。对 3D Tiles 的渲染入口形如 `GET /spatial/resources/{source_id}/versions/{version_id}/tileset.json?scene_layer_id={layer_id}`，递归子清单和文件 URL 均携带该图层上下文；文件服务按当前用户访问权、图层范围和命中目录项核验。`scene_layer_id` 是展示实例，不是派生资源 ID。临时查询仅返回清单/路径，不会写场景。

7010 同源代理继续把 `/data-api/` 转到 7110。7110 返回相对于数据服务根路径的 `file_path` / `entry_path`，7010 统一补 `/data-api`，不能硬编码主机或把数据卷路径暴露给浏览器。3D Tiles SDK 若无法给子资源请求附 Bearer，需由 7010 的新加载适配器使用 Cesium `Resource` 传递授权头；不得把长期 JWT 写到 Tileset URL。必须先验证当前 TJH 加载器的请求头能力，再切换旧 Mesh/PointCload 路由。

## 7010 调用链收敛

1. 全局资源目录只用于发现 `source_kind/source_resource_id`、当前发布版本和可用的空间检索模式。`search_scene_resource_catalog` 不再把派生资源作为新目录项展示。
2. “预览标绘范围”调用 `POST /spatial/resources/query` 或只读计数端点，显示源 ID、版本、命中数量、精度与缺失状态；不写图层。
3. “加载到场景”提交 `source_id + source_version_id + scope_geometry + selection_mode` 给 `/api/scenes/{scene_id}/layers`，得到 `layer_id`，刷新统一场景树并核验同一范围、版本、目录和默认显隐。
4. 3D Tiles 使用服务返回的筛选后 Tileset 入口；Raster TMS 使用带图层上下文的瓦片模板；ODM 影像列表请求带图层范围；POI/矢量带范围及属性条件。图层勾选只改 `default_visible`，不改变源文件或查询范围。
5. Agent 将 `create_scene_resource_subset_from_plot` 和 `load_scene_*_subset_from_plot` 迁移为“只读预览”与“保存范围图层”工具。新返回字段为 `source_resource_id`、`source_version_id`、`scene_layer_id`、`selected_count`、`selection_mode` 和 `render_status`；移除提示词中“创建子资源 ID”和“先移除原资源”的前提。已有工具名可在一个兼容窗口中保留，但返回 `deprecated` 并转调新行为，之后再删除。

## 迁移和上线顺序

1. **盘点与备份**：导出当前 `scene_layer`、旧 `scene_resource`、两个子集 SQLite、PostGIS 目录及数据卷清单；统计旧子集 ID 在场景中的引用，标出源资源与版本指纹不匹配、文件缺失、无空间范围的数据。当前 7110 目录不是 Git 工作树，修改前另留代码快照。
2. **影子索引**：新增 PostGIS 文件目录和瓦片节点表，在入库发布流程写入；对已发布点云 3D Tiles、旧 Mesh/PointCload 镜像、旧/时序 TMS 回填。回填按批次可续跑，用大小、SHA-256、文件总数和随机瓦片对照旧路径；先只读比较，不切现有渲染。
3. **统一只读查询**：实现统一端点和动态 Tileset 清单；对每类资源对照现有查询结果，检查所有返回文件实际可读、范围外文件不可读、根/子 Tileset 引用无断链。索引未就绪时返回明确错误。
4. **场景模型迁移**：给 `scene_layer` 补 `source_version_id`、`selection_mode`；将旧子集图层的 `subset_resource_id` 解析成原 `source_id`、原范围和源版本，保留原 `layer_id`、父目录、排序及显隐。若旧子集指纹与当前可用文件版本不符，不自动改指新版本：保留旧兼容读取并列入人工核对。旧场景关联先迁移为 `scene_layer`，再停止合成旧节点，避免树中重复。旧子集 URL 和 ID 在迁移窗口只读兼容，不再创建新记录。
5. **7010 切换**：统一资源选择、Agent、树勾选/删除和各渲染器。按资源类别逐项灰度，观察浏览器实际请求的源 ID、版本、图层 ID 与文件路径；完成后停用三个 `Create*Subset` 调用和旧 SDK 的范围子集入口。
6. **回滚**：切换前保留旧路由、SQLite 和场景表快照。若新索引或渲染失败，按类别切回旧读取路径；不删除旧子集记录或数据卷文件，直到所有旧场景可用新图层重开且对账完成。

## 验收条件

- 同一原资源在不同场景范围中保持相同 `source_resource_id`；仅 `scene_layer_id` 不同，目录中没有新增资源行。
- 重进场景后范围、版本、目录和显隐一致；源资源发布新版本不会暗中改变已钉住版本的图层。
- 3D Tiles 根和递归子清单均可加载，所有引用均在命中目录内；范围外二进制请求被拒绝，缺索引时不加载全量。
- 栅格跨 zoom 仅请求范围内已登记瓦片；ODM 返回原照片 ID 与相对路径；视频和矢量使用各自真实空间粒度。
- 7110 数据卷字节与 PostGIS 目录按版本、路径、大小、SHA-256 对账；查询和动态清单不写入新的资源、子集 SQLite 或瓦片副本。
- 7010 与 Agent 的响应统一报告原资源 ID、源版本、场景图层 ID、命中依据和精度限制；失败时不声称已完成局部加载。

## 7010 当前对接状态（2026-11-07）

7010 已新增 `QuerySpatialResources` 调用，并将 Agent 的旧子资源预览/创建/加载工具替换为 `query_scene_resource_files_from_plot` 与 `save_scene_resource_scope_from_plot`。后者先查询至少一条命中文件，再把 `source_kind`、原 `source_id`、查询返回的 `source_version_id`、WGS84 `scope_geometry`、`scope_crs`、`selection_mode`、可选 `parent_folder_id` 和 `default_visible=false` 写入场景图层。写入后回读核对这些字段；不一致时尝试撤销刚创建的图层。旧子资源只保留既有场景读取，7010 不再提供新的子资源创建入口。

7110 对接时须保证统一查询响应含 `source_kind`、`source_resource_id`、非空 `source_version_id`、`selection_mode`、`items`、布尔 `has_more`、`next_cursor`。当前 7010 对文件型来源要求每项含 `item_kind`、安全的 POSIX `relative_path` 和以 `/spatial/` 开头的服务内 `file_path`。场景图层列表须回传 `layer_id`（或 `id`）、`source_kind`、`source_id`、`source_version_id`、`selection_mode`、`scope_geometry`、`parent_folder_id` 和布尔 `default_visible`。若 7110 对文件路径或要素项采用不同结构，应在联调时同步更新 7010 的响应校验与文档，而非跳过校验。

当前新范围图层在 7010 保存为隐藏，资源树和旧 SDK 加载器阻止按完整源资源路径显示。动态 3D Tiles、范围栅格和 ODM 范围照片的 7010 渲染适配尚未完成；这一步需在 7110 提供带图层上下文的有效入口后逐类联调。普通完整资源和既有 POI、视频工具暂沿用现有接口，不代表全链路已完成切换。

## 7110 接口上线后的 7010 核查（2026-11-07）

7110 `/healthz` 与 `/openapi.json` 在线可访问；未带 JWT 的统一查询返回 401。代码核查确认旧目录三维资源在查询时规范成 `pointcloud_resource`，场景保存也使用此来源身份；7110 还会轮转、反向及四舍五入规范化多边形环。7010 已对应调整响应校验和范围回读比较，并补充直接按 `pointcloud_resource` ID 查询的入口。旧 ImageAirImage 与旧 RasterTMS 在新查询中尚无逐文件索引，不能作为新范围图层保存。

7110 当前场景树节点不附 `source_version_id`、`selection_mode` 与 `render_descriptor`，而且 `pointcloud_resource` 节点未指定 `type`。7010 普通场景加载时会并行读取场景图层列表，把这些字段补到树节点；当列表不可读时，仍根据来源和 `scopeGeometry` 阻止旧加载器误取完整源资源。分享场景尚无图层列表补充，范围图层保持不可显示。待 7110 直接在树节点返回这些字段后可移除 7010 补充逻辑。

**仍未完成的联调**：7010 需要新增带 Bearer 的动态 3D Tiles 和范围 TMS 加载器，处理 7110 清单中以 `/spatial/` 开头的子资源 URI，经 `/data-api` 代理读取；ODM 范围影像也需由图层定义筛选照片。当前范围图层继续隐藏，不能作为已渲染结果验收。需用登录态验证实际的查询、保存、重开、边界内外文件请求；只读健康检查不足以证明这些流程。
