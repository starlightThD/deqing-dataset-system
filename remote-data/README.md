# 历史远端服务调查与采集工具

本目录保存 7110 复刻旧接口时使用的调查记录、接口契约和采集脚本。**当前 7110 运行时只读取已落盘的本地镜像，不会从这里配置的旧服务地址回源。**

- `REMOTE_ACCESS_SPEC.md`、`DATA_SURVEY.md`：历史实测结果和迁移记录；其中的资源数量、网络状态与旧服务地址是调查时的快照。
- `api_spec.json`、`local_schema.sql`：旧接口契约与最初的本地目录 DDL。
- `read_data.py`：显式输入用户名、密码后读取旧接口；省略 `--password` 时交互输入。
- `mirror.py` 及 `pull_*.py`：历史采集工具，依赖 `apilib.py`；需要先设置 `REMOTE_DATA_PASSWORD`，可选 `REMOTE_DATA_USERNAME`。本地缓存令牌在 `.token`，不会提交。
- `local_service.py`：仅用于对照旧接口形态的标准库样例；启动时必须显式给 `--password`。

下载结果、资源快照及采集状态写在 `out/` 或同目录的生成文件中，均不进入源码仓库。要复现旧平台采集，请先核对目标地址与授权；不要把访问凭据写入脚本、文档或命令行历史。
