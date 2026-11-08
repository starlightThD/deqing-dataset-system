# 倾斜影像提供方提交包

网页和新脚本使用 `oblique` 类型的统一数据包文件夹或 ZIP：在包根增加 `package.json`，将下方原有位姿包结构放进 `data/`，然后调用 `scripts/upload_data_package.py`。包头模板见 [`examples/data-packages/oblique/package.json.example`](../examples/data-packages/oblique/package.json.example)。

提供方提交包不需要 `manifest.json` 或 `resource_id`。目录结构和字段模板见[仓库中的 oblique 数据包示例](../examples/data-packages/oblique/README.md)。

```text
<package>/
├── images/                 # 原始照片
├── camera_groups.json      # 相机内参与畸变
├── poses.jsonl             # 每张照片一条相机位姿
├── provenance.json         # 处理软件和精度来源说明
└── README.md               # 可选的人类阅读说明
```

## 自动生成和导入

该脚本是 7110 统一数据包入口调用的内部适配器；不再单独作为网页或新脚本上传模式。

`scripts/import_oblique_resource_package.py` 会校验照片/位姿一一对应、相机组引用、源 CRS 一致性、相机中心与矩阵平移、旋转矩阵、WGS84 转换和文件路径。它会自行计算每张照片的大小和 SHA-256；若提供方同时填写这些值，脚本会核对。

脚本从包内容生成内部 `deqing.oblique-resource.v1` manifest，并计算确定性资源 ID。清单写入 PostGIS 的资源元数据，不会修改提交包。文件存入数据卷 `oblique-images/{resource_id}/`，数据库保存相机组、位姿和文件索引。

先校验：

```bash
docker exec -e DEQING_POSTGIS_DSN_FILE=/workspace/data/postgis_dsn \
  -e PYTHONPATH=/workspace/data-service-source \
  deqing-data /workspace/.venv/bin/python \
  /workspace/data-service-source/scripts/import_oblique_resource_package.py \
  /workspace/imports/<package> --name "资源名称" --validate-only
```

确认生成的校验清单并人工核对源 CRS、轴序和相机中心后，去掉 `--validate-only`，并附加 `--confirm-location-review --location-review-source "<核验依据>"` 执行入库。运行环境应设置 `DEQING_POSTGIS_DSN_FILE` 和 `PYTHONPATH`，且必须由管理员执行。重复导入同一包会得到相同资源 ID，更新了影像、位姿或相机参数则会生成新的 ID。

确认状态只表示管理员核验了 CRS、轴序和相机中心转换，不代表绝对地理精度已知。没有地面控制点或独立测量依据时，精度状态保持 `unknown`。没有 DEM/深度时，地面覆盖范围保持空值。
