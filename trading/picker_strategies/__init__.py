"""选股自动交易系统 — 选股策略插件包.

一个插件 = 一个 py 文件暴露 PickStrategy 子类 (ID 唯一), 零注册自动发现
(见 registry.py, 与 strategy/registry.py 同构). 内置:
- rsi_rebound     超跌反弹选股
- volume_breakout 放量突破选股
"""
