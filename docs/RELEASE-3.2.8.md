# StockAI 3.2.8

2026-11-05，正式版。此次避免桌面升级后仍显示旧页面。

## 桌面页面缓存

桌面、源码桌面和远程窗口启动时删除上一轮 HTTP 缓存，并把页面缓存类型设为不落盘。登录 Cookie 等持久存储仍保留在原来的 profile 目录。首页与 `index.html` 响应带 `Cache-Control: no-store`。

## 运行中的版本号

工作台导航栏的版本改为读取 `/api/health` 返回的后台版本，写请求上的客户端版本头也用同一来源。前端构建不再注入编译期常量。

## 下载

- Windows 10/11 x64：`StockAI-3.2.8-Setup.exe`。
- 独立 Web：`StockAI-3.2.8-Public.zip`；独立同步 Hub：`StockAI-3.2.8-Hub.zip`。
- 每个包附同名 `.sha256`，校验文件统一使用 LF；安装包另附构建元数据，两个 ZIP 含逐文件校验清单。
- 安装目录与研究工作区分开，沿用现有每用户安装方式。已安装的桌面版可在「同步与版本 → 关于与升级」检查本版。

[下载本版](https://github.com/EdgarLi-Xun/StockAI/releases/tag/v3.2.8) · [更新记录](../CHANGELOG.md) · [桌面指南](Windows-EXE使用说明.md)
