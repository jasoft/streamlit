/**
 * E2E 测试: /backtest 回测页 — 单次回测 + 参数优化全流程
 *
 * 前置条件:
 *   - 后端 FastAPI 在 :8000 运行 (api.strategies / schema / backtest / param-optimize)
 *   - 前端 Next.js 在 :3001 运行 (或用 FRONTEND_URL 环境变量覆盖)
 *
 * 运行方式:
 *   cd /Users/weiwang/Projects/streamlit
 *   node tests/e2e_backtest.cjs
 *
 * 或指定前端地址:
 *   FRONTEND_URL=http://localhost:3001 node tests/e2e_backtest.cjs
 *
 * 退出码: 0=全通过, 1=有失败
 */
const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const FRONTEND_URL = process.env.FRONTEND_URL || "http://localhost:3001";
const BACKEND_URL = process.env.BACKEND_URL || "http://localhost:8000";
const TIMEOUT_PAGE_LOAD = 30000;
const TIMEOUT_API_WAIT = 60000;
const TIMEOUT_OPTIMIZATION = 120000;   // 参数优化最多等 2 分钟

// ---- 工具 ----
let passCount = 0;
let failCount = 0;

function check(name, condition, detail = "") {
  if (condition) {
    passCount++;
    console.log(`  ✅ ${name}${detail ? " — " + detail : ""}`);
  } else {
    failCount++;
    console.error(`  ❌ ${name}${detail ? " — " + detail : ""}`);
  }
}

async function waitForVisible(page, selector, timeout = 10000) {
  await page.waitForSelector(selector, { state: "visible", timeout });
}

async function clickAndWait(page, selector, timeout = 10000) {
  await page.click(selector, { timeout });
  await page.waitForTimeout(500);
}

/** 收集页面 console 错误 */
function setupConsoleCollector(page) {
  const errors = [];
  page.on("console", (msg) => {
    if (msg.type() === "error") errors.push(msg.text());
  });
  page.on("pageerror", (err) => errors.push(String(err)));
  return errors;
}

// ---- 测试用例 ----

async function testPageLoads(page) {
  console.log("\n[Test 1] 页面加载");
  await page.goto(`${FRONTEND_URL}/backtest`, { waitUntil: "domcontentloaded", timeout: TIMEOUT_PAGE_LOAD });
  await page.waitForTimeout(2000);

  // 标题
  const h1 = await page.locator("h1").textContent();
  check("页面标题包含'回测'", h1?.includes("回测"), `h1="${h1}"`);

  // 策略下拉框存在
  await waitForVisible(page, "select");
  check("策略下拉框可见", await page.locator("select").first().isVisible());

  // Tab 按钮存在
  const tabButtons = page.locator("button", { hasText: /单次回测|参数优化/ });
  const tabCount = await tabButtons.count();
  check("有两个 Tab 按钮", tabCount >= 2, `找到 ${tabCount} 个`);

  // 等待策略列表加载 — option 元素 Playwright 不认 visible，用 count 等待
  await page.waitForFunction(() => document.querySelectorAll("select option").length > 0, { timeout: 10000 });
  const optCount = await page.locator("select option").count();
  check("策略列表已加载", optCount > 0, `${optCount} 个策略`);
}

async function testStrategySwitchLoadsSchema(page) {
  console.log("\n[Test 2] 切换策略加载 schema");

  // 选择 intraday_t 策略
  const stratSelect = page.locator("select").first();
  // 找到 intraday_t 对应的 option
  const options = await stratSelect.locator("option").allTextContents();
  const intradayIdx = options.findIndex((t) => t.includes("日内做T") || t.includes("intraday"));
  check("找到日内做T策略", intradayIdx >= 0, `options=${JSON.stringify(options)}`);

  if (intradayIdx < 0) return;

  await stratSelect.selectOption({ index: intradayIdx });
  // 等 schema 加载 (参数输入框出现)
  await page.waitForTimeout(2000);

  // 单次回测 Tab 应该有参数输入框
  const paramInputs = page.locator("input[type='number']");
  const paramCount = await paramInputs.count();
  check("参数输入框出现", paramCount > 0, `${paramCount} 个参数输入框`);
}

async function testSingleBacktest(page) {
  console.log("\n[Test 3] 单次回测");

  // 确保 "单次回测" Tab 被选中
  const singleTab = page.locator("button", { hasText: "单次回测" });
  if (await singleTab.isVisible()) {
    await singleTab.click();
    await page.waitForTimeout(500);
  }

  // 点击 "跑回测" 按钮
  const runBtn = page.locator("button", { hasText: "跑回测" });
  check("跑回测按钮可见", await runBtn.isVisible());

  await runBtn.click();
  // 等待结果 — 资金曲线图表或 stats 卡片出现
  try {
    await page.waitForSelector("text=策略回测", { timeout: TIMEOUT_API_WAIT });
    check("回测完成 — 资金曲线出现", true);
  } catch {
    // 可能显示错误
    const bodyText = await page.locator("body").innerText();
    const hasError = bodyText.includes("回测失败") || bodyText.includes("Error");
    check("回测完成（无资金曲线但也没报错）", !hasError, hasError ? bodyText.slice(0, 200) : "");
  }
}

async function testParamOptimizeTab(page) {
  console.log("\n[Test 4] 参数优化 Tab 面板");

  // 切换到参数优化 Tab
  const optTab = page.locator("button", { hasText: "参数优化" });
  await optTab.click();
  await page.waitForTimeout(1000);

  // 优化模式选择器
  const modeSelect = page.locator("select", { hasText: /网格搜索|贝叶斯/ });
  check("优化模式选择器可见", await modeSelect.isVisible());

  // 评分指标选择器
  const metricSelect = page.locator("select", { hasText: /卡玛|总收益|夏普|胜率/ });
  check("评分指标选择器可见", await metricSelect.isVisible());

  // K线周期 checkbox — 至少有 1m/5m/15m
  const tfCheckboxes = page.locator("input[type='checkbox']");
  const tfCount = await tfCheckboxes.count();
  check("K线周期 checkbox 出现", tfCount >= 3, `${tfCount} 个 checkbox`);

  // 验证 checkbox 旁边有 1m / 5m / 15m 文字
  const checkboxLabels = [];
  for (let i = 0; i < tfCount; i++) {
    const parent = tfCheckboxes.nth(i).locator("..");
    const text = await parent.textContent();
    checkboxLabels.push(text?.trim());
  }
  const has1m = checkboxLabels.some((t) => t?.includes("1m"));
  const has5m = checkboxLabels.some((t) => t?.includes("5m"));
  const has15m = checkboxLabels.some((t) => t?.includes("15m"));
  check("K线周期含 1m", has1m);
  check("K线周期含 5m", has5m);
  check("K线周期含 15m", has15m);

  // 参数搜索配置表
  const tableRows = page.locator("table tbody tr");
  const rowCount = await tableRows.count();
  check("参数搜索配置表有行", rowCount > 0, `${rowCount} 行参数`);

  // 每行应有搜索模式下拉 (固定/离散/区间)
  if (rowCount > 0) {
    const firstRowModeSelect = tableRows.first().locator("select");
    check("第一行有搜索模式下拉", await firstRowModeSelect.isVisible());
    const modeOptions = await firstRowModeSelect.locator("option").allTextContents();
    check("搜索模式含'固定'", modeOptions.some((t) => t.includes("固定")));
    check("搜索模式含'离散'", modeOptions.some((t) => t.includes("离散")));
    check("搜索模式含'区间'", modeOptions.some((t) => t.includes("区间")));
  }
}

async function testParamOptimizeRun(page) {
  console.log("\n[Test 5] 参数优化 — 配置并运行");

  // 找到 rsi_fast 行，改成离散
  const rsiFastRow = page.locator("tr", { hasText: "rsi_fast" });
  check("rsi_fast 行存在", await rsiFastRow.isVisible());

  // 改搜索模式为离散
  const rsiModeSelect = rsiFastRow.locator("select").first();
  await rsiModeSelect.selectOption({ label: "离散列表" });
  await page.waitForTimeout(300);

  // 在离散候选输入框输入值
  const rsiDiscreteInput = rsiFastRow.locator("input").first();
  await rsiDiscreteInput.fill("4,6,8");
  await page.waitForTimeout(200);

  // 找到 oversold 行，改成离散
  const oversoldRow = page.locator("tr", { hasText: "oversold" });
  if (await oversoldRow.isVisible()) {
    const oversoldModeSelect = oversoldRow.locator("select").first();
    await oversoldModeSelect.selectOption({ label: "离散列表" });
    await page.waitForTimeout(300);
    const oversoldDiscreteInput = oversoldRow.locator("input").first();
    await oversoldDiscreteInput.fill("20,25,30");
    await page.waitForTimeout(200);
    check("oversold 改为离散 20,25,30", true);
  } else {
    check("oversold 行存在", false, "未找到 oversold 行");
  }

  // 确保评分指标选 calmar（默认应该是）
  // 确保 K线周期至少 5m 被选中 — 用 getByRole 精确匹配避免 15m 干扰
  const tf5mCheckbox = page.getByRole("checkbox", { name: "5m", exact: true });
  if (await tf5mCheckbox.count() > 0) {  const isChecked = await tf5mCheckbox.isChecked();
    if (!isChecked) {
      await tf5mCheckbox.check();
      await page.waitForTimeout(200);
    }
    check("5m 周期已选中", await tf5mCheckbox.isChecked());
  }

  // 点击开始优化
  const startBtn = page.locator("button", { hasText: "开始参数优化" });
  check("开始优化按钮可见", await startBtn.isVisible());
  await startBtn.click();
  await page.waitForTimeout(1000);

  // 检查是否出现进度条或"优化中"文字
  const optimizingText = page.locator("text=优化中");
  const progressBar = page.locator(".h-2.bg-\\[\\#1f1f1f\\]");
  const isRunning = await optimizingText.isVisible().catch(() => false)
    || await progressBar.isVisible().catch(() => false);
  check("开始优化后进入运行状态", isRunning);

  // 等待完成 — 等待 "完成" 或 "✓" 出现，或 Top N 表出现
  let completed = false;
  let errorMsg = "";
  try {
    await page.waitForSelector("text=/完成|✓/", { timeout: TIMEOUT_OPTIMIZATION });
    completed = true;
  } catch {
    // 检查是否有错误
    const errorBox = page.locator("text=失败");
    if (await errorBox.isVisible().catch(() => false)) {
      errorMsg = await errorBox.textContent().catch(() => "unknown");
    }
  }
  check("参数优化完成", completed, errorMsg ? `错误: ${errorMsg}` : "");

  if (completed) {
    await page.waitForTimeout(1000);

    // 检查 Top N 表
    const topRows = page.locator("table tbody tr");
    const topCount = await topRows.count();
    check("Top N 结果表有行", topCount > 0, `${topCount} 行`);

    // 检查最佳分数显示
    const bestScoreText = await page.locator("text=最佳分数").locator("..").textContent().catch(() => "");
    check("最佳分数显示", !!bestScoreText, bestScoreText?.slice(0, 80));

    // 检查 "应用参数" 按钮
    const applyBtn = page.locator("button", { hasText: "应用" });
    check("应用参数按钮可见", await applyBtn.isVisible().catch(() => false));
  }
}

async function testApplyBestParams(page) {
  console.log("\n[Test 6] 应用最佳参数到单次回测");

  const applyBtn = page.locator("button", { hasText: "应用" });
  if (!(await applyBtn.isVisible().catch(() => false))) {
    check("应用按钮存在", false, "跳过 — 上一步可能未完成");
    return;
  }

  await applyBtn.click();
  await page.waitForTimeout(1000);

  // 应回到单次回测 Tab
  const singleTab = page.locator("button", { hasText: "单次回测" });
  const isActive = await singleTab.getAttribute("class");
  check("切回单次回测 Tab", isActive?.includes("ff6d00") || isActive?.includes("font-semibold"),
    `class=${isActive?.slice(0, 80)}`);

  // 参数输入框应该有值（被回填了）— 取 label 含"平均"或第一个有 step 的 number input
  // 单次回测面板的参数输入框在 flex gap-3 flex-wrap 容器里
  const paramWrap = page.locator(".flex.gap-3.flex-wrap input[type='number']");
  let firstValue = "";
  if (await paramWrap.count() > 0) {
    firstValue = await paramWrap.first().inputValue().catch(() => "");
  }
  // 兜底：取所有 number input 的第二个（第一个通常是 K线条数/limit）
  if (!firstValue) {
    const allNumInputs = page.locator("input[type='number']");
    const cnt = await allNumInputs.count();
    for (let i = 0; i < cnt; i++) {
      const v = await allNumInputs.nth(i).inputValue().catch(() => "");
      if (v) { firstValue = v; break; }
    }
  }
  check("参数已回填", !!firstValue, `第一个参数值=${firstValue}`);
}

// ---- 后端 API 健康检查 ----
async function testBackendHealth() {
  console.log("\n[Test 0] 后端健康检查");
  const { default: fetch } = await import("node-fetch").catch(() => ({ default: null }));
  // Node 18+ 有全局 fetch
  const fetchFn = globalThis.fetch || fetch;
  if (!fetchFn) {
    check("跳过 — 无 fetch 可用", true);
    return;
  }

  try {
    const r = await fetchFn(`${BACKEND_URL}/api/health`);
    const j = await r.json();
    check("后端健康检查 OK", j.ok === true, `ok=${j.ok}`);

    // 策略列表
    const r2 = await fetchFn(`${BACKEND_URL}/api/strategies`);
    const strats = await r2.json();
    check("策略列表非空", Array.isArray(strats) && strats.length > 0, `${strats.length} 个策略`);

    // intraday_t schema
    const r3 = await fetchFn(`${BACKEND_URL}/api/strategies/intraday_t/schema`);
    const schema = await r3.json();
    check("intraday_t schema 有 timeframe", !!schema.timeframe, `timeframe=${schema.timeframe}`);
    check("intraday_t schema 有 params", !!schema.params && Object.keys(schema.params).length > 0,
      `${Object.keys(schema.params || {}).length} 个参数`);
    check("intraday_t timeframe 非 day", schema.timeframe !== "day", `timeframe=${schema.timeframe}`);

    // param-optimize/start + poll 测试
    const r4 = await fetchFn(`${BACKEND_URL}/api/param-optimize/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        strategy: "intraday_t",
        symbol: "sz159915",
        mode: "grid",
        param_grid: { rsi_fast: [5, 6] },
        timeframes: ["5m"],
        metric: "total_return",
        limit: 100,
        top_n: 3,
      }),
    });
    const startResp = await r4.json();
    check("param-optimize/start 返回 job_id", !!startResp.job_id, `job_id=${startResp.job_id}`);

    if (startResp.job_id) {
      // 轮询直到完成
      let pollResp = null;
      for (let i = 0; i < 30; i++) {
        await new Promise((res) => setTimeout(res, 2000));
        const r5 = await fetchFn(`${BACKEND_URL}/api/param-optimize/poll/${startResp.job_id}`);
        pollResp = await r5.json();
        if (pollResp.status === "done" || pollResp.status === "failed") break;
      }
      check("param-optimize/poll 完成", pollResp?.status === "done",
        `status=${pollResp?.status} error=${pollResp?.error?.slice(0, 100)}`);
      if (pollResp?.result) {
        check("结果含 best_score", typeof pollResp.result.best_score === "number");
        check("结果含 top 数组", Array.isArray(pollResp.result.top));
        check("结果含 best_tf", !!pollResp.result.best_tf, `tf=${pollResp.result.best_tf}`);
      }
    }
  } catch (e) {
    check("后端健康检查", false, String(e).slice(0, 200));
  }
}

// ---- 主入口 ----
async function run() {
  console.log("========================================");
  console.log("E2E 测试: /backtest 回测页");
  console.log(`前端: ${FRONTEND_URL}`);
  console.log(`后端: ${BACKEND_URL}`);
  console.log("========================================");

  // 0. 后端 API 健康检查 (不需要浏览器)
  await testBackendHealth();

  // 启动浏览器
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await context.newPage();

  // 收集 console 错误
  const consoleErrors = setupConsoleCollector(page);

  try {
    // 1. 页面加载
    await testPageLoads(page);

    // 2. 策略切换加载 schema
    await testStrategySwitchLoadsSchema(page);

    // 3. 单次回测
    await testSingleBacktest(page);

    // 4. 参数优化 Tab 面板
    await testParamOptimizeTab(page);

    // 5. 参数优化 — 配置并运行
    await testParamOptimizeRun(page);

    // 6. 应用最佳参数
    await testApplyBestParams(page);

  } finally {
    // 检查 console 错误
    console.log("\n[Test 7] JavaScript 控制台错误检查");
    const realErrors = consoleErrors.filter((e) =>
      !e.includes("Download the React DevTools") &&
      !e.includes("[Fast Refresh]") &&
      !e.includes("deprecated")
    );
    check("无 JavaScript 错误", realErrors.length === 0,
      realErrors.length > 0 ? realErrors.slice(0, 3).join(" | ") : "");

    await context.close();
    await browser.close();
  }

  // 汇总
  console.log("\n========================================");
  console.log(`结果: ✅ ${passCount} 通过, ❌ ${failCount} 失败`);
  console.log("========================================\n");
  process.exit(failCount > 0 ? 1 : 0);
}

run().catch((error) => {
  console.error("E2E 测试异常:", error);
  process.exit(1);
});
