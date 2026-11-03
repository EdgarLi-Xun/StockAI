# StockAI 研究工作台与 AI 助手：验收登记清单

| 项目 | 内容 |
| --- | --- |
| 清单版本 | 1.1，2026-11-02 继续开发后复核 |
| 创建日期 | 2026-11-01（Asia/Shanghai） |
| 对应范围 | 七项研究增强与项目内 AI 助手，包含报告追问 |
| 当前结论 | 65 项受控通过，13 项发布联调阻塞；不代表生产验收通过 |

完整业务口径见 [完整需求](PRD-研究工作台与AI助手-完整需求.md)，实施依赖见 [实施方案](实施方案-公网多用户与模型接入.md)。本清单按三个需求文档的验收表整理；检查点变化时同步更新，不能只改执行结论。

状态限定为未执行、通过、失败、阻塞、不适用。不适用必须记录依据，不能用于跳过已确认的交付范围；受控替身通过不等于真实服务或目标环境通过。证据应使用脱敏日志、运行记录、截图或可重现样本的路径，不填写 Key、邮件令牌、数据库正文或真实密码。

## 执行批次信息

| 记录项 | 内容 |
| --- | --- |
| 执行日期与人员 | 2026-11-01 首批；2026-11-02 继续开发及完整复测，Codex 受控执行 |
| 源码版本、构建与迁移版本 | 基线 898a5b8 + 工作区；公网 3.1.0-preview.1；研究 schema 2；帮助 research-workbench/1，回答校验 assistant-answer/2；两种前端构建通过 |
| 操作系统、Python、浏览器与机器资源 | Windows Python3.13.7 / SQLite3.50.4（开发回滚日志）；WSL2 Docker Python3.13.18 / SQLite3.54.1；IAB 360/1280；资源详见本轮记录 |
| 数据夹具、时钟与统计窗口 | 交易日/价格/费用为确定样本；Asia/Shanghai；20用户完整数量夹具、400000消息、18000历史日线，详见负载 JSON |
| 模型及邮件环境 | 模型协议与回答、SMTP 故障为替身；真实 age 往返；无真实模型/SMTP/生产域名 |
| 自动结果、浏览器记录与外部联调证据 | [本轮验收记录](验收记录-研究工作台与AI助手.md)；外部联调未执行 |
| 已通过 / 失败 / 阻塞 / 不适用 | 65 / 0 / 13 / 0；未执行 0；通过范围限定为受控环境 |

## 1. 研究增强模块

需求来源：[PRD-研究工作台增强](PRD-研究工作台增强.md)。

| 编号 | 验收检查点 | 计划验证 | 状态 | 证据/问题 |
| --- | --- | --- | --- | --- |
| ENH-AC-01 | 注入过期行情、未核验风险及缺失历史证据，面板分别显示原因、资料时间和影响对象，不显示为健康 | 隔离规则验证 + 页面定位 | 通过 | 受控通过：[就绪度、修复与快照](../tests/test_workbench_final.py)；[界面记录](验收记录-研究工作台与AI助手.md#浏览器验证) |
| ENH-AC-02 | 同一证券同时存在三项问题，受影响证券数为一，问题计数与详细列表一致 | 隔离规则验证 + 页面定位 | 通过 | 受控通过：[就绪度、修复与快照](../tests/test_workbench_final.py)；[界面记录](验收记录-研究工作台与AI助手.md#浏览器验证) |
| ENH-AC-03 | 点击处理入口能定位对应页面；普通用户不能借入口执行管理员核验或共享数据导入 | 隔离规则验证 + 页面定位 | 通过 | 受控通过：[就绪度、修复与快照](../tests/test_workbench_final.py)；[界面记录](验收记录-研究工作台与AI助手.md#浏览器验证) |
| ENH-AC-04 | 零候选显示本次实际筛选原因；修复资料后当前面板更新，旧报告的排除原因与快照不变 | 隔离规则验证 + 页面定位 | 通过 | 受控通过：[就绪度、修复与快照](../tests/test_workbench_final.py)；[界面记录](验收记录-研究工作台与AI助手.md#浏览器验证) |
| ENH-AC-05 | 日/月统计包含报告、对话、测试、重试及单独调用的摘要，按用户、配置、用途和 Asia/Shanghai 边界一致汇总 | 可控时钟/并发 + 用量界面 | 通过 | 受控通过：[次数、窗口及并发](../tests/test_workbench.py)；[已知/未知用量](../tests/test_workbench_edges.py) |
| ENH-AC-06 | 剩余一次额度时并发发起请求，最多一次实际发送；切换配置、重启和重复请求不能绕过；发送前失败可释放，发送不明保留计数 | 可控时钟/并发 + 用量界面 | 通过 | 受控通过：[次数、窗口及并发](../tests/test_workbench.py)；[已知/未知用量](../tests/test_workbench_edges.py) |
| ENH-AC-07 | 达到日或月上限、设为零、降低上限或队列跨日时，发送前按当前窗口与设置检查；报告归档规则版本，对话及测试显示超限，原档不覆盖 | 可控时钟/并发 + 用量界面 | 通过 | 受控通过：[次数、窗口及并发](../tests/test_workbench.py)；[已知/未知用量](../tests/test_workbench_edges.py) |
| ENH-AC-08 | 供应商不返回用量、采用订阅或调用超时，界面不显示虚构的零用量、剩余余额或确定费用；可估算费用带来源、日期和币种 | 可控时钟/并发 + 用量界面 | 通过 | 受控通过：[次数、窗口及并发](../tests/test_workbench.py)；[已知/未知用量](../tests/test_workbench_edges.py) |
| ENH-AC-09 | 分别触发价格、涨跌幅、风险变化和候选失效规则，通知包含正确对象、条件、数据时间、来源及失效原因 | 事件/时钟 + 邮件替身 | 通过 | 受控通过：[边沿/免打扰](../tests/test_workbench_edges.py)；[邮件替身与恢复](../tests/test_workbench_integrity.py) |
| ENH-AC-10 | 缺失或过期行情不触发价格阈值；重复采集、补跑和重启不重复发送同一事件，新的条件变化仍受冷却约束 | 事件/时钟 + 邮件替身 | 通过 | 受控通过：[边沿/免打扰](../tests/test_workbench_edges.py)；[邮件替身与恢复](../tests/test_workbench_integrity.py) |
| ENH-AC-11 | 免打扰保留记录并在结束后合并有效待发项；规则停用、删除或过期后不再投递未发提醒 | 事件/时钟 + 邮件替身 | 通过 | 受控通过：[边沿/免打扰](../tests/test_workbench_edges.py)；[邮件替身与恢复](../tests/test_workbench_integrity.py) |
| ENH-AC-12 | 两个用户的规则、通知和邮件互不串用；检查提醒不产生额外模型调用；邮件未配置或未开启时不发送 | 事件/时钟 + 邮件替身 | 通过 | 受控通过：[边沿/免打扰](../tests/test_workbench_edges.py)；[邮件替身与恢复](../tests/test_workbench_integrity.py) |
| ENH-AC-13 | 修改研究理由后生成新版本，仍可查看最初判断、时间及关联报告，不将结果写回原始判断 | 交易日样本 + 版本/权限验证 | 通过 | 受控通过：[交易日、版本](../tests/test_workbench.py)；[5% 与含费净收益40](../tests/test_workbench_integrity.py) |
| ENH-AC-14 | 已知交易日样本中正确定位第 1/5/20 个观察日；未到期、缺价、停牌和公司行动口径不明各有明确状态 | 交易日样本 + 版本/权限验证 | 通过 | 受控通过：[交易日、版本](../tests/test_workbench.py)；[5% 与含费净收益40](../tests/test_workbench_integrity.py) |
| ENH-AC-15 | 没有模拟成交时只显示观察结果；关联成交时按该账户实际成交价格和费用计算，两类收益不混写 | 交易日样本 + 版本/权限验证 | 通过 | 受控通过：[交易日、版本](../tests/test_workbench.py)；[5% 与含费净收益40](../tests/test_workbench_integrity.py) |
| ENH-AC-16 | 其他用户不可读写日记；默认模型输入无个人备注，删除及账号注销按个人数据规则处理 | 交易日样本 + 版本/权限验证 | 通过 | 受控通过：[交易日、版本](../tests/test_workbench.py)；[5% 与含费净收益40](../tests/test_workbench_integrity.py) |
| ENH-AC-17 | 用已知现金、持仓和未成交委托样本核对总仓位与现金占比，预留资金和待成交数量不被重复计入 | 确定数值样本 + 账户隔离 | 通过 | 受控通过：[仓位、行业分母与缺价](../tests/test_workbench_integrity.py)；[回撤](../tests/test_workbench.py) |
| ENH-AC-18 | 缺失行业和无成分证据的 ETF 独立标注，不虚构行业分类；有效分类可追溯来源及时间 | 确定数值样本 + 账户隔离 | 通过 | 受控通过：[仓位、行业分母与缺价](../tests/test_workbench_integrity.py)；[回撤](../tests/test_workbench.py) |
| ENH-AC-19 | 已知权益序列可复算当前及最大回撤；零权益、缺史和估值缺口不显示虚假的完整风险指标 | 确定数值样本 + 账户隔离 | 通过 | 受控通过：[仓位、行业分母与缺价](../tests/test_workbench_integrity.py)；[回撤](../tests/test_workbench.py) |
| ENH-AC-20 | 切换本人账户后指标与对应账户一致；其他用户不能读取该组合，现有单只预算和成交校验继续有效 | 确定数值样本 + 账户隔离 | 通过 | 受控通过：[仓位、行业分母与缺价](../tests/test_workbench_integrity.py)；[回撤](../tests/test_workbench.py) |
| ENH-AC-21 | 策略、买入持有及指数基准展示清楚的共同区间和口径；缺失基准不伪造，相异口径不直接宣称胜出 | 历史回测样本 + 快照重演 | 通过 | 受控通过：[实验、指数快照及复算](../tests/test_workbench_integrity.py)；[比较/取消](../tests/test_workbench.py) |
| ENH-AC-22 | 多个实验可并排查看参数、版本、哈希、样本内外、费用和结果，保存新实验不覆盖旧实验 | 历史回测样本 + 快照重演 | 通过 | 受控通过：[实验、指数快照及复算](../tests/test_workbench_integrity.py)；[比较/取消](../tests/test_workbench.py) |
| ENH-AC-23 | 独立成功、失败和中断尝试均保留状态；同一请求重试提交不重复计数，变更参数或样本划分形成可追踪记录 | 历史回测样本 + 快照重演 | 通过 | 受控通过：[实验、指数快照及复算](../tests/test_workbench_integrity.py)；[比较/取消](../tests/test_workbench.py) |
| ENH-AC-24 | 基准与策略时序不使用未来数据；快照重演仍一致，证据不足仍标为未验证，不能将筛选后的结果伪装成未看过的样本外表现 | 历史回测样本 + 快照重演 | 通过 | 受控通过：[实验、指数快照及复算](../tests/test_workbench_integrity.py)；[比较/取消](../tests/test_workbench.py) |
| ENH-AC-25 | 从报告页追问排除原因或预算，使用指定版本事实和可核对的计算，引用可打开；同时通过 CHAT-02 与 CHAT-16 | 受控模型 + 报告交互/引用 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| ENH-AC-26 | 比较两个报告时两侧来源、数据时间和差异清楚；同事实换模型不写成市场变化，同时通过 CHAT-15 | 受控模型 + 报告交互/引用 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| ENH-AC-27 | 比较对象缺失、越权或证据不足时不补造，撤销授权后历史与摘要不继续外发，同时通过 CHAT-05、CHAT-07 与 CHAT-17 | 受控模型 + 报告交互/引用 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| ENH-AC-28 | 用量上限、停用和队列并发限制同样作用于报告追问，超限不换 Key 或隐式重发，同时通过 CHAT-11、CHAT-12 与 CHAT-18 | 受控模型 + 报告交互/引用 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |

## 2. 项目内 AI 对话

需求来源：[PRD-项目内AI对话](PRD-项目内AI对话.md)。

| 编号 | 验收检查点 | 计划验证 | 状态 | 证据/问题 |
| --- | --- | --- | --- | --- |
| CHAT-01 | 询问如何配置模型或生成早报；根据对应版本的项目说明回答，引用可打开的帮助资料 | 受控模型/资料 + 真实模型引用联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-02 | 从本人报告页面询问为何排除某证券；使用选定报告版本的事实、排除原因及数据时间，不改写原档 | 受控模型/资料 + 真实模型引用联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-03 | 询问旅游攻略或其他项目代码，或用“忽略限制”绕开范围；仅说明可回答范围，不生成无关答案；连续多轮和角色切换同样受限 | 固定范围/攻击样本 + 真实模型联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-04 | 项目问题缺少对象或所需资料；引导选择对象或明确资料缺口，不编造行情、研究记录或来源 | 受控模型/资料 + 真实模型引用联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-05 | 用户 A 构造用户 B 的会话、消息、报告或引用 ID；无法读取、追问、删除或引用 B 的数据；后台也无他人对话正文入口 | 权限/删除验证 + 页面检查 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| CHAT-06 | 未授权成交明细却询问逐笔成交；模型请求中不出现该明细，界面提示所需授权 | 权限/删除验证 + 页面检查 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| CHAT-07 | 已讨论成交后撤销授权，或切换到未授权配置再追问；新请求、历史和摘要均不再发送受限明细；历史引用不绕过当前权限 | 权限/删除验证 + 页面检查 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| CHAT-08 | 问题、引用文档或历史消息包含越权指令或索要凭据的内容；不扩大检索范围，不泄露秘密，不执行写入动作 | 固定范围/攻击样本 + 真实模型联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-09 | 报告已过期，或知识库同时包含规划和已实现功能；标明数据时间，区分计划与现状，不声称是实时数据或已上线能力 | 固定范围/攻击样本 + 真实模型联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-10 | 重复发送、断线刷新、连续点击重新连接；同一请求只执行一次；状态可恢复，不产生隐式重复调用 | 并发/故障注入 + 重连交互 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| CHAT-11 | 报告和对话同时排队，或中途更换默认模型、删除 Key、停用账号；共享并发限制；已提交配置固定，撤销与停用阻止未发送调用 | 并发/故障注入 + 重连交互 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| CHAT-12 | 停止生成、供应商超时、Key 无效或限流；状态准确、错误可见，遵守重试边界；不确定发送不自动重发 | 并发/故障注入 + 重连交互 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| CHAT-13 | 模型编造引用、数值与事实快照不符，或回答中混入无关内容；不作为有效完整回答发布，提示校验失败或资料不足 | 固定范围/攻击样本 + 真实模型联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-14 | 长对话、删除会话和账号注销；历史裁剪有提示，删除后在线上下文不可再用；注销后在途结果不得写回 | 权限/删除验证 + 页面检查 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| CHAT-15 | 选择两个报告版本比较，或对同事实快照切换模型解释；引用两侧资料、数据时间和程序计算差异；区分事实变化与措辞变化，不改写报告 | 受控模型/资料 + 真实模型引用联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-16 | 追问为何排除证券或费用为何超预算；对照指定版本的排除记录及程序计算解释，不以模型生成数字替代已有结果 | 受控模型/资料 + 真实模型引用联调 | 阻塞 | 受控部分已通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py)；真实模型引用/范围/质量联调待提供凭据 |
| CHAT-17 | 比较对象不存在、无权访问或缺少事实证据；提示选择有效资料或说明缺口，不拼接他人资料、不以最新行情填补旧事实 | 权限/删除验证 + 页面检查 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| CHAT-18 | 剩余一次日/月额度时对话与报告并发，或对话需额外摘要调用；所有模型请求分别预留并计数，最多一次实际发送；超限可见，不隐式更换配置或重发 | 并发/故障注入 + 重连交互 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |

## 3. 补充整体验收

需求来源：[PRD-研究工作台与AI助手-完整需求](PRD-研究工作台与AI助手-完整需求.md)。

| 编号 | 验收检查点 | 计划验证 | 状态 | 证据/问题 |
| --- | --- | --- | --- | --- |
| COMP-AC-01 | 真实无问题、读取失败、状态未知和不适用四类结果；面板状态可区分，失败或未知不成为健康 | 资料状态/快照 + 页面验证 | 通过 | 受控通过：[就绪度、修复与快照](../tests/test_workbench_final.py)；[界面记录](验收记录-研究工作台与AI助手.md#浏览器验证) |
| COMP-AC-02 | 仅缺历史证据但最新行情有效；阻断严格回测，不错误隐藏行情或阻断带缺口规则报告 | 资料状态/快照 + 页面验证 | 通过 | 受控通过：[就绪度、修复与快照](../tests/test_workbench_final.py)；[界面记录](验收记录-研究工作台与AI助手.md#浏览器验证) |
| COMP-AC-03 | 当前资料修复后再次查看旧报告及其追问；当前面板更新，旧快照和旧排除原因保持原样 | 资料状态/快照 + 页面验证 | 通过 | 受控通过：[就绪度、修复与快照](../tests/test_workbench_final.py)；[界面记录](验收记录-研究工作台与AI助手.md#浏览器验证) |
| COMP-AC-04 | 同一用户只剩一次日/月额度，跨入口竞争；只允许一次发送，剩余、预留与日志一致；更换配置及 Ollama 不绕过 | 可控时钟/崩溃/并发 + 计数核对 | 通过 | 受控通过：[次数、窗口及并发](../tests/test_workbench.py)；[已知/未知用量](../tests/test_workbench_edges.py) |
| COMP-AC-05 | 本地午夜、月末、客户端改时钟和发送跨窗口；按服务端本地窗口及持久发送意图归属，既不多扣也不多返还 | 可控时钟/崩溃/并发 + 计数核对 | 通过 | 受控通过：[次数、窗口及并发](../tests/test_workbench.py)；[已知/未知用量](../tests/test_workbench_edges.py) |
| COMP-AC-06 | 连接前失败、已发失败、崩溃不明、降低上限；仅可证明未发送者释放，其他占用保留；上限调整不改历史已用 | 可控时钟/崩溃/并发 + 计数核对 | 通过 | 受控通过：[次数、窗口及并发](../tests/test_workbench.py)；[已知/未知用量](../tests/test_workbench_edges.py) |
| COMP-AC-07 | 两次已知 token 加一次未知 token，订阅且无计费单价；展示已知小计与未知条数，不声称完整总量或确定费用 | 可控时钟/崩溃/并发 + 计数核对 | 通过 | 受控通过：[次数、窗口及并发](../tests/test_workbench.py)；[已知/未知用量](../tests/test_workbench_edges.py) |
| COMP-AC-08 | 首次即满足、持续满足、未知采样后恢复、假后再真及无新报价的证据过期；依规则产生一次或新的跨越事件，未知不重置；时间条件按 60 秒周期检查，资料入库后的本地评估满足目标 | 事件时钟/投递竞争 + SMTP 替身 | 通过 | 受控通过：[边沿/免打扰](../tests/test_workbench_edges.py)；[邮件替身与恢复](../tests/test_workbench_integrity.py) |
| COMP-AC-09 | 冷却期内新触发及跨午夜免打扰；冷却抑制与免打扰待发分开，恢复时只合并仍有效的提醒 | 事件时钟/投递竞争 + SMTP 替身 | 通过 | 受控通过：[边沿/免打扰](../tests/test_workbench_edges.py)；[邮件替身与恢复](../tests/test_workbench_integrity.py) |
| COMP-AC-10 | 编辑/停用/到期/删除与投递竞争、移除自选、SMTP 确认不明；旧版未发项取消，移除自选提示并默认停用关联提醒；站内无重复，邮件不明不自动重发 | 事件时钟/投递竞争 + SMTP 替身 | 通过 | 受控通过：[边沿/免打扰](../tests/test_workbench_edges.py)；[邮件替身与恢复](../tests/test_workbench_integrity.py) |
| COMP-AC-11 | 盘前、开盘后、休市日创建及事后引用旧报告写日记；使用判断之后的明确开盘起点，不把新理由追溯为过去预测 | 交易日/价格/成交确定样本 | 通过 | 受控通过：[交易日、版本](../tests/test_workbench.py)；[5% 与含费净收益40](../tests/test_workbench_integrity.py) |
| COMP-AC-12 | 开盘 10 元、目标收盘 10.50 元及连续节假日样本；观察价格收益 5%，第 1/5/20 日按交易日而非自然日定位 | 交易日/价格/成交确定样本 | 通过 | 受控通过：[交易日、版本](../tests/test_workbench.py)；[5% 与含费净收益40](../tests/test_workbench_integrity.py) |
| COMP-AC-13 | 固定目标日停牌、缺价、日历未知或公司行动口径不明；对应状态明确，不换日期、不伪造或混用复权计算 | 交易日/价格/成交确定样本 | 通过 | 受控通过：[交易日、版本](../tests/test_workbench.py)；[5% 与含费净收益40](../tests/test_workbench_integrity.py) |
| COMP-AC-14 | 100 股按 9.90 买、10.40 卖，双边各费 5 元；模拟净收益 40 元；未成交或部分成交不按完整观察数量记收益 | 交易日/价格/成交确定样本 | 通过 | 受控通过：[交易日、版本](../tests/test_workbench.py)；[5% 与含费净收益40](../tests/test_workbench_integrity.py) |
| COMP-AC-15 | 现金 4,000、持仓 6,000，含预留和待买委托；仓位 60%、现金 40%，可用与预留分开，待买未当持仓 | 现金/行业/权益确定样本 | 通过 | 受控通过：[仓位、行业分母与缺价](../tests/test_workbench_integrity.py)；[回撤](../tests/test_workbench.py) |
| COMP-AC-16 | 已知行业 3,000、未知分类 1,000，持仓总额 6,000；行业持仓占比 50%，权益占比按同一权益计算，未知不被分配为零风险 | 现金/行业/权益确定样本 | 通过 | 受控通过：[仓位、行业分母与缺价](../tests/test_workbench_integrity.py)；[回撤](../tests/test_workbench.py) |
| COMP-AC-17 | 权益 10,000→12,000→10,800，另含缺价/零权益样本；有效序列回撤 10%；无可靠分母或估值时不给完整比例 | 现金/行业/权益确定样本 | 通过 | 受控通过：[仓位、行业分母与缺价](../tests/test_workbench_integrity.py)；[回撤](../tests/test_workbench.py) |
| COMP-AC-18 | 买入持有遇预算、一手、费用、停牌与不可成交开盘；与策略沿用同一规则，未投入现金保留，不能无条件全仓成交 | 历史/实验/报告快照对照 | 通过 | 受控通过：[实验、指数快照及复算](../tests/test_workbench_integrity.py)；[比较/取消](../tests/test_workbench.py) |
| COMP-AC-19 | 指数与策略不同区间、总收益口径和子区间起算；明确共同区间、归一化及差异，不拼出虚假的连续实盘曲线 | 历史/实验/报告快照对照 | 通过 | 受控通过：[实验、指数快照及复算](../tests/test_workbench_integrity.py)；[比较/取消](../tests/test_workbench.py) |
| COMP-AC-20 | 校验失败、排队取消、开始后失败和主动重跑；尝试数仅统计实际开始的独立实验，旧结果及样本划分可追溯 | 历史/实验/报告快照对照 | 通过 | 受控通过：[实验、指数快照及复算](../tests/test_workbench_integrity.py)；[比较/取消](../tests/test_workbench.py) |
| COMP-AC-21 | “上次报告”不明确、同事实换模型、旧报告无原始事实；要求选对象或说明缺口，不用当前数据代写旧差异 | 历史/实验/报告快照对照 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| COMP-AC-22 | 4,000 及 4,001 码点输入、过多事实和长对话；前后端限制一致，保留当前问题和关键事实，不够时请求缩小上下文 | 输入/断线/范围/引用专项 + 页面 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| COMP-AC-23 | 同会话连续发送、同请求重连、同标识换内容；顺序明确，原请求只执行一次，不同内容报冲突而非复用错答 | 输入/断线/范围/引用专项 + 页面 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| COMP-AC-24 | 断线恢复、停止、重新生成、授权撤销及历史报告跨日追问；恢复不推理，停止状态真实；新生成重新计数，历史按新权限投影；追问不误作历史补跑拒绝 | 输入/断线/范围/引用专项 + 页面 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| COMP-AC-25 | 项目相关、无关、模糊、伪装相关、多轮绕过的固定验收集；逐类符合范围规则，无关不成功作答，模糊补对象，引用资料仍受权限限制 | 输入/断线/范围/引用专项 + 页面 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| COMP-AC-26 | 旧文档写有计划功能、伪造引用、无关输出及可执行文本；按运行版本检索，内容经校验后展示，不执行脚本或发布伪造事实 | 输入/断线/范围/引用专项 + 页面 | 通过 | 受控通过：[范围/顺序/停止](../tests/test_workbench.py)；[授权/历史/删除](../tests/test_workbench_integrity.py)；[固定集](../tests/test_workbench_edges.py) |
| COMP-AC-27 | 普通用户、管理员、未验证、停用用户及构造他人 ID；页面与服务端权限一致，新增对象与引用无跨用户读取或修改入口 | 跨用户/删除/迁移/恢复专项 | 通过 | 受控通过：[归属与真实 age 恢复](../tests/test_workbench_integrity.py)；[原多用户回归](../tests/test_cloud.py) |
| COMP-AC-28 | 并发编辑同一日记/规则，删除与在途任务写回竞争；旧版本保存报冲突，删除结果不被复活，不误删独立报告或账本 | 跨用户/删除/迁移/恢复专项 | 通过 | 受控通过：[归属与真实 age 恢复](../tests/test_workbench_integrity.py)；[原多用户回归](../tests/test_cloud.py) |
| COMP-AC-29 | 恢复包含已删除对话/规则和旧用量的备份；应用最新删除与用量状态，旧规则不投递；无法核对时阻止相应恢复或暂停 AI | 跨用户/删除/迁移/恢复专项 | 通过 | 受控通过：[归属与真实 age 恢复](../tests/test_workbench_integrity.py)；[原多用户回归](../tests/test_cloud.py) |
| COMP-AC-30 | 旧版本迁移、失败回滚及不含新对象的账号；数量、归属和关联正确；未知字段不补假数据，失败可回到原版本 | 跨用户/删除/迁移/恢复专项 | 通过 | 受控通过：[schema2 原样复制、失败事务回滚与重复启动](../tests/test_research_continuation.py)；[归属与真实 age 恢复](../tests/test_workbench_integrity.py)；[完整复测摘要](evidence/research-2026-11-02/tests.json)。目标主机整站回滚仍属发布门禁 |
| COMP-AC-31 | 360 像素手机、桌面、键盘及各种空/加载/错误状态；主要操作可达，长表格可用，确认、冲突和状态反馈清晰 | 手机/桌面/键盘浏览器验证 | 通过 | 受控通过：[360/1280 与键盘、草稿记录](验收记录-研究工作台与AI助手.md#浏览器验证) |
| COMP-AC-32 | 第 10 节夹具、20 用户混合队列和重启注入；满足并发、受理、读取及恢复目标，记录测量证据，不用外部未验能力冒充通过 | 目标环境受控负载 + 重启恢复 | 阻塞 | 受控部分已通过：[20 登录会话 HTTP/队列测量](验收记录-研究工作台与AI助手.md#linux-负载测量)；正式主机性能/重启验收未执行 |

## 发布结论

本次已完成源码开发及上述受控验证。发布门禁尚未关闭：真实模型、SMTP、生产域名/主机、目标环境整站回滚仍待联调。受控通过不代替这些条件，也不把真实服务未验证写成通过。完整证据与限制见 [本轮验收记录](验收记录-研究工作台与AI助手.md)。
