#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import akshare as ak
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IWENCAI_SCRIPT = PROJECT_ROOT / "skills" / "financial-data" / "scripts" / "cli_index.py"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "hs300_industry_weight"
DEFAULT_QUERY = "沪深300成分股 所属同花顺一级行业 总市值"


@dataclass(frozen=True)
class IndustryWeightResult:
    summary: pd.DataFrame
    holdings: pd.DataFrame
    metadata: dict[str, Any]


def normalize_stock_code(value: Any) -> str | None:
    match = re.search(r"(\d{6})", str(value))
    return match.group(1) if match else None


def find_market_cap_column(df: pd.DataFrame) -> str:
    candidates = [column for column in df.columns if column.startswith("总市值[")]
    if candidates:
        return candidates[0]
    if "总市值" in df.columns:
        return "总市值"
    raise ValueError(f"问财结果缺少总市值字段，实际字段: {list(df.columns)}")


def extract_bracket_date(column_name: str) -> str | None:
    match = re.search(r"\[(\d{8})\]", column_name)
    return match.group(1) if match else None


def fetch_csindex_weights(index_symbol: str = "000300") -> pd.DataFrame:
    df = ak.index_stock_cons_weight_csindex(symbol=index_symbol)
    required = {"日期", "成分券代码", "成分券名称", "权重"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"中证权重结果缺少字段: {sorted(missing)}")
    return df


def fetch_iwencai_industry_records(
    query: str = DEFAULT_QUERY,
    limit: int = 300,
    timeout: int = 45,
) -> dict[str, Any]:
    if not IWENCAI_SCRIPT.exists():
        raise FileNotFoundError(f"找不到问财脚本: {IWENCAI_SCRIPT}")

    completed = subprocess.run(
        [
            sys.executable,
            str(IWENCAI_SCRIPT),
            "--query",
            query,
            "--limit",
            str(limit),
            "--timeout",
            str(timeout),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip())

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"问财返回不是合法 JSON: {completed.stdout[:500]}") from exc

    if not payload.get("success"):
        raise RuntimeError(f"问财查询失败: {payload}")
    if not payload.get("datas"):
        raise RuntimeError("问财查询成功但没有返回成分股数据")
    return payload


def normalize_industry_dataframe(payload: dict[str, Any]) -> tuple[pd.DataFrame, str | None]:
    df = pd.DataFrame(payload["datas"])
    required = {"股票代码", "股票简称", "所属同花顺一级行业"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"问财结果缺少字段: {sorted(missing)}")

    market_cap_column = find_market_cap_column(df)
    df = df.copy()
    df["code6"] = df["股票代码"].map(normalize_stock_code)
    df["总市值"] = pd.to_numeric(df[market_cap_column], errors="coerce")
    return df[["code6", "股票代码", "股票简称", "所属同花顺一级行业", "总市值"]], extract_bracket_date(market_cap_column)


def calculate_industry_weights(
    weights_df: pd.DataFrame,
    industry_df: pd.DataFrame,
    *,
    industry_date: str | None = None,
) -> IndustryWeightResult:
    weights = weights_df.copy()
    weights["code6"] = weights["成分券代码"].map(normalize_stock_code)
    weights["权重"] = pd.to_numeric(weights["权重"], errors="coerce")

    merged = weights.merge(
        industry_df[["code6", "股票代码", "股票简称", "所属同花顺一级行业", "总市值"]],
        on="code6",
        how="left",
        suffixes=("", "_iwencai"),
    )
    missing = merged[merged["所属同花顺一级行业"].isna()][["成分券代码", "成分券名称", "权重"]]
    if not missing.empty:
        names = ", ".join(missing["成分券名称"].astype(str).head(10))
        raise RuntimeError(f"有 {len(missing)} 只成分股未匹配行业: {names}")

    summary = (
        merged.groupby("所属同花顺一级行业", dropna=False)
        .agg(
            指数权重占比=("权重", "sum"),
            成分数=("code6", "count"),
            总市值=("总市值", "sum"),
        )
        .reset_index()
        .sort_values("指数权重占比", ascending=False)
    )
    total_market_cap = summary["总市值"].sum()
    summary["市值占比"] = summary["总市值"] / total_market_cap * 100 if total_market_cap else 0
    summary["指数权重占比"] = summary["指数权重占比"].round(3)
    summary["市值占比"] = summary["市值占比"].round(3)

    weight_dates = sorted(str(value) for value in weights["日期"].dropna().unique())
    metadata = {
        "index_symbol": str(weights["指数代码"].iloc[0]) if "指数代码" in weights.columns and not weights.empty else "000300",
        "index_name": str(weights["指数名称"].iloc[0]) if "指数名称" in weights.columns and not weights.empty else "沪深300",
        "weight_date": weight_dates[-1] if weight_dates else None,
        "industry_date": industry_date,
        "constituent_count": int(len(merged)),
        "matched_count": int(merged["所属同花顺一级行业"].notna().sum()),
        "weight_sum": round(float(weights["权重"].sum()), 3),
        "industry_classification": "同花顺一级行业",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    return IndustryWeightResult(summary=summary, holdings=merged, metadata=metadata)


def run_analysis(output_dir: str | Path = DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    weights = fetch_csindex_weights("000300")
    iwencai_payload = fetch_iwencai_industry_records(DEFAULT_QUERY, limit=300)
    industry_df, industry_date = normalize_industry_dataframe(iwencai_payload)
    result = calculate_industry_weights(weights, industry_df, industry_date=industry_date)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    result.summary.to_csv(output_path / "hs300_industry_weight_summary.csv", index=False)
    result.holdings.to_csv(output_path / "hs300_industry_weight_holdings.csv", index=False)
    (output_path / "hs300_industry_weight_metadata.json").write_text(
        json.dumps(result.metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return {
        "summary": result.summary,
        "holdings": result.holdings,
        "metadata": result.metadata,
        "output_dir": str(output_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="计算沪深300成分股行业权重占比")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="输出目录")
    parser.add_argument(
        "--format",
        choices=["table", "json", "csv"],
        default="table",
        help="标准输出格式",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_analysis(args.output_dir)
    summary = result["summary"]
    metadata = result["metadata"]

    if args.format == "json":
        print(
            json.dumps(
                {
                    "metadata": metadata,
                    "summary": summary.to_dict(orient="records"),
                    "output_dir": result["output_dir"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.format == "csv":
        print(summary.to_csv(index=False), end="")
    else:
        print(
            f"{metadata['index_name']}行业权重 | 权重日期 {metadata['weight_date']} | "
            f"行业日期 {metadata['industry_date']} | 成分股 {metadata['matched_count']}/{metadata['constituent_count']}"
        )
        print(summary.to_string(index=False))
        print(f"输出目录: {result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
