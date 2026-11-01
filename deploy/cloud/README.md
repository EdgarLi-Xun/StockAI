# StockAI 公网多用户版 3.0

独立入口为 `backend.public_app:app`，独立前端构建为 `frontend/dist-public`。现有 Windows / 8765 本地版继续使用原来的数据与入口。公网版不提供本机自动管理员身份，不会自动迁移本地文件。

首发部署目标是单台 Linux x86_64、20 个账号、两个报告执行槽。Compose 只公开 Caddy 的 80/443，应用、SQLite、报告与 Ollama 不直接公开。一个 Uvicorn worker 持有站点独占锁；增加 worker 会被拒绝。

## 构建与初始化

也可从 [GitHub Release](https://github.com/EdgarLi-Xun/StockAI/releases/latest) 下载 `StockAI-3.0.0-Public.zip` 并核对同名 `.zip.sha256`。该包包含构建后的 `frontend/dist-public`，可跳过下方前端 npm 步骤，进入 `deploy/cloud` 后配置环境并构建应用镜像。

在有 Docker Compose、Node.js 24 和网络的 Linux 主机上，从仓库根目录执行：

```sh
cd frontend
npm ci
npm run build:public
cd ../deploy/cloud
cp .env.example .env
# 编辑 .env：填写真实域名、固定 HTTPS 根地址、SMTP、age 接收公钥。
docker compose build app
docker compose run --rm -it app python -m backend.cloud.cli init --email admin@example.com
```

管理员密码由终端交互输入，不放在命令参数、镜像或代码中。`init` 输出的管理员 UUID 用于可选迁移。管理员由站点运维明确初始化并视为已验证；其余用户必须使用七天内有效、单次使用的邀请码，并验证邮箱。

在单独保管的运维环境中使用标准 `age-keygen` 创建身份文件。将其输出的**接收公钥**填入 `STOCKAI_BACKUP_RECIPIENT`。解密身份不要挂载进正在运行的应用容器，也不要放进研究数据卷。主密钥文件与 age 解密身份用途不同，必须分别保管。

初始化完成后启动：

```sh
docker compose up -d
docker compose ps
docker compose logs --tail=50 app
```

域名需要指向服务器；80/443 应可被 Caddy 证书签发流程访问。正式环境不要设置 `STOCKAI_CLOUD_DEV=1`。SMTP 默认使用验证证书的隐式 TLS；需要 STARTTLS 时同时调整端口与开关。开放邀请前，应以受控测试邮箱验证一次注册与重置流程。

应用以 UID/GID 10001 运行，Docker 命名卷初始化目录权限。使用自定义 bind mount 时，应预先赋予该 UID 所需权限。三个持久卷分别保存研究数据、独立注销权威记录、凭据主密钥，不要合并到同一个可整体回退的目录。首次上线前还需独立备份注销权威目录和主密钥目录；它们不包含在普通研究备份中。

研究卷挂载到 `/var/lib/stockai`，实际数据库在其 `live` 子目录，供离线恢复原子替换；不要将实际数据目录本身设为不可重命名的挂载点。

## 可选：从本地版迁移副本

在开放注册前进行。目标必须只有明确初始化的管理员，且没有个人研究数据。原数据目录只读挂载；迁移不会改写原数据库、报告或旧启动入口。

```sh
docker compose stop app
docker compose run --rm -it -v /srv/stockai-local-copy:/legacy:ro app \
  python -m backend.cloud.cli migrate --source /legacy --admin-id <管理员UUID>
docker compose up -d app
```

迁移保留旧报告文件内容，给报告建立个人归属与版本系列；复制模拟账户、订单、持仓、账本、回测及历史任务。旧未完成任务标为待核查，不自动重发模型请求。个人自动计划和自动 AI 关闭；迁移的模型配置需重新确认。旧的 SMTP、局域网密码等站点秘密不作为个人偏好导入。

迁移先建立加密回滚点，再在独立临时副本中检查数量、外键、报告文件哈希和归属，通过后才切换目录。重复执行会返回已完成状态。缺少文件、目标不空或校验失败时，当前目标和原始数据保持原样。

## 个人模型与任务

登录后进入“模型配置”，分别保存多套 MiniMax 国内站、国际站、OpenAI 兼容 HTTPS 服务或服务器 Ollama 配置，再选择个人默认。Key 读取接口只返回掩码；密文使用 AES-GCM，并绑定站点、用户、资源及用途。

MiniMax 预设依据当前官方工具接入说明：国内 `https://api.minimax.cn/v1`，国际 `https://api.minimax.io/v1`，默认 `MiniMax-M3.1-Flash`。它使用 Chat Completions 参数和独立推理字段；不发送 Ollama 参数。不同订阅的实际可用性需要本人 Key 验证。[国内接入](https://platform.minimax.cn/docs/m-plan/other-tools)、[国际接入](https://platform.minimax.io/docs/m-plan/other-tools)、[协议说明](https://platform.minimax.cn/docs/api-reference/text-openai-api)。

通用模型地址必须是公网 HTTPS。每次发送前验证全部 DNS 地址，并将实际连接固定到已验证 IP，TLS 仍校验原主机名；不跟随重定向、不使用环境代理。服务器 Ollama 地址仅从站点环境变量读取，个人用户不能将其改成自己的电脑。

默认模型输入由字段白名单重新构造，排除账号身份、账户名称、自由备注、凭据和逐笔成交。资金持仓汇总、模拟成交明细是每套配置各自独立的授权，默认关闭。连接测试也会发起真实模型请求，可能消耗额度。

新账号的三个自动计划及自动 AI 均关闭。开启计划但不启用自动 AI 时仅生成规则报告；开启自动 AI 后，退出登录不影响已授权计划。计划使用 Asia/Shanghai，只补跑当天。共享行情由站点统一去重采集，个人任务不各自刷新全市场。

提交任务时固定模型配置版本、凭据引用、授权和个人参数；切换默认仅影响新任务。每次真正发送及重试前再次检查账号、Key 与当前授权。明确限流、临时 502/503 或发送前连接失败最多重试一次；无效 Key、额度不足、校验失败和已发送但结果不确定的超时不自动重试，不切换其他配置。失败会生成带原因的规则报告。对规则报告“补充 AI”会用原事实快照生成新版本，保留旧归档。

## 备份、恢复与注销

每日上海时间 02:00 后建立一次加密备份，并清理超过30天的系统副本。管理员也可在页面创建备份；导出前需再次验证密码，授权五分钟内有效、单次使用，且绑定本人会话和具体备份。下载后的副本由管理员按保留期限管理。

离线手动备份、状态与主密钥轮换：

```sh
docker compose stop app
docker compose run --rm app python -m backend.cloud.cli status
docker compose run --rm app python -m backend.cloud.cli backup
docker compose run --rm app python -m backend.cloud.cli rotate-key
docker compose up -d app
```

轮换保留旧密钥版本，以验证独立注销记录、备份登记和仍在保留期内的备份；不要自行删除旧版本。备份采用 SQLite 在线备份接口加报告文件清单，不复制正在写入的主库/WAL。下载目录只存 `.age` 密文，清单包含站点、版本、数量和文件校验和。

恢复必须停服，提供匹配的 age 解密身份，以及**可信、最新且独立保存**的注销权威目录和主密钥目录。不能用所选旧研究备份重建或覆盖它们；缺失或签名、序号不连续时程序拒绝上线。运维仍需确保拿到的是当前权威目录，而非人为回退的历史副本。

```sh
docker compose stop app
docker compose run --rm -it -v /srv/offline/stockai-age-identity.txt:/ops/identity:ro app \
  python -m backend.cloud.cli restore --id <备份ID> --identity /ops/identity
docker compose up -d app
```

恢复核验密文受控登记、清单、站点、路径、数量及外键；先在副本中应用全部注销记录，再清除会话、邮件令牌和导出授权，取消旧在途任务，关闭自动 AI，并将模型配置标记为“需要重新连接”。旧 Key 不恢复为可用状态。替换前保留加密恢复点；签名切换记录使进程在目录替换中崩溃时可于启动前回滚或完成清理。

仅在离线恢复命令期间，让容器内 UID/GID 10001 能读取该身份文件，挂载保持只读；恢复后撤去该挂载。不要通过命令参数直接传入解密私钥内容。

用户自助注销需要当前密码与完整确认语句。先持久写入独立注销记录，再删除主库中的个人数据、凭据与报告文件；发布事务和短发布锁防止在途任务重新写回。若最后一名管理员自行注销，可停服后以 `create-admin --email <新管理员邮箱>` 交互初始化新的管理员身份；它不会继承已注销者的私人研究。

## 开发验证

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-cloud.lock "pytest==9.2.0"
.\.venv\Scripts\python.exe -m pytest tests
cd frontend
npm run build:public
```

Windows 本机若 SQLite 尚无 WAL-reset 修复，明确开发模式使用 DELETE journal；生产模式拒绝启动。部署镜像从官方源码构建 SQLite 3.54.1，并核验上游 SHA3-256；运行时再次检查实际动态库版本。[SQLite 修复说明](https://sqlite.org/wal.html#walreset)、[官方下载](https://sqlite.org/download.html)。

本机预览可运行 `python scripts/preview-cloud.py`，地址为 `http://127.0.0.1:8766`。它只在 `.tmp/cloud-preview` 创建隔离的开发账号和数据；登录信息存放于其中的 `preview-login.txt`，不作为正式公网账号，也不读取原本地数据。

详细测试覆盖和仍需真实外部服务验证的项目见 [验收记录](../../docs/验收记录-公网多用户与模型接入.md)。
