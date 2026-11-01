# StockAI

股票与贵金属研究、AI 解释、模拟交易和历史回测工具。支持 Windows 桌面与浏览器共用一套本地数据，也提供可独立部署的公网多用户模式。

**所有交易均为模拟；项目没有真实证券账户接入或实盘下单接口。**

[下载发行包](https://github.com/EdgarLi-Xun/StockAI/releases/latest) · [本地版完整指南](docs/LOCAL.md) · [公网部署指南](deploy/cloud/README.md) · [更新记录](CHANGELOG.md)

![StockAI 本地版研究总览](docs/screenshots/dashboard.jpg)

## 可以做什么

- **行情观察**：自选股票、ETF、指数及黄金白银，展示行情来源、时间、币种、单位和数据缺口。
- **研究报告**：标准版早报、完整版复盘、定时归档；模型不可用时生成带原因的规则报告。
- **风险与预算检查**：检查证券状态、证据时效、交易单位、费用和预算；候选最多三只，也可以为零。
- **模拟交易**：虚拟资金、限价委托、现金与持仓预留、T+1、费用、滑点和交易流水。
- **历史回测**：收盘信号、后续开盘撮合、样本内外划分、费用敏感性，以及带 SHA256 的快照重演。
- **个人研究空间**：公网模式提供邀请注册、邮箱验证、个人自选/报告/模拟账户/回测、模型配置和任务队列。
- **备份与恢复**：本地 ZIP 备份；公网模式提供加密备份、离线恢复、主密钥轮换和账号注销流程。

## 选择运行模式

| | Windows 本地版 | 公网多用户版 |
| --- | --- | --- |
| 适用场景 | 自己在电脑上研究，桌面与浏览器共用数据 | 在 Linux 服务器上供多个用户分别使用 |
| 入口 | `Start-StockAI.cmd` / `Start-Web.cmd` | `backend.public_app:app`，由 Caddy 提供 HTTPS |
| 默认地址 | `http://127.0.0.1:8765` | 配置的 HTTPS 域名，容器内部端口 8766 |
| 模型服务 | 本机 Ollama | MiniMax、OpenAI 兼容 HTTPS 服务、服务器端 Ollama |
| 个人数据 | 项目下的 `data/` | 独立研究数据卷，按用户隔离 |
| 操作说明 | [本地指南](docs/LOCAL.md) | [部署与迁移](deploy/cloud/README.md) |

首次 GitHub Release 沿用现有项目版本 **v2.0.0**。仓库同时包含独立的 **3.0 公网模块**，其 API 和容器版本沿用 3.0.0；Windows ZIP 面向本地版，公网部署可克隆源码或下载同一 Release 的 `StockAI-3.0.0-Public.zip`。两种模式不会自动迁移或混用数据。

## 快速开始：Windows 发行包

需要标准版 **Python 3.11 / 3.12 / 3.13**，安装时包含 Python Launcher（`py`）。请勿使用 free-threaded Python。桌面模式使用 PySide6 / Qt WebEngine。

1. 在 [Releases](https://github.com/EdgarLi-Xun/StockAI/releases/latest) 下载 **`StockAI-2.0.0-Windows.zip`**，解压到可写目录。
2. 双击 **`Install-StockAI.cmd`**，等待依赖安装完成。首次安装需要联网，依赖安装在项目 `.venv/` 内。
3. 双击 **`Start-StockAI.cmd`** 打开桌面窗口，或双击 **`Start-Web.cmd`** 打开浏览器。
4. 在“系统设置”核对费用与模型配置，再点击“同步数据”。

发行 ZIP 已包含构建后的本地页面，**不需要安装 Node.js**。GitHub 自动生成的 “Source code” 压缩包不包含构建产物，需要按下节安装。

只运行浏览器版、避免安装桌面依赖：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1 -WebOnly
.\Start-Web.cmd
```

校验下载文件：

```powershell
Get-FileHash .\StockAI-2.0.0-Windows.zip -Algorithm SHA256
# 与 Release 中的 StockAI-2.0.0-Windows.zip.sha256 比较。
```

关闭桌面窗口后，共享后台服务会继续运行。停止服务：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\stop.ps1
```

## 从源码运行与开发

需要 Git、上述 Python 版本，以及 **Node.js 22.12+**；本次发布在 Python 3.13 / Node.js 24 上验证。以下命令在 Windows PowerShell 中执行：

```powershell
git clone https://github.com/EdgarLi-Xun/StockAI.git
cd StockAI
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
.\Start-StockAI.cmd
```

源码不提交 `frontend/dist/`，安装脚本会执行 `npm ci` 并构建页面。只需 Web 版时，为安装命令添加 `-WebOnly`。

前后端分别开发：

```powershell
# 终端 1：后端，默认 127.0.0.1:8765
.\.venv\Scripts\python.exe run.py --serve

# 终端 2：前端，127.0.0.1:5173，/api 代理到 8765
npm --prefix frontend run dev
```

### 本地模型与首次研究

在本机运行 Ollama，使设置中的服务地址和模型名与实际安装一致。默认地址为 `http://127.0.0.1:11434`，默认模型名为 `qwen3-14b-16k:latest`；该名称需要本机已有对应模型，也可以在设置中改为自己安装的模型。

模型离线时仍可进行行情、风险与预算计算，并生成规则报告。价格、交易资格、预算、数量和来源由程序计算；模型只生成经过校验的定性解释。

首次同步后，在证券详情核查官方上市、停复牌、公告与交易规则证据，并在系统设置确认费率。**抓到行情不代表证券已通过风险核验**，证据未知、过期或费用后超预算的证券不会进入候选。具体操作与回测数据格式见 [本地指南](docs/LOCAL.md) 和 [历史数据导入](docs/HISTORICAL-DATA.md)。

## 部署公网多用户版

首发部署目标为 **Linux x86_64、单机、单个 Uvicorn worker、20 个账号、两个报告执行槽**。需要 Docker Compose、可用域名、SMTP 服务和由运维保管的 age 备份身份；从源码构建前端还需 Node.js 24。

Release 中的 **`StockAI-3.0.0-Public.zip`** 包含公网源码、部署配置及预构建的公网页面，使用该包可以跳过下方 `npm ci` / `npm run build:public`，从 `deploy/cloud` 的环境配置开始。下载后用同名 `.zip.sha256` 核对完整性。

从源码根目录开始：

```sh
cd frontend
npm ci
npm run build:public
cd ../deploy/cloud
cp .env.example .env
# 编辑 .env：域名、固定 HTTPS 地址、SMTP 和 age 接收公钥。

docker compose build app
docker compose run --rm -it app python -m backend.cloud.cli init --email admin@example.com
docker compose up -d
```

管理员密码由终端交互输入。其他用户通过单次邀请码注册并验证邮箱。Caddy 对外提供 80/443，应用端口、SQLite 和 Ollama 保持内部访问；正式环境不启用 `STOCKAI_CLOUD_DEV=1`。

用户可保存多套个人模型配置并选择默认项；API Key 加密保存，资金持仓汇总和逐笔模拟成交授权默认关闭。连接测试与实际 AI 任务可能消耗模型服务额度。新账号的自动计划和自动 AI 默认关闭。

![公网版个人模型配置，来自隔离开发预览](docs/screenshots/public-models.png)

部署前请阅读完整的 [公网部署、迁移与恢复指南](deploy/cloud/README.md)，尤其是研究数据、注销权威记录、凭据主密钥三个独立目录的保管方式，以及 [公网验证范围](docs/验收记录-公网多用户与模型接入.md)。本次发布不等于已在你的域名或服务器上线。

本机隔离预览（仅开发用途）：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-cloud.txt
npm --prefix frontend run build:public
.\.venv\Scripts\python.exe scripts\preview-cloud.py
```

打开 `http://127.0.0.1:8766`；自动生成的预览登录信息保存在 `.tmp/cloud-preview/preview-login.txt`，预览不会读取或迁移本地 `data/`。公网前端在开发模式下如需热更新，应自行将 Vite 的 API 代理改为 8766；默认开发命令连接本地版。

## 测试与打包

在完成源码安装后执行。开发依赖包含本地与公网模块的测试所需依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest
npm --prefix frontend run build
npm --prefix frontend run build:public
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\package.ps1 -SkipBuild
.\.venv\Scripts\python.exe scripts\package-cloud.py
```

两种发行包与各自 SHA256 文件输出到 `dist/`。Windows ZIP 包含本地源码、预构建的本地页面、安装/启动脚本和文档；Public ZIP 包含公网源码、部署配置、预构建的公网页面及逐文件哈希清单。发行包不包含本机数据库、报告归档、凭据、虚拟环境或依赖缓存。

真实 age 加密往返测试需要额外提供 age / age-keygen；缺少工具时会明确跳过对应测试。测试使用隔离数据，不需要真实模型 Key，也不会以自动化测试结果冒充真实 SMTP、域名 TLS 或跨机器安装验收。验证记录见 [本地版验收](docs/ACCEPTANCE.md)、[公网版验收](docs/验收记录-公网多用户与模型接入.md) 和 [首发验证](docs/RELEASE-2.0.0.md)。

开发流程另在隔离 Linux 容器中验证了 46 项公网专项，以及 20 用户 / 两个执行槽的受控任务处理，详见 [Linux 补充验证](docs/CLOUD-LINUX-VALIDATION.md)。

## 数据与使用边界

- 行情主要通过 AKShare 接入免费数据源；接口可能限流、延迟、停用或无法覆盖部分代码。页面展示数据来源、采集时间与缺口，项目不保证实时行情。
- 上市、公告、交易规则与税费需要按具体产品核验。默认费率属于模拟假设，ETF 与股票费用分开处理。
- 免费历史日线通常缺少逐日风险状态、涨跌停和公司行动证据。严格回测在证据不足时拒绝验证；非严格模式保留“未验证实验”标识。
- 固定自选池有幸存者偏差；历史回测和 AI 解释不代表未来收益，不构成投资建议。
- 本地服务默认只监听回环地址。局域网访问需显式开启并配置密码与允许网段，参阅 [LAN / 反向代理](docs/LAN.md)。

## 项目结构与文档

```text
backend/          FastAPI、本地采集/研究/模拟交易/回测
  cloud/          公网身份、个人空间、模型配置、队列与加密备份
frontend/         Vue 3 + TypeScript + Vite + ECharts
desktop/          PySide6 / Qt WebEngine 桌面壳
config/           提示模板与示例配置
deploy/cloud/     Docker Compose、Caddy 与公网运维说明
scripts/          安装、启动管理、备份、预览与发行打包
tests/            本地与公网隔离回归测试
docs/             使用指南、需求、架构决策与验收记录
```

| 文档 | 内容 |
| --- | --- |
| [本地版完整指南](docs/LOCAL.md) | 研究流程、模拟交易、回测、调度、备份、升级与卸载 |
| [公网部署指南](deploy/cloud/README.md) | 初始化、迁移、模型配置、备份恢复与注销 |
| [历史数据格式](docs/HISTORICAL-DATA.md) | 导入字段、规则与证据格式 |
| [局域网与代理](docs/LAN.md) | LAN 设置和内网 HTTPS 代理示例 |
| [公网产品需求](docs/PRD-公网多用户与模型接入.md) | 账号与个人模型接入范围 |
| [架构决策](docs/adr/) | 模型所有权、多用户边界、管理权限和恢复策略 |
| [首次发布记录](CHANGELOG.md) | v2.0.0 功能范围与版本说明 |

遇到问题时，请附上运行模式、操作系统、Python / Node.js 版本、复现步骤和脱敏日志，到 [Issues](https://github.com/EdgarLi-Xun/StockAI/issues) 反馈。请勿上传数据库、API Key、邮件密码或备份私钥。
