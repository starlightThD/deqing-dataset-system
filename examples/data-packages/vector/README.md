# 矢量数据包

```text
vector-package/
├── package.json
└── data/
    ├── parcels.shp
    ├── parcels.shx
    ├── parcels.dbf
    ├── parcels.prj       # 或 package.json metadata.source_srid
    └── parcels.cpg       # 可选
```

`data/` 根目录恰好放一组同名 Shapefile 组件。含 Z 坐标时填写垂直 CRS 和单位；服务保留原始 Z，不替用户变换垂直基准。复制 `package.json.example` 为包根的 `package.json` 并填写审核证据。统一导入：

```bash
python3 scripts/upload_data_package.py /path/to/vector-package --token-file /path/to/admin-token.json
```
