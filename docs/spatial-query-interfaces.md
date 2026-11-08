# 按经纬度查询与获取数据

各数据类型分别提供自己的空间查询接口。当前范围查询统一使用 WGS84（EPSG:4326），坐标顺序为经度、纬度；bbox 格式为 `minLon,minLat,maxLon,maxLat`。查询结果按类型返回要素、影像、瓦片或资源版本入口，不把所有类型合并成一个接口。

## 当前接口

| 数据类型 | 查询入口 | 空间匹配方式 | 获取方式与限制 |
| --- | --- | --- | --- |
| 矢量 Shapefile | `GET /spatial/datasets/{dataset_id}/features?bbox=...`；`POST /spatial/datasets/{dataset_id}/query` | PostGIS 几何相交；支持 bbox、GeoJSON Geometry 和属性条件 | 返回局部 GeoJSON 要素。 |
| 用户标绘 | `GET /spatial/annotations?bbox=...`；`POST /spatial/annotations/query` | PostGIS 几何相交；POST 支持 bbox 和/或 GeoJSON Geometry | 返回当前账户自己的标绘，不跨用户暴露。 |
| 倾斜影像 | `GET /spatial/oblique-images?bbox=...`；`POST /spatial/oblique-images/query` | 按候选覆盖多边形与 bbox 或 Polygon/MultiPolygon 相交 | 返回候选影像和 `/spatial/oblique-images/{id}/file`。覆盖范围来自地面假设，不能作为真实覆盖保证；无法估算覆盖的照片通过 `unbounded_count` 单独提示。 |
| 旧目录 3D Tiles 模型/点云 | `POST /spatial/resource-subsets` | 提交 WGS84 Polygon/MultiPolygon，按瓦片包围体相交 | 创建虚拟区域资源，返回 3D Tiles 入口。只筛瓦片，不精确裁切边界瓦片；目前仅支持文档列出的旧目录类型。 |
| 旧目录 Raster TMS | `GET /spatial/rasters/{resource_id}/tiles?bbox=...&zoom={z}` | 读取本地 `tilemapresource.xml`，转换 bbox 坐标系并计算该层级相交的 TMS 瓦片 | 返回每张瓦片的 `file_url`、经纬度 bounds 和 `available`。只有本地镜像中已存在的瓦片标为可用；旧路径 `/api/Raster/GetTmsFile/{id}/{z}/{x}/{y}.png` 可读取瓦片。每次选一个 zoom，单次候选上限 25,000。 |
| 版本化点云 `/pointcloud` | `GET /pointcloud/spatial-resources?bbox=...` | 按管理员登记的资源 `spatial_extent` 与 bbox 相交 | 返回资源、已发布版本及 manifest/入口文件 URL。这里只定位整个资源，不裁切 PLY 或 3D Tiles 内容。未登记 extent 的资源不会进入空间查询。 |
| 视频资料组 `/videos` | `GET /videos/groups?bbox=...&spatial_filter=<mode>` | `footprint_intersects` 按正射产品足迹相交；`camera_center_inside` 按推定 WGS84 标定相机中心是否位于 bbox 内；响应给出 `match_reason` | `GET /videos/groups/{id}` 返回独立 `camera_pose`，并标出水平、垂直、朝向和时间状态。相机位姿来源及误差约定见[视频组契约](video-group-api.md)。 |

## 栅格查询示例

```http
GET /spatial/rasters/{resource_id}/tiles?bbox=114.35,30.52,114.36,30.54&zoom=14&limit=100
Authorization: Bearer <token>
```

每个 `items` 元素包含计算得到的 `z`、`x`、`y`，瓦片 WGS84 包围框 `bounds`，是否已在本地 `available`，以及可直接读取的 `file_url`。

`zoom` 对应 `tilemapresource.xml` 的 `TileSet.order`，不是根据资源目录 `EpsgId` 推导的 XYZ 级别；空间参考读取 TMS 根 XML，瓦片行号沿用 TMS 原点约定，可直接放入旧文件路径。`available=false` 表示索引里没有对应的本地瓦片对象；7110 不会为这个新空间查询回源下载。`bounds` 是瓦片投影范围转换回 WGS84 后的包围框。

## 点云资源范围

创建或更新版本化点云资源时，可以登记 WGS84 GeoJSON geometry：

```json
POST /pointcloud/resources
{
  "name": "FastGS 点云",
  "srs": "EPSG:4547",
  "spatial_extent": {
    "type": "Polygon",
    "coordinates": [[[114.35,30.52],[114.36,30.52],[114.36,30.54],[114.35,30.54],[114.35,30.52]]]
  }
}
```

`spatial_extent` 必须是 WGS84 经纬度 geometry。它是资源级范围索引；服务不会从 PLY 字节中猜坐标系或自动计算范围。查询命中后，可使用结果里的 `manifest_url` 查看文件清单，再用 `entrypoint_url` 或文件路径读取。需要返回真正落在 AOI 内的点时，还需基于 PLY 坐标系做点级过滤或先生成空间分块/3D Tiles。

## 暂未提供范围查询的类型

旧目录里的 WMS/WMTS 目前没有 7110 本地可用的图层服务地址；旧远程视频方向信息兼容接口也未自动加入 `/videos` 的新资料组目录，需按新契约导入并提供有效 WGS84 覆盖；文本资源尚无通用的位置字段。因此这些旧资源暂时不能可靠地提供经纬度查询。新视频资料组契约见[视频组接口说明](video-group-api.md)。
