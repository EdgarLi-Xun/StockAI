# Windows 单文件 EXE 验收记录

执行日期：2026-11-02，Asia/Shanghai。主交付为 `StockAI-3.1.0-preview.1-Windows.exe`，一个文件包含 Python、Qt WebEngine、完整研究后端、已构建 Web 和 age 备份工具。用户补充要求是桌面为主、附属 Web，共用同一后台与数据；不再以独立 Linux 部署包作为此项交付。

## 最终产物

- 文件：`dist/StockAI-3.1.0-preview.1-Windows.exe`
- 大小：286,298,685 字节，约 273.0 MiB。
- SHA-256：`9450d019c2f11712f076084beb85b9fb479d0645ad2b0265aa94b0730f00b53f`
- Python 3.13.7、PySide6/Qt 6.12.0、AKShare 1.20.21、PyInstaller 6.22.3。
- 旁附 `.exe.sha256` 和 `.exe.build.json`；运行只需要 EXE，数据保存到独立的持久工作区。

## 执行证据

| 验证 | 结果 |
| --- | --- |
| 初次创建、回环限制、来源/CSRF、公共部署不开放本机初始化、独立 Cookie、共享用户资料、采集分派、端口占用 | 首批桌面及既有公网回归 55 通过；最后桌面专项 10 通过，包含新增编码回归 |
| 采集、报告、研究工作台与关键业务流程 | 46 通过，0 失败 |
| TypeScript 与完整 Web 静态资源 | `npm run build:public` 通过 |
| 源码 | compileall 及默认 Git 配置下的 diff whitespace 检查通过 |
| 真实单文件搬移测试 | 仅复制 EXE 到空目录，子进程 PATH 只保留 Windows，移除 PYTHONHOME/PYTHONPATH；自检退出码 0 |
| 共享后台 | 两个独立 Cookie 会话登录后得到同一归属，读取就绪度和用量成功，静态资源完整 |
| Qt 桌面 | 实际创建 Qt/WebEngine 窗口并加载本机页面，`desktop_loaded=true`，正常退出码 0 |
| 采集子进程 | 冻结 EXE 白名单分派实际执行，中文 JSON 为 UTF-8，返回预期错误；没有发起行情网络请求 |
| 本机备份 | 内置 age-keygen 实际生成独立备份身份；旧主密钥与权威记录不被重建 |

脱敏证据见 [tests.json](evidence/windows-exe-2026-11-02/tests.json)。批次有重复，不将 55、46、10 相加为独立覆盖数量。原研究功能完整回归和真实服务边界继续见 [研究验收记录](验收记录-研究工作台与AI助手.md)。

最终 EXE 在隔离工作区启动后，浏览器重新打开其本机服务并保留原账号会话，正确显示研究工作台和真实空状态；进程路径核对为搬移后的 `StockAI.exe`。见 [附属 Web 截图](evidence/windows-exe-2026-11-02/companion-web.png)。该截图仅证明 Web 页面，不代替原生菜单的手动检查。

构建中发现并修复两项真实分发故障：第三方工具 PATH 中的同名 ICU DLL 被误收，导致 Qt 无法导入；冻结子进程继承 Windows 本机代码页，导致中文 JSON 解码失败。最终构建隔离 DLL 搜索路径、收集一致的 Qt 配套运行库，并显式设置管道 UTF-8。修复后的产物重新执行上述搬移与桌面检查，不使用源码运行结果代替 EXE 结果。

## 范围及限制

当前机器验证通过，尚未在另一台未安装开发工具的 Windows 机器上独立验收；清空环境路径和搬移检查不冒充第二台机器测试。Qt 原生窗口的自动化操作授权超时，因此托盘点击、系统默认浏览器菜单及完整原生键盘交互未作通过登记；程序自带的窗口创建/加载/退出冒烟已实际完成。本机 Web 的页面检查与原生窗口手动交互是分别记录的证据。

真实模型、SMTP 和行情联网仍需本人配置并联调。安装包不带账号、Key、研究数据库或模型权重，不会自动复制原源码版数据。以上开发验证仅使用项目 `.tmp` 下隔离工作区，原有本地服务及资料未被停止或修改。

打包依据：[PyInstaller 运行时](https://pyinstaller.org/en/stable/runtime-information.html)、[Qt/PyInstaller 部署说明](https://doc.qt.io/qtforpython-6/deployment/deployment-pyinstaller.html)。
