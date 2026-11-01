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
