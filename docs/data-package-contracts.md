# 7110 统一数据包契约

`oblique`、`pointcloud`、`raster`、`vector`、`video` 统一使用一个根目录契约和同一上传协议。网页端和命令行都上传**一个完整文件夹或 ZIP**；服务端依据根目录 `package.json` 的 `type` 校验并导入。管理员以外的角色不能登记、更新或删除这些数据。

```text
package-name/
├── package.json       # 统一包头，必须位于根目录
└── data/              # 类型专属载荷；保留其原始相对路径
    └── ...
```

```json
{
  "schema": "deqing.data-package.v1",
  "type": "vector",
  "name": "生态修复边界",
  "description": "来源和处理说明",
  "location_review": {
    "confirmed": true,
    "source_crs": "EPSG:4549",
    "axis_order": "easting,northing",
    "method": "按源 CRS 转换并在 GIS 中核对 WGS84 范围",
    "evidence": "填写坐标系说明文件及独立 GIS 核验记录"
  },
  "metadata": { "source_srid": 4549 }
}
```

`location_review` 是五类包共用的位置审核记录；类型元数据放入 `metadata`。各字段模板见 [`examples/data-packages`](../examples/data-packages/README.md)。提交包会校验大小、相对路径、SHA-256 和类型结构，再调用类型适配器做几何、CRS、位姿或瓦片完整性校验。

## 同一上传 API

```text
POST   /admin/data-packages/uploads
PUT    /admin/data-packages/uploads/{upload_id}/files/{file_index}/chunks/{chunk_index}
GET    /admin/data-packages/uploads/{upload_id}
POST   /admin/data-packages/uploads/{upload_id}/finalize
DELETE /admin/data-packages/uploads/{upload_id}   # 取消尚未执行的上传草稿
```

目录会以相对文件清单登记后逐块传输；ZIP 作为单个文件逐块传输，服务端安全解压。块大小为 8 MiB，普通单文件上限 24 GiB、压缩包和展开后的数据包总量上限 100 GiB，最多 10,000 个文件。视频资料还需为服务端生成的 `calibration.xml` 预留空间。`GET` 用于恢复断点状态，`finalize` 返回 202，随后通过 `GET` 查询入库结果。上传失败的会话可由管理员删除后重新传；删除会话记录不会删除已经完成入库的资源。

网页控制台在每类资源卡片中都使用相同的目录选择器或 `.zip` 文件选择器。服务端按类型调用内部适配器；旧的 `/spatial`、`/pointcloud`、`/videos` 细分写入接口仍保留兼容，但不再作为网页和新导入脚本的上传方式。命令行方式也只需一个入口：

```bash
python3 scripts/upload_data_package.py /path/to/package-folder \
  --base-url http://192.168.150.41:7110 --token-file /path/to/admin-token.json

python3 scripts/upload_data_package.py /path/to/package.zip \
  --base-url http://192.168.150.41:7110 --token-file /path/to/admin-token.json
```

## 五类数据的 `data/` 内容

| `type` | `data/` 结构 | `metadata` 中必需字段 | 入库检查 |
|---|---|---|---|
| `vector` | 根目录恰好一组同名 `.shp/.shx/.dbf`，可含 `.prj/.cpg` | 无 `.prj` 时 `source_srid` 必填；可选 `encoding`、`vertical_crs`、`vertical_unit` | 几何有效、具有地图坐标、转换后范围可用；含 Z 时原样保留 Z 并记录垂直 CRS/单位 |
| `pointcloud` | PLY：单个 `.ply`；3D Tiles：根 `tileset.json` 及其所有引用文件 | `format`、`content_kind`、`srs`、`label`、`spatial_extent` | WGS84 GeoJSON 覆盖范围和审核记录必需；校验 PLY/tileset 引用并发布版本 |
| `raster` | 一个或多个 `imagery_YYYYMMDD/meta.json` 和 `{z}/{x}/{y}.png` TMS 瓦片树 | 无额外必填项；名称来自包头 | 期次、瓦片矩阵、CRS、范围及瓦片文件必须符合模板；导入到持久数据卷 |
| `oblique` | `camera_groups.json`、`poses.jsonl`、`images/`，可带 `provenance.json` | 无额外必填项；名称来自包头 | 每张原图恰好对应一条位姿；校验相机内参、矩阵、源 CRS 和 WGS84 相机中心；无深度时覆盖范围为 unavailable |
| `video` | 一份 MP4 和标准定向视频成果文件，见 [video README](../examples/data-packages/video/README.md) | 无额外必填项；名称来自包头 | 精标定、正射 GeoTIFF CRS/产品范围和位置依据必需；服务端生成统一 `calibration.xml` 后发布 |

### 点云 `metadata` 示例

```json
{
  "format": "3dtiles",
  "content_kind": "mesh",
  "srs": "EPSG:4549",
  "label": "source-2026-11",
  "entrypoint": "tileset.json",
  "spatial_extent": {"type":"Polygon","coordinates":[[[119.9,30.4],[120,30.4],[120,30.5],[119.9,30.5],[119.9,30.4]]]},
  "resource_metadata": {},
  "version_metadata": {}
}
```

PLY 包使用 `format: "ply"`，将 `entrypoint` 设为 `cloud.ply`（或实际 PLY 文件名），`data/` 只能含一个 PLY 数据文件。空间范围坐标顺序为 `[longitude, latitude]`，它描述资源登记范围，不执行瓦片裁切。原始 PLY 是否可直接由地图前端显示，仍取决于对应加载器；3D Tiles 可沿现有 Tiles 加载路径访问。

## 文件访问、查询和删除

上传协议统一，入库后的查询 API 按资源语义保留：矢量按 bbox/GeoJSON 查询，栅格按范围枚举瓦片，倾斜影像按相机位置查询，视频分别支持产品足迹和相机中心筛选，点云/模型按登记范围筛选。管理员重命名使用 `PATCH /admin/resources/{kind}/{uuid}`，请求体为 `{"name":"新名称"}`；`kind` 可为 `vector`、`raster`、`imagery`、`video`、`pointcloud`、`subset` 或 `legacy`。名称去除首尾空白后应为 1–200 个可显示字符。旧镜像 `legacy` 的重命名只写入本地显示名覆盖，不修改原始镜像；资源 ID、文件路径及现有场景图层名称均不改变。管理员删除本地资源使用 `DELETE /admin/resources/{kind}/{uuid}`；这和取消未完成的数据包上传是两个不同操作。

旧镜像资源仍是只读来源。文本及尚未定义的扩展类型不属于当前五类契约，不能通过此上传入口入库。
