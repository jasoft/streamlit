#!/usr/bin/env python3
"""组合交易 API 冒烟测试 (TestClient, 离线): 行情/持仓/ths_trade 全部打桩.

覆盖: 创建(权重归一/名称补全) -> 列表对照(现价/持仓/实际权重/偏差) -> 预览
(buy/sell/sync) -> 试算执行 -> 真实执行(打桩) + 历史落盘 -> 组合调整 -> 删除 -> 400 错误.
运行: uv run python tests/test_portfolios_api.py
"""
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from backend import portfolios as pf_mgr  # noqa: E402
from backend.main import app  # noqa: E402
from trading import portfolios as engine  # noqa: E402

PASS = 0


def ok(cond, name, detail=""):
    global PASS
    status = "✅" if cond else "❌"
    print(f"{status} {name} {detail}")
    assert cond, name
    PASS += 1


QUOTES = {"510300": {"last": 4.0, "name": "沪深300ETF"},
          "601899": {"last": 20.0, "name": "紫金矿业"},
          "000001": {"last": 10.0, "name": "平安银行"}}
THS = {"ok": True, "positions": {
    "510300": {"name": "沪深300ETF", "qty": 10_000, "available": 10_000,
               "cost": 3.8, "last": 4.0},
    "601899": {"name": "紫金矿业", "qty": 2_000, "available": 2_000,
               "cost": 18.0, "last": 20.0},
}, "msg": ""}


def main():
    client = TestClient(app)
    with tempfile.TemporaryDirectory() as td:
        engine.STORE_PATH = Path(td) / "portfolios.json"

        # 打桩: 行情 / 持仓 / ths 下单 (订单全部成功)
        pf_mgr.fetch_quotes = lambda codes: _async({"510300": QUOTES["510300"],
                                                    "601899": QUOTES["601899"],
                                                    "000001": QUOTES["000001"]})
        pf_mgr.fetch_ths_positions = lambda: _async(dict(THS))
        engine._call_ths_trade = lambda side, code, qty, price, dry_run=False: {
            "ok": True, "result_text": "", "stdout_tail": ""}

        # ---------- 1. 空列表 ----------
        r = client.get("/api/portfolios")
        ok(r.status_code == 200 and r.json()["portfolios"] == [], "空组合列表")
        ok(r.json()["ths_ok"] is True, "同花顺持仓可达")

        # ---------- 2. 创建 (权重自动归一 + 名称补全) ----------
        r = client.post("/api/portfolios", json={
            "name": "红利双雄", "items": [
                {"code": "510300", "weight": 60, "name": "沪深300ETF"},
                {"code": "601899", "weight": 40}]})
        body = r.json()
        ok(r.status_code == 200 and body["ok"], "创建组合")
        pf = body["portfolio"]
        pid = pf["id"]
        ok(abs(sum(i["weight"] for i in pf["items"]) - 100) < 1e-6, "权重归一到 100")
        ok(pf["items"][1]["name"] == "紫金矿业", "名称按行情自动补全",
           pf["items"][1]["name"])

        # 非法请求 -> 400
        r = client.post("/api/portfolios", json={
            "name": "坏代码", "items": [{"code": "abc", "weight": 100}]})
        ok(r.status_code == 400, "非法代码返回 400")

        # ---------- 3. 列表对照 (现价/持仓/实际权重/偏差) ----------
        r = client.get("/api/portfolios").json()
        p = r["portfolios"][0]
        ok(p["market_value"] == 10_000 * 4 + 2_000 * 20, "组合市值 = 持仓x现价",
           p["market_value"])
        rows = {i["code"]: i for i in p["items"]}
        ok(rows["510300"]["price"] == 4.0 and rows["510300"]["position"] == 10_000,
           "现价 + 真实持仓对照")
        ok(rows["510300"]["actual_weight"] == 50.0
           and rows["601899"]["actual_weight"] == 50.0, "实际权重计算")
        ok(rows["601899"]["drift"] == 10.0, "偏差 = 实际 - 目标", rows["601899"]["drift"])

        # ---------- 4. 预览 ----------
        r = client.get(f"/api/portfolios/{pid}/preview",
                       params={"action": "buy", "amount": 100_000}).json()
        rb = {x["code"]: x for x in r["plan"]["rows"]}
        ok(rb["510300"]["qty"] == 15_000 and rb["601899"]["qty"] == 2_000,
           "买入预览: 6万@4=15000 / 4万@20=2000", f"{rb['510300']['qty']}/{rb['601899']['qty']}")
        r = client.get(f"/api/portfolios/{pid}/preview", params={"action": "sync"}).json()
        rs = {x["code"]: x for x in r["plan"]["rows"]}
        ok(rs["510300"]["side"] == "buy" and rs["601899"]["side"] == "sell",
           "同步预览: 低配买/超配卖",
           f"{rs['510300']['side']}{rs['510300']['qty']}/{rs['601899']['side']}{rs['601899']['qty']}")
        ok(r["plan"]["total_value"] == 80_000, "同步总市值口径")

        # ---------- 5. 试算执行 (dry_run=true, 不碰同花顺) ----------
        r = client.post(f"/api/portfolios/{pid}/buy", json={
            "total_amount": 100_000, "dry_run": True}).json()
        ok(r["ok"] and r["dry_run"] and all(o["status"] == "planned"
                                            for o in r["orders"]), "试算执行")
        ok(client.get("/api/portfolios").json()["portfolios"][0]["history"] == [],
           "试算不记历史")

        # ---------- 6. 真实执行 + 历史落盘 ----------
        r = client.post(f"/api/portfolios/{pid}/buy", json={
            "total_amount": 100_000, "dry_run": False, "pad_pct": 0.5}).json()
        ok(r["ok"] and not r["dry_run"] and r["ok_count"] == 2
           and r["fail_count"] == 0, "真实执行成功 2 笔", r.get("summary"))
        ok(all(o["limit_price"] == round(o["limit_price"], 3)
               for o in r["orders"]), "限价按 tick 取整")
        hist = client.get("/api/portfolios").json()["portfolios"][0]["history"]
        ok(len(hist) == 1 and hist[0]["action"] == "buy"
           and hist[0]["ok_count"] == 2, "执行历史落盘")
        ok(all(o["limit_price"] == 4.02 for o in r["orders"]
               if o["code"] == "510300"), "买限价 = 价*(1+pad%) 向上取 tick",
           [o["limit_price"] for o in r["orders"] if o["code"] == "510300"])

        # ---------- 7. 卖出执行 (按权重卖出 4 万) ----------
        r = client.post(f"/api/portfolios/{pid}/sell", json={
            "total_amount": 40_000, "dry_run": False}).json()
        sides = {o["code"]: (o["side"], o["qty"]) for o in r["orders"]}
        ok(sides["510300"] == ("sell", 6_000) and sides["601899"] == ("sell", 800),
           "按权重分摊卖出 (6万x60%@4 / 6万x40%@20)", str(sides))

        # ---------- 8. 组合调整 (移出成分 -> 同步时按 ETF 剔除口径清仓) ----------
        r = client.put(f"/api/portfolios/{pid}", json={
            "items": [{"code": "510300", "weight": 100}]}).json()
        ok(r["ok"] and len(r["portfolio"]["items"]) == 1
           and r["portfolio"]["items"][0]["weight"] == 100.0, "组合调整: 只留单标的")
        ok(r["portfolio"]["removed_codes"] == ["601899"], "移出成分被记录")
        r = client.get(f"/api/portfolios/{pid}/preview",
                       params={"action": "sync"}).json()
        rows2 = {x["code"]: x for x in r["plan"]["rows"]}
        ok(rows2["510300"]["target_value"] == 80_000, "同步按新配比: 市值全部归单标的",
           rows2["510300"]["target_value"])
        ok(rows2["601899"]["side"] == "sell" and rows2["601899"]["qty"] == 2_000,
           "移出成分同步清仓")

        # ---------- 9. 删除 ----------
        ok(client.delete(f"/api/portfolios/{pid}").json()["ok"], "删除组合")
        ok(client.delete(f"/api/portfolios/{pid}").status_code == 404, "重复删除 404")
        ok(client.get("/api/portfolios").json()["portfolios"] == [], "删除后列表为空")

    print(f"\n全部 {PASS} 项断言通过 ✅")


def _async(v):
    async def _f():
        return v
    return _f()


if __name__ == "__main__":
    main()
