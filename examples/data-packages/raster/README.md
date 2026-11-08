# 时序 TMS 栅格数据包

```text
raster-package/
├── package.json
└── data/
    ├── imagery_20260101/
    │   ├── meta.json
    │   └── {z}/{x}/{y}.png
    └── imagery_20260201/
        ├── meta.json
        └── {z}/{x}/{y}.png
```

每期 `meta.json` 声明 scheme、CRS、瓦片大小、zoom 范围、WGS84 bounds 和瓦片路径模板；同一集合的矩阵和范围必须一致。复制旁边的 `package.json.example`，核实源 CRS、TMS 和 bounds 后，使用统一入口：

```bash
python3 scripts/upload_data_package.py /path/to/raster-package --token-file /path/to/admin-token.json
```
