# 7110 统一数据包样例

网页与脚本统一上传一个完整文件夹或 ZIP。每个包根目录包含 `package.json` 和 `data/`；将下面对应类型的模板复制为 `package.json`，填写实际位置审核信息，再把原始数据放入 `data/`。

- [矢量](vector/README.md)：同名 `.shp/.shx/.dbf`，可选 `.prj/.cpg`。
- [点云、模型和地形](pointcloud/README.md)：PLY 或闭合完整的 3D Tiles 文件树。
- [时序栅格](raster/README.md)：期次元数据和 TMS 瓦片树。
- [倾斜影像](oblique/README.md)：相机组、逐图位姿和原图。
- [定向视频](video/README.md)：MP4、精标定和正射产品附件。

上传命令对所有类型相同：

```bash
python3 scripts/upload_data_package.py /path/to/package-folder \
  --base-url http://192.168.150.41:7110 --token-file /path/to/admin-token.json
```

也可以把整个包压缩为 ZIP，传 ZIP 文件路径。示例仅提供结构和字段模板，不含业务数据；空间范围、CRS、轴序和证据必须替换成真实且已审核的值。不要把管理员令牌放入数据包。
