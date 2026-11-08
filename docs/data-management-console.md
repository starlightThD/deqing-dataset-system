# 7110 数据管理终端

管理员入口：`http://<7110 主机>:7110/admin/console`

## 统一上传入口

`oblique`、`pointcloud`、`raster`、`vector`、`video` 五类数据均通过同一个数据包协议上传。网页控制台和命令行都选择完整文件夹或 ZIP；包根目录放 `package.json` 和 `data/`。包头中的 `type` 决定校验与入库适配器，用户不再按类型填写不同的上传表单或调用不同的上传协议。

命令行统一使用：

```bash
python3 scripts/upload_data_package.py /path/to/package-folder \
  --base-url http://192.168.150.41:7110 --token-file /path/to/admin-token.json

python3 scripts/upload_data_package.py /path/to/package.zip \
  --base-url http://192.168.150.41:7110 --token-file /path/to/admin-token.json
```

每类 `data/` 载荷和 `metadata` 字段见[统一数据包契约](data-package-contracts.md)及 [`examples/data-packages/`](../examples/data-packages/README.md)。`upload_spatial.py`、`upload_pointcloud_package.py` 等原有脚本作为 7110 内部适配器或兼容工具保留；对外推荐的网页和脚本上传流程均走统一数据包入口。查询、文件读取及删除 API 仍按资源类型提供。

统一上传 API（全部要求管理员 JWT）：

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/admin/data-packages/uploads` | 创建文件夹或 ZIP 上传会话，登记 SHA-256 清单 |
| `PUT` | `/admin/data-packages/uploads/{id}/files/{index}/chunks/{chunk}` | 按 8 MiB 分块上传，校验 `Content-Range` 和块长度 |
| `GET` | `/admin/data-packages/uploads/{id}` | 查询状态与已收分块，供中断续传 |
| `POST` | `/admin/data-packages/uploads/{id}/finalize` | 校验总文件 SHA-256、包结构及类型契约，再入库 |
| `DELETE` | `/admin/data-packages/uploads/{id}` | 取消尚未开始入库的上传草稿 |

单个普通文件最大 24 GiB，ZIP 文件最多 100 GiB，解压后的数据包最多 100 GiB、10,000 个文件。服务端拒绝路径穿越、重复路径、符号链接、超限文件和异常 ZIP 压缩比。`finalize` 接受后返回 `202`，客户端轮询状态直至 `complete` 或 `failed`；失败草稿可删除后修正重传，正在整理或入库的会话不能取消。大文件走分块上传，不经过 7010 代理。

## 控制台能力与权限

- 使用 `/api/Login/Login` 登录，账户 JWT 仅存于当前标签页 `sessionStorage`；只有 `Role=admin` 可进入终端。
- 资源目录汇总旧资源快照、版本化点云与 3D Tiles、视频组、PostGIS 矢量数据、时序栅格和倾斜影像索引；支持类型、名称/ID、CRS 和空间范围状态筛选。
- 上传包要求 `location_review.confirmed=true`，并提供源 CRS、坐标轴顺序、转换/核验方法和可复查证据。各类型适配器进一步校验几何、位姿、瓦片引用、TMS 期次或视频成果。
- 所有数据包导入和资源删除只允许管理员。普通注册用户仍只读公共数据，可注册、修改自己的密码，并读写只属于自己的标绘。
- 管理员可通过资源详情调用 `DELETE /admin/resources/{kind}/{uuid}` 删除受支持的本地资源。共享镜像文件、旧平台只读快照和被其他资源引用的文件不会被强制删除。

## 查询、读取和删除

上传接口统一不改变资源本身的读取语义。矢量使用 `/spatial/datasets` 和要素查询；栅格使用 `/spatial/raster-collections` 及期次瓦片路径；倾斜影像使用 `/spatial/oblique-images`；视频使用 `/videos/groups` 和资料组文件路径；PLY/3D Tiles 使用 `/pointcloud` 以及兼容的 Mesh/点云路径。可按经纬度范围查询的接口汇总见[空间查询接口目录](spatial-query-interfaces.md)。

旧目录镜像及其共享文件仍是只读数据来源；文本和未定义扩展类型不属于当前五类上传契约。新增类型需要先确定元数据、位置语义、存储方式、查询/加载接口和服务端校验，再扩展 `deqing.data-package.v1`。
