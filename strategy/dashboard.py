"""自动化交易看板: 回测 / 实盘策略管理 / 配置.

启动 (项目根目录):
  uv run streamlit run strategy/dashboard.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy import config as config_mod  # noqa: E402
from strategy import manager, registry, trader  # noqa: E402
from strategy.engine import backtest  # noqa: E402

st.set_page_config(page_title="自动化交易系统", page_icon="📈", layout="wide")
INIT_CASH = 100_000


def subprocess_positions():
    import subprocess as sp
    r = sp.run([sys.executable, str(Path(__file__).resolve().parent.parent
                                    / "scripts" / "ths_trade.py"), "positions"],
               capture_output=True, text=True, timeout=120)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"ok": False, "stderr": r.stderr[-500:]}


strategies = registry.discover()
page = st.sidebar.radio("页面", ["📈 回测", "🚦 实盘策略管理", "⚙️ 配置"], label_visibility="collapsed")


@st.cache_data(ttl=300, show_spinner="拉取行情中...")
def load_data(code: str, qfq: bool, tf: str = "day") -> pd.DataFrame:
    return trader._fetch(code, qfq, tf)


def param_widgets(name: str, cfg: dict) -> dict:
    """按策略 PARAMS schema 在侧栏生成控件, 实时改参数."""
    strat = strategies[name]
    st.sidebar.subheader(f"{strat.TITLE} 参数")
    values = {}
    tunable = {k: v for k, v in strat.PARAMS.items() if isinstance(v, dict)}
    for key, spec in tunable.items():
        label = f"{name}.{key}"
        if spec["type"] == "int":
            values[key] = st.sidebar.slider(
                label, spec.get("min", 1), spec.get("max", 999),
                int(cfg["params"].get(key, spec["default"])), 1)
        else:
            values[key] = st.sidebar.number_input(
                label, float(spec.get("min", 0.0)), float(spec.get("max", 9999.0)),
                float(cfg["params"].get(key, spec["default"])))
    return strat.validate_params(values)


def symbol_input(name: str, cfg: dict) -> list:
    st.sidebar.subheader(f"{name} 标的")
    raw = st.sidebar.text_input("代码 (逗号分隔, 带交易所前缀)",
                                value=",".join(cfg["symbols"]), key=f"sym_{name}")
    return [s.strip() for s in raw.split(",") if s.strip()]


# ============================== 页面 1: 回测 ==============================
if page == "📈 回测":
    st.title("📈 回测")
    cfg = config_mod.load(strategies)
    qfq = st.sidebar.checkbox("前复权 (fdata)", value=False,
                              help="有分红的标的建议开启")
    selected = st.sidebar.multiselect("选择策略 (可多选)", list(strategies),
                                      default=[n for n, c in cfg["strategies"].items() if c["enabled"]])

    plan = []  # [(name, params, symbols)]
    for name in selected:
        params = param_widgets(name, cfg["strategies"][name])
        symbols = symbol_input(name, cfg["strategies"][name])
        plan.append((name, params, symbols))

    if not plan:
        st.info("左侧选择至少一个策略")
        st.stop()

    combined = None
    for name, params, symbols in plan:
        with st.expander(f"🧭 {strategies[name].TITLE} · {name} · 参数 {params}", expanded=True):
            strat_results = []
            tf = getattr(strategies[name], "TIMEFRAME", "day")
            for symbol in symbols:
                try:
                    df = load_data(symbol, qfq, tf)
                except Exception as e:
                    st.error(f"{symbol} 数据获取失败: {e}")
                    continue
                target = strategies[name].target_position(df, params)
                r = backtest(df, target, cash=INIT_CASH)
                strat_results.append((symbol, r))

                st.markdown(f"#### {symbol}")
                cols = st.columns(6)
                s = r["stats"]
                cols[0].metric("总收益", f"{s['总收益率%']:+.1f}%",
                               f"vs 持有 {s['买入持有总收益%']:+.1f}%", delta_color="off")
                cols[1].metric("年化", f"{s['年化收益率%']:+.1f}%")
                cols[2].metric("最大回撤", f"{s['最大回撤%']:.1f}%")
                cols[3].metric("Sharpe", s["Sharpe"])
                cols[4].metric("交易次数", s["交易次数"])
                cols[5].metric("胜率", f"{s['胜率%']}%" if s["胜率%"] is not None else "—")

                # 资金曲线 vs 买入持有
                d = r["df"]
                bh = d.set_index("date")["close"] / d["close"].iloc[0] * INIT_CASH
                eq = r["equity"]
                fig = go.Figure()
                fig.add_trace(go.Scattergl(x=eq.index, y=eq.values, name="策略",
                                           line=dict(color="#2962ff", width=1.6)))
                fig.add_trace(go.Scattergl(x=bh.index, y=bh.values, name="买入持有",
                                           line=dict(color="#9e9e9e", width=1.2, dash="dash")))
                fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10),
                                  hovermode="x unified",
                                  legend=dict(orientation="h", y=1.02, x=0))
                fig.update_yaxes(tickformat=",.0f")
                st.plotly_chart(fig, use_container_width=True, key=f"eq_{name}_{symbol}")

                # 价格 + 均线决策位 + 买卖点 (画目标仓位叠加)
                buys = [m for m in r["markers"] if m["action"] == "买入"]
                sells = [m for m in r["markers"] if m["action"] == "卖出"]
                fig2 = go.Figure()
                fig2.add_trace(go.Scattergl(x=d["date"], y=d["close"], name="收盘价",
                                            line=dict(color="#424242", width=1.2)))
                fig2.add_trace(go.Scattergl(x=[m["date"] for m in buys],
                                            y=[m["price"] for m in buys], name="买入",
                                            mode="markers",
                                            marker=dict(symbol="triangle-up", size=9, color="#00c853")))
                fig2.add_trace(go.Scattergl(x=[m["date"] for m in sells],
                                            y=[m["price"] for m in sells], name="卖出",
                                            mode="markers",
                                            marker=dict(symbol="triangle-down", size=9, color="#ff1744")))
                fig2.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                                   hovermode="x unified",
                                   legend=dict(orientation="h", y=1.02, x=0))
                st.plotly_chart(fig2, use_container_width=True, key=f"px_{name}_{symbol}")

                l, rr = st.columns([1, 2])
                with l:
                    fig3 = go.Figure(go.Scattergl(
                        x=eq.index, y=(eq / eq.cummax() - 1) * 100,
                        fill="tozeroy", line=dict(color="#d32f2f", width=1)))
                    fig3.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10),
                                       title="回撤 %", yaxis_ticksuffix="%")
                    st.plotly_chart(fig3, use_container_width=True, key=f"dd_{name}_{symbol}")
                with rr:
                    fig4 = go.Figure(go.Bar(
                        x=list(r["逐年收益%"].keys()), y=list(r["逐年收益%"].values()),
                        marker_color=["#00c853" if v >= 0 else "#ff1744"
                                      for v in r["逐年收益%"].values()]))
                    fig4.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10),
                                       title="逐年收益 %", yaxis_ticksuffix="%")
                    st.plotly_chart(fig4, use_container_width=True, key=f"yr_{name}_{symbol}")

                strat_results[-1] = (symbol, r)

            # 合并同策略多标的等权曲线
            if len(strat_results) > 1:
                eqs = [r["equity"] / INIT_CASH for _, r in strat_results]
                merged = pd.concat(eqs, axis=1).ffill().mean(axis=1) * INIT_CASH * len(eqs)
                fig5 = go.Figure(go.Scattergl(x=merged.index, y=merged.values, name="等权合并",
                                              line=dict(color="#6200ea", width=1.8)))
                fig5.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                                   title="多标的等权合并资金曲线", yaxis_ticksuffix="")
                st.plotly_chart(fig5, use_container_width=True, key=f"mg_{name}")

            if combined is None:
                combined = pd.concat(
                    [r["equity"] / INIT_CASH for _, r in strat_results], axis=1
                ).ffill().mean(axis=1)
            else:
                combined = combined + pd.concat(
                    [r["equity"] / INIT_CASH for _, r in strat_results], axis=1
                ).ffill().mean(axis=1)

    if combined is not None and len(selected) > 1:
        st.subheader("全部策略等权合并")
        st.line_chart(combined * INIT_CASH, height=300, color="#00bfa5")

# ========================= 页面 2: 实盘策略管理 =========================
elif page == "🚦 实盘策略管理":
    st.title("🚦 实盘策略管理")
    st.caption("每个策略一个独立常驻进程: 盘中每 poll_seconds 评估一轮并记录处理结果, "
               "到 execute_time 执行下单. dry-run=只填单不提交 (同花顺需在运行).")
    cfg = config_mod.load(strategies)

    def live_panel(name: str, running: bool):
        """实时面板: 最新评估 + 处理结果流水 + 分时图 + 日K图. 运行中每 60s 自动刷新."""
        scfg = cfg["strategies"][name]
        qfq = scfg["live"]["qfq"]

        @st.fragment(run_every="60s" if running else None)
        def panel():
            evals = trader.read_evals(name, tail=60)
            if evals:
                st.subheader("最新评估")
                latest = {}
                for e in evals:  # 每标的取最新一条
                    latest[e["symbol"]] = e
                cols = st.columns(len(latest) or 1)
                for c, (symbol, e) in zip(cols, latest.items()):
                    ma_key = next((k for k in e if k.startswith("ma")), "ma20")
                    c.metric(f"{symbol}", f"{e['price']}",
                             f"{ma_key} {e[ma_key]}", delta_color="off")
                    c.markdown(f"**{e['msg']}** · 目标仓位 {e['target']} · {e['ts']}")
            if evals and any(e["alert"] for e in evals):
                st.warning("⚠️ 有触发信号但尚未执行 —— 到执行时刻由策略进程自动下单, "
                           "或点「立即跑一轮」手动触发 (dry-run)")

            st.subheader("日内分时 (5m)")
            for symbol in scfg["symbols"]:
                try:
                    intraday = trader.fetch_intraday(symbol, "5m", limit=100)
                    daily = load_data(symbol, qfq)
                except Exception as e:
                    st.error(f"{symbol} 数据获取失败: {e}")
                    continue
                intraday = intraday.sort_values("date")
                daily = daily.assign(date=pd.to_datetime(daily["date"]))
                day = intraday["date"].dt.date.iloc[-1]
                intraday = intraday[intraday["date"].dt.date == day]
                w = int(scfg["params"].get("window", scfg["params"].get("slow", 20)))
                ma_now = (daily["close"].rolling(w).mean().iloc[-1]
                          if len(daily) >= w else None)
                prev_close = daily[daily["date"].dt.date < day]["close"].iloc[-1] \
                    if len(daily[daily["date"].dt.date < day]) else None

                fig = go.Figure()
                fig.add_trace(go.Scattergl(x=intraday["date"], y=intraday["close"],
                                           name="分时", line=dict(color="#2962ff", width=1.5)))
                if prev_close is not None:
                    fig.add_hline(y=prev_close, line_dash="dot", line_color="#9e9e9e",
                                  annotation_text=f"昨收 {prev_close:.3f}")
                if ma_now is not None:
                    fig.add_hline(y=ma_now, line_dash="dash", line_color="#ff6d00",
                                  annotation_text=f"MA{w} {ma_now:.3f}")
                last = intraday.iloc[-1]
                fig.add_trace(go.Scattergl(x=[last["date"]], y=[last["close"]],
                                           name="最新", mode="markers+text",
                                           text=[f"{last['close']:.3f}"], textposition="top right",
                                           marker=dict(size=10, color="#00c853")))
                fig.update_layout(height=300, margin=dict(l=10, r=10, t=10, b=10),
                                  title=f"{symbol} · {day}", hovermode="x unified",
                                  legend=dict(orientation="h", y=1.02, x=0))
                st.plotly_chart(fig, use_container_width=True, key=f"intra_{name}_{symbol}")

            st.subheader("历史日K (近 120 日) 与应有仓位")
            state = trader.load_state(name)
            w = int(scfg["params"].get("window", scfg["params"].get("slow", 20)))
            for symbol in scfg["symbols"]:
                try:
                    daily = load_data(symbol, qfq)
                except Exception as e:
                    st.error(f"{symbol} 数据获取失败: {e}")
                    continue
                d = daily.assign(date=pd.to_datetime(daily["date"])).tail(120).reset_index(drop=True)
                d["ma"] = d["close"].rolling(w).mean()
                fig = go.Figure()
                fig.add_trace(go.Candlestick(
                    x=d["date"], open=d["open"], high=d["high"],
                    low=d["low"], close=d["close"], name="日K",
                    increasing_line_color="#ef5350", decreasing_line_color="#26a69a",
                    showlegend=False))
                fig.add_trace(go.Scattergl(x=d["date"], y=d["ma"], name=f"MA{w}",
                                           line=dict(color="#ff6d00", width=1.4)))
                rec = state.get(symbol)
                if rec and rec.get("price"):
                    color = "#00c853" if rec.get("target") == 1 else "#d32f2f"
                    fig.add_trace(go.Scattergl(
                        x=[pd.to_datetime(rec["date"])], y=[rec["price"]],
                        name="当前仓位", mode="markers+text",
                        text=["持仓" if rec.get("target") == 1 else "空仓"],
                        textposition="top center",
                        marker=dict(size=12, color=color, symbol="diamond")))
                fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10),
                                  title=symbol, hovermode="x unified",
                                  xaxis_rangeslider_visible=False,
                                  legend=dict(orientation="h", y=1.02, x=0))
                st.plotly_chart(fig, use_container_width=True, key=f"dk_{name}_{symbol}")

            if evals:
                st.subheader(f"处理结果流水 (最近 {min(len(evals), 30)} 条, "
                             "每次取数一条)")
                ev = pd.DataFrame(evals[-30:][::-1])
                show_cols = [c for c in ["ts", "symbol", "price", "msg", "target"]
                             if c in ev.columns]
                st.dataframe(ev[show_cols], use_container_width=True, height=300)

        return panel

    rows = manager.status()
    for row in rows:
        name = row["name"]
        if row["running"]:
            header = (f"🟢 {strategies[name].TITLE} ({name}) · "
                      f"运行中 pid {row['pid']} · {row['status']}")
        else:
            header = f"⚪ {strategies[name].TITLE} ({name}) · 未运行"
        with st.expander(header, expanded=row["running"]):
            c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
            if row["running"]:
                if c2.button("⏹ 停止", key=f"stop_{name}"):
                    st.toast(manager.stop(name)["msg"])
                    st.rerun()
            else:
                if c1.button("▶ 启动", key=f"start_{name}"):
                    st.toast(manager.start(name)["msg"])
                    st.rerun()
            if c3.button("⚡ 立即跑一轮 (dry-run)", key=f"run_{name}"):
                summary = trader.run_once(name, cfg["strategies"][name], dry_run=True)
                st.json(summary)

            st.write(f"标的: {', '.join(row['symbols'])} · "
                     f"启用: {'是' if row['enabled'] else '否'} · "
                     f"下次评估: {row['next_run']} · 最近运行: {row['last_run']}")

            live_panel(name, row["running"])()

            log = manager.STATE_DIR / f"{name}.log"
            if log.exists():
                with st.popover("查看运行日志"):
                    st.text(log.read_text(encoding="utf-8", errors="replace")[-3000:])

    st.divider()
    if st.button("读取同花顺实际持仓 (对账用)"):
        r = subprocess_positions()
        st.json(r)

# ============================== 页面 3: 配置 ==============================
else:
    st.title("⚙️ 配置")
    st.caption("保存到 strategy/config.json, 对运行中的策略进程在**下一次运行**生效; "
               "改参数/标的后建议重启策略进程.")
    cfg = config_mod.load(strategies)
    new_cfg = {"strategies": {}}
    for name, strat in strategies.items():
        scfg = cfg["strategies"][name]
        with st.expander(f"{strat.TITLE} ({name})", expanded=True):
            c1, c2 = st.columns(2)
            scfg["enabled"] = c1.checkbox("启用", value=scfg["enabled"], key=f"en_{name}")
            syms = c2.text_input("标的 (逗号分隔)", value=",".join(scfg["symbols"]), key=f"cfg_sym_{name}")
            scfg["symbols"] = [s.strip() for s in syms.split(",") if s.strip()]
            scfg["cash_per_symbol"] = st.number_input(
                "每标的资金 (元)", min_value=1000, step=1000,
                value=int(scfg["cash_per_symbol"]), key=f"cash_{name}")

            st.markdown("**策略参数**")
            tunable = {k: v for k, v in strat.PARAMS.items() if isinstance(v, dict)}
            cols = st.columns(len(tunable) or 1)
            for i, (key, spec) in enumerate(tunable.items()):
                if spec["type"] == "int":
                    scfg["params"][key] = cols[i].number_input(
                        key, int(spec.get("min", 1)), int(spec.get("max", 999)),
                        int(scfg["params"].get(key, spec["default"])), key=f"p_{name}_{key}")
                else:
                    scfg["params"][key] = cols[i].number_input(
                        key, float(spec.get("min", 0.0)), float(spec.get("max", 9999.0)),
                        float(scfg["params"].get(key, spec["default"])), key=f"p_{name}_{key}")

            st.markdown("**实盘运行**")
            l1, l2, l3 = st.columns(3)
            scfg["live"]["dry_run"] = l1.checkbox(
                "dry-run (只填单不提交)", value=scfg["live"]["dry_run"], key=f"dr_{name}")
            scfg["live"]["execute_time"] = l2.text_input(
                "执行时刻", value=scfg["live"]["execute_time"], key=f"et_{name}")
            scfg["live"]["qfq"] = l3.checkbox(
                "前复权数据", value=scfg["live"]["qfq"], key=f"qfq_{name}")
            new_cfg["strategies"][name] = scfg

    if st.button("💾 保存配置", type="primary"):
        config_mod.save(new_cfg)
        st.success("已保存到 strategy/config.json")
        st.rerun()
