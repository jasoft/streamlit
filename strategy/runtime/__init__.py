"""策略运行时: 把信号函数桥接到事件驱动执行.

模块:
- portfolio: 内存持仓 + state.json 落盘, 跟踪现金/持仓/订单状态
- broker:    订单意图 -> 成交事件 (Backtest 同步次日开盘, Live 调 ths_trade)
- ctx:      策略 on_bar 看到的 Context (history/position/submit_order/...)
- runner:   --mode {backtest,paper,live} 切换 + K线结束触发 + 事件循环

设计要点:
- 策略只描述信号 (signal 向量化 / on_bar 事件驱动), 不碰 broker
- Context 提供 submit_order (订单意图层), 不直接调 ths_trade
- BacktestBroker 对齐 engine.py 口径: 收盘信号 -> 次日开盘成交 (无未来函数)
- LiveBroker 当前为同步简化版 (submit 即判定 ok), 预留 _poll_loop 异步轮询扩展点
"""
