"use client";

import { paramCn } from "@/lib/fmt";

export type ParamSpec = {
  type: "int" | "float" | "const";
  default?: number;
  min?: number | null;
  max?: number | null;
  value?: number; // const 类型
};

export type ParamSchema = Record<string, ParamSpec>;

type Props = {
  schema: ParamSchema;
  params: Record<string, number>;
  onChange: (params: Record<string, number>) => void;
  disabled?: boolean;
};

/** 根据 /api/strategies/{name}/schema 动态生成参数控件.
 * int/float -> number input (min/max 约束), const -> 只读显示.
 */
export default function ParamForm({ schema, params, onChange, disabled }: Props) {
  return (
    <div className="grid grid-cols-2 gap-3">
      {Object.entries(schema).map(([key, spec]) => {
        if (spec.type === "const") {
          return (
            <div key={key} className="flex flex-col gap-1">
              <label className="text-xs text-[#888]">{paramCn(key)}</label>
              <div className="text-sm text-[#666] font-mono py-1">{spec.value}</div>
            </div>
          );
        }
        const step = spec.type === "int" ? 1 : 0.1;
        const val = params[key] ?? spec.default ?? 0;
        return (
          <div key={key} className="flex flex-col gap-1">
            <label className="text-xs text-[#888]">
              {paramCn(key)}
              {spec.min != null && spec.max != null && (
                <span className="text-[#666] ml-1">({spec.min}-{spec.max})</span>
              )}
            </label>
            <input
              type="number"
              step={step}
              min={spec.min ?? undefined}
              max={spec.max ?? undefined}
              value={val}
              disabled={disabled}
              onChange={(e) =>
                onChange({ ...params, [key]: parseFloat(e.target.value) || 0 })
              }
              className="bg-[#1a1a1a] border border-[#2a2a2a] rounded px-2 py-1 text-sm text-[#e0e0e0] focus:border-[#4fc3f7] outline-none disabled:opacity-50"
            />
          </div>
        );
      })}
    </div>
  );
}
