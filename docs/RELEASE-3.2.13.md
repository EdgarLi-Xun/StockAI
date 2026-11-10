# StockAI 3.2.13

2026-11-10，正式版。此次修复软件内升级被安装包拒绝的问题。

## 软件内升级

「下载并准备安装」会先把新版本静默装到工作区里的升级临时目录做检查，通过后再换到正式程序目录。安装包不再把这个临时目录当成研究资料目录拒绝。

在安装向导里把程序选进工作区时，仍会提示并停止。正式程序目录仍是 `%LOCALAPPDATA%\Programs\StockAI`，研究资料仍在 `%LOCALAPPDATA%\StockAI\workspace`。

## 下载

- Windows 10/11 x64：`StockAI-3.2.13-Setup.exe`。
- 独立 Web：`StockAI-3.2.13-Public.zip`；独立同步 Hub：`StockAI-3.2.13-Hub.zip`。
- 每个包附同名 `.sha256`，校验文件统一使用 LF；安装包另附构建元数据，两个 ZIP 含逐文件校验清单。
- 安装目录与研究工作区分开，沿用现有每用户安装方式。已安装的桌面版可在「同步与版本 → 关于与升级」检查本版。

[下载本版](https://github.com/EdgarLi-Xun/StockAI/releases/tag/v3.2.13) · [更新记录](../CHANGELOG.md) · [桌面指南](Windows-EXE使用说明.md)
