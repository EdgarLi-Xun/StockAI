# StockAI 3.2.9

2026-11-05，正式版。此次修复桌面更新「暂停下载」不生效。

## 暂停下载

点击暂停后关闭当前安装包连接，不再等待正在读取的网络数据块。没有下载线程时，卡住的「正在下载」也会改成已暂停，随后可以继续或取消该计划。

## 下载

- Windows 10/11 x64：`StockAI-3.2.9-Setup.exe`。
- 独立 Web：`StockAI-3.2.9-Public.zip`；独立同步 Hub：`StockAI-3.2.9-Hub.zip`。
- 每个包附同名 `.sha256`，校验文件统一使用 LF；安装包另附构建元数据，两个 ZIP 含逐文件校验清单。
- 安装目录与研究工作区分开，沿用现有每用户安装方式。已安装的桌面版可在「同步与版本 → 关于与升级」检查本版。

[下载本版](https://github.com/EdgarLi-Xun/StockAI/releases/tag/v3.2.9) · [更新记录](../CHANGELOG.md) · [桌面指南](Windows-EXE使用说明.md)
