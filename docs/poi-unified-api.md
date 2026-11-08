# POI 统一资源与查询接口

7110 对外只公布一个“全部 POI”资源，固定 ID：`f9a13db2-664e-5240-a237-4efbdf3f8eb0`。全部去重点位保存在 PostGIS 的 `poi_feature` 表中，每个 POI ID 一行；`dataset_id` 均指向这一统一资源。不会按“商业”“旅游”等旧数据包名称拆分目录、计数或区域查询。

旧数据包的 UUID、名称、原始属性和几何仍保存在每个点的 `sources` 来源信息中，仅用于单点详情溯源。旧 28 份数据包元数据仍在 `spatial_dataset` 中，但不在公开数据集列表里出现；其逐组要素读取返回 410，提示改用统一接口。其他非 POI 矢量数据继续使用 `spatial_feature`。

所有接口使用 `Authorization: Bearer <7110 只读密钥或用户 JWT>`；坐标为 WGS84 / EPSG:4326。普通用户只读。

## 统一摘要

`GET /spatial/pois/summary` 返回唯一资源的 `id`、`name`、`unique_poi_count`、`bounds` 与 `catalog_version`。为现有 7010 前端过渡，`categories` 暂时只含“全部 POI”这一项，`supplemental_datasets` 为空；它们不表示分类数据集。

`GET /spatial/datasets` 同样只列出这一个 POI 资源及其他非 POI 矢量资源。新建场景 POI 图层时使用统一 ID。

## 区域查询

`POST /spatial/pois/query` 每次提供 `bbox` 或 `geometry` 之一；统一 POI 查询不限制区域面积，但仍校验 WGS84 坐标和几何合法性。`bbox` 为 `[minLon,minLat,maxLon,maxLat]`；`geometry` 为 GeoJSON Polygon/MultiPolygon。无需 `dataset_ids`；为旧客户端兼容，如提供该字段，只能包含上述统一 ID。`limit` 默认 200、最大 1,000；下一页按原条件传回 `next_cursor`。

```http
POST /spatial/pois/query
Authorization: Bearer <JWT>
Content-Type: application/json

{"bbox":[119.95,30.49,119.98,30.52],"limit":100}
```

响应是 GeoJSON FeatureCollection。每个点的 `id` 是原始 POI ID；`poi.category` 固定为“全部 POI”，`poi.dataset_id` 固定为统一资源 ID。属性中的原始 `type` 等字段仍作为数据保存，但服务不据此拆分资源。区域命中由点几何决定。

## 单点详情与来源

`GET /spatial/pois/{poi_id}` 返回单点坐标、属性及 `sources` 来源记录。来源中可能出现“商业”“旅游”等原始数据包名称，这是溯源文本，不是公开目录或查询分组。旧场景中的 POI 图层已重新关联到统一资源，原有区域范围和显示名称保留。

迁移前备份：服务器 `/home/hj/deqing-dataset-system/backups/poi-unified-catalog-20261107.dump` 与 `legacy_scenes-before-poi-unification-20261107.sqlite3`。一次性迁移脚本为 `scripts/unify_poi_catalog.py`。
