# 7110 统一资源空间查询

实现依据：[7010 与 7110 资源查询统一路径设计](7010-7110-resource-query-contract.md)。

## 身份与查询

`POST /spatial/resources/query` 使用登录接口取得的用户 JWT。请求包含 `source_kind`、原资源 `source_resource_id`、可选的固定 `source_version_id`、EPSG:4326 GeoJSON Polygon/MultiPolygon、`selection_mode`、`limit` 与 `cursor`。响应返回原资源 ID、实际固定版本、命中项及下一页游标。查询不创建资源、子集或场景图层。

统一“全部 POI”资源的查询和场景图层不限制区域面积，仍校验 WGS84 坐标、几何合法性及单页数量。其他 `spatial_dataset` 的查询和场景图层范围面积上限为 2,000 平方千米；其余资源使用 `DEQING_RESOURCE_SUBSET_MAX_AREA_KM2` 指定的上限（默认 1,000 平方千米）。

| 来源 | `selection_mode` | 空间粒度 |
| --- | --- | --- |
| `pointcloud_resource`；桥接的 `legacy_resource` | `intersecting_tiles` | 3D Tiles 内容瓦片包围体 |
| `raster_collection` | `intersecting_tiles` | 已登记的实际瓦片 |
| `spatial_dataset` | `feature_intersects` | PostGIS 要素几何 |
| `oblique_resource` | `camera_centers_inside` 或 `candidate_footprint_intersects` | 原影像相机点或估计足迹 |
| `video_group` | `footprint_intersects` 或 `camera_center_inside` | 已发布资料组 |

区域内无命中返回空 `items`；无完整文件索引返回 `index_unavailable`，缺少所请求的空间依据返回 `extent_unavailable`。旧镜像中未持有全部瓦片字节的资源保持旧读取路由，不声称可以逐瓦片筛选。

## 场景图层

`POST /api/scenes/{scene_id}/layers` 保存 `source_kind`、原 `source_id`、钉住的 `source_version_id`、`scope_geometry`、`selection_mode`、目录和显隐。服务先用相同查询规则检查索引和至少一个命中，再写入图层。`GET` 返回相同定义和 `render_descriptor`；`layer_id` 是场景中的展示实例，不是资源 ID。`pointcloud_version` 的旧式请求会映射为点云资源 ID 和版本 ID。

兼容场景树 `GET /api/cim/CIMScene/GetResourceTreeForCIMScene/{scene_id}` 的持久图层节点也返回 `sourceVersionId`、`selectionMode` 和 `renderDescriptor`。范围 3D Tiles 节点使用 `Scoped3DTiles` 类型，保留原 `sourceId` 与独立的 `layerId`。7010 重进场景时可直接恢复固定版本、筛选方式和加载入口。

3D Tiles 的 `render_descriptor.entry_path` 指向动态清单：

```text
/spatial/resources/{source_id}/versions/{version_id}/tileset.json?scene_layer_id={layer_id}
```

动态清单里的子清单及二进制 URI 使用相对于当前清单的路径，并携带同一 `scene_layer_id`。这样从 `/data-api/spatial/...` 打开根清单时，浏览器解析子文件仍会经过 `/data-api` 代理。7110 对每次读取验证版本、图层与瓦片范围；范围外返回 404。栅格图层返回带图层上下文的 TMS URL 模板。7010 在根相对的渲染入口前补 `/data-api`，并由加载器给清单、子清单、瓦片请求传递 `Authorization: Bearer <JWT>`。不要将 JWT 放入 URL。

## 索引与入库

PostGIS 的 `resource_index_version`、`resource_file`、`tileset_node` 保存发布状态、文件 SHA-256、空间范围和 3D Tiles 层级。数据卷保留原字节。脚本 `scripts/backfill_resource_index.py` 可按来源和版本重跑；只有大小、SHA-256、文件数及内部引用通过后才把索引标为 `ready`。发布中的资源不可查询。新点云 3D Tiles、时序栅格、视频和 ODM 数据包入库时也会建立索引。已发布版本不得原地覆盖。

部署中的回填示例：

```bash
docker exec -e DEQING_POSTGIS_DSN_FILE=/workspace/data/postgis_dsn \
  -e PYTHONPATH=/workspace/data-service-source -w /workspace/data-service-source \
  deqing-data /workspace/.venv/bin/python scripts/backfill_resource_index.py
```

当前实现保留旧 `/api/Mesh`、`/api/PointCload`、`/api/DownLoad` 和旧子集读取路径，供 7010 按资源类型切换及回滚。旧镜像资源没有完整文件目录时，统一查询返回 `index_unavailable`。3D Tiles 采用包围体相交，边界瓦片可能包含范围外几何。
