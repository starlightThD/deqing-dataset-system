# 区域 3D Tiles 子资源

7110 提供管理员创建的虚拟区域子资源。服务按 WGS84 多边形筛选 3D Tiles 瓦片树 JSON，并通过旧接口路由读取原资源瓦片，不复制数 GB 的瓦片文件。

## 创建子资源

```http
POST /spatial/resource-subsets
Authorization: Bearer <admin-account-jwt>
Content-Type: application/json

{
  "source_resource_id": "48564a7a-e7ea-404a-b0ce-bb97d0779a7f",
  "geometry": {
    "type": "Polygon",
    "coordinates": [[[114.3578477,30.5426531],[114.3579579,30.5422770],
      [114.3585667,30.5423924],[114.3584492,30.5427794],
      [114.3578477,30.5426531]]]
  },
  "crs": "EPSG:4326",
  "mode": "intersecting_tiles",
  "name": "限制范围-模型子集"
}
```

`source_resource_id` 必须是资源目录中类型为 `1000012`（`Terrain3dtiles` 加载器 / Mesh 文件路由）或 `13000006`（`PointCloud3Dtiles`）的资源。样例 `48564a7a-e7ea-404a-b0ce-bb97d0779a7f` 在目录中名为“模型_3dTiles”，但数字类型实际是 `1000012`，因此由 Mesh 路由加载；这与 `Model3dtiles` 资源类型不同。

目前只支持 `intersecting_tiles`。范围支持 `Polygon` 或 `MultiPolygon`，坐标系必须是 WGS84，默认面积上限为 1,000 平方千米；设置 `DEQING_RESOURCE_SUBSET_MAX_AREA_KM2` 可调整上限。相同的源资源、源指纹、规范化多边形和模式会复用已有子资源 ID。

响应示例：

```json
{
  "status": "completed",
  "subset_resource_id": "<new UUID>",
  "source_resource_id": "48564a7a-e7ea-404a-b0ce-bb97d0779a7f",
  "resource_type": "Terrain3dtiles",
  "type_id": 1000012,
  "mode": "intersecting_tiles",
  "tileset_url": "/api/Mesh/GetMesh3DTilesFile/<new UUID>/tileset.json",
  "selected_top_level_tile_count": 5,
  "source_top_level_tile_count": 473,
  "bounds": [114.3578477, 30.542277, 114.3585667, 30.5427794],
  "precision_notice": "按 3D Tiles 瓦片包围体筛选；边界瓦片仍可能包含多边形范围外的点或模型，这不是几何裁切。"
}
```

派生资源会立即加入现有目录，前端可通过 `POST /api/cim/CIMScene/GetCIMSceneResourceByPage` 查询，然后用已有场景关联接口加入场景。旧 Mesh 或 PointCload 文件路由提供筛选后的 `tileset.json` 和子瓦片 JSON；命中的二进制内容从原资源镜像读取。按当前 7110 权限规则，创建和场景关联仍只有管理员可执行。

使用 `GET /spatial/resource-subsets/{subset_resource_id}` 查询源资源 ID、源指纹、几何范围、命中瓦片数、入口 URL 和精度说明。子资源目录记录及各文件的坐标变换上下文保存在 `DEQING_DATA_DIR/legacy_resource_subsets.sqlite3`。

## 筛选方式与边界

- 筛选会应用瓦片 `boundingVolume`、3D Tiles `transform`、层级 `refine` 和内容包围体；保留必要的祖先节点，剔除不相交的内容，并在浏览器请求子 tileset JSON 时继续递归筛选。
- 支持 `box`、`sphere`、`region` 包围体。遍历到隐式瓦片、外部内容 URL、缺少或无效的包围体、或同一子 tileset 路径出现冲突坐标变换时，会返回 `422 unsupported_source`。
- 包围体只是范围包络。边界瓦片中仍可能包含多边形外的点、顶点或三角面；目前没有实现 `exact_clip`。
- 派生子资源是当前本地镜像上的虚拟视图，不会补回缺失文件，也不会远端回源。命中的瓦片文件若未在本地，读取时会返回镜像缺失响应。
- 子资源绑定源资源的上传时间、文件数、文件大小和根 tileset 哈希。源资源替换后应创建新子资源；旧 ID 仍绑定创建时的源指纹。
- 创建子资源需要管理员账户 JWT；读取子资源详情需要有效账户 JWT。瓦片文件路由保持原旧接口的公开读取行为。

## 其他资源格式

`ImageAirImage` 不是 3D Tiles 图层。可由管理员调用 `POST /spatial/oblique-image-subsets`，提交 `source_resource_id`、WGS84 `geometry`、`mode: "camera_centers_inside"` 和 `name`。7110 从 PostGIS 选出相机位置落在多边形内的照片，最多 200 张，登记为可加入场景的 `ImageAirImage` 子资源，照片二进制仍复用原始镜像。`GET /spatial/oblique-image-subsets/{id}` 返回子资源详情；旧 ARLayer 的数量、分页和单张照片文件接口可直接读取该子资源。子集索引持久保存在 `DEQING_DATA_DIR/legacy_oblique_subsets.sqlite3`。

该筛选条件服务于轻量地图加载，与 `/spatial/oblique-images/query` 的估算覆盖范围不同。相机在范围外但拍到范围内的照片不会入选；相机在范围内也不保证照片内容都落在范围内。管理员可缩小多边形处理超过 200 张的情况。
