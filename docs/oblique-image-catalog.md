# 倾斜影像粗配准目录

7110 的 PostGIS 目录把现有原图、相机内参与位姿、候选地面覆盖关联起来。原始 JPEG 继续保存在 `/workspace/data/legacy-mirror/objects`；数据库只记录稳定影像 ID、文件键、对象相对路径、大小与 SHA-256。现有 103 GB 数据不会被复制第二份。`oblique_resource`、`oblique_group`、`oblique_image` 三张表分别管理资源、相机组和单张影像；空间列使用 WGS84 `geometry(Point,4326)` 和 `geometry(Polygon,4326)`，均有 GiST 索引。每次目录导入都必须显式确认相机 CRS 和计算假设已审查，并保存审查来源；该确认不改变候选范围仍属估算、没有保证误差界的事实。

## 空间范围的含义

导入器从 ContextCapture XML 读取每张照片的中心和 3×3 旋转矩阵，并按相机组保存像幅、焦距、传感器尺寸、主点和原始畸变系数。以资源内相机 Z 中位数减 120 m 作为**假设地面**，再在该高程上下 30 m 的两个平面投射图像四角；对三个平面的投射点取凸包，最后在 PostGIS 中按米外扩 15 m。这是查找候选照片的粗筛条件，**不是实测地面覆盖，也不是误差上界**。相机 Z 与地面高程的基准由原 XML 继承，未独立核验。

若四角射线朝上、接近地平线，或水平交点超过相对航高的 8 倍，照片的 `candidate_footprint` 为 `NULL`，`coverage_status=horizon_or_upward_ray`。这些照片仍有相机中心、原图和位姿；范围查询无法断言其覆盖位置。带畸变参数而尚未做反畸变的照片标记 `distortion_unmodeled`。所有记录的 `coverage_assumptions.guaranteed_bound` 均为 `false`。地形、建筑物高度、遮挡、姿态/内参误差及 XML 坐标基准差异都可能使候选范围偏移或漏检。需要保证召回时，应结合邻近区域、资源整体范围和 `unbounded_count` 人工复核；将来可用 DEM、深度或重建结果重算范围，无须移动原图。

## 查询与读取

使用兼容登录获得的 JWT 或 7110 的只读 Bearer 凭据：

```bash
curl -H "Authorization: Bearer $TOKEN" \
  'http://192.168.150.41:7110/spatial/oblique-images?bbox=114.35,30.52,114.36,30.54&limit=100'
curl -X POST 'http://192.168.150.41:7110/spatial/oblique-images/query' \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"geometry":{"type":"Polygon","coordinates":[[[114.35,30.52],[114.36,30.52],[114.36,30.54],[114.35,30.54],[114.35,30.52]]]},"limit":100}'
curl -H "Authorization: Bearer $TOKEN" \
  'http://192.168.150.41:7110/spatial/oblique-images/<image-id>'
curl -H "Authorization: Bearer $TOKEN" \
  -o photo.jpg 'http://192.168.150.41:7110/spatial/oblique-images/<image-id>/file'
```

`bbox` 顺序为 `min_lon,min_lat,max_lon,max_lat`，单位为度。列表可加 `resource_id`、`limit`（最大 1000）、`offset`；结果中的 `total` 是候选多边形相交条数，`unbounded_count` 是该资源下无法给出可靠范围的影像总数，**它不受 bbox 筛选**。列表返回相机中心、候选多边形、覆盖状态和 `file_url`；详情额外返回原始文件键、位姿、内参、畸变与全部计算假设。文件接口支持 HTTP Range；未存储的原图返回 404。查询和下载均需只读或管理员身份，写入仍只由管理员在容器内执行。

`POST /spatial/oblique-images/query` 接受 `bbox` 数组和/或 GeoJSON `Polygon`/`MultiPolygon`，可附 `resource_id`、`limit`、`offset`。同时提供 bbox 和 geometry 时，两项条件都会应用。它仍然匹配粗估的 `candidate_footprint`，无法将 `candidate_footprint` 为空的照片空间定位；使用结果前应结合 `unbounded_count` 和每条记录的 `coverage_status` 判断覆盖可信度。

## 重建目录

此导入只读已有 SQLite 清单、相机 XML 和本地文件镜像；不会访问远端，也不会复制或重新哈希 JPEG。运行者必须确认位置审查并写明依据；导入可重复执行，使用同一文件键生成的 UUID 保持不变：

```bash
docker exec -e DEQING_POSTGIS_DSN_FILE=/workspace/data/postgis_dsn deqing-data \
  /workspace/.venv/bin/python /workspace/data-service-source/scripts/import_oblique_images.py \
  --source-root /workspace/legacy/out \
  --mirror-root /workspace/data/legacy-mirror \
  --confirm-location-review \
  --location-review-source "已核对 ContextCapture 坐标系说明；接受平地模型仅作候选范围"
```

可调整 `--agl-m`、`--ground-window-m`、`--buffer-m` 和 `--max-horizontal-factor`，修改后重新导入会覆盖空间估算与计算假设，不会移动或重写原图。运行参数属于模型假设，不能把调大后的范围解释成有保证的误差界。若未来接入准确地面/深度，应新增计算版本与更可信的覆盖方法，并保留原始相机参数以便重算。

2026-10-28 首次导入结果：8 个资源、40 个相机组、23,554 张影像；23,554 张原图均在本地镜像找到；23,476 张生成候选范围，78 张因射线问题只记录位姿，其中 2,022 张有未应用的畸变系数。该数字只说明当前导入清单，不说明目录涵盖其他影像来源。
