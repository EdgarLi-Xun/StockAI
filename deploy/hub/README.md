# 独立同步 Hub

Hub 仅管理账号、空间、设备、密文对象和条件写入，不运行 Qt、行情采集、模型或通知服务。它可与桌面、Web 分机运行。

1. 从项目根目录构建：`docker compose -f deploy/hub/compose.yml build`。
2. 复制 `.env.example` 为本机 `.env`，设置独立域名、容量和 age 接收公钥。只通过 Caddy 发布 HTTPS，8770 不发布到宿主公网。
3. 停服时创建账号：`docker compose -f deploy/hub/compose.yml run --rm -it hub python -m backend.hub.cli create-user --email me@example.com`，密码在交互提示中输入。
4. `docker compose -f deploy/hub/compose.yml up -d`。健康路径为 `/v1/health`。

业务索引在 `hub_data` 的 `live/hub.sqlite3`。最新控制记录、密文对象、撤销和已消费设备授权记录在独立 `hub_control`。控制卷不能随业务数据库回退；启动会核验控制链并补齐索引。两个卷和运维密钥都应备份，但日常恢复不得拿旧控制记录覆盖最新记录。

原生运行：安装 `requirements-hub.txt`，设置 `STOCKAI_HUB_DATA_DIR`、`STOCKAI_HUB_CONTROL_DIR`，再运行 `python -m backend.hub.cli create-user` / `serve`。生产必须在可信 TLS 代理后，显式设置 `STOCKAI_HUB_BEHIND_TLS_PROXY=1`，并将后端监听限制在回环或私有容器网络。开发 HTTP 仅限隔离测试环境的 `STOCKAI_HUB_DEV=1`。

Web 的账号与 Hub 账号独立。Hub 首次建空间需要本人的 Hub 登录；加入同一空间需同一 Hub 账号、现有设备发出的单次授权以及同步解密密钥证明。管理员不持有业务解密密钥。

升级入口为 `python -m backend.upgrades.cli --help`。执行前停止 Hub，提供离线 age 身份；缺少执行环境时仅准备计划。容器与原生路径需保持一致，不得同时启动两个进程写同一个空间索引。

本地受控 TLS 和接口测试不代表此 Compose 已在目标 Linux 主机通过。首次生产部署应完成域名、证书、持久卷权限、容量耗尽、备份恢复、跨机断网与升级回退联调。详见 `docs/验收记录-同步与升级.md`。

停用 Hub 账号：在停服并持有服务锁时执行 `python -m backend.hub.cli disable-user --email me@example.com`。停用写入独立权威链，撤销全部会话和设备，业务索引回退也不能重新激活这些授权。
