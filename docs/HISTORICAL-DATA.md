# 历史行情导入

证券详情的“导入历史 JSON”接受数组。每条记录必须包含带时区时间戳、正数且相互一致的 OHLC、非负成交量、复权口径、真实来源及 URL。全批次事务写入，任何错误回滚整个批次。

以下只是字段结构说明。请替换为实际数据，不要把示例作为真实行情导入。

```json
[
  {
    "ts": "2025-01-02T15:00:00+08:00",
    "open": 5.00,
    "high": 5.10,
    "low": 4.90,
    "close": 5.05,
    "volume": 1000000,
    "amount": 5000000,
    "adjustment": "none",
    "source": "替换为实际行情提供方",
    "source_url": "https://www.sse.com.cn/替换为真实证据路径",
    "fetched_at": "2025-01-02T16:00:00+08:00",
    "quality": "valid",
    "metadata": {
      "currency": "CNY",
      "volume_unit": "share",
      "historical_status_verified": true,
      "status_source_url": "https://www.sse.com.cn/替换为当日状态证据",
      "trading_status": "active",
      "risk_flag": "clear",
      "upper_limit": 5.50,
      "lower_limit": 4.50,
      "corporate_actions_verified": true,
      "corporate_actions_source_url": "https://www.sse.com.cn/替换为公司行动证据",
      "split_ratio": 1,
      "cash_dividend": 0
    }
  }
]
```

`historical_status_verified` 与 `corporate_actions_verified` 只能在实际逐日核对证据后设置。历史风险状态包括 ST、停复牌和特殊涨跌幅安排；普通免费日线默认不具备这些证据。停牌时使用 `trading_status: suspended`，风险警示设置其他 `risk_flag`，引擎不会撮合。

公司行动以生效日的 `split_ratio`（新股数/旧股数）和每旧股 `cash_dividend` 入账。引擎只在事件生效时调整已有历史指标与持仓，不提前使用未来公司行动；特殊除权参考价可用 `reference_price` 明示。涉及无法处理的碎股会终止实验。

回测严格要求不复权 `adjustment: none` 作为成交价格。前复权、后复权数据不能直接作为买卖成交价。缺少任何严格证据时，可以做明确标记的未验证实验，但不得宣称已完成可靠历史验证。
