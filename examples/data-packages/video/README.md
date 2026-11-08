# 定向视频数据包

```text
video-package/
├── package.json
└── data/
    ├── 原始视频.mp4
    ├── 01_粗搜索匹配图.png
    ├── 02_精搜索匹配图.png
    ├── 03_范围示意图.png
    ├── 04_粗标定结果.txt
    ├── 05_精标定结果.txt
    ├── 06_裁剪OBJ/
    │   ├── region.obj
    │   └── region.mtl
    ├── 07_投影图.png
    ├── 08_精匹配图.png
    └── 09_正射叠加图.tif
```

目录必须含一份 MP4 和当前契约列出的标定、正射与成果文件。精标定需提供 fx/fy/cx/cy、Xs/Ys/Zs、phi/omega/kappa、畸变参数及重投影误差；GeoTIFF 必须带 CRS。复制包头并核对相机中心和产品覆盖范围后，用统一入口上传：

```bash
python3 scripts/upload_data_package.py /path/to/video-package --token-file /path/to/admin-token.json
```
