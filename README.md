# 德清数据集服务

7110 数据服务提供统一资源目录、五类标准数据包导入、PostGIS 空间查询、用户与场景管理，以及旧前端接口兼容。代码与配置模板适合进入 Git 仓库；影像、瓦片、数据库、令牌和本机凭据保留在服务数据卷或本地目录。项目目录和提交边界见[仓库整理说明](docs/repository-guide.md)，详细资料从[文档索引](docs/README.md)进入。

| 目录 | 内容 |
| --- | --- |
| `dataset_service/` | FastAPI 服务与管理终端 |
| `docs/` | API、数据包和部署文档 |
| `examples/data-packages/` | 五类数据包模板 |
| `scripts/` | 导入、读取与迁移工具 |
| `deploy/` | 现有容器的部署脚本和 Compose 覆盖配置 |
| `remote-data/` | 历史远端服务调查和采集工具；不属于 7110 运行时回源链路 |
| `tests/` | 已有自动化测试 |

本目录是独立的 Python 3.10+ 服务项目。`garden` 旧导入流程会复制影像和本地坐标 COLMAP 模型，但没有可供地图定位的 WGS84 相机位置与覆盖范围，因此新增导入和发布已暂停；已有已发布版本仍可读取。地图资源必须走带位置审查的专用入口。

## 本地启动

```bash
uv sync
export DEQING_DATA_DIR="$PWD/.data"
export DEQING_SOURCE_ALIASES='{"garden":"/absolute/path/to/garden"}'
uv run deqing-data
```

服务监听 `0.0.0.0:7110`。首次启动自动生成 `$DEQING_DATA_DIR/credentials.json`，权限为 `0600`，其中有独立的 `admin` 和 `reader` Bearer 凭据。用 `uv run deqing-data-credentials --data-dir "$DEQING_DATA_DIR"` 在服务主机查看；不要把凭据放进仓库、前端或日志。`/healthz`、旧前端注册和登录接口无需 Bearer；其他接口需要 `Authorization: Bearer <token>`，可使用静态 Bearer 凭据或登录返回的 JWT。只读身份访问管理员接口返回 403。

默认来源别名为 `garden -> /imports/garden`，可以用 `DEQING_SOURCE_ALIASES` JSON 对象配置额外的更新测试副本。接口仅接收别名；不接收客户端传入的文件路径。`DEQING_EXPECTED_IMAGES` 默认是 185，本地小型数据验证时可以调整。

## API

场景目录、独立图层实例和区域预览接口见 [7110 场景目录与区域图层接口](docs/7110-scene-layers.md)。
多期栅格瓦片目录与时间查询接口见 [7110 多期影像瓦片存储](docs/7110-temporal-raster.md)。

| 方法与路径 | 角色 | 用途 |
| --- | --- | --- |
| `GET /healthz` | 公共 | 数据库健康检查 |
| `POST /admin/datasets/import` | admin | 暂停；返回 422，直到 `garden` 有完整 WGS84 位置注册和审查流程 |
| `GET /admin/imports/{id}` | admin | 任务进度、版本号和失败原因 |
| `POST /admin/imports/{id}/publish` | admin | 已发布记录可幂等读取；未发布版本在补齐 WGS84 位置审查前不能发布 |
| `PATCH /admin/datasets/garden` | admin | 修改 `name` 或 `description` |
| `POST /admin/datasets/garden/versions/{version}/activate` | admin | 切换当前版本或回滚 |
| `GET /admin/audit` | admin | 最近的审计记录；`limit` 为 1–1000 |
| `GET /datasets`、`GET /datasets/garden` | admin/reader | 已发布数据集和当前版本 |
| `GET /datasets/garden/versions` | admin/reader | 已发布版本历史 |
| `GET /datasets/garden/versions/{version}/manifest` | admin/reader | 文件清单、SHA-256 和校验结果 |
| `GET /datasets/garden/versions/{version}/files/{relative_path}` | admin/reader | 仅下载清单中的文件；支持单段 `Range: bytes=...` |

兼容用户登录 JWT 也可用于以上受保护 API：内置 admin 登录后获得管理员角色；通过公开注册入口创建的账号固定为 reader。所有数据库写接口仍检查角色，注册操作只允许写入新普通用户，不接受客户端指定角色。

交互文档在 `/docs`。已有历史导入任务可能处于 `queued`、`copying`、`validating`、`validated`、`duplicate`、`failed` 或 `published` 状态；尚未发布的 `garden` 任务不能发布，因为这条旧流程不会核验地图所需的相机 WGS84 位置和覆盖范围。此前已发布的版本不受影响。

如需恢复 `garden` 导入，需先补齐相机位姿到 WGS84 的可追溯转换、数据覆盖范围，以及地图资源登记与审查记录；仅有 COLMAP 局部坐标不能作为地图位置。

## 旧数据服务接口兼容

同一个 7110 服务现在提供原数据平台的路径和响应形态。登录使用 `POST /api/Login/Login`，请求中的 `Password` 是明文密码的 MD5 小写十六进制；响应包含 3 天有效的 HS256 JWT，`AuthInfo` 保持 JSON **字符串**。普通用户可通过 `POST /api/Login/Register`（别名 `POST /api/Register/Register`）注册；请求字段为 `UserName`、`Password`（同样提交 MD5）和可选的 `RealName`，注册账号固定为只读角色。`GET /api/User/GetByToken` 返回当前 JWT 对应的账户信息。`POST /api/cim/CIMScene/GetCIMSceneResourceByPage` 从已保存的 369 条完整快照分页；`GET /api/DownLoad/GetFileStream?<FastDFS键>` 使用整段裸查询串作为键，找不到文件返回 204。旧前端选择资源弹窗需要的图层大类、资源类型和数据集下拉接口已由现有快照推导。`POST /api/Resource/GetByPage`、用户/角色/组织分页和空的 `GET /api/Layer/GetFile` 也已提供兼容路径。

### 管理终端

管理员可打开 `http://<7110 主机>:7110/admin/console` 查看统一资源目录。终端聚合旧资源快照、版本化点云/3D Tiles、视频组、PostGIS 矢量数据、时序栅格和倾斜影像索引，支持按类型、名称和空间范围状态筛选，并显示已登记的 WGS84 范围和各资源的访问入口。五类数据统一选择完整文件夹或 ZIP 上传；包内 `package.json` 声明数据类型，服务端校验后调用对应入库适配器。管理员可重命名资源并删除本地管理资源，数据包格式与上传协议见 [数据包格式与导入入口](docs/data-package-contracts.md)；旧镜像源快照及共享镜像文件保留只读，重命名旧资源时只在 7110 保存显示名覆盖。重命名不更改资源 ID、文件路径或已保存的场景图层名称。页面使用现有账户登录和 JWT；浏览器标签关闭后会清除会话，不保存密码或管理员密钥。

全部 POI 点存入 PostGIS 的 `poi_feature` 表，并作为唯一的“全部 POI”资源通过 [统一 POI 查询接口](docs/poi-unified-api.md) 按范围或 POI ID 读取。旧数据包名称仅保留在来源溯源信息中，不形成公开分类。

“接入与加载”页列出目前已有的格式入口与 API 模板。首版负责统一目录、核对空间登记和引导至已有接口文档；各类写入仍由现有服务端管理员授权校验，目录浏览不会改变资源或文件。新数据格式应先登记类别、元数据、空间范围和加载入口，再接入统一目录。

服务器的兼容资源放在 `/workspace/legacy/out` 和 `/workspace/data/legacy-mirror`，与 `garden` 的版本数据库分开。7110 只读取本地资源快照、文件与已经落盘的镜像对象，不再向旧数据服务发起请求。文件键本地缺失时返回 204，其他未保存的旧读取接口返回 404；本地镜像本身不可用时返回 503。已下载的镜像数据原样保留。

兼容登录用户保存在 `/workspace/data/legacy_users.sqlite3`（`0600`），服务启动时会将配置的内置账号标为唯一管理员；已有普通账号与新注册账号均为 `reader`。角色字段由服务端维护，注册请求不能设置角色。登录时按用户名查库，并用 PBKDF2-SHA256 校验前端提交的密码 MD5；库内不存明文或裸 MD5。每次登录签发独立的 3 天 JWT 会话，服务端只保存 Token 摘要；`POST /api/Login/Logout` 撤销当前会话，前端应同时清除本地 Token。普通用户可以读写自己的标绘，数据存入 PostGIS 并按用户 ID 隔离；服务管理员 Bearer 不绑定账户，不能读取个人标绘。场景、场景资源关系和自定义场景目录由账户 JWT 识别身份，只有 `admin` 可写，所有已登录账户可读；它们保存在 `/workspace/data/legacy_scenes.sqlite3`（`0600`），与只读远端镜像分开。除注册和个人标绘外，数据集导入/发布/激活、元数据修改和 PostGIS Shapefile 导入均要求 `admin`。`/workspace/data/legacy_auth.json`（`0600`）保存兼容管理员账号初始配置与 JWT 签名密钥。需要修改内置账号密码时，在容器中运行 `docker exec -it deqing-data /workspace/.venv/bin/deqing-data-legacy-auth --set-password`。浏览器跨域请求已启用 CORS，可通过 `DEQING_CORS_ORIGINS` JSON 数组限定来源。

旧客户端只改地址即可验证已有接口：

```bash
python3 remote-data/read_data.py --base-url http://192.168.150.41:7110 \
  --username admin --page-size 3
```

前端完整场景与地图仍取决于本地是否具有对应的场景配置、资源树、Raster TMS、GeoServer 服务和瓦片。7110 不会从旧地址补充缺失内容；旧标绘路径已桥接到用户隔离的 PostGIS 存储，DEM CTB、MVT、GLB、视频和 GeoServer 路径则只读取本地已有镜像。可用接口和数据缺口见 [旧前端兼容说明](docs/legacy-compatibility.md)。

## 服务器部署

### PostgreSQL / PostGIS 空间数据

7110 新增 `/spatial` API。PostgreSQL 16 + PostGIS 3.5 使用同一 Docker 内网中的独立 `deqing-postgis` 容器和持久化卷；Shapefile 的空间要素写入 `geometry(Geometry,4326)`，属性写入 `jsonb`，可通过 GeoJSON、边界框与相交查询读取。用户标绘通过 `/spatial/annotations` 读写，按账户 JWT 隔离。矢量数据作为标准数据包上传；SHP 组件、坐标约定和权限见[PostGIS 空间接口说明](docs/spatial-postgis.md)。旧前端通过 7010 的 `/legacy-data/spatial/...` 使用接口时，可沿用兼容登录 JWT。

所有类型的官方上传入口统一为 [`scripts/upload_data_package.py`](scripts/upload_data_package.py)，输入一个数据包文件夹或 ZIP；`scripts/upload_spatial.py` 等类型脚本保留为服务内部适配器和兼容工具。使用 [`scripts/read_spatial.py`](scripts/read_spatial.py) 列出矢量数据集并导出 GeoJSON；命令示例见[空间接口说明](docs/spatial-postgis.md#命令行脚本)。

现有倾斜影像已建立粗配准目录：原图保留在文件镜像，PostGIS 保存文件引用、相机参数和带假设的候选覆盖。按地理范围查图、按影像 ID 读取原图及重建方式见[倾斜影像粗配准目录](docs/oblique-image-catalog.md)。

空间检索分别面向矢量、个人标绘、倾斜影像、旧目录 3D Tiles、Raster TMS 和已登记空间范围的版本化点云；各类型使用自己的匹配与获取方式。接口目录、TMS 瓦片范围查询和点云资源范围登记说明见[按经纬度查询与获取数据](docs/spatial-query-interfaces.md)。

点云 API 已加入 `/pointcloud`：支持资源与版本登记、类别定义、可续传分块上传、PLY/3D Tiles 包校验发布、文件清单和文件读取，以及外部语义分割任务状态记录。普通用户只读已发布数据；上述点云写入操作全部要求管理员身份。新的网页和命令行导入统一使用数据包协议，底层点云 API 文档见[点云接口说明](docs/pointcloud-api.md)。

视频资料组 API 已加入 `/videos`：以统一 `calibration.xml` 契约登记精标定与 WGS84 产品覆盖范围，采用 SHA-256 清单和可续传分块上传保存 MP4 及组内任意附件，并支持按 bbox 查询、返回目录树和读取文件。写入只允许管理员，普通用户可读已发布组。网页和脚本使用五类共用的数据包上传入口；底层资料组 API 见[视频资料组接口说明](docs/video-group-api.md)。

服务已部署在 `192.168.150.41` 的现有 `deqing-data` 容器内，没有重建镜像或容器。现有 `deqing_data-workspace` 卷、`deqing_default` 网络和 7110–7159 端口映射继续使用。`http://192.168.150.41:7110/healthz` 已返回 200。

当前部署的代码在容器卷内 `/workspace/data-service-source`，虚拟环境在 `/workspace/.venv`，数据库、凭据和已发布版本在 `/workspace/data`，旧资源快照和文件在 `/workspace/legacy/out`。服务器宿主机 `/data1/hj/data/garden` 已复制到卷内 `/workspace/imports/garden` 作为导入源，原始目录未改动。第二版测试来源是单独的 `/workspace/imports/garden-update`，仅追加了 `DSC07956.JPG` 的测试内容。卷内现有两版数据，当前版本为 `20261023T043605005697Z-0a78a7baa3ac`。

2026-11-02 趁远端网络可用更新了旧服务镜像：7110 本地资源目录现有 370 条资源、16 个数据集和 24 种资源类型；容器 `/workspace/data/legacy-mirror` 中 23,599 个非空文件键对应的缓存对象均已核验存在且长度匹配，约 103 GiB。这里包含远端当前能枚举、能用文件键取回的数据；没有根键或相对路径映射的 3D Tiles 子瓦片、未暴露文件键的视频原片和部分 TMS 瓦片仍无法从旧接口取得。补采及核验细节见 [DATA_SURVEY.md](remote-data/DATA_SURVEY.md)。

在运行中的容器里部署时，不能给它新增 Docker 只读挂载，也不能修改其原始启动命令。当前方案使用卷内来源快照，以及宿主机 `hj` 用户的 cron 每分钟检查一次服务进程；服务停止或容器重启后会自动重新执行 `docker exec -d`。守护脚本是 [ensure_service.sh](deploy/ensure_service.sh)，安装在服务器 `/home/hj/deqing-dataset-system/deploy/ensure_service.sh`。已通过一次容器重启验证恢复。

以下是复现或更新部署的步骤。源码完成审查并提交到 Git 后，可只同步该提交中的文件，避免传输本机凭据和采集缓存：

```bash
git archive --format=tar HEAD | \
  ssh 192.168.150.41 'mkdir -p /home/hj/deqing-dataset-system && tar -xf - -C /home/hj/deqing-dataset-system'
```

SSH 登录服务器后执行（现有 Ubuntu 容器缺少 `ensurepip`，先从系统 pip 在命名卷中安装 `virtualenv`）：

```bash
docker exec deqing-data python3 -m pip install --target /workspace/venv-bootstrap virtualenv
docker exec deqing-data env PYTHONPATH=/workspace/venv-bootstrap python3 -m virtualenv /workspace/.venv
docker cp /home/hj/deqing-dataset-system/. deqing-data:/workspace/data-service-source/
docker exec deqing-data /workspace/.venv/bin/pip install /workspace/data-service-source
```

首次部署旧资源快照时，仅复制已取得的目录和索引；不复制 `remote-data/.token`：

```bash
ssh 192.168.150.41 'docker exec deqing-data mkdir -p /workspace/legacy/out'
tar -C remote-data/out -cf - catalog.db resources indexes assets images | \
  ssh 192.168.150.41 'docker exec -i deqing-data tar -C /workspace/legacy/out -xf -'
```

守护脚本中的环境变量配置了两个来源别名。服务日志在 `/workspace/data/service.log`，访问凭据在 `/workspace/data/credentials.json`。更新 Python 代码后需要重新安装项目并重启 Uvicorn 进程；守护脚本会自动拉起新进程。

如果以后要求容器直接读取宿主机原始 `garden`、以 Docker 方式强制只读挂载，并由容器主进程启动服务，则需要修改现有 Compose 配置并重新创建该容器；可参考 [覆盖配置](deploy/compose.data.override.yaml)。这不要求重建镜像。当前账号 `hj` 无权读取 `/home/ljj/deqing-docker/compose.yaml`，因此当前运行方案未修改它。

## 已验证范围

本地测试覆盖导入、文本与二进制模型一致性、发布、只读角色拒绝管理操作、文件和 Range 下载、元数据更新、第二版本与回滚。运行：`uv sync --extra test && uv run pytest -q`。服务器上的原始 `garden` 已只读解析：1 个相机、185 张注册影像、138,766 个三维点。两版实际数据已导入、发布，抽查下载 SHA-256 与清单一致；只读身份写入返回 403；容器重启后服务和两版数据可访问。可在容器中运行 `python /workspace/data-service-source/deploy/verify_demo.py --second-alias garden_update` 重复验收。
