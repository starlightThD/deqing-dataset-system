# 文档索引

从[项目 README](../README.md)了解运行方式；以下文档按使用任务分组。

## 上传与管理

- [五类统一数据包契约](data-package-contracts.md)：`oblique`、`pointcloud`、`raster`、`vector`、`video` 的目录和上传协议。
- [管理终端](data-management-console.md)：资源目录、上传、重命名与删除。
- [点云与 3D Tiles](pointcloud-api.md)、[视频资料组](video-group-api.md)、[倾斜影像包](oblique-resource-package.md)、[PostGIS 矢量](spatial-postgis.md)、[多期栅格](7110-temporal-raster.md)：类型专属规则。

## 查询与场景

- [统一资源空间查询](unified-resource-query.md)及[7010–7110 接口契约快照](7010-7110-resource-query-contract.md)。
- [各类型空间查询入口](spatial-query-interfaces.md)、[场景目录与图层](7110-scene-layers.md)、[区域瓦片子集的历史方案](spatial-resource-subsets.md)。
- [统一 POI](poi-unified-api.md)、[倾斜影像粗配准目录](oblique-image-catalog.md)。

## 部署与旧接口

- [仓库结构与提交边界](repository-guide.md)：源码、数据卷和凭据的划分。
- [旧前端兼容范围](legacy-compatibility.md)：本地镜像可读内容及缺口。
- [远端调查资料](../remote-data/README.md)：历史采集工具，不是当前 7110 的回源通道。

接口若与历史调查记录不一致，以当前服务代码和 `/docs` 为准；带日期的远端可用性与数据量只代表当时的快照。
