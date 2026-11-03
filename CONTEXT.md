# StockAI

StockAI 是股票研究与模拟交易工具。以下术语用于区分使用者、模型服务及个人保存的模型接入选择。

## Language

**用户（User）**：
拥有 StockAI 登录身份，并管理自己模型配置与个人研究空间的使用者。
_Avoid_: 模拟账户、角色

**模拟账户（Simulation Account）**：
用于记录模拟资金、持仓与交易的研究账户，不代表使用者的登录身份。
_Avoid_: 用户、登录账号

**模型服务（Model Service）**：
向 StockAI 提供模型推理的服务，例如 MiniMax 或本地 Ollama。
_Avoid_: 模型、模型配置

**模型配置（Model Profile）**：
用户保存的一套模型接入选择，包含所用模型服务、模型及相应访问凭据。一个用户可以保存多套模型配置。
_Avoid_: 用户、模型服务

**默认模型配置（Default Model Profile）**：
用户从自己保存的模型配置中选定的首选配置。
_Avoid_: 全站默认模型、共享模型配置

**个人研究空间（Personal Research Workspace）**：
归属于单个用户的研究内容，包括自选、研究报告、模拟账户与持仓、回测及相关个人设置。
_Avoid_: 全站研究数据、共享账户

**共享市场资料（Shared Market Data）**：
供各用户共同使用的公开行情及证券基础资料，不包含个人研究选择或研究结果。
_Avoid_: 个人研究空间、全部数据

**站点管理员（Site Administrator）**：
承担账号、邀请码、共享市场资料和站点运行管理职责的用户角色。
_Avoid_: 所有个人研究空间的所有者

**规则报告（Rules-based Report）**：
以程序计算的研究事实和规则结果为内容、没有有效 AI 解释的研究报告。
_Avoid_: AI 报告、失败报告

**报告版本（Report Version）**：
同一研究报告在生成或补充 AI 时形成的独立归档结果。
_Avoid_: 覆盖历史报告

**模型数据授权（Model Data Permission）**：
用户授予某套模型配置使用指定个人模拟研究明细的许可，例如模拟账户汇总或逐笔成交。
_Avoid_: 模型 Key、站点权限

## 2026-11-01 研究增强与项目助手

本轮新增位于 backend/cloud/research*.py、alerts.py、experiments.py、assistant.py 和 frontend/src/ResearchWorkbench.vue。完整需求在 docs/PRD-研究工作台与AI助手-完整需求.md，操作/验收见本轮同名使用说明与验收记录。Linux Web 优先，8766 独立开发预览；原8765本地入口不提供这些新接口。研究 schema1，规则/帮助 research-workbench/1，控制目录新增 research-authority.sqlite3 / research-head.json，不随旧业务备份回退。源码未提交/未部署。工作区还有独立的代码注释改动，须保留。

## 2026-11-02 继续开发

当前公网版本 `3.1.0-preview.1`、研究 schema2、回答校验 `assistant-answer/2`；帮助仍为 `research-workbench/1`。历史观察/估值改为按修订追加，保留 schema1 表及原始 payload，复制与版本标记同事务升级。创建请求持久指纹支持稳定重试，提醒保留冻结参考、显式到期和续期，组合按有效段计算；助手核对对象类型、费用差异、行情质量与非建议性解释；缺失/不完整 usage 保持未知。隔离 8766 预览独立升级，原8765服务继续运行。最终批次及可分发证据见 docs/验收记录-研究工作台与AI助手.md；保留所有独立注释改动，不自动提交或部署。

## 2026-11-02 用户补充：桌面为主，附带 Web

用户明确主交付是一个 EXE，双击打开桌面窗口，同时提供共享同一后台/数据的本机 Web。新增 stockai_desktop.py、desktop/application.py、host.py、check.py 与 scripts/package-exe.py，用 PyInstaller 6.22.3 单文件包含 Python/Qt/网页/后端/age。新桌面复用完整个人研究 API，CloudConfig.desktop_mode 仅能用显式本机回环配置，首次同源+CSRF 创建本机已验证管理员，不需要公网邀请码/邮件。独立实例 Cookie 防止不同本机服务互相覆盖会话；新桌面工作区位于 LocalAppData/StockAI/workspace，数据独立于单文件解包目录。关闭窗口驻留托盘，托盘/菜单退出停止本机服务；已有8765被占用时顺延，绝不停止原服务。采集子进程使用白名单 --module 分派，修复冻结程序不能按 python -m 启动的问题。完整开发与真实 EXE 证据见 docs/Windows-EXE验收记录.md。原 Linux 部署保持可选。

## 2026-11-03 任务中心与全局助手

3.2.0-preview.3：PublicApp 持续挂载 AssistantDock，内部只保留一份 view=assistant 的 ResearchWorkbench；其他增强页单独缓存。切换容器不得重新发送或销毁会话。任务中心顶部入口覆盖管理/增强页，jobs 列表含所属用户的结果路由与安全重试策略，管理员可见共享采集。采集结果保留原字段并带 _request 参数，partial/unavailable 必须映射真实状态。帮助库 research-workbench/2。API privateEpoch 防止旧身份的迟到成功/401 串入新身份。

布局陷阱：ResearchWorkbench 的 research-main 默认 margin:auto；嵌入限高助手面板必须清除该 margin，并使用纵向 flex 和 min-height:0，否则输入区会在容器外被裁切。360px 下固定面板宽度用包含块百分比，不能用含滚动条的100vw。验收 HTTP 替身、图像及最终测试汇总见 docs/修复记录-任务中心与全局助手.md。Windows sandbox 的 loopback 连接被限制，FastAPI/真实 HTTP 验证须使用已授权的本机回环执行权限，不能把挂起误判成业务死锁。未提交、未部署；保留已有独立修改。

## 2026-11-03 GitHub 预览发布

本轮用户明确要求 commit、push 和发布新版本，覆盖前述仅保留工作区改动的历史状态；发布版本沿用 `3.2.0-preview.3`，标记为 GitHub 预览版。主交付为单文件 Windows EXE，附同版 Public / Hub ZIP、SHA256 和构建元数据。发布说明与最终摘要见 docs/RELEASE-3.2.0-preview.3.md 及 docs/evidence/release-3.2.0-preview.3/artifacts.json。

最终验证为 381 项后端功能回归、1 项 20 用户/40 万消息规模测试、8 项前端异步/身份隔离测试；两套页面构建、搬移后的真实 EXE 与解压 ZIP 检查通过。规模夹具仅修复 Windows 缺少可选 resource 内存指标的记录，未修改业务和延迟断言。发行前修复了旧响应覆盖、删除恢复意图绑定、同用户密钥轮换、更新候选排序、持锁远端协调和 SMTP 明确拒绝后的受控重试。

版本来源是 backend/cloud/__init__.py，Vite 与新发行包从这里读取；pyproject.toml 和 frontend/package.json 的 2.0.0 是原源码本地入口的保留元信息，不应据此把当前研究版降回旧版本。最终测试后以逐文件摘要检查运行输入不再变化；运行数据和签名私钥不得进入 Git 或发行包。真实 NAS、跨机、Linux/Docker、生产签名源、真实模型与 SMTP 的待验范围继续保留。
