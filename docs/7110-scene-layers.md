# 7110 场景目录与区域图层接口

场景树是索引。目录、图层实例、默认显示意图和 WGS84 区域保存在 `legacy_scenes.sqlite3` 的 `scene_layer_folder` 与 `scene_layer` 表中；照片、瓦片、矢量要素及点云文件仍由资源服务提供。7110 场景库中的 `scene_resource`、`scene_poi_layer`、`scene_video_layer` 继续读取；旧镜像场景树及仅属于它的 `legacy_scene_resource` 不再通过场景接口提供。个人标绘每次按当前 JWT 用户合并，不进入公共场景索引。

## 权限与持久接口

所有接口要求 `Authorization: Bearer <登录 JWT>`。GET 允许有效账户，写入仅允许管理员。图层的删除只解除当前场景关联，不删除源资源或区域子资源。

| 方法和路径 | 说明 |
| --- | --- |
| `GET /api/scenes/{scene_id}/folders` | 列出持久目录 |
| `POST /api/scenes/{scene_id}/folders` | 新建区域子目录，传 `label`、`layer_category`、可选 `parent_id`、`sort` |
| `PATCH /api/scenes/{scene_id}/folders/{folder_id}` | 改名、排序、移动；不能移入自己的子目录 |
| `DELETE /api/scenes/{scene_id}/folders/{folder_id}` | 仅删除空目录 |
| `GET /api/scenes/{scene_id}/layers` | 列出场景图层实例 |
| `POST /api/scenes/{scene_id}/layers` | 关联一个图层，支持 `request_id` 幂等 |
| `PATCH /api/scenes/{scene_id}/layers/{layer_id}` | 修改 `display_name`、`parent_folder_id`、`default_visible`、`sort` |
| `DELETE /api/scenes/{scene_id}/layers/{layer_id}` | 移除单个实例 |
| `POST /api/cim/CIMScene/UpdateTreeResourceSort` | 兼容旧前端拖拽，接受 `SceneId` 与 JSON 字符串 `SceneTreeJson`；仅持久化已知目录和图层排序 |
| `GET /api/ArScene/GetResourceTreeForScene/{scene_id}` | 旧树结构中叠加持久目录和图层；CIM 同名树路径也生效 |

`layer_category` 取 `imagery | terrain | model | pointcloud | poi | vector | raster | video`；`source_kind` 取 `legacy_resource | spatial_dataset | pointcloud_version | video_group | oblique_resource`。一个源允许建立多个图层实例，各有独立的 `layer_id`、目录、范围和显示意图。未指定 `parent_folder_id` 时自动建立该类别的稳定目录。树叶的 `id` 是 `layer_id`，旧加载器所需的全局资源 ID 放在 `attribute.Id`；另附 `sourceId`、`subsetResourceId`、`scopeGeometry`、`scopeCrs`。`attribute.IsChecked` 取 `default_visible`，表示下次进入场景时尝试显示，不保证文件实际可读。树叶的 `availability` 若为 `unavailable` 会给出目录缺失等原因；`indexed` 仍须由前端加载器检查文件。

## 创建区域图层

先以有效账户做只读预览；选中来源后由管理员创建区域子资源；最后由管理员把子资源关联场景。

```http
POST /spatial/resource-subsets/preview
Content-Type: application/json

{"source_resource_id":"<旧 3D Tiles 资源 UUID>","geometry":{"type":"Polygon","coordinates":[[[120.0,30.0],[120.01,30.0],[120.01,30.01],[120.0,30.01],[120.0,30.0]]]},"crs":"EPSG:4326"}
```

预览只读，返回根 tileset 的相交顶层瓦片数量、入口可用状态与筛选精度。它**不会**检查所有子瓦片文件；边界瓦片可能包含范围外数据。倾斜影像使用 `POST /spatial/oblique-image-subsets/preview`，请求体相同，返回相机中心命中数、估算覆盖候选数、未定位照片数、可读取照片数及 200 张上限提示。预览也不会创建资源。

创建入口仍是 `POST /spatial/resource-subsets` 和 `POST /spatial/oblique-image-subsets`。其 `subset_resource_id` 可直接用于场景图层关联：

```http
POST /api/scenes/<scene_id>/layers
Content-Type: application/json

{"request_id":"agent-job-123-layer-1","layer_category":"terrain","source_kind":"legacy_resource","source_id":"<原资源 UUID>","subset_resource_id":"<派生资源 UUID>","scope_geometry":{"type":"Polygon","coordinates":[[[120.0,30.0],[120.01,30.0],[120.01,30.01],[120.0,30.01],[120.0,30.0]]]},"scope_crs":"EPSG:4326","display_name":"区域地形","default_visible":true}
```

服务会核对派生 ID 对应原源和实际 AOI；不能把未建立子资源的旧完整源标成区域图层。POI/普通矢量图层使用 `source_kind=spatial_dataset`、`source_id=<数据集 UUID>`、`layer_category=poi` 或 `vector`，且必须传 `scope_geometry`。它们的树节点附 `scopeBbox` 供旧 POI 加载器过渡使用；严格按面查询时前端应使用完整 `scopeGeometry` 调用现有空间要素查询接口。视频资料组使用 `source_kind=video_group`、`layer_category=video`，按完整资料组关联。ODM 倾斜影像包使用 `source_kind=oblique_resource`、`layer_category=imagery`，按完整资源包关联；树节点类型为 `OdmImage`，前端按需读取单张照片。新版点云版本可按完整版本关联，但暂不把未裁切的 PLY 或 3D Tiles 称作区域图层。

## 当前边界与前端配合

- Raster TMS 尚无跨 zoom 的虚拟区域瓦片入口，因此目前不接受“原 Raster 资源 + AOI”作为区域图层。仍可关联全量 Raster 资源；前端应明确其覆盖全量。
- 新版点云的区域裁切、PLY 转瓦片与无地理范围资源的空间判定尚未实现。
- 旧模型目录的局部子集取决于该类型的 tileset 路由和筛选器；当前只读预览与创建针对 `Terrain3dtiles`、`PointCloud3Dtiles`。
- 前端需以 `layer_id` 管理多个 POI 和同源不同区域的实例，用 `scopeGeometry` 限制要素查询，并在重开场景时根据 `IsChecked` 重新尝试加载。加载失败应显示原因并保留树节点。
- `GET /api/scenes/{scene_id}/layers` 是诊断场景索引的直接入口；可用于核对刷新、重登和另一浏览器读取是否保持同一图层 ID、范围、排序和默认显示值。
