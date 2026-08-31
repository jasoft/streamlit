"""fdata watch 示例策略: 突破线 + 大幅波动告警.

用法:
  .venv/bin/python scripts/fdata.py watch rb IF2612 sz159915 \
      --interval 1 --strategy scripts/example_tick_strategy.py --verbose

约定: 定义 on_tick(quotes[, feed_errors]) -> list[dict] | None
quotes 是与 `fdata.py quote` 相同的统一结构列表。
可用策略内状态(模块级变量)做去重/冷却, 例如 _last_alert。
"""

_last_alert = {}  # code -> 上次告警价格, 用于避免同方向重复报


def on_tick(quotes, feed_errors=None):
    signals = []
    for q in quotes:
        qd = q["quote"]
        if qd["last"] is None:
            continue
        code = q["code"]

        # 1) 价格突破告警: 阈值表可按需修改
        thresholds = {"IF2612": 4550.0, "rb": 3200.0, "sz159915": 3.5}
        th = thresholds.get(code)
        if th and qd["last"] > th:
            prev = _last_alert.get(code)
            if prev is None or prev <= th:  # 只在向上穿越那一刻报一次
                signals.append({"symbol": code, "event": "breakout",
                                "price": qd["last"], "threshold": th})
            _last_alert[code] = qd["last"]

        # 2) 大幅波动告警: 当日涨跌幅绝对值超 1.5%
        if qd["change_pct"] is not None and abs(qd["change_pct"]) >= 1.5:
            signals.append({"symbol": code, "event": "big_move",
                            "pct": qd["change_pct"], "price": qd["last"]})
    return signals
