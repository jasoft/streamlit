"use client";

// 自选股代码选择器: 输入框 + 自选股下拉.
// 用在任意需要选择/输入股票代码的地方: 保留手动输入任意代码的能力,
// 聚焦时可从自选股 (代码/名称模糊过滤) 里直接选.
import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useWatchlist, type WatchStock } from "@/lib/watchlist";

type Props = {
  value?: string;              // replace 模式受控值
  onChange: (v: string) => void;
  mode?: "replace" | "append"; // append: 选中追加到逗号分隔列表, 输入框为独立搜索框
  placeholder?: string;
  width?: string;              // 输入框宽度类, 默认 w-36
  wrapClassName?: string;      // 外层定位容器附加类
  inputClassName?: string;     // 完全替换默认输入框样式时使用
  extraSymbols?: string[];     // 自选股之外的兜底常用列表 (如 charts 常用标的)
  invalid?: boolean;           // 红色边框 (外部校验失败)
  onKeyDown?: (e: React.KeyboardEvent<HTMLInputElement>) => void;
};

const DEFAULT_INPUT_CLS =
  "bg-[#1a1a1a] border rounded px-2 py-1 font-mono text-sm text-white placeholder-[#555] outline-none focus:border-[#4fc3f7]";

export default function SymbolPicker({
  value, onChange, mode = "replace", placeholder, width = "w-36",
  wrapClassName = "", inputClassName, extraSymbols, invalid, onKeyDown,
}: Props) {
  const { data } = useWatchlist();
  const stocks = data?.stocks ?? [];
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");          // append 模式的独立搜索词
  const boxRef = useRef<HTMLDivElement>(null);

  const inputValue = mode === "replace" ? (value ?? "") : text;
  const needle = inputValue.trim().toLowerCase();

  // 点击组件外部收起下拉
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const matched = useMemo(() => {
    const list = needle
      ? stocks.filter((s: WatchStock) =>
          s.symbol.toLowerCase().includes(needle) ||
          (s.code && s.code.includes(needle)) ||
          (s.name ?? "").toLowerCase().includes(needle))
      : stocks;
    return list.slice(0, 50);
  }, [stocks, needle]);

  const extras = useMemo(() =>
    (extraSymbols ?? []).filter(
      (s) => !stocks.some((st) => st.symbol === s)),
    [extraSymbols, stocks]);

  const pick = (sym: string) => {
    if (mode === "append") {
      const cur = (value ?? "").trim().replace(/,\s*$/, "");
      onChange(cur ? `${cur},${sym}` : sym);
      setText("");
    } else {
      onChange(sym);
    }
    setOpen(false);
  };

  const cls = inputClassName ?? `${DEFAULT_INPUT_CLS} ${width} ${
    invalid ? "border-[#ef5350]" : "border-[#333]"
  }`;

  return (
    <div ref={boxRef} className={`relative ${wrapClassName}`}>
      <input
        value={inputValue}
        onChange={(e) => {
          if (mode === "replace") onChange(e.target.value);
          else setText(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={(e) => {
          if (e.key === "Escape") { setOpen(false); return; }
          onKeyDown?.(e);
        }}
        placeholder={placeholder ?? (mode === "append" ? "搜索自选股追加" : "601899 / sz159915")}
        className={cls}
      />
      {open && (
        <div className="absolute left-0 top-full mt-1 z-50 w-64 max-h-60 overflow-y-auto
                        bg-[#1a1a1a] border border-[#2a2a2a] rounded shadow-lg shadow-black/50">
          {matched.length === 0 && extras.length === 0 ? (
            <div className="px-3 py-2 text-xs text-[#666]">
              {needle ? "无匹配的自选股" : "自选股为空"}
              <Link href="/watchlist" className="ml-1 text-[#ff6d00] hover:underline"
                onClick={() => setOpen(false)}>
                去 ⭐自选股 页添加
              </Link>
            </div>
          ) : (
            <>
              {matched.length > 0 && (
                <>
                  <div className="px-3 pt-1.5 pb-0.5 text-[10px] text-[#666]">
                    ⭐ 自选股
                  </div>
                  {matched.map((s) => (
                    <button key={s.symbol} type="button"
                      onMouseDown={(e) => { e.preventDefault(); pick(s.symbol); }}
                      className="w-full flex items-center justify-between gap-2 px-3 py-1.5
                                 text-left text-xs hover:bg-[#262626]">
                      <span className="font-mono text-white">{s.code || s.symbol}</span>
                      <span className="flex items-center gap-1.5 min-w-0">
                        {s.source === "ths" && (
                          <span className="text-[10px] text-[#ffb74d] shrink-0">持仓</span>
                        )}
                        <span className="text-[#888] truncate">{s.name}</span>
                      </span>
                    </button>
                  ))}
                </>
              )}
              {extras.length > 0 && (
                <>
                  <div className="px-3 pt-1.5 pb-0.5 text-[10px] text-[#666]">常用</div>
                  {extras.map((s) => (
                    <button key={s} type="button"
                      onMouseDown={(e) => { e.preventDefault(); pick(s); }}
                      className="w-full flex items-center px-3 py-1.5 text-left text-xs
                                 font-mono text-white hover:bg-[#262626]">
                      {s}
                    </button>
                  ))}
                </>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
