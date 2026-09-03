/** Playwright E2E: /config 已删策略配置, /charts 有实盘 checkbox.
 *  用法:  node tests/playwright_config_charts.cjs  (后端 8000 & 前端 3001 要先起)
 */
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const FRONT = process.env.FRONT_URL || "http://127.0.0.1:3001";

async function waitForHydrate(page) {
  // 等到页面里不再有 "加载中..." 这种 SSR 占位文字 (或 body 有实质内容)
  for (let i = 0; i < 40; i++) {
    const txt = await page.locator("body").innerText();
    if (txt.length > 100 && !txt.includes("Loading...") && !txt.includes("加载中...")) return txt;
    await page.waitForTimeout(250);
  }
  return page.locator("body").innerText();
}

function consoleCheck(errors, warns) {
  const fatal = errors.filter(e => {
    const m = (e.text() || "").toLowerCase();
    // 忽略 benign: SourceMap、React hydration 双跑的无害 warning、Next.js 的 banner
    if (m.includes("sourcemap") || m.includes("devtools") || m.includes("next.js")) return false;
    if (m.includes("strictmode") || m.includes("component rendered")) return false;
    return true;
  });
  return fatal;
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  const ctx = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await ctx.newPage();
  let errors = [];
  let warns = [];
  page.on("console", m => { if (m.type() === "error") errors.push(m); else if (m.type() === "warning") warns.push(m); });

  const checks = [];
  function mark(name, ok, extra = "") {
    checks.push({ page: name, ok, extra });
    console.log(`  ${ok ? "✓" : "✗"} ${name}${extra ? "  ↳ " + extra : ""}`);
  }

  try {
    // ============ /config ============
    console.log("\n【/config 页面】");
    await page.goto(FRONT + "/config", { waitUntil: "domcontentloaded", timeout: 60000 });
    const txtCfg = await waitForHydrate(page);
    errors = [];

    const hasCosts = txtCfg.includes("交易成本") && txtCfg.includes("手续费") && txtCfg.includes("滑点");
    const hasTabs = txtCfg.includes("股票 / ETF / 指数") && txtCfg.includes("期货") && txtCfg.includes("期权");
    const hasOldSection = txtCfg.includes("实盘策略配置");
    mark("已展示交易成本 Section", hasCosts,
         hasCosts ? "含 交易成本/手续费/滑点 文案" : `body 片段: ${txtCfg.slice(0, 400)}`);
    mark("三大类 Tab 文案齐全 (股票/期货/期权)", hasTabs);
    mark("已删除旧「实盘策略配置」Section", !hasOldSection,
         hasOldSection ? "⚠️ 仍包含旧标题 '实盘策略配置'" : "OK");

    const fatalCfg = consoleCheck(errors, warns);
    mark("控制台 0 致命报错", fatalCfg.length === 0,
         fatalCfg.length ? String(fatalCfg.slice(0, 3).map(m => m.text())) : "");

    // ============ /charts ============
    console.log("\n【/charts 页面】");
    await page.goto(FRONT + "/charts", { waitUntil: "domcontentloaded", timeout: 60000 });
    // 先新建一张图 (才有工具栏 实盘 checkbox)
    const createBtn = page.getByRole("button", { name: /创建/ });
    if (await createBtn.isVisible({ timeout: 5000 }).catch(() => false)) {
      await createBtn.click();
      await page.waitForTimeout(1500);
    }
    const txtCharts = await waitForHydrate(page);

    const hasLiveLabel = /⚡\s*实盘/.test(txtCharts) || txtCharts.includes("实盘") && txtCharts.includes("真实交易");
    const hasScan = txtCharts.includes("扫描");
    const hasPaper = /纸面|paper|模拟/.test(txtCharts);
    mark("已展示「⚡ 实盘」置顶 checkbox 文案", hasLiveLabel,
         hasLiveLabel ? "命中 '⚡ 实盘' 或 实盘+真实交易" : `body 片段: ${txtCharts.slice(0, 500)}`);
    mark("「扫描」按钮已渲染", hasScan);
    mark("纸面模式/模拟 文案存在 (关闭状态提示)", hasPaper);

    const fatalCharts = consoleCheck(errors, warns);
    mark("控制台 0 致命报错", fatalCharts.length === 0,
         fatalCharts.length ? String(fatalCharts.slice(0, 3).map(m => m.text())) : "");

    // 点击 ⚡ 实盘 checkbox → 弹出 confirm 提示
    // 优先通过 title/aria-label 找，找不到就按文字搜索包含"实盘"的 label
    console.log("\n【交互: 切换 实盘 checkbox → 触发 confirm 二次确认】");
    const liveCheck = page.getByRole("checkbox", { name: /实盘|真实交易/ });
    let triggered = false;
    if (await liveCheck.count() > 0) {
      page.once("dialog", async (d) => {
        triggered = /真实订单|真实下单|同花顺|下真实单/.test(d.message());
        await d.accept();
      });
      await liveCheck.first().click();
      await page.waitForTimeout(600);
    }
    mark("勾实盘 → 弹出 confirm(二次风险提示) 并命中关键词", triggered,
         triggered ? "confirm 文案含'真实订单/下真实单'" :
           (await liveCheck.count() === 0 ? "未找到 checkbox (可能 aria-label 不一致)"
                                         : "checkbox 点了但 confirm 没弹/文案不对"));

  } catch (e) {
    console.error("PW 意外异常:", e && e.stack ? e.stack : e);
    process.exit(2);
  } finally {
    await browser.close();
  }

  const passed = checks.filter(c => c.ok).length;
  const failed = checks.length - passed;
  console.log(`\n🏁 Playwright 总计 ${checks.length} 项: ${passed} 通过, ${failed} 失败`);
  if (failed) {
    for (const c of checks) {
      if (!c.ok) console.error("  FAIL:", c.page, c.extra);
    }
    process.exit(1);
  }
}

run();
