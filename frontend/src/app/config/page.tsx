"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";

export default function ConfigPage() {
  const [cfg, setCfg] = useState<any>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => { api.config().then(setCfg).catch(console.error); }, []);

  const handleSave = async () => {
    await api.saveConfig(cfg);
    setSaved(true);
    setTimeout(() => setSaved(false), 2000);
  };

  if (!cfg) return <div className="text-[#666] p-8">加载中...</div>;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <h1 className="text-2xl font-bold">⚙️ 配置</h1>
        <button
          onClick={handleSave}
          className="px-4 py-1.5 bg-[#ff6d00] text-white text-sm rounded hover:bg-[#e65100]"
        >
          保存
        </button>
        {saved && <span className="text-[#26a69a] text-sm">✓ 已保存</span>}
      </div>

      {Object.entries(cfg.strategies ?? {}).map(([name, scfg]: [string, any]) => (
        <div key={name} className="border border-[#2a2a2a] rounded-lg bg-[#141414] p-4">
          <div className="flex items-center gap-3 mb-3">
            <h3 className="font-semibold">{name}</h3>
            <label className="flex items-center gap-1 text-sm text-[#888]">
              <input
                type="checkbox"
                checked={scfg.enabled}
                onChange={(e) => {
                  const next = { ...cfg, strategies: { ...cfg.strategies, [name]: { ...scfg, enabled: e.target.checked } } };
                  setCfg(next);
                }}
              />
              启用
            </label>
          </div>

          <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
            <div>
              <label className="block text-xs text-[#666] mb-1">标的 (逗号)</label>
              <input
                value={(scfg.symbols ?? []).join(",")}
                onChange={(e) => {
                  const next = { ...cfg, strategies: { ...cfg.strategies, [name]: { ...scfg, symbols: e.target.value.split(",").map((s: string) => s.trim()) } } };
                  setCfg(next);
                }}
                className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-full"
              />
            </div>
            {(scfg.live?.execute_time ?? true) !== undefined && (
              <div>
                <label className="block text-xs text-[#666] mb-1">执行时刻</label>
                <input
                  value={scfg.live?.execute_time ?? ""}
                  onChange={(e) => {
                    const next = { ...cfg, strategies: { ...cfg.strategies, [name]: { ...scfg, live: { ...scfg.live, execute_time: e.target.value } } } };
                    setCfg(next);
                  }}
                  className="bg-[#1a1a1a] border border-[#333] rounded px-2 py-1 w-full"
                />
              </div>
            )}
            <div>
              <label className="flex items-center gap-2 text-xs text-[#666]">
                <input
                  type="checkbox"
                  checked={scfg.live?.dry_run ?? true}
                  onChange={(e) => {
                    const next = { ...cfg, strategies: { ...cfg.strategies, [name]: { ...scfg, live: { ...scfg.live, dry_run: e.target.checked } } } };
                    setCfg(next);
                  }}
                />
                Dry-run (只模拟, 不下单)
              </label>
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
