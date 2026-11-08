# 倾斜影像位姿数据包

```text
oblique-package/
├── package.json
└── data/
    ├── camera_groups.json
    ├── poses.jsonl
    ├── provenance.json       # 可选
    └── images/
        ├── image-0001.jpg
        └── ...
```

每张原图恰好对应一条 pose。相机到世界矩阵、坐标轴、CRS 和垂直单位必须来自可追溯的标定资料。没有 DEM/深度时，系统只登记相机中心和姿态，不把相机点当成影像覆盖范围。复制示例包头并完成位置复核后，使用统一入口：

```bash
python3 scripts/upload_data_package.py /path/to/oblique-package --token-file /path/to/admin-token.json
```
