# 仓库结构与提交边界

本文用于准备源码仓库。7110 的运行数据与服务源码分开管理；克隆代码不会自动获得服务器上已入库的影像、瓦片、视频或数据库。

## 目录职责

| 路径 | 内容 | 建议提交 |
| --- | --- | --- |
| `dataset_service/` | FastAPI 服务、数据库适配器、管理终端静态文件 | 是 |
| `docs/` | API 契约、数据包格式、部署与历史兼容说明 | 是 |
| `examples/data-packages/` | 五类资源的声明模板 | 是 |
| `scripts/` | 数据包上传、读取及一次性迁移脚本 | 是；凭据文件除外 |
| `deploy/` | Compose 覆盖配置、服务守护脚本 | 是；实际环境文件除外 |
| `tests/` | 已有测试源码 | 是 |
| `remote-data/` | 旧平台调查文档和历史采集程序 | 仅源码、契约与文档 |
| `remote-data/out/` | 采集结果、SQLite 清单和镜像文件 | 否 |
| `private/`、`.data/` | 本机配置、数据库及服务数据 | 否 |
| `deliverables/` | 临时交付压缩包和二进制文件 | 否 |

`remote-data/resources.json` 和 `gs_wms.xml`、`gs_wfs.xml` 是远端响应快照，默认不提交；重新采集时可生成。`remote-data/.token` 与 `scripts/deqing-credentials.json` 是本机凭据，必须留在仓库外。`.gitignore` 也排除虚拟环境、缓存、数据库备份和常见私钥文件。

## 从源码启动

安装 Python 3.10+ 和 `uv`，执行 `uv sync`，设置本机 `DEQING_DATA_DIR` 后运行 `uv run deqing-data`；最小启动命令见[根 README](../README.md#本地启动)。`/healthz`、基础服务和交互文档可在没有远端镜像时启动。PostGIS 空间接口需要 PostgreSQL/PostGIS 与 `DEQING_POSTGIS_DSN` 或 `DEQING_POSTGIS_DSN_FILE`；旧资源兼容接口需要 `DEQING_LEGACY_OUT` 指向已经迁移的本地镜像。新机器须另行迁移数据库卷和对象文件，源码仓库不提供它们。

`deploy/compose.postgis.yaml` 使用服务器本机的 `private/postgis.env`；可从 [`deploy/postgis.env.example`](../deploy/postgis.env.example) 复制并更换密码，此文件不能提交。`deploy/compose.data.override.yaml` 是现有 Docker 部署的覆盖配置，不是可独立启动的完整 Compose 文件。`deploy/ensure_service.sh` 针对现有 `deqing-data` 容器和卷路径，复制到新环境前应核对名称与路径。

## 上传远程仓库前

1. 确认目标仓库地址及公开级别。历史调查文档包含旧平台地址、内网拓扑和数据来源，若需公开仓库，应先确定这些信息的公开范围。
2. 确认项目源码和历史采集资料的授权与许可证；目前项目根目录没有许可证，不应擅自声明开源许可。
3. 确认线上管理员账号密码、服务级 Bearer 和数据库密码未被写入提交内容；若曾经共享过演示密码，应在发布前更换。凭据保持在服务数据卷或本机私有配置中。
4. 提交前查看待提交文件列表和大文件列表。不要用 `git add -f` 强行加入被忽略的数据或凭据。

本次整理只准备本地目录与说明；远程仓库地址、公开级别和正式提交仍待确定。
