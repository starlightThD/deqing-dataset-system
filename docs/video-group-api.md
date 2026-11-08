# 视频资料组存储与查询契约

网页和新脚本统一把视频作为 `video` 类型数据包文件夹或 ZIP 上传，见[统一数据包契约](data-package-contracts.md)。以下 `/videos` 分块 API 是底层资料组服务接口，供兼容客户端和统一上传适配器使用；新入库不要再直接走另一种上传流程。

7110 用 `/videos` 管理可空间定位的视频资料组。一个资料组包含一个必需视频、一个规范化精标定 XML，以及零个或多个辅助文件。空间索引可选为有来源依据的产品覆盖面，或显式的“仅相机点位”；后一种不登记产品覆盖，不参与足迹相交查询。资料组元数据和查询范围在 PostGIS 中，文件按资料组 ID 存放在 `DEQING_DATA_DIR/videos/groups/`。目录路径使用 POSIX 相对路径，数据库清单记录相对路径、角色、MIME 类型、大小和 SHA-256。

## 固定格式与文件名

每组必须有：

| 路径 | 含义 |
| --- | --- |
| `video.mp4` | 原始视频；契约要求 `video/mp4` |
| `calibration.xml` | 下述 1.0 格式，包含精标定、空间覆盖及文件清单 |

可选文件建议使用稳定的语义路径：

| 规范路径 | 当前样例来源 |
| --- | --- |
| `evidence/coarse-search-match.png` | `01_粗搜索匹配图.png` |
| `evidence/fine-search-match.png` | `02_精搜索匹配图.png` |
| `evidence/coverage-sketch.png` | `03_范围示意图.png` |
| `evidence/rough-calibration.txt` | `04_粗标定结果.txt` |
| `evidence/fine-calibration-source.txt` | `05_精标定结果.txt` |
| `model/region.obj`、`model/region.mtl` | `06_裁剪OBJ/` |
| `evidence/projection.png` | `07_投影图.png` |
| `evidence/fine-match.png` | `08_精匹配图.png` |
| `raster/orthophoto.tif` | `09_正射叠加图.tif` |

迁移时服务保留所登记文件的字节，只在资料组内采用上述规范路径。精标定文本同时写入 `calibration.xml`，也可作为 `evidence/fine-calibration-source.txt` 保存原始来源。导入脚本会把组目录下唯一的 `.mp4` 规范化为 `video.mp4`，因此原视频文件名可以不同。新生产端建议直接生成规范名称。任意其他辅助文件可按 `evidence/`、`model/`、`raster/`、`document/` 等相对路径扩展；不允许绝对路径、`..` 路径段或重复路径。

## `calibration.xml` 1.0

XML 是本组规范元数据。新入库视频必须有可追溯的 WGS84 产品覆盖 Polygon、可转换到 WGS84 的相机中心，以及 `LocationReview status="confirmed"`，确认源 CRS、轴顺序、相机中心和覆盖范围。仅有相机点位、没有可信产品覆盖范围时，服务会拒绝创建草稿；`Calibration/Pose` 不能代替产品覆盖多边形。精标定字段包括内参、外参、畸变和重投影误差。坐标系、单位、误差含义不确定时，必须明确标注，不得伪装成已验证值。

结构示例：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<VideoGroup contractVersion="1.0" name="视频资料组名称">
  <SpatialReference horizontal="EPSG:4549" axisOrder="easting,northing" source="raster/orthophoto.tif GeoTIFF metadata"/>
  <Coverage geometryCRS="EPSG:4326" sourceCRS="EPSG:4549"
            source="raster/orthophoto.tif" semantics="orthophoto_product_footprint">
    <Polygon coordinateOrder="longitude,latitude">
      <Point lon="119.96" lat="30.51"/>
      <Point lon="119.97" lat="30.51"/>
      <Point lon="119.97" lat="30.52"/>
      <Point lon="119.96" lat="30.52"/>
      <Point lon="119.96" lat="30.51"/>
    </Polygon>
  </Coverage>
  <LocationReview status="confirmed" source="已核对项目坐标系说明与正射影像 GeoTIFF 元数据"
                  coordinateOrder="longitude,latitude"
                  fields="source_crs,axis_order,camera_center_wgs84,coverage_wgs84"/>
  <Calibration source="fine-calibration" coordinateReference="EPSG:4549"
               coordinateReferenceStatus="inferred" method="self_calibration">
    <Intrinsics fx="..." fy="..." cx="..." cy="..." unit="pixel"/>
    <Pose x="..." y="..." z="..." phi="..." omega="..." kappa="..."
          positionUnit="metre" verticalUnit="metre" angleUnit="degree"/>
    <Distortion model="Brown-Conrady" k1="..." k2="..." p1="..." p2="..."/>
    <Accuracy metric="mean_reprojection_error" value="..." unit="pixel"/>
  </Calibration>
  <Assets>
    <Asset path="video.mp4" role="video" sizeBytes="..." sha256="64位十六进制摘要" mediaType="video/mp4"/>
  </Assets>
</VideoGroup>
```

实现要求 `SpatialReference`、完整的 `Calibration` 数值字段、经审查的 `LocationReview`、闭合有效且有来源说明的 WGS84 `Coverage/Polygon`，及一个 `video.mp4` 清单项。`camera_center_only` 模式不再接受，因为当前目标是只发布已处理且具有地图位置依据的数据。其他辅助资产都需在 `Assets` 中列出路径、角色、大小、SHA-256 和 MIME 类型。服务会先校验 XML，再根据清单建立断点续传任务；发布前会再次校验位置审查记录、空间范围和每个上传文件的大小与 SHA-256。

新增上传还会在创建草稿前检查：相机 CRS 必须可解析，轴顺序必须明确且与投影/地理 CRS 类型匹配；Coverage 必须声明来源 CRS、来源文件和范围语义；标定 CRS 必须与位姿 CRS 相同，状态只能为 `verified` 或 `inferred`；内参、位姿单位、畸变模型和误差单位必须显式。相机中心若无法转换到 WGS84，服务会拒绝创建资料组。单独的 `phi/omega/kappa` 目前仍不足以推导视线方向，接口会保留原值并将 orientation 标记为 unavailable。

命令行导入器也要求提供 `--location-review-source`，并将其写入同一 XML 审查记录；不提供可追溯的位置依据时不会生成可上传包。

## API 与权限

所有写入均要求管理员 Bearer/JWT。普通用户和管理员可以读取已发布资料组；普通用户不能上传或更改资料。单文件默认上限 24 GiB，资料组默认上限 100 GiB，分块最大 16 MiB。

| 方法与路径 | 用途 |
| --- | --- |
| `POST /videos/groups` | 以 `application/xml` 提交 `calibration.xml`，创建草稿组并返回各资产的 `upload_id` |
| `GET /videos/groups/{id}/uploads` | 管理员查看每个资产的续传状态 |
| `GET /videos/uploads/{upload_id}` | 查看已接收分块 |
| `PUT /videos/uploads/{upload_id}/chunks/{index}` | 上传二进制块，要求正确的 `Content-Range` |
| `POST /videos/uploads/{upload_id}/complete` | 组装文件并校验 SHA-256 |
| `POST /videos/groups/{id}/finalize` | 确认完整文件组并发布 |
| `GET /videos/groups?bbox=minLon,minLat,maxLon,maxLat&spatial_filter=...` | 按明确的覆盖足迹相交或相机中心包含条件查询；管理员也能看到草稿 |
| `GET /videos/groups/{id}` | 返回资料组元数据、独立的 `camera_pose`、平铺文件清单及可展开的 `directory` 树 |
| `GET /videos/groups/{id}/files/{relative_path}` | 读取组内任意已登记文件；支持常规文件 Range 请求 |
| `POST /videos/groups/{id}/playback-ticket` | 登录用户领取 6 小时播放地址；若组目录有 `preview_h264.mp4`，优先返回 H.264 预览地址及 `codec: h264`，否则返回原片地址及 `codec: source` |
| `GET /videos/groups/{id}/play/video.mp4?expires=...&signature=...` | 按 Range 读取原始 MP4，可能采用浏览器不支持的 H.265 编码 |
| `GET /videos/groups/{id}/play/preview_h264.mp4?expires=...&signature=...` | 按 Range 读取组目录中的 H.264 预览文件；签名绑定预览文件，不可用于读取原片 |
| `POST /api/scene-video-groups` | 管理员以 `{"scene_id":"...","group_id":"..."}` 将已发布资料组关联到场景 |
| `DELETE /api/scene-video-groups/{id}?scene_id=...` | 管理员移除场景关联，原始资料组不删除 |

场景树中的资料组节点类型为 `FixedVideo`。新入库组都必须有已核验的产品足迹；历史 `camera_center_only` 记录可能仍在旧库中，但新上传会被拒绝。旧的 `MediaDirectionalVideoDynamic` 轨迹视频接口不用于这些资料组。播放地址包含短期授权签名，不应复制给其他人；登出后已签发的地址会在到期时失效。

现有三组已发布视频的原片为 H.265，已在组目录中各生成一份 720p H.264 `preview_h264.mp4`，原片字节和资产清单保持不变。后续新增 H.265 视频时需生成同名预览文件，否则票据会回退原片，部分浏览器无法播放。

### 相机位姿及可信状态

`GET /videos/groups/{id}` 顶层的 `camera_pose` 将位姿分成来源值、地图中心、朝向和时间适用性，避免把投影坐标或未定义的旋转角直接当作 Cesium 相机姿态：

```json
{
  "camera_pose": {
    "status": "inferred",
    "center_wgs84": {
      "longitude": 119.9662,
      "latitude": 30.5129,
      "elevation_m": null,
      "coordinate_order": ["longitude", "latitude", "elevation_m"],
      "horizontal_status": "inferred",
      "elevation_status": "unavailable"
    },
    "orientation": {
      "status": "unavailable",
      "heading_pitch_roll_degrees": null,
      "world_axes": {"x": "unavailable", "y": "unavailable", "z": "unavailable"},
      "camera_axes": {"x": "unavailable", "y": "unavailable", "z": "unavailable"},
      "angle_convention": {
        "status": "unavailable",
        "rotation_order": "unavailable",
        "intrinsic_or_extrinsic": "unavailable",
        "handedness": "unavailable",
        "rotation_direction": "unavailable",
        "unit": "degree"
      },
      "angle_unit": "degree",
      "raw_angles": {"phi": -107.8, "omega": 12.77, "kappa": -0.85}
    },
    "original_pose": {"x": 496758.7983, "y": 3376971.0064, "z": 53.064, "phi": -107.8, "omega": 12.77, "kappa": -0.85},
    "original_crs": {
      "horizontal_crs": "EPSG:4549",
      "axis_order": "easting,northing",
      "coordinate_reference_status": "inferred",
      "axis_order_status": "inferred",
      "vertical_reference_status": "unavailable"
    },
    "conversion": {
      "target_crs": "EPSG:4326",
      "output_axis_order": ["longitude", "latitude"],
      "horizontal_method": "pyproj Transformer.from_crs(..., always_xy=True)",
      "input_axis_order": "easting,northing",
      "horizontal_status": "inferred",
      "vertical_status": "unavailable"
    },
    "temporal_model": {
      "status": "unavailable",
      "kind": "single_calibration_solution",
      "is_fixed_camera": null,
      "pose_sample_count": 1
    }
  }
}
```

每个分量的状态只使用 `verified`、`inferred`、`unavailable`。经纬度仅在 CRS、轴序、单位足以执行水平转换时提供；垂直基准未知时 `elevation_m` 为 `null`，原始 Z 仍在 `original_pose` 中。只有源文件明确给出旋转轴、坐标手性、旋转顺序和方向时才计算朝向。`phi/omega/kappa` 标签与角度单位本身不足以确定 heading/pitch/roll。没有带时间戳的轨迹，也没有证据证明单个位姿在整段视频内恒定时，`temporal_model.status` 必须为 `unavailable`，不能把单次标定解描述成已确认的固定机位。

#### 粗标定视线预览字段

2026-11-04 为“视频1”、“德清视频定位成果_157-视频5”和“德清视频定位成果_157-视频7”写入测试用的粗视线参数。三组 `camera_pose.orientation.status` 和 `camera_pose.orientation.view_direction.status` 均为 `inferred`，不是 `verified`。视线由 `evidence/rough-calibration.txt` 的 3×4 投影矩阵按 `P = K[R|t]` 假设分解；字段包含 EPSG:4549 下的单位方向向量、网格方位角、向下俯角、相机中心、世界到相机旋转矩阵、内参和图像尺寸。前端绘制这条测试视线时应使用 `view_direction.origin`，它来自同一粗 PnP 解；现有 `center_wgs84` 仍是精标定相机中心，不能与粗方向混作一个位姿。

`view_direction.map_preview_ray` 给出 150 米水平显示箭头的 WGS84 起止点，仅用于前端预览；终点不是射线落地点，不能作为拍摄足迹。垂直基准仍不可用，`footprint_status` 为 `unavailable`。视频1的粗、精相机中心水平相差约 200.4 米；本次按后续重采集的测试前提补入粗方向，但须保留该误差说明，且方向起点必须取粗解 `view_direction.origin`，不能与精标定的 `center_wgs84` 混用。

2026-11-06，视频1（`09649728-7990-4317-94de-df537c99138b`）将原先对准 OSGB 根包围盒中心的临时方向改为另一种可视化试算：按 `Rz(kappa)·Ry(omega)·Rx(phi)` 生成矩阵，将矩阵解释为 camera-to-world，并把相机 `+Z` 作为前向轴；为保持图像水平，预览矩阵的 roll 暂设为 0。结果为网格方位角约 `0.104°`、下俯角约 `12.555°`。2 秒帧的地平线目测约在图像第 100 行，该候选方向估算地平线约在第 109 行；原模型中心方向约在第 241 行。这只是单帧目测比较，没有控制点重投影验证。它**与配套《相机参数对接说明.md》规定的 world-to-camera、相机 `-Z` 前向相冲突**，状态保持 `inferred`，不得视作已验证朝向。原方向保存在组元数据 `orientation_conversion_history` 中，原 `calibration.xml` 另存于服务数据卷的 `.orientation-backups/` 目录，便于回退。

同日根据用户对地图方向的目视反馈，又将上述方向向西修正约 `40°`：当前方向为网格方位角约 `320.104°`、下俯角仍为 `12.555°`，状态仍为 `inferred`。修正后的预览箭头终点约为 `(119.9667065, 30.5109438)`（WGS84，距相机水平约 150 米）。前端刷新后应按此试算值检查；若仍有偏差，需要用已知地物控制点做 2D–3D 对应和重投影验证，不能继续仅凭角度猜测。

当前三组定向视频的 `05_精标定结果.txt` 只列出 `Xs/Ys/Zs` 和 `phi/omega/kappa` 数值；归一化 XML 中的 EPSG:4549 与 `easting,northing` 轴序是根据正射 GeoTIFF 和坐标量级推定的。因此水平中心会以 `inferred` 状态转换到 WGS84；原始 Z 没有垂直 CRS，WGS84 高程不可用；旋转约定与视频期间的运动状态也不可用。标定解不替代逐帧轨迹。

### 区域条件与命中依据

现有 `bbox` 继续有效。省略 `spatial_filter` 时，带 `bbox` 的旧调用仍按正射产品足迹相交查询；显式模式如下：

```http
GET /videos/groups?bbox=119.96,30.50,119.98,30.53&spatial_filter=footprint_intersects
GET /videos/groups?bbox=119.96,30.50,119.98,30.53&spatial_filter=camera_center_inside
```

`footprint_intersects` 检查 `spatial_extent` 与 bbox 相交，返回项的 `match_reason.condition` 为 `footprint_intersects`，几何依据为 `orthophoto_product_footprint`。`camera_center_inside` 检查推导出的相机中心点是否严格位于 bbox 内（边界点不算内部）；返回项包含 `camera_center_wgs84`，且 `match_reason` 会标出条件、几何依据、位置状态和时间状态。该条件只对单次标定中心做点位查询，不能代表视频全过程相机轨迹。指定 `spatial_filter` 时必须同时提供 `bbox`。

历史 `camera_center_only` 资料组的 `spatial_extent` 仍可能返回 `null`，不会命中 `footprint_intersects`；显式 `camera_center_inside` 仍可按相机中心查询。新入库流程不再允许这类无覆盖范围的组。

分块请求体是原始字节：

```http
PUT /videos/uploads/{upload_id}/chunks/0
Authorization: Bearer <admin-token>
Content-Type: application/octet-stream
Content-Range: bytes 0-8388607/248513923

<binary bytes>
```

读取示例：

```http
GET /videos/groups?bbox=119.9678,30.5148,119.9725,30.5211
Authorization: Bearer <user-token>

GET /videos/groups/{id}/files/video.mp4
Authorization: Bearer <user-token>
```

响应中的文件路径是组内逻辑路径，不暴露容器宿主机路径。客户端从组详情的 `files[].url` 获取文件；`directory` 提供相同资产的树形视图。

## 资料范围与当前样例说明

`/home/thd/Downloads/视频1/09_正射叠加图.tif` 自带 EPSG:4549 和像元边界，因此样例查询范围由它的完整栅格边界转换到 EPSG:4326 后建立。该范围代表**正射产品足迹**，不保证等于视频所有帧的拍摄区域，也不做帧级检索。视频文件没有 GPS 元数据。

`05_精标定结果.txt` 的相机位置坐标系未注明。生成样例 XML 时会基于正射 CRS 与数值范围暂推 EPSG:4549，并标记为 `inferred`；位姿点位于正射边界外，XML 会保留该警告。空间查询使用的是 GeoTIFF 足迹，不会使用这个推定的相机点。

样例 `region.mtl` 引用了资料组未提供的外部纹理路径。MTL 会原样保存和返回，但当前资料包不是完整自包含的 OBJ+纹理包；如需完整模型显示，还需补齐这些纹理或另行修订材质引用。

## 统一数据包导入

将标准 `video` 数据包目录或 ZIP 按[视频样例结构](../examples/data-packages/video/README.md)整理；包根 `package.json` 提供统一包头，视频与标定成果放在 `data/`。网页和脚本使用同一入口：

```bash
python3 scripts/upload_data_package.py /path/to/video-package \
  --base-url http://192.168.150.41:7110 \
  --token-file /path/to/admin-token.json
```

服务端适配器生成规范化 `calibration.xml`，检查并摘要附件，然后发布资料组。通用脚本在包旁保存续传状态；传输中断后使用同一个包路径重跑即可续传。旧的 `import_video_group.py` 仍保留作兼容工具，不作为网页或新脚本的独立上传模式。
