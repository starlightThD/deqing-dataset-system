# 7110 多期影像瓦片存储

四期影像按一个时间序列栅格集登记。PostGIS 保存数据集范围、坐标系、瓦片方案、分辨率和期次索引；PNG 文件保存在 `$DEQING_DATA_DIR/rasters` 持久目录中。期次日期由目录名 `imagery_YYYYMMDD` 读取。

当前导入批次：

| 期次 | CRS | 方案 | zoom | 瓦片数 |
| --- | --- | --- | --- | ---: |
| 2022-08-04 | EPSG:3857 | TMS | 1–17 | 33,106 |
| 2023-05-23 | EPSG:3857 | TMS | 1–17 | 33,106 |
| 2024-10-11 | EPSG:3857 | TMS | 1–17 | 33,106 |
| 2025-01-10 | EPSG:3857 | TMS | 1–17 | 33,106 |

瓦片请求保持源 TMS 行号方向：`GET /spatial/raster-collections/{collection_id}/epochs/{YYYY-MM-DD}/tiles/{z}/{x}/{y}.png`。所有查询接口要求 Bearer 登录。前端需使用接口返回的 `tile_url_template`，并设置 TMS scheme；不可把它当 XYZ 行号直接拼接。

## 读取 API

- `GET /spatial/raster-collections`：数据集目录和期次数量。
- `GET /spatial/raster-collections/{collection_id}`：范围、CRS、TMS 配置、每期瓦片统计及 URL 模板。
- `GET /spatial/raster-collections/{collection_id}/tiles?acquisition_date=2022-08-04&bbox=minLon,minLat,maxLon,maxLat&zoom=17`：返回范围与层级相交的瓦片列表和本地可用状态。
- `GET /spatial/raster-collections/{collection_id}/epochs/{date}/tiles/{z}/{x}/{y}.png`：读取单张瓦片。

新增期次按[栅格数据包结构](../examples/data-packages/raster/README.md)整理到 `data/imagery_YYYYMMDD/{z}/{x}/{y}.png` 并附 `meta.json`，然后通过统一数据包入口上传：

```bash
python3 scripts/upload_data_package.py /path/to/raster-package \
  --base-url http://192.168.150.41:7110 --token-file /path/to/admin-token.json
```

登记必须显式确认已核对 CRS、WGS84 bounds、瓦片方案和矩阵，并提供可追溯的位置依据；审查信息会保存在集合 metadata 中。元数据应保持 CRS、scheme、tileSize、bounds、minzoom、maxzoom 和 tile path template 在同一个集合内一致。`register_temporal_raster.py` 保留为统一上传服务调用的内部适配器。
