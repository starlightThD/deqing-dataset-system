# 点云、模型和地形数据包

将 `package.json.example` 复制为包根目录的 `package.json`，填写真实范围、源 CRS、内容类型和位置核验依据；把有效载荷放进 `data/`。上传时与其他四类数据使用同一个文件夹/ZIP 入口和同一分块协议。

PLY 目录示例：

```text
deqing-cloud/
├── package.json
└── data/
    └── cloud.ply
```

3D Tiles 目录示例：

```text
deqing-model/
├── package.json
└── data/
    ├── tileset.json
    └── Data/...
```

`metadata.format` 为 `ply` 或 `3dtiles`；`content_kind` 为 `pointcloud`、`mesh`、`model` 或 `terrain`。PLY 包只允许一个非空 `.ply` 文件。3D Tiles 根目录必须有 `tileset.json`，并携带其引用的全部文件。`spatial_extent` 必须是可信 WGS84 GeoJSON Polygon/MultiPolygon；它用于地图范围筛选，不代表瓦片已被裁切。上传前须核实 `location_review` 中的 CRS、轴序和范围依据。

统一上传：

```bash
python3 scripts/upload_data_package.py /path/to/deqing-model \
  --base-url http://192.168.150.41:7110 --token-file /path/to/admin-token.json
```

可将整个目录压成 ZIP 后使用相同脚本。旧的 [`scripts/upload_pointcloud_package.py`](../../../scripts/upload_pointcloud_package.py) 是统一服务调用的内部适配器/兼容工具；新的网页和脚本入库流程请走 `upload_data_package.py`。
