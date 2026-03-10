const assert = require("node:assert/strict");
const { chromium } = require("playwright");

const TARGET_URL = process.env.STOCKVIEW_URL || "http://127.0.0.1:8502";
const ERROR_PATTERNS = [
  "Traceback",
  "ModuleNotFoundError",
  "StreamlitSetPageConfigMustBeFirstCommandError",
  "ImportError",
];

async function readBody(page) {
  return page.locator("body").innerText();
}

function assertNoAppError(text, label) {
  for (const pattern of ERROR_PATTERNS) {
    assert.ok(!text.includes(pattern), `${label} 页面出现错误: ${pattern}`);
  }
}

async function openSidebarPage(page, label, expectedText, timeout = 120000) {
  await page.getByText(label, { exact: true }).click();
  await page.getByText(expectedText, { exact: false }).waitFor({ timeout });
  await page.waitForTimeout(1000);
  const text = await readBody(page);
  assertNoAppError(text, label);
  return text;
}

async function run() {
  const browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1600, height: 900 } });
  const page = await context.newPage();

  try {
    await page.goto(TARGET_URL, { waitUntil: "domcontentloaded", timeout: 60000 });
    await page.waitForTimeout(3000);

    const checks = [];

    let text = await openSidebarPage(page, "综合面板", "预估成交额", 120000);
    checks.push({ page: "综合面板", ok: true, marker: "预估成交额" });

    await page.getByText("🏢 龙头股分析", { exact: true }).click();
    await page.getByText("龙头股活跃度分析", { exact: false }).waitFor({ timeout: 120000 });
    text = await readBody(page);
    assertNoAppError(text, "综合面板/龙头股分析");
    checks.push({ page: "龙头股分析", ok: true, marker: "龙头股活跃度分析" });

    await page.getByText("📊 指数对比", { exact: true }).click();
    await page.getByText("指数40日收益差分析", { exact: false }).waitFor({ timeout: 120000 });
    text = await readBody(page);
    assertNoAppError(text, "综合面板/指数对比");
    checks.push({ page: "指数对比", ok: true, marker: "指数40日收益差分析" });

    await openSidebarPage(page, "IF-IM 风格配对", "历史相似状态 vs 基线", 180000);
    checks.push({ page: "IF-IM 风格配对", ok: true, marker: "历史相似状态 vs 基线" });

    await openSidebarPage(page, "指数成交额风格对比", "各分组命中率", 180000);
    checks.push({ page: "指数成交额风格对比", ok: true, marker: "各分组命中率" });

    await openSidebarPage(page, "市场拥挤度", "原始数据", 180000);
    checks.push({ page: "市场拥挤度", ok: true, marker: "原始数据" });

    await openSidebarPage(page, "创业板成交占比", "最新创业板成交占比", 180000);
    checks.push({ page: "创业板成交占比", ok: true, marker: "最新创业板成交占比" });

    await openSidebarPage(page, "使用说明", "统一入口", 60000);
    checks.push({ page: "使用说明", ok: true, marker: "统一入口" });

    console.log(JSON.stringify({ target: TARGET_URL, checks }, null, 2));
  } finally {
    await context.close();
    await browser.close();
  }
}

run().catch((error) => {
  console.error(error);
  process.exit(1);
});
