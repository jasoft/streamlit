# frontend/ — Next.js 15 前端

量化交易系统主 UI，从 Streamlit 看板重写而来。访问 http://localhost:3001 → /charts。

> 完整手册见项目根目录 `SKILL.md` 第五/十一/十二章。

---

## 快速开始

```bash
cd frontend
npm install          # 首次安装依赖
cd ..
./dev.sh             # 一键启后端 (:8000) + 前端 (:3001), Ctrl-C 同停
# 访问 http://localhost:3001 → 自动跳 /charts
```

自定义端口：
```bash
BACKEND_PORT=8001 FRONTEND_PORT=3002 ./dev.sh
```

**启动前请确认后端 FastAPI（backend/main.py）在运行，否则 /api/* 和 /ws/* 全部 404**。

---

## 页面结构 & 路由

| 路径 | 文件 | 说明 |
|---|---|---|
| `/` | `app/page.tsx` | 根路径 → **重定向 /charts**（已删除的 /live 绝不跳转） |
| `/charts` | `app/charts/page.tsx` | ★ 主入口：多图会话管理 + 策略挂载 + 流式测试 + 账户快照 |
| `/backtest` | `app/backtest/page.tsx` | 批量回测多策略多标的 + 参数网格优化 |
| `/config` | `app/config/page.tsx` | 策略参数在线编辑（schema 自动生成控件，写 strategy/config.json） |

---

## 主要组件

### `components/KLineChart.tsx` — K线图（ECharts 6）

- **拖拽缩放**：`dataZoom` 支持左右拖动 + 滚轮/框选放大缩小；K线图与分时图交互状态独立（缩放 K 线不影响分时）
- **双轨日期 marker 匹配**（挂载策略买卖点必对）：
  - 分钟级：精确字符串匹配 `date === "2026-09-02 14:55:00"`，用于实盘/流式 marker
  - 日级：前缀匹配 `date.startsWith("2026-09-02")`，用于日线回测 marker
  - 两种都要支持，否则总有一类 marker 不显示
- **Tooltip 从渲染闭包数据数组取值**：不要用 ECharts 的 `p.data`（会错位），从实际保存的 `ohlc` / `ma` / `vols` 数组按下标取
- **Canvas 只初始化一次**：用 `setOption` 增量更新避免闪烁

### `components/IntradayChart.tsx` — 分时图

三条核心元素：
- **白线**：价格收盘价（每分钟 close）
- **黄线 VWAP**：均价 = 累计成交额 / 累计成交量，**随时间推进同步绘制，不预先画完整直线**（流式测试尤其注意）
- **量柱**：按涨跌染色（涨=红，跌=绿）

显示完整交易时段区域：09:31–11:30 / 13:01–15:00，哪怕当前只有上午数据，也要画出下午的空时间格子（时间占满，不预填价格）。非交易时段（当日分钟数=0）自动取上一完整交易日的分时数据。

### `components/ParamForm.tsx` — 参数表单

由策略 schema（`/api/strategies/{name}/schema` 返回）自动生成控件：
- `INT` 参数：数字输入 + min/max 限制
- `FLOAT` 参数：数字输入 + 步长
- `STRING` / `SELECT`：文本/下拉
- 支持中文标签（`paramCn(key)`）

---

## 核心库（src/lib/）

### `lib/fmt.ts` — 全局数字格式化（**全站必须使用，禁止直接 toFixed()**）

保证所有页面数字显示一致：

```typescript
import { fmtPrice, fmtMoney, fmtPct, fmtNum, paramCn, statCn, formatStat } from "@/lib/fmt";

fmtPrice(3.567);         // "3.567"   股价<10元 三位小数
fmtPrice(12.3);          // "12.3"    股价≥10元 两位小数, 自动去尾零
fmtMoney(1234567.89);    // "1,234,567.89"  金额千分位, 最多两位小数
fmtPct(0.05234);         // "5.23%"   小数比率 → 乘 100 → 百分比
fmtPct(5.234, false);    // "5.23%"   已是百分数, 不乘 100
fmtNum(123.456);         // "123.46"  通用数字, 最多两位小数

paramCn("window");       // "均线窗口"     参数英文名 → 中文
statCn("total_return");  // "总收益"       统计英文名 → 中文
formatStat("total_return", 2.023);  // "202.3%"   识别统计类型自动格式化
```

### `lib/api.ts` — REST API 封装

```typescript
import { getHealth, getKline, getIntraday, postBacktest, getPositions, ... } from "@/lib/api";
```

全部走 `fetch()`，相对路径（`/api/xxx`）→ next.config.ts rewrite → FastAPI :8000。自动 parse JSON，错误抛 `Error` 含后端 message。

### `lib/ws.ts` — WebSocket 管理（两个单例）

#### `MarketWs` — 实盘 tick 推送，连接 `/ws/market`

```typescript
import { MarketWs } from "@/lib/ws";
MarketWs.subscribe("sz159915", (msg) => {
  // msg: {type:"tick", symbol, snapshot:{last,pre_close,...}, bars:[最后1-2根bar], source}
  // bars 只含最新 1-2 根 (增量), 前端 appendData 合并到已有 bars
});
MarketWs.unsubscribe("sz159915");
```

- **单例全局共享**：多页面/多组件订阅不重复建连接
- **自动重连**：断开 1.5s 后自动重连，重连时重新订阅已注册 symbol
- Query 传 `?symbols=sz159915,sh510300,sh000001`，后端自动扩展全局 tick 循环覆盖列表，无需重启

#### `MockStreamWs` — 流式逐根测试，连接 `/ws/mock_stream`

```typescript
import { MockStreamWs } from "@/lib/ws";
const ws = new MockStreamWs({
  symbol: "sz159915",
  strategy: "ma20_trend",
  tf: "5m",
  speed: "1x",           // 1x=5s / 2x=2.5s / 5x=1s / 10x=0.5s / 20x=0.25s 每bar
  params: { window: 20 },
  onInfo: (info) => { /* {type:"info", pre_close, msg} 初始化昨收参考轴 */ },
  onBar:  (data) => {
    // {type:"bar", bar, orders:[...], markers:[...], snapshot, target}
    // 逐根推进: 更新图表 + 应用 orders + 画 markers + 更新持仓快照
  },
  onDone:  () => { /* 流停止时: 还原 preStreamStateRef → 返回原视图 */ },
  onError: (err) => {},
});
ws.stop();   // 用户点"结束流式"
```

- **不自动重连**：流式测试是一次性的，停止即结束
- **preStreamStateRef**：开流式前保存当前图表/状态，结束后还原 → 用户看到的是"跑实验前的状态"，不是残留的流式结果

---

## 前后端通信（next.config.ts rewrite）

```typescript
// next.config.ts
async rewrites() { return [
  { source: "/api/:path*",         destination: "http://localhost:8000/api/:path*" },
  { source: "/ws/market",          destination: "ws://localhost:8000/ws/market" },
  { source: "/ws/mock_stream",     destination: "ws://localhost:8000/ws/mock_stream" },
]; }
```

- 前端所有请求都是同域相对路径（`/api/xxx` / `/ws/market`），**无 CORS 问题**
- 改后端端口时要同步改 next.config.ts（或用 BACKEND_PORT 环境变量）
- **三条规则不能少**，缺任何一条对应功能就 404

---

## 核心后端 REST 端点（给前端用）

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/kline?symbol=sz159915&tf=day&limit=3&qfq=true` | K线 OHLCV |
| GET | `/api/intraday/sz159915` | 当日分时 1m bars + VWAP + MACD + 昨收 |
| GET | `/api/quote/sz159915` | 实时快照 |
| POST | `/api/backtest` | vectorbt 回测（挂载策略调用） |
| POST | `/api/optimize` | 参数网格搜索 |
| GET | `/api/strategies` | 策略列表 |
| GET | `/api/strategies/ma20_trend/schema` | 策略参数 schema（ParamForm 用） |
| GET | `/api/positions` | 同花顺实际持仓（subprocess 调 ths_trade） |
| GET | `/api/charts/sessions` | 读图会话元数据（SQLite） |
| PUT | `/api/charts/sessions` | 保存图会话（防抖 800ms） |

---

## 前端硬约束 & 常见坑

### 1. ECharts 初始化（KLineChart + IntradayChart）

**React StrictMode 下 useEffect 会 mount→unmount→mount 两次执行**。必须：

```typescript
useEffect(() => {
  chartRef.current = echarts.init(chartDom, ...); // 初始化一次
  // ... setOption, 绑定事件 ...
  return () => {
    chartRef.current?.dispose();
    chartRef.current = null;   // ★ 必须置 null! 否则二次 mount 用旧实例 → 闪烁/报错
  };
}, []);
```

### 2. ECharts merge 模式不要传空 series

错误 ❌：
```typescript
chartRef.current.setOption({ series: [
  { name: "MA5", type: "line", data: [] },   // 空 data 会导致 ECharts 创建无 type/name 的新 series
]}, { merge: true });
```

正确 ✓：
```typescript
const series = [
  ma5.length > 0 && { name: "MA5", type: "line", data: ma5 },
  ma10.length > 0 && { name: "MA10", type: "line", data: ma10 },
].filter(Boolean);
chartRef.current.setOption({ series }, { merge: true });
```

### 3. 图表更新用增量 setOption，不重建实例

任何时候不要 `dispose() + init()` 更新数据。所有 UI 更新（新 bars、挂载 markers、流式推进）都只用：
```typescript
chartRef.current.setOption({ series: updatedSeries, xAxis: {...}, yAxis: {...} }, { merge: true });
```

### 4. 数字显示必须用 fmt.ts

不要写 `value.toFixed(2)`。写 `fmtPrice(value)` / `fmtMoney(value)` / `fmtPct(value)` 对应场景。否则同样的数据在不同页面会显示成不一样的位数。

### 5. 图会话保存是防抖 + 只存元数据（不含 bars/markers 大数据）

- `charts/page.tsx` 中 `saveSessionsDebounced` 防抖 800ms，用户停止操作 800ms 才 PUT `/api/charts/sessions`
- 只保存 {id, symbol, tf, strategy, params, chartType, x, y, w, h} 等元数据，bars/markers 下次打开重新拉
- SQLite 在 `backend/charts.db`，跨浏览器/设备/刷新恢复

### 6. 账户快照（/api/positions）30s 自动刷新 + 手动刷新按钮

rows 可能是对象（ths_trade 返回的 rows 有时是对象不是数组），前端要加运行时类型检查：
```typescript
const normalized = rows.map(r => Array.isArray(r) ? r : Object.values(r));
```

---

## 目录结构

```
frontend/
├── next.config.ts          rewrite: /api/*→:8000, /ws/*→:8000
├── package.json            依赖 (npm install)
├── tsconfig.json / tailwind.config.ts
└── src/
    ├── app/
    │   ├── page.tsx              / → 重定向 /charts
    │   ├── charts/page.tsx       ★ 主页面: 多图会话 + 挂载策略 + 流式 + 账户
    │   ├── backtest/page.tsx     批量回测
    │   └── config/page.tsx       策略参数配置
    ├── components/
    │   ├── KLineChart.tsx        ★ K线 (拖拽缩放/双轨日期marker/增量setOption)
    │   ├── IntradayChart.tsx     ★ 分时 (白价格+黄VWAP+染色量柱/完整时段)
    │   ├── ParamForm.tsx         schema 驱动参数表单
    │   ├── AccountSnapshot.tsx   同花顺账户快照 (30s 自动刷新)
    │   └── ...
    └── lib/
        ├── api.ts                REST API 封装 (fetch, 同域 rewrite)
        ├── ws.ts                 ★ MarketWs (tick推送/自动重连) + MockStreamWs (流式)
        └── fmt.ts                ★ 全局数字格式化 + 中英文转换
```

---

## 更多文档

- 根目录 `SKILL.md` 第五章（前端系统详解）+ 第十一章（常见 Bug 排查前端图表分类）
- `backend/main.py` 注释：每个端点上方都有功能说明和返回结构
- `strategy/strategies/*.py` 策略模板：signal() 写法参考
