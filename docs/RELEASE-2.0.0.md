# v2.0.0 首发验证记录

日期：2026-11-01，Asia/Shanghai。该版本为仓库首次 GitHub Release，保留本地版 2.0.0 与独立公网模块 3.0.0 的现有版本标识。

## 验证环境与结果

| 项目 | 结果 |
| --- | --- |
| 环境 | Windows；Python 3.13.7、Node.js 24.18.0、npm 11.16.0 |
| 全量后端回归 | **130 passed**，237.51 秒，无跳过项 |
| 本地前端 | `npm --prefix frontend run build`，Vue / TypeScript 检查与 Vite 生产构建通过 |
| 公网前端 | `npm --prefix frontend run build:public`，Vue / TypeScript 检查与 Vite 生产构建通过 |
| age 加密测试 | 本机具有 age / age-keygen，真实加解密往返与恢复相关测试执行通过 |
| Windows ZIP | ZIP 完整性、必要入口与静态资源、运行数据排除、SHA256 一致性检查通过 |
| 解压运行 | 从发行 ZIP 创建空数据库，健康 API、首页、图标、会话和 43 项初始观察池检查通过 |
| 公网 ZIP | 全部构建资源、逐文件哈希清单与 SHA256 检查通过；解压后健康 API、首页、CSRF 入口和未登录访问边界检查通过 |
| 发布内容 | 文档本地链接与常见凭据特征扫描通过，未发现待提交的个人运行数据或真实密钥 |

后端验证命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -x -o faulthandler_timeout=40 --basetemp=.tmp/pytest-release-20261101-final
```

使用项目内隔离的临时目录，并在正常 Windows 执行环境运行；受限沙箱曾出现临时目录权限与本地 API 线程停滞，未将中断的沙箱运行计为通过。

测试出现一项 Starlette TestClient 关于 httpx 迁移的弃用提示，不影响测试结果。开发依赖已包含公网测试所需的 cryptography 与 argon2-cffi。生产依赖锁文件和本地运行环境的 SQLite 版本不同：本机 SQLite 3.50.4 仅用于显式开发模式，正式公网启动仍强制校验修复版本，部署镜像按现有配置构建 SQLite 3.54.1。

最终回归包含开发期间新增的模型凭据回显防护测试：模型响应若包含明文 Key 会被拒绝，返回的请求标识也不能归档该 Key。两套前端在确认对话框更新后重新完成生产构建。

## 发行范围

- Git 仓库包含本地版、公网模块、前端源码、部署配置、测试和文档。
- Windows ZIP 包含本地版预构建页面、源码及安装启动脚本；不打包个人数据库、报告归档、真实环境配置、API Key、虚拟环境、Node.js 依赖或公网前端构建。
- Public ZIP 包含公网源码、预构建页面、部署配置和逐文件哈希清单；修复打包扩展名白名单漏收 JavaScript 文件的问题，并检查所有公网构建输出均已收录。
- 打包脚本检查 ZIP 完整性，并为本次 ZIP 生成同名 `.zip.sha256`，避免复用旧校验和。
- GitHub 自动生成的 Source code 包来自版本标签，需要自行安装依赖并构建页面。

## 验证边界

此次未重新执行其他 Windows 设备的全新安装、实际局域网/TLS、真实 SMTP 和付费模型服务验证，也未将 GitHub Release 等同于公网服务上线。Qt 桌面与本地真实数据的先前验收见 [本地验收记录](ACCEPTANCE.md)，公网自动覆盖及目标环境待验收项见 [公网验收记录](验收记录-公网多用户与模型接入.md)。
