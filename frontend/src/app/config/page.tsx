"use client";

import { useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

// 成本大类 key: stock / futures / options
type CostCat = "stock" | "futures" | "options";
type TradeCosts = Record<CostCat, {
  buy_fee: number;
  sell_fee: number;
  sell_stamp_duty: number;
  slippage: number;
}>;

const CATS: { key: CostCat; label: string; badge: string; hint: string }[] = [
  {
    key: "stock",
    label: "📈 股票 / ETF / 指数",
    badge: "A股 · sh/sz/bj 开头",
    hint: "默认：买入万1，卖出万1 + 印花税千1（合计单次卖出约千1.1）",
  },
  {
    key: "futures",
    label: "🥇 期货",
    badge: "合约代码 (if/rb/ic 等)",
    hint: "默认：买卖各万0.2，无印花税，滑点万1（按券商实际情况自调）",
  },
  {
    key: "options",
    label: "📊 期权",
    badge: "暂复用股票配置",
    hint: "当前只支持股票类期权，默认费率同股票；如需单独设置后可扩展",
  },
];

const DEFAULT_COSTS: TradeCosts = {
  stock: { buy_fee: 0.0001, sell_fee: 0.0001, sell_stamp_duty: 0.001, slippage: 0.0001 },
  futures: { buy_fee: 0.00002, sell_fee: 0.00002, sell_stamp_duty: 0, slippage: 0.0001 },
  options: { buy_fee: 0.0001, sell_fee: 0.0001, sell_stamp_duty: 0, slippage: 0.0001 },
};

// ---------- 小工具：小数 <-> 百分比 / 万分比 显示 ----------
const fmtPct = (v: number) => `${(v * 100).toFixed(4)}%`;
const fmtWan = (v: number) => `万${(v * 10000).toFixed(2)}`;
const roundtripCost = (c: { buy_fee: number; sell_fee: number; sell_stamp_duty: number; slippage: number }) =>
  c.buy_fee + c.sell_fee + c.sell_stamp_duty + 2 * c.slippage;

export default function ConfigPage() {
  const [cfg, setCfg] = useState<any>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState<CostCat>("stock");
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    api.config()
      .then((c) => {
        const tc = { ...DEFAULT_COSTS, ...(c.trade_costs || {}) } as any;
        (Object.keys(DEFAULT_COSTS) as CostCat[]).forEach((k) => {
          tc[k] = { ...DEFAULT_COSTS[k], ...(tc[k] || {}) };
        });
        // 保留 strategies 字段 (后端会传, PUT 回去时要完整, 但此页面不再展示)
        setCfg({ strategies: c.strategies || {}, trade_costs: tc });
      })
      .catch((e) => setErr(e?.message ?? String(e)));
  }, []);

  if (err) {
    return (
      <div className="p-8 space-y-2">
        <h1 className="text-2xl font-bold">⚙️ 配置</h1>
        <div className="text-sm text-[#f44336] bg-[#2a0808] border border-[#7f1d1d] rounded p-3 whitespace-pre-wrap">
          加载配置失败：{err}
        </div>
      </div>
    );
  }
  if (!cfg) return <div className="text-[#666] p-8">加载中...</div>;

  const tradeCosts: TradeCosts = cfg.trade_costs || DEFAULT_COSTS;

  const updateCost = (cat: CostCat, field: keyof TradeCosts[CostCat], raw: string) => {
    const n = parseFloat(raw);
    const val = Number.isFinite(n) ? Math.max(0, n) : 0;
    const nextCat = { ...tradeCosts[cat], [field]: val };
    setCfg({ ...cfg, trade_costs: { ...tradeCosts, [cat]: nextCat } });
  };

  const resetDefaults = () => {
    setCfg({ ...cfg, trade_costs: JSON.parse(JSON.stringify(DEFAULT_COSTS)) });
  };

  const handleSave = async () => {
    setSaving(true);
    setSaved(false);
    try {
      await api.saveConfig(cfg);
      setSaved(true);
      setTimeout(() => setSaved(false), 2200);
    } catch (e: any) {
      alert("保存失败:\n" + (e?.message ?? String(e)));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-6">
      {/* ============== 顶部：标题 + 保存 ============== */}
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">⚙️ 系统配置</h1>
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-5 py-1.5 bg-[#ff6d00] text-white text-sm rounded hover:bg-[#e65100] disabled:opacity-50"
        >
          {saving ? "保存中..." : "💾 保存全部"}
        </button>
        <button
          onClick={resetDefaults}
          className="px-4 py-1.5 bg-[#2a2a2a] text-[#ccc] text-sm rounded hover:bg-[#3a3a3a]"
          title="重置交易成本为默认值"
        >
          ↺ 重置成本默认值
        </button>
        {saved && <span className="text-[#26a69a] text-sm animate-pulse">✓ 已保存到 config.json</span>}
      </div>

      {/* ============== 唯一 Section: 交易成本 ============== */}
      <section className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-5 space-y-4">
        <div>
          <h2 className="text-lg font-semibold">💰 交易成本（手续费 / 滑点 / 印花税）</h2>
          <p className="text-xs text-[#888] mt-1">
            回测引擎会根据标的代码自动识别大类（股票/期货/期权），并套用对应成本。
            输入值为<strong className="text-[#ffd54f]"> 成交金额比例 </strong>
            （如 0.0001 = 万分之一），右侧会实时换算成常见表述。
          </p>
          <p className="text-xs text-[#555] mt-1">
            🧭 实盘策略配置已迁移到「图会话 /charts」页内：每图独立选择标的、周期、策略参数。
            每张图顶部有 <code className="text-[#ef5350] bg-[#222] px-1 rounded">实盘</code> 开关，
            勾选即下真实单，未勾选为纸面模拟。
          </p>
        </div>

        {/* Tab 切换 */}
        <div className="flex gap-2 border-b border-[#2a2a2a]">
          {CATS.map((c) => (
            <button
              key={c.key}
              onClick={() => setTab(c.key)}
              className={`px-4 py-2 text-sm -mb-px transition ${
                tab === c.key
                  ? "border-b-2 border-[#ff6d00] text-[#ff6d00] font-semibold"
                  : "text-[#888] hover:text-white"
              }`}
            >
              {c.label}
              <span className="ml-2 text-[10px] opacity-70">[{c.badge}]</span>
            </button>
          ))}
        </div>

        {/* Tab 内容 */}
        {CATS.map((c) => (
          <CostCategoryPanel
            key={c.key}
            hidden={tab !== c.key}
            values={tradeCosts[c.key]}
            hint={c.hint}
            onChange={(field, raw) => updateCost(c.key, field, raw)}
          />
        ))}

        {/* 汇总对比 */}
        <div className="pt-3 border-t border-[#2a2a2a] grid grid-cols-1 md:grid-cols-3 gap-3">
          {CATS.map((c) => {
            const v = tradeCosts[c.key];
            const total = roundtripCost(v);
            return (
              <div key={c.key} className="bg-[#0f0f0f] border border-[#2a2a2a] rounded p-3">
                <div className="text-xs text-[#888]">{c.label.split(" ")[1] || c.key} · 完整一买一卖成本</div>
                <div className="text-lg font-mono text-[#ffd54f] mt-1">
                  {fmtPct(total)} <span className="text-[#666] text-sm">({fmtWan(total)})</span>
                </div>
                <div className="text-[11px] text-[#666] mt-1">
                  买入 {fmtPct(v.buy_fee + v.slippage)} · 卖出 {fmtPct(v.sell_fee + v.sell_stamp_duty + v.slippage)}
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}

/* =================== 单大类 成本配置面板 =================== */
function CostCategoryPanel(props: {
  hidden: boolean;
  hint: string;
  values: TradeCosts[CostCat];
  onChange: (field: keyof TradeCosts[CostCat], raw: string) => void;
}) {
  const { hidden, hint, values, onChange } = props;
  if (hidden) return null;

  const fields: { key: keyof TradeCosts[CostCat]; label: string; desc: string }[] = [
    { key: "buy_fee", label: "买入手续费", desc: "券商佣金（买入单边），通常 万1 ~ 万3" },
    { key: "sell_fee", label: "卖出手续费", desc: "券商佣金（卖出单边）" },
    { key: "sell_stamp_duty", label: "卖出印花税", desc: "仅卖出单边；A 股目前千 1；期货 0" },
    { key: "slippage", label: "滑点", desc: "成交价与理论价的偏差比例；双向各加一次" },
  ];

  return (
    <div className="space-y-3">
      <div className="text-xs text-[#888] bg-[#0f0f0f] rounded px-3 py-2 border border-[#1f1f1f]">
        💡 {hint}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        {fields.map((f) => (
          <CostNumberField
            key={f.key}
            label={f.label}
            desc={f.desc}
            value={values[f.key]}
            onChange={(raw) => onChange(f.key, raw)}
          />
        ))}
      </div>
    </div>
  );
}

/* =================== 单数值输入框（带换算提示） =================== */
function CostNumberField(props: {
  label: string;
  desc: string;
  value: number;
  onChange: (raw: string) => void;
}) {
  const { label, desc, value, onChange } = props;
  const rawStr = useMemo(() => parseFloat(value.toFixed(8)).toString(), [value]);
  return (
    <div className="bg-[#0f0f0f] border border-[#2a2a2a] rounded p-3 space-y-2">
      <div>
        <div className="text-sm font-medium">{label}</div>
        <div className="text-[11px] text-[#666] leading-tight">{desc}</div>
      </div>
      <div className="flex items-center gap-2">
        <input
          type="number"
          step="0.00001"
          min="0"
          value={rawStr}
          onChange={(e) => onChange(e.target.value)}
          className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-full font-mono text-sm"
        />
      </div>
      <div className="text-[11px] text-[#888] font-mono flex justify-between">
        <span className="text-[#90caf9]">{fmtPct(value)}</span>
        <span className="text-[#ffd54f]">{fmtWan(value)}</span>
      </div>
    </div>
  );
}
