#!/usr/bin/env python3
"""组合交易引擎离线测试: 不碰行情/不碰同花顺, 直接喂 prices/positions 断言分配结果.

覆盖: tick/限价取整、权重归一化、买入分配(整手+补一手)、卖出分配(零股/清仓/封顶)、
同步仓位(目标市值/差额/T+1可卖封顶/门槛/组合外持仓)、执行排序(先卖后买)、CRUD。
"""
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from trading import portfolios as pf_mod

PASS = 0


def ok(cond, name, detail=""):
    global PASS
    status = "✅" if cond else "❌"
    print(f"{status} {name} {detail}")
    assert cond, name
    PASS += 1


ITEMS = [
    {"code": "510300", "name": "沪深300ETF", "weight": 50},
    {"code": "601899", "name": "紫金矿业", "weight": 30},
    {"code": "000001", "name": "平安银行", "weight": 20},
]
PRICES = {"510300": 4.000, "601899": 20.00, "000001": 10.00}


async def main():
    # ---------- 1. tick / 限价取整 ----------
    ok(pf_mod.price_tick("510300") == 0.001 and pf_mod.price_tick("159915") == 0.001,
       "ETF tick=0.001")
    ok(pf_mod.price_tick("601899") == 0.01 and pf_mod.price_tick("000001") == 0.01,
       "股票 tick=0.01")
    ok(pf_mod.round_price(4.0009, "510300", "buy") == 4.001,
       "买限价向上取整到 tick", f"{pf_mod.round_price(4.0009, '510300', 'buy')}")
    ok(pf_mod.round_price(4.0009, "510300", "sell") == 4.000,
       "卖限价向下取整到 tick")
    ok(pf_mod.round_price(20.005, "601899", "buy") == 20.01
       and pf_mod.round_price(20.005, "601899", "sell") == 20.00,
       "股票限价按 0.01 取整")

    # ---------- 2. 权重归一化 ----------
    norm = pf_mod.normalize_items(ITEMS)
    ok(abs(sum(x["weight"] for x in norm) - 100.0) < 1e-6, "权重和归一到 100")
    ok(norm[0]["weight"] == 50.0 and norm[1]["weight"] == 30.0, "等比缩放不变")
    ok(norm[0]["name"] == "沪深300ETF", "name 保留")
    for bad, why in [([{"code": "12345", "weight": 100}], "代码位数不足"),
                     ([{"code": "510300", "weight": 50}, {"code": "510300", "weight": 50}], "代码重复"),
                     ([{"code": "510300", "weight": 0}], "权重为 0"),
                     ([], "空组合")]:
        try:
            pf_mod.normalize_items(bad)
            ok(False, f"非法输入应报错: {why}")
        except ValueError:
            ok(True, f"非法输入报错: {why}")

    # ---------- 3. 买入分配 ----------
    plan = pf_mod.alloc_buy(ITEMS, 100_000, PRICES)
    r = {x["code"]: x for x in plan["rows"]}
    # 5万@4=12500股整 / 3万@20=1500股整 / 2万@10=2000股整, 无剩余
    ok(r["510300"]["qty"] == 12_500 and r["601899"]["qty"] == 1_500
       and r["000001"]["qty"] == 2_000, "整除金额整手分配",
       f"{r['510300']['qty']}/{r['601899']['qty']}/{r['000001']['qty']}")
    ok(plan["used"] == 100_000 and plan["leftover"] == 0, "无剩余预算")
    # 非整除金额: floor 后剩余预算按权重从大到小补一手
    plan_b = pf_mod.alloc_buy(ITEMS, 99_999, PRICES)
    rb = {x["code"]: x for x in plan_b["rows"]}
    # floor: 12400/1400/1900, 剩 3399 元 -> 510300 补一手(400) -> 601899 补一手(2000)
    # -> 000001 需 1000 > 剩 999 不补
    ok(rb["510300"]["qty"] == 12_500 and rb["601899"]["qty"] == 1_500
       and rb["000001"]["qty"] == 1_900, "剩余预算按权重补一手",
       f"{rb['510300']['qty']}/{rb['601899']['qty']}/{rb['000001']['qty']}")
    ok(plan_b["used"] <= 99_999 and plan_b["leftover"] == 999, "补一手后余量正确",
       f"used={plan_b['used']} leftover={plan_b['leftover']}")
    plan2 = pf_mod.alloc_buy(ITEMS, 300, PRICES)  # 每只都买不起一手
    ok(all(x["qty"] == 0 and x["note"] for x in plan2["rows"]), "金额过小全部跳过")
    plan3 = pf_mod.alloc_buy(ITEMS, 100_000, {"510300": 4.0})  # 行情缺失
    ok(any("行情缺失" in x["note"] for x in plan3["rows"]), "缺行情标跳过并说明")
    ok(plan3["used"] <= 100_000, "缺行情不超额")

    # ---------- 4. 卖出分配 ----------
    pos = {"510300": 12_400, "601899": 1_500, "000001": 2_000}
    sell = pf_mod.alloc_sell(ITEMS, 50_000, PRICES, pos)
    s = {x["code"]: x for x in sell["rows"]}
    # 510300: 2.5万/4 = 6250 -> 6300 (half-up) ; 601899: 1.5万/20=750 ; 000001: 1万/10=1000
    ok(s["510300"]["qty"] == 6_300 and s["601899"]["qty"] == 800,
       "按权重四舍五入取整手", f"{s['510300']['qty']}/{s['601899']['qty']}")
    ok(s["000001"]["qty"] == 1_000, "卖出分配 3")
    # 超出持仓封顶: 单标的 (weight=100) 但持仓只有 100 股
    single = [{"code": "601899", "name": "紫金矿业", "weight": 100}]
    sell2 = pf_mod.alloc_sell(single, 50_000, PRICES, {"601899": 100})
    ok(sell2["rows"][0]["qty"] == 100, "可卖数量封顶")
    # 清仓: sell_amount >= 99.9% 持仓市值 -> 含零股一起清; 97% 则走整手留零股尾
    sell3 = pf_mod.alloc_sell(single, 31_000, PRICES, {"601899": 1_550})
    ok(sell3["rows"][0]["qty"] == 1_550, "清仓含零股尾",
       f"qty={sell3['rows'][0]['qty']}")
    sell3b = pf_mod.alloc_sell(single, 30_100, PRICES, {"601899": 1_550})
    ok(sell3b["rows"][0]["qty"] == 1_500, "接近清仓但未达阈值按整手卖",
       f"qty={sell3b['rows'][0]['qty']}")
    # 零股持仓: 分配金额够一半市值 -> 整笔清; 不够 -> 跳过
    odd = [{"code": "000001", "name": "平安银行", "weight": 100}]
    sell4 = pf_mod.alloc_sell(odd, 400, PRICES, {"000001": 50})
    ok(sell4["rows"][0]["qty"] == 50 and "零股" in sell4["rows"][0]["note"], "零股整笔清仓")
    sell5 = pf_mod.alloc_sell(odd, 10, PRICES, {"000001": 50})
    ok(sell5["rows"][0]["qty"] == 0, "零股金额过小跳过")
    sell6 = pf_mod.alloc_sell(ITEMS, 50_000, PRICES, {})
    ok(all(x["qty"] == 0 for x in sell6["rows"]), "无持仓全部跳过")

    # ---------- 5. 同步仓位 (人工ETF调仓) ----------
    sync_pos = {"510300": 10_000, "601899": 2_000, "000001": 0,
                "600036": 300}  # 600036 组合外
    prices = {**PRICES, "600036": 40.0}
    plan = pf_mod.sync_plan(ITEMS, prices, sync_pos, min_order_value=1000)
    ok(plan["total_value"] == 10_000 * 4 + 2_000 * 20, "总市值只含组合内标的",
       f"{plan['total_value']}")
    t = {x["code"]: x for x in plan["rows"]}
    # total = 80_000; 目标: 510300 4万 (持4万, 差0) / 601899 2.4万 (持4万, 卖 1.6万=800股)
    # 000001 目标 1.6万, 持仓 0 -> 买 1600 股
    ok(t["510300"]["side"] == "" and abs(t["510300"]["delta_value"]) < 1e-6,
       "已达标不动", f"delta={t['510300']['delta_value']}")
    ok(t["601899"]["side"] == "sell" and t["601899"]["qty"] == 800,
       "超配卖出贴近目标", f"{t['601899']['side']} {t['601899']['qty']}")
    ok(t["000001"]["side"] == "buy" and t["000001"]["qty"] == 1_600,
       "低配买入凑整手", f"{t['000001']['side']} {t['000001']['qty']}")
    ok(len(plan["external"]) == 1 and plan["external"][0]["code"] == "600036",
       "组合外持仓只展示不动")
    # 门槛: min_order_value 调大 -> 601899 差 8000 还是 >= 门槛; 000001 差 1.6万也够;
    # 用小差额标的验证: 权重改 1%
    tiny = [{"code": "601899", "weight": 1.0}, {"code": "510300", "weight": 99.0}]
    plan2 = pf_mod.sync_plan(tiny, prices, sync_pos, min_order_value=3000)
    t2 = {x["code"]: x for x in plan2["rows"]}
    # 601899 目标 800 元, 持 4 万 -> 卖差额 39200 -> 不受门槛影响 (大额)
    ok(t2["601899"]["side"] == "sell", "大额卖出不受门槛限制")
    # 买入门槛: 000001 目标差额 2000 元 (5% x 4万) 够一手但 < 3000 -> 跳过
    tiny2 = [{"code": "000001", "weight": 5.0}, {"code": "510300", "weight": 95.0}]
    plan3 = pf_mod.sync_plan(tiny2, prices, sync_pos, min_order_value=3000)
    t3 = {x["code"]: x for x in plan3["rows"]}
    ok(t3["000001"]["side"] == "" and "门槛" in t3["000001"]["note"],
       "小额买入低于门槛跳过", t3["000001"]["note"])
    # 买入凑不满一手跳过
    plan4 = pf_mod.sync_plan([{"code": "000001", "weight": 0.1},
                              {"code": "510300", "weight": 99.9}],
                             prices, sync_pos)
    t4 = {x["code"]: x for x in plan4["rows"]}
    ok(t4["000001"]["side"] == "" and "不足一手" in t4["000001"]["note"],
       "差额不足一手跳过", t4["000001"]["note"])
    # T+1 可卖封顶: 601899 可卖 100 < 持仓 2000
    plan5 = pf_mod.sync_plan(ITEMS, prices, sync_pos,
                             available={"601899": 100}, min_order_value=1000)
    t5 = {x["code"]: x for x in plan5["rows"]}
    ok(t5["601899"]["qty"] == 100 and "T+1" in t5["601899"]["note"],
       "卖出按可卖数量封顶", t5["601899"]["note"])
    # 零股持仓清尾: 000001 仅 0.1% 权重但持有 60 股 (超配) -> 整笔清
    tiny_items = [{"code": "510300", "weight": 50.0},
                  {"code": "601899", "weight": 49.9},
                  {"code": "000001", "weight": 0.1}]
    plan6 = pf_mod.sync_plan(tiny_items, prices, {**sync_pos, "000001": 60})
    t6 = {x["code"]: x for x in plan6["rows"]}
    ok(t6["000001"]["side"] == "sell" and t6["000001"]["qty"] == 60,
       "零股尾仓直接清", f"{t6['000001']['qty']}")

    # ---------- 6. 执行计划 ----------
    rows = [{"code": "601899", "qty": 400, "price": 20.0, "side": "sell", "name": "紫金矿业"},
            {"code": "000001", "qty": 0, "price": 10.0, "side": "buy"},
            {"code": "510300", "qty": 12_400, "price": 4.0}]
    out = pf_mod.execute_plan(rows, default_side="buy", pad_pct=0.3, dry_run=True)
    ok(len(out["orders"]) == 2, "空数量行剔除", f"{len(out['orders'])}")
    ok(out["orders"][0]["side"] == "sell" and out["orders"][1]["side"] == "buy",
       "先卖后买")
    o_buy = out["orders"][1]
    ok(o_buy["limit_price"] == 4.012, "买限价 = 价*(1+pad) 向上取 tick",
       f"{o_buy['limit_price']}")
    o_sell = out["orders"][0]
    ok(o_sell["limit_price"] == 19.94, "卖限价 = 价*(1-pad) 向下取 tick",
       f"{o_sell['limit_price']}")
    ok(all(o["status"] == "planned" and o["ok"] is None for o in out["orders"]),
       "试算不产生成交状态")
    # 实盘路径: monkeypatch _call_ths_trade
    calls = []

    def fake_ths(side, code, qty, price, dry_run=False):
        calls.append((side, code, qty, price))
        return {"ok": side == "buy", "result_text": "" if side == "buy" else "余额不足",
                "stdout_tail": ""}

    orig = pf_mod._call_ths_trade
    pf_mod._call_ths_trade = fake_ths
    try:
        out2 = pf_mod.execute_plan(rows, default_side="buy", pad_pct=0, dry_run=False)
    finally:
        pf_mod._call_ths_trade = orig
    ok(len(calls) == 2 and calls[0][0] == "sell", "实盘先卖后买调用 ths_trade")
    ok(out2["ok_count"] == 1 and out2["fail_count"] == 1, "成功/失败统计")
    ok(out2["orders"][0]["status"] == "failed"
       and "余额不足" in out2["orders"][0]["result_text"], "失败订单带券商文案")

    # ---------- 7. CRUD (临时存储, 不碰 backend/portfolios.json) ----------
    orig_store = pf_mod.STORE_PATH
    with tempfile.TemporaryDirectory() as td:
        pf_mod.STORE_PATH = Path(td) / "portfolios.json"
        try:
            created = pf_mod.create_portfolio("测试组合", ITEMS, note="单测")
            ok(created["id"].startswith("pf_") and len(created["items"]) == 3, "创建组合")
            ok(pf_mod.get_portfolio(created["id"]) is not None, "按 id 查找")
            updated = pf_mod.update_portfolio(created["id"], items=[
                {"code": "510300", "weight": 100}])
            ok(len(updated["items"]) == 1 and updated["items"][0]["weight"] == 100.0,
               "调整: 减仓到单标的")
            pf_mod.record_history(created["id"],
                                  {"ts": "t", "action": "buy", "dry_run": True,
                                   "orders": []})
            ok(pf_mod.get_portfolio(created["id"])["history"][0]["action"] == "buy",
               "执行历史落盘")
            ok(pf_mod.delete_portfolio(created["id"]) is True, "删除组合")
            ok(pf_mod.get_portfolio(created["id"]) is None, "删除后不存在")
            ok(pf_mod.delete_portfolio("pf_nope") is False, "删除不存在返回 False")
            try:
                pf_mod.create_portfolio("  ", ITEMS)
                ok(False, "空名称应报错")
            except ValueError:
                ok(True, "空名称报错")

            # ---------- 8. 移出成分: 记录 + 同步清仓 (ETF 剔除成分口径) ----------
            pf2 = pf_mod.create_portfolio("成分调整", [
                {"code": "510300", "weight": 60}, {"code": "601899", "weight": 40}])
            upd = pf_mod.update_portfolio(pf2["id"], items=[
                {"code": "510300", "weight": 100}])
            ok(upd["removed_codes"] == ["601899"], "移出成分被记录",
               str(upd["removed_codes"]))
            back = pf_mod.update_portfolio(pf2["id"], items=[
                {"code": "510300", "weight": 50}, {"code": "601899", "weight": 50}])
            ok(back["removed_codes"] == [], "加回成分清空移出记录")
            liq_plan = pf_mod.sync_plan(
                [{"code": "510300", "weight": 100}],
                {"510300": 4.0, "601899": 20.0},
                {"510300": 10_000, "601899": 2_000}, liquidate=["601899"])
            ok(liq_plan["total_value"] == 80_000, "清仓标的市值并入 total",
               str(liq_plan["total_value"]))
            lr = {x["code"]: x for x in liq_plan["rows"]}
            ok(lr["601899"]["side"] == "sell" and lr["601899"]["qty"] == 2_000,
               "移出成分整笔清仓")
            ok(lr["510300"]["side"] == "buy" and lr["510300"]["qty"] == 10_000,
               "清仓资金按新配比再分配", f"{lr['510300']['side']}{lr['510300']['qty']}")
            ext_plan = pf_mod.sync_plan(
                [{"code": "510300", "weight": 100}],
                {"510300": 4.0, "600036": 40.0},
                {"510300": 10_000, "600036": 300})
            ok(any(e["code"] == "600036" for e in ext_plan["external"])
               and all(x["code"] == "510300" for x in ext_plan["rows"]),
               "无关持仓只展示不动")
        finally:
            pf_mod.STORE_PATH = orig_store

    print(f"\n全部 {PASS} 项断言通过 ✅")


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
