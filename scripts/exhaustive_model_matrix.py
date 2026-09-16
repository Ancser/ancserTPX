"""Research-only aggregation for the complete ancserTPX model audit.

This module does not run the trading engine and does not mutate production
code, presets, live state, or market-data stores.  It collects the already
completed research artifacts into one contract/model/method matrix so that a
future run can distinguish a failed edge from missing data or an invalid
implementation matchup.

The word "all" is deliberately finite here: all model families registered in
the current engines, all declared categorical method grids in the saved
studies, and all instruments in the canonical contract registry.  A literal
Cartesian product of every floating-point parameter is neither finite nor a
statistically meaningful experiment; the report records the exact grids that
were actually evaluated.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


REPORT_VERSION = "2026-09-14-exhaustive-model-matrix-v1"
DEFAULT_MARKET_ROOT = Path(r"F:\ancserQuant\ancserMarketData")


CONTRACT_SPECS: dict[str, dict[str, Any]] = {
    "ENQ": {"label": "NQ (Mini alias)", "point_value": 20.0, "tick_size": 0.25},
    "NQ": {"label": "NQ (Mini)", "point_value": 20.0, "tick_size": 0.25},
    "MNQ": {"label": "MNQ (Micro)", "point_value": 2.0, "tick_size": 0.25},
    "MES": {"label": "MES (Micro ES)", "point_value": 5.0, "tick_size": 0.25},
    "GC": {"label": "GC (Gold)", "point_value": 100.0, "tick_size": 0.10},
    "MGC": {"label": "MGC (Micro Gold)", "point_value": 10.0, "tick_size": 0.10},
    "ZL": {"label": "ZL (Soybean Oil)", "point_value": 600.0, "tick_size": 0.01},
}


CURRENT_MODELS = (
    "fade",
    "sigma",
    "factor",
    "momentum",
    "betafib",
    "pi",
    "optionwall",
    "delta_absorption",
)

FACTOR_FAMILIES = ("emapmo", "momentum_reversion", "icefishball")


MODEL_EXPLANATIONS: dict[str, dict[str, Any]] = {
    "fade": {
        "family": "mean reversion / opening-range false break",
        "mechanism": "在前一日 VA/POC 附近或 15 分鐘開盤區間假突破後，押注價格回到價值區。",
        "works_when": "日內接受價值、波動收斂、突破沒有延續時；MNQ 的 range/normal 子樣本較好。",
        "fails_when": "真正的趨勢日或高波動日；逆著單邊移動平均回歸會連續止損，成本也會把接近 PF=1 的結果推成負值。",
        "decision": "長期兩商品都未通過；可以當 regime 條件下的研究元件，不能當全天候主模型。",
    },
    "sigma": {
        "family": "rolling distribution fade",
        "mechanism": "以 rolling STD/MAD 的外側 sigma 水位做均值回歸，含 blind/rejection/acceptance 變體。",
        "works_when": "只有在非常明確的平衡日、且外側觸碰後真的有拒絕時才可能短暫有效。",
        "fails_when": "外側只是趨勢的第一段；高頻觸發、固定幾何和交易成本讓長期 PF 在每年與多數 regime 都低於 1。",
        "decision": "目前最清楚的淘汰項；近期漂亮格點是視窗/樣本選擇，不足以推翻 6 年結果。",
    },
    "factor": {
        "family": "EMAPMO / momentum-reversion / icefishball factor",
        "mechanism": "完成 5 分鐘 K 後用 PMO/Signal 交叉與門檻判斷方向，再用 ATR、range 或 trend-tick 風控。",
        "works_when": "有方向延續或先跌後反轉的日子；近期 MES 和歷史 MNQ 的部分 long-only 高原有正期望。",
        "fails_when": "固定百分比 ROC 門檻遇到商品/波動 regime 改變；down/high regime 與 14-tick 往返成本會吃掉薄 edge。",
        "decision": "可作候選訊號層，但目前長期最壞年 PF 仍低於 1，不能稱為穩健模型。",
    },
    "momentum": {
        "family": "INTRAMOM / opening continuation",
        "mechanism": "用開盤後前 30/45/60 分鐘的方向與進場時段，押注初始推動延續。",
        "works_when": "開盤有真實資訊衝擊、方向延續且回撤淺；MNQ 6 年帳面 PF 尚可。",
        "fails_when": "開盤假突破、午盤反轉或高波動均值回歸；14-tick 壓力下 MNQ edge 消失，MES 全期轉負。",
        "decision": "有條件的方向性元件，不是可跨商品、跨 regime 直接複製的模型。",
    },
    "betafib": {
        "family": "SESSFIB / session fib retrace",
        "mechanism": "量化 RTH 推動腿，夜盤在 HL/OC 的 Fibonacci 回撤位掛單並以 ATR/daily/fib 風控。",
        "works_when": "有清楚的單段 RTH impulse 且夜盤回撤後恢復方向時；部分 down/high 子樣本較好。",
        "fails_when": "推動腿是噪聲、日內為 range 或回撤不是 continuation；長期不同錨點和 RR 也沒有找到共同高原。",
        "decision": "兩商品長期均沒有最壞年 PF>1 的變體；不應以單一 fib/RR 最佳值部署。",
    },
    "pi": {
        "family": "外部 PI/Discord 訊號 + 共用退出引擎",
        "mechanism": "由 QQQ/SPY 外部訊號提供進場，MNQ/MES 價格負責執行，測試 signal set、方向和 SL/TP/time/trail。",
        "works_when": "外部訊號本身有資訊，且交易落在 range/normal、成本可控的區段；目前 MNQ 的 6 個月樣本帳面較好。",
        "fails_when": "訊號在趨勢/高波動 regime 延遲，或外部訊號覆蓋、商品對應、訊號重播有誤；目前樣本太短，trail 沒有穩定救回未到 TP 的虧損。",
        "decision": "目前是 coverage-limited，不是長期已證實；不得把高 PF 當成持續性證據。",
    },
    "optionwall": {
        "family": "QQQ option wall → MNQ RTH",
        "mechanism": "用有時間順序的 OI/volume gamma、文章方向與 wall room 閘門產生外部方向，再由 MNQ ATR SL/持有時間執行。",
        "works_when": "QQQ 的期權定位、現貨方向和可用 room 同時一致，且只在 RTH 有流動性時。",
        "fails_when": "跨資產映射不一致、wall 已被穿越、非 RTH 缺乏有效期權資訊；MES 沒有對應 tape，因此不能把零交易解讀成 MES 無 edge。",
        "decision": "MNQ 目前看似最強但僅 74 訊號/約 8.8 個月，仍 coverage-limited；MES 不適用。",
    },
    "delta_absorption": {
        "family": "Databento MBO delta / absorption",
        "mechanism": "用已完成 MBO 分鐘的 aggressive delta、CVD、imbalance、passive rejection、value/VWAP 位置辨識 effort-versus-result。",
        "works_when": "RTH 有足夠成交，攻擊量與價格不延續同時出現，並且位於可解釋的 value/VWAP 區域。",
        "fails_when": "低流動性 session、只有單一 MBO 特徵、或 compact 1 分鐘聚合失去逐筆先後順序；一個月樣本不足以判斷持久性。",
        "decision": "MNQ 只可視為一個月的 provisional lead；MES 目前回測使用硬編碼 MNQ provider，該結果無效，須先修實作和補 MES cache。",
    },
}


RESEARCH_FAMILY_EXPLANATIONS: dict[str, str] = {
    "ORB": "開盤區間突破；趨勢延續日有效，假突破/開盤噪聲會反噬。",
    "VWAPREV": "偏離 VWAP 後回歸；需要 balance，遇到單邊日會一直逆勢。",
    "IBS": "用日內收盤位置/內部強弱做反轉；對日級 regime 和 gap 很敏感。",
    "RSI2": "短週期超買超賣反轉；短樣本有 MNQ PASS，但仍是單一窗口的候選，不是長期證明。",
    "GAPFADE": "開盤 gap 回補；MES 近期帳面直接為負，gap 若代表新資訊則不應回補。",
    "INTRAMOM": "開盤前段動量延續；MES 近期有 PASS，但 MNQ 的 MC 未過，跨商品不一致。",
    "DONCHIAN": "突破近期高低點；依賴趨勢延續，容易把 range 邊界當突破。",
    "BBREV": "Bollinger 外側回歸；MES 近期有候選，MNQ 第三段翻負，仍受 regime 影響。",
    "SESSFIB": "RTH 推動腿 fib 回撤；小樣本高 PF 多是 2–3 筆，不可採納。",
    "ONCONT": "隔夜/開盤 continuation；大量格點是 G0 訊號不足，不能從小 n 的高 PF 推斷 edge。",
}


METHOD_AXIS_MANIFEST: dict[str, dict[str, Any]] = {
    "fade": {
        "tested": "limit / rejection / or15 entry variants; long-term audit uses documented limit candidate",
        "not_a_single_cartesian_grid": True,
    },
    "sigma": {
        "tested": "rolling window, STD/MAD, blind/rejection entry, acceptance none/filter/switch, inner1/half/center target, stop span",
        "not_a_single_cartesian_grid": True,
    },
    "factor": {
        "tested": "emapmo, momentum_reversion, icefishball long-term family comparison; EMAPMO side × PMO mode × SL rule × RR grid",
        "not_a_single_cartesian_grid": True,
    },
    "momentum": {
        "tested": "long/short side, first 30/45/60 minutes, entry window, RR and one-trade limits",
        "not_a_single_cartesian_grid": True,
    },
    "betafib": {
        "tested": "fib 0.382/0.5/0.618/0.786, HL/OC anchor, ATR/daily/fib risk basis, RR and one-trade limits",
        "not_a_single_cartesian_grid": True,
    },
    "pi": {
        "tested": "signal sets/marker kinds/levels, long-only vs both, fixed SL/TP, directional time exit, ladder and continuous trail variants",
        "not_a_single_cartesian_grid": True,
    },
    "optionwall": {
        "tested": "primary_strict submodel, side and ATR/hold/trade-count dimensions; source is MNQ/RTH only",
        "not_a_single_cartesian_grid": True,
    },
    "delta_absorption": {
        "tested": "window/baseline/strength/weakening/stall, whole/outside source, raw/location/reclaim/reclaim_vwap gate, absorption/exhaustion/both, side/profile",
        "not_a_single_cartesian_grid": True,
    },
    "public_research": {
        "tested": "ORB, VWAPREV, IBS, RSI2, GAPFADE, INTRAMOM, DONCHIAN, BBREV, SESSFIB, ONCONT with their saved family-specific grids",
        "not_a_single_cartesian_grid": True,
    },
}


def _load(path: Path) -> dict[str, Any] | list[Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _finite(value: Any, default: float | None = None) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _fmt_number(value: Any, digits: int = 2) -> str:
    number = _finite(value)
    if number is None:
        return "—"
    return f"{number:.{digits}f}"


def _fmt_money(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "—"
    return f"${number:,.2f}"


def _fmt_pf(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "—"
    if number > 100:
        return "∞"
    return f"{number:.2f}"


def _as_path(path: Path) -> str:
    return path.resolve().as_posix()


def _link(path: Path, label: str | None = None) -> str:
    target = _as_path(path)
    return f"[{label or path.name}](<{target}>)"


def _safe_sort(values: Iterable[Any]) -> list[str]:
    return sorted({str(value) for value in values})


def _base_metrics(result: dict[str, Any]) -> dict[str, Any]:
    wrapper = result.get("baseline") or {}
    base = wrapper.get("baseline") or {}
    stress = wrapper.get("stress_14t") or {}
    walk = wrapper.get("walk_forward") or {}
    mc = wrapper.get("monte_carlo") or {}
    return {
        "n": _int(base.get("n")),
        "pnl": base.get("pnl"),
        "pf": base.get("pf"),
        "win_rate": base.get("win_rate"),
        "max_dd": base.get("max_dd"),
        "avg_trade": base.get("avg_trade"),
        "stress_14t_pnl": stress.get("pnl"),
        "stress_14t_pf": stress.get("pf"),
        "walk_forward_pass": bool(walk.get("pass")),
        "walk_forward_segments": walk.get("segments") or [],
        "monte_carlo_loss_probability": mc.get("p_loss"),
        "monte_carlo_pf_p5": mc.get("pf_p5"),
        "monte_carlo_dd_p95": mc.get("dd_p95"),
        "monte_carlo_pass": bool(wrapper.get("monte_carlo_pass")),
    }


def _current_audits(research_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol in ("MNQ", "MES"):
        path = research_dir / f"all_model_long_term_audit_{symbol.lower()}_20260914.json"
        payload = _load(path)
        if not isinstance(payload, dict):
            continue
        for result in payload.get("results", []):
            if not isinstance(result, dict):
                continue
            model = str(result.get("key") or "").lower()
            metrics = _base_metrics(result)
            status = str(result.get("status") or "UNKNOWN")
            validity = "valid_for_research"
            extra_notes: list[str] = []
            if symbol == "MES" and model == "delta_absorption":
                validity = "invalid_contract_provider_mismatch"
                status = "INVALID_IMPLEMENTATION"
                extra_notes.append(
                    "backend/strategy/delta_absorption.py hard-codes CachedDeltaContextProvider(symbol=\"MNQ\"); MES result is not MES MBO evidence."
                )
            if symbol == "MES" and model == "optionwall":
                validity = "not_applicable_no_mes_option_tape"
                status = "NOT_APPLICABLE"
                extra_notes.append(
                    "Option Wall is deliberately MNQ-only; zero MES trades mean no MES option tape was supplied, not a failed MES test."
                )
            rows.append(
                {
                    "experiment": "current_production_long_term",
                    "symbol": symbol,
                    "model": model,
                    "label": result.get("label"),
                    "status": status,
                    "audit_status": result.get("status"),
                    "validity": validity,
                    "metrics": metrics,
                    "data_span": result.get("data_span"),
                    "external_coverage": result.get("external_coverage"),
                    "status_reasons": list(result.get("status_reasons") or []) + extra_notes,
                    "config_note": result.get("config_note"),
                    "evidence": _as_path(path),
                }
            )
    return rows


def _contract_inventory(
    market_root: Path,
    audits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    continuous = market_root / "source" / "futures" / "continuous_1m"
    audit_data = {
        row["symbol"]: row.get("data_span")
        for row in audits
        if row.get("model") == "fade"
    }
    output: list[dict[str, Any]] = []
    for symbol, spec in CONTRACT_SPECS.items():
        candidates = [
            continuous / f"{symbol}_accumulated_1m.pkl",
            continuous / f"{symbol.lower()}_accumulated_1m.pkl",
        ]
        file_path = next((item for item in candidates if item.exists()), None)
        if symbol in {"MNQ", "MES"}:
            status = "tested"
            reason = "有完整 canonical 1m store，已跑現行模型與研究矩陣。"
        elif file_path is None:
            status = "unavailable"
            reason = "合約經濟規格存在，但沒有 canonical continuous 1m candles；不能誠實回測。"
        else:
            status = "data_present_not_in_matrix"
            reason = "有檔案但不在本次已完成模型矩陣的實際執行範圍，需另開同樣的 long-term audit。"
        output.append(
            {
                "symbol": symbol,
                "label": spec["label"],
                "point_value": spec["point_value"],
                "tick_size": spec["tick_size"],
                "continuous_store": _as_path(file_path) if file_path else None,
                "status": status,
                "reason": reason,
                "observed_data": audit_data.get(symbol),
                "models_run": list(CURRENT_MODELS) if symbol in {"MNQ", "MES"} else [],
            }
        )
    return output


def _best_sweep_row(rows: list[dict[str, Any]], metric: str) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(rows, key=lambda item: _finite(item.get(metric), -float("inf")) or -float("inf"))


def _stability_sweeps(research_dir: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    families = ("MOMENTUM", "BETAFIB", "FACTOR")
    for symbol in ("MNQ", "MES"):
        path = research_dir / f"stability_sweep_2026_{symbol}_6y.json"
        payload = _load(path)
        if not isinstance(payload, dict):
            continue
        rows = [row for row in payload.get("all", []) if isinstance(row, dict)]
        for family in families:
            family_rows = [
                row for row in rows if str(row.get("tag") or "").startswith(family + "/")
            ]
            if not family_rows:
                continue
            best_pf = _best_sweep_row(family_rows, "pf")
            best_pnl = _best_sweep_row(family_rows, "pnl")
            proxy_rows = [
                row
                for row in family_rows
                if (_finite(row.get("worst_year_pf"), 0.0) or 0.0) > 1.0
                and (_finite(row.get("worst_seg_pf"), 0.0) or 0.0) > 1.0
            ]
            spec_keys = {
                "factor_side_mode",
                "factor_pmo_signal_mode",
                "factor_sl_rule",
                "factor_tp_rule",
                "rr_ratio",
                "sl_ticks",
                "tp_ticks",
                "momentum_first_minutes",
                "momentum_entry_hour",
                "betafib_entry_fib",
                "betafib_anchor",
                "betafib_risk_basis",
            }
            axes: dict[str, list[str]] = {}
            for key in sorted(spec_keys):
                values = _safe_sort(row.get("spec", {}).get(key) for row in family_rows)
                if len(values) > 1:
                    axes[key] = values
            output.append(
                {
                    "experiment": "six_year_stability_grid",
                    "symbol": symbol,
                    "family": family,
                    "variants": len(family_rows),
                    "axes": axes,
                    "strict_persistence_proxy": "worst_year_pf > 1 and worst_third_pf > 1",
                    "strict_proxy_passes": len(proxy_rows),
                    "best_pf": None
                    if best_pf is None
                    else {
                        "tag": best_pf.get("tag"),
                        "n": best_pf.get("n"),
                        "pnl": best_pf.get("pnl"),
                        "pf": best_pf.get("pf"),
                        "worst_year_pf": best_pf.get("worst_year_pf"),
                        "worst_seg_pf": best_pf.get("worst_seg_pf"),
                    },
                    "best_pnl": None
                    if best_pnl is None
                    else {
                        "tag": best_pnl.get("tag"),
                        "n": best_pnl.get("n"),
                        "pnl": best_pnl.get("pnl"),
                        "pf": best_pnl.get("pf"),
                        "worst_year_pf": best_pnl.get("worst_year_pf"),
                        "worst_seg_pf": best_pnl.get("worst_seg_pf"),
                    },
                    "evidence": _as_path(path),
                }
            )
    return output


def _emapmo_sweeps(research_dir: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for symbol in ("MNQ", "MES"):
        path = research_dir / f"emapmo_full_sweep_{symbol}.json"
        payload = _load(path)
        if not isinstance(payload, dict):
            continue
        rows = [row for row in payload.get("results", []) if isinstance(row, dict)]
        best_pf = _best_sweep_row(rows, "pf")
        best_pnl = _best_sweep_row(rows, "pnl")
        best_trades = _best_sweep_row(rows, "trades")
        output.append(
            {
                "experiment": "emapmo_method_risk_grid",
                "symbol": symbol,
                "variants": len(rows),
                "grid": payload.get("grid"),
                "sample_note": "2026-only short window (approximately 2.5 months); this is method/risk sensitivity, not long-term validation.",
                "walk_forward_passes": sum(1 for row in rows if row.get("wf_pass")),
                "monte_carlo_passes": sum(1 for row in rows if row.get("mc_pass")),
                "long_term_ok_flags": sum(1 for row in rows if row.get("long_term_ok")),
                "best_pf": None
                if best_pf is None
                else {
                    "label": best_pf.get("label"),
                    "n": best_pf.get("trades"),
                    "pnl": best_pf.get("pnl"),
                    "pf": best_pf.get("pf"),
                    "segments": best_pf.get("seg_pfs"),
                    "wf": best_pf.get("wf_pass"),
                    "mc": best_pf.get("mc_pass"),
                    "long_term_ok": best_pf.get("long_term_ok"),
                },
                "best_pnl": None
                if best_pnl is None
                else {
                    "label": best_pnl.get("label"),
                    "n": best_pnl.get("trades"),
                    "pnl": best_pnl.get("pnl"),
                    "pf": best_pnl.get("pf"),
                    "segments": best_pnl.get("seg_pfs"),
                    "wf": best_pnl.get("wf_pass"),
                    "mc": best_pnl.get("mc_pass"),
                    "long_term_ok": best_pnl.get("long_term_ok"),
                },
                "best_trade_count": None
                if best_trades is None
                else {
                    "label": best_trades.get("label"),
                    "n": best_trades.get("trades"),
                    "pnl": best_trades.get("pnl"),
                    "pf": best_trades.get("pf"),
                    "segments": best_trades.get("seg_pfs"),
                    "wf": best_trades.get("wf_pass"),
                    "mc": best_trades.get("mc_pass"),
                },
                "evidence": _as_path(path),
            }
        )
    return output


def _factor_family_long_term(research_dir: Path) -> list[dict[str, Any]]:
    path = research_dir / "factor_family_long_term_audit_20260914.json"
    payload = _load(path)
    if not isinstance(payload, dict):
        return []
    output: list[dict[str, Any]] = []
    payload_symbols = [str(item) for item in (payload.get("symbols") or ["MNQ", "MES"])]
    payload_results = [
        item for item in payload.get("results", []) if isinstance(item, dict)
    ]
    for index, result in enumerate(payload_results):
        metrics = _base_metrics(result)
        params = result.get("parameters") or {}
        contract_id = str(params.get("contract_id") or "")
        symbol = str(result.get("symbol") or "")
        if not symbol:
            # The first version of the companion omitted symbol from the
            # serialized engine result.  Its writer is ordered by symbol then
            # by FACTOR_FAMILIES, so this fallback is deterministic.
            symbol = payload_symbols[
                min(index // max(1, len(FACTOR_FAMILIES)), len(payload_symbols) - 1)
            ]
        output.append(
            {
                "experiment": "factor_family_long_term",
                "symbol": symbol or ("MES" if ".MES." in contract_id else "MNQ"),
                "family": params.get("factor_signal_family") or "unknown",
                "label": result.get("label"),
                "status": result.get("status"),
                "validity": "valid_replay",
                "metrics": metrics,
                "data_span": result.get("data_span"),
                "status_reasons": result.get("status_reasons") or [],
                "evidence": _as_path(path),
            }
        )
    return output


def _public_sweeps(research_dir: Path) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for symbol in ("MNQ", "MES"):
        path = research_dir / f"public_strategies_{symbol}.json"
        payload = _load(path)
        if not isinstance(payload, dict):
            continue
        by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in payload.get("results", []):
            if isinstance(row, dict):
                by_family[str(row.get("strategy") or "UNKNOWN")].append(row)
        for family, rows in sorted(by_family.items()):
            best = _best_sweep_row(rows, "pf")
            adequately_sampled = [row for row in rows if _int(row.get("n")) >= 40]
            best_adequate = _best_sweep_row(adequately_sampled, "pf") or best
            output.append(
                {
                    "experiment": "public_research_family_grid",
                    "symbol": symbol,
                    "family": family,
                    "variants": len(rows),
                    "pass_count": sum(1 for row in rows if row.get("gate") == "PASS"),
                    "gate_counts": dict(Counter(str(row.get("gate") or "UNKNOWN") for row in rows)),
                    "sample_note": "2026-only short window (approximately 2.5 months); selected variants require a fresh holdout.",
                    "parameter_keys": _safe_sort(
                        key for row in rows for key in (row.get("params") or {}).keys()
                    ),
                    "best_all": None
                    if best is None
                    else {
                        "params": best.get("params"),
                        "n": best.get("n"),
                        "pnl": best.get("pnl"),
                        "pf": best.get("pf"),
                        "segments": best.get("seg_pf"),
                        "wf": best.get("wf_pass"),
                        "gate": best.get("gate"),
                    },
                    "best_n_ge_40": None
                    if best_adequate is None
                    else {
                        "params": best_adequate.get("params"),
                        "n": best_adequate.get("n"),
                        "pnl": best_adequate.get("pnl"),
                        "pf": best_adequate.get("pf"),
                        "segments": best_adequate.get("seg_pf"),
                        "wf": best_adequate.get("wf_pass"),
                        "gate": best_adequate.get("gate"),
                    },
                    "evidence": _as_path(path),
                }
            )
    return output


def _pi_exit_studies(research_dir: Path) -> list[dict[str, Any]]:
    path = research_dir / "pi_trail_extended_study_latest.json"
    payload = _load(path)
    if not isinstance(payload, dict):
        return []
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in payload.get("results", []):
        if isinstance(row, dict):
            by_symbol[str(row.get("symbol") or "UNKNOWN")].append(row)
    output: list[dict[str, Any]] = []
    for symbol, rows in sorted(by_symbol.items()):
        def base(row: dict[str, Any]) -> dict[str, Any]:
            return (row.get("robustness") or {}).get("baseline") or {}

        def stress(row: dict[str, Any]) -> dict[str, Any]:
            return (row.get("robustness") or {}).get("stress_14t") or {}

        def score(row: dict[str, Any]) -> float:
            return _finite(base(row).get("pf"), -float("inf")) or -float("inf")

        best = max(rows, key=score) if rows else None
        current = next((row for row in rows if row.get("key") == "current"), None)
        wf_count = sum(
            1 for row in rows if bool((row.get("robustness") or {}).get("walk_forward", {}).get("pass"))
        )
        mc_count = sum(
            1 for row in rows if bool((row.get("robustness") or {}).get("monte_carlo_pass"))
        )
        all_segment_pf = sum(
            1
            for row in rows
            if all(
                (_finite(segment.get("pf"), 0.0) or 0.0) > 1.0
                for segment in (row.get("robustness") or {}).get("walk_forward", {}).get("segments", [])
            )
            and len((row.get("robustness") or {}).get("walk_forward", {}).get("segments", [])) == 3
        )
        output.append(
            {
                "experiment": "pi_exit_and_trail_grid",
                "symbol": symbol,
                "variants": len(rows),
                "variant_modes": dict(Counter(str((row.get("variant") or {}).get("mode")) for row in rows)),
                "walk_forward_passes": wf_count,
                "monte_carlo_passes": mc_count,
                "all_three_walk_forward_pf_gt_1": all_segment_pf,
                "sample_note": "PI source coverage is approximately 6.3 months; no 12-month persistence claim is allowed.",
                "best_pf": None
                if best is None
                else {
                    "label": best.get("label"),
                    "variant": best.get("variant"),
                    "baseline": base(best),
                    "stress_14t": stress(best),
                    "wf": (best.get("robustness") or {}).get("walk_forward"),
                    "mc_pass": (best.get("robustness") or {}).get("monte_carlo_pass"),
                },
                "current": None
                if current is None
                else {
                    "label": current.get("label"),
                    "variant": current.get("variant"),
                    "baseline": base(current),
                    "stress_14t": stress(current),
                    "diagnostics": current.get("diagnostics"),
                    "status": current.get("status"),
                },
                "evidence": _as_path(path),
            }
        )
    return output


def _mbo_sessions(research_dir: Path) -> dict[str, Any]:
    path = research_dir / "mbo_session_comparison_2026-08-15_2026-09-14.json"
    payload = _load(path)
    if not isinstance(payload, dict):
        return {"status": "missing", "evidence": _as_path(path)}
    symbols: dict[str, Any] = {}
    for symbol, detail in (payload.get("symbols") or {}).items():
        if not isinstance(detail, dict):
            continue
        session_rows: list[dict[str, Any]] = []
        for session, value in (detail.get("session_results") or {}).items():
            if not isinstance(value, dict):
                continue
            stats = value.get("stats") or {}
            session_rows.append(
                {
                    "session": session,
                    "complete_sessions": value.get("complete_sessions"),
                    "trades": stats.get("trades"),
                    "pnl": stats.get("pnl"),
                    "pf": stats.get("pf"),
                    "stress_14t_pf": stats.get("stress_14t_pf"),
                    "max_dd": stats.get("max_dd"),
                    "expectancy": stats.get("expectancy"),
                    "by_direction": stats.get("by_direction"),
                }
            )
        symbols[symbol] = {
            "cache": detail.get("cache"),
            "inventory": detail.get("inventory"),
            "session_results": sorted(session_rows, key=lambda row: row["session"]),
            "candle_input": detail.get("candle_input"),
        }
    return {
        "status": payload.get("status"),
        "start": payload.get("start"),
        "end": payload.get("end"),
        "symbols": symbols,
        "interpretation": "RTH 是唯一在 MNQ 看到 gross PF>2 且 14-tick stress 仍接近 2 的 session；其他 session 或商品不一致，不能外推成 all-session edge。",
        "evidence": _as_path(path),
    }


def _pa_orderflow(research_dir: Path) -> dict[str, Any]:
    path = research_dir / "price_action_databento_orderflow_study_current.json"
    payload = _load(path)
    if not isinstance(payload, dict):
        return {"status": "missing", "evidence": _as_path(path)}
    rows = [row for row in payload.get("results", []) if isinstance(row, dict)]
    sufficiently_sized = [row for row in rows if _int(row.get("evaluation_trades")) >= 15]
    best = _best_sweep_row(sufficiently_sized, "evaluation_pf")
    return {
        "status": payload.get("status"),
        "rows": len(rows),
        "coverage": payload.get("coverage"),
        "study": payload.get("study"),
        "best_evaluation_pf_n_ge_15": None
        if best is None
        else {
            "context": best.get("context"),
            "setup": best.get("setup"),
            "side": best.get("side"),
            "n": best.get("evaluation_trades"),
            "pnl": best.get("evaluation_pnl"),
            "pf": best.get("evaluation_pf"),
            "stress_14t_pf": best.get("evaluation_slip14_pf"),
        },
        "interpretation": "PA×MBO 的高 PF 多集中在少數 setup/side 且 evaluation n 小；完整 discovery-selected audit 沒有足夠樣本的可晉級項。",
        "evidence": _as_path(path),
    }


def _supplementary_evidence(research_dir: Path) -> list[dict[str, Any]]:
    names = (
        "orderflow_context_combination_study.json",
        "orderflow_filter_exit_study.json",
        "orderflow_event_engine_study.json",
        "pi_orderflow_filter_study.json",
        "price_action_context_study_current.json",
        "price_action_missing_detail_study_current.json",
        "robustness_sweep_latest.json",
    )
    output: list[dict[str, Any]] = []
    for name in names:
        path = research_dir / name
        payload = _load(path)
        if payload is None:
            continue
        result_rows = payload.get("results") if isinstance(payload, dict) else None
        output.append(
            {
                "file": _as_path(path),
                "top_level_keys": list(payload.keys()) if isinstance(payload, dict) else [],
                "result_rows": len(result_rows) if isinstance(result_rows, list) else None,
                "status": payload.get("status") if isinstance(payload, dict) else None,
                "note": "原始結果保留在檔案內；本報告只摘要，不把短樣本候選宣稱為長期 edge。",
            }
        )
    return output


def _report_data(market_root: Path, research_dir: Path) -> dict[str, Any]:
    audits = _current_audits(research_dir)
    return {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "meaning_of_all": "所有目前 engine 可選 model、已保存的離散 method/exit grids、contract registry；不把無限連續參數或無資料合約偽裝成已測。",
            "current_engine_models": list(CURRENT_MODELS),
            "research_families": sorted(RESEARCH_FAMILY_EXPLANATIONS),
            "contract_registry": list(CONTRACT_SPECS),
            "sessions": ["ASIA", "EURO", "PRE", "RTH", "AH"],
            "cost_stress": "14 ticks round-trip plus canonical commission/fees where the source study supports it",
            "robustness": "long-term audit uses 3 equal-time walk-forward segments and 500-seed Monte Carlo; all short grids remain explicitly sample-limited",
        },
        "method_axis_manifest": METHOD_AXIS_MANIFEST,
        "experiment_counts": {
            "current_production_long_term_rows": len(audits),
            "factor_family_long_term_rows": 6,
            "six_year_method_grid_rows": 120,
            "emapmo_short_grid_rows": 1890,
            "public_research_grid_rows": 1432,
            "pi_exit_trail_rows": 36,
            "pa_mbo_rows": 306,
            "mbo_session_cells": 10,
        },
        "contract_inventory": _contract_inventory(market_root, audits),
        "current_production_results": audits,
        "factor_family_long_term": _factor_family_long_term(research_dir),
        "six_year_stability_sweeps": _stability_sweeps(research_dir),
        "emapmo_method_risk_sweeps": _emapmo_sweeps(research_dir),
        "public_strategy_sweeps": _public_sweeps(research_dir),
        "pi_exit_and_trail": _pi_exit_studies(research_dir),
        "mbo_session_comparison": _mbo_sessions(research_dir),
        "pa_mbo": _pa_orderflow(research_dir),
        "supplementary_evidence": _supplementary_evidence(research_dir),
        "model_explanations": MODEL_EXPLANATIONS,
        "research_family_explanations": RESEARCH_FAMILY_EXPLANATIONS,
        "integrity_flags": [
            {
                "severity": "blocking",
                "issue": "MES delta result is invalid for contract inference",
                "evidence": _as_path(Path(r"F:\ancserQuant\ancserTPX\backend\strategy\delta_absorption.py")),
                "line": 272,
                "detail": "DeltaAbsorptionStrategy default provider is instantiated with symbol=MNQ, while the MES audit requested MES. Do not compare its PF with MES models until the provider derives the active contract and MES MBO cache is present.",
            },
            {
                "severity": "blocking_for_generalization",
                "issue": "Option Wall is MNQ-only",
                "evidence": _as_path(Path(r"F:\ancserQuant\ancserTPX\backend\strategy\option_wall.py")),
                "line": 74,
                "detail": "A zero-trade MES row is not a negative MES edge; there is no MES option-wall tape/model mapping in the current implementation.",
            },
            {
                "severity": "data",
                "issue": "ENQ/NQ/GC/MGC/ZL cannot be statistically tested from the present canonical store",
                "evidence": _as_path(Path(r"F:\ancserQuant\ancserTPX\backend\db\models.py")),
                "line": 674,
                "detail": "They have economics in the registry, but this workspace currently has canonical 1m continuous data only for MNQ and MES.",
            },
            {
                "severity": "method",
                "issue": "confluence is live-only and trend is retired/aliased",
                "evidence": _as_path(Path(r"F:\ancserQuant\ancserTPX\backend\backtest\engine.py")),
                "line": 108,
                "detail": "confluence is not a historical BacktestEngine model; legacy trend routes to factor, so neither is an independent backtest family here.",
            },
        ],
        "decision": {
            "overall": "沒有任何一個組合目前能被證明為跨合約、跨 session、跨 regime 且扣成本後持久。",
            "strongest_but_not_ready": [
                "MNQ OPTION WALL：gross/stress 表現最好，但只有 74 個外部訊號、約 8.8 個月，且只適用 MNQ/RTH。",
                "MNQ DELTA ABSORPTION：RTH 一個月表現好，但只有 31 個 MBO cache 日、非 RTH 弱，仍是 provisional。",
                "MES FACTOR：現行 long-term audit 帳面 PF 1.50、WF 通過，但 14-tick PF 1.17 且沒有通過完整 MC/接受門檻。",
                "短樣本 public grid：MNQ RSI2、MES BBREV/INTRAMOM/ORB 有候選，但沒有 dual-symbol 長期證據。",
            ],
            "clear_failures": [
                "SIGMA：6 年 MNQ/MES 都是負期望且成本壓力更差。",
                "BETAFIB：6 年兩商品沒有最壞年 PF>1 的變體；近期最佳值無法形成高原。",
                "目前 MOMENTUM：MNQ gross 正但 14-tick stress <1，MES 全期略負且 stress 很差。",
                "現行 FADE：MNQ 接近 break-even、MES gross 微正，但兩者扣成本和 walk-forward 都不合格。",
            ],
            "next_required_before_promotion": [
                "先修正 Delta provider 的 active-symbol 綁定並補齊 MES MBO，再重新跑同一套 session/14-tick/MC/WF。",
                "為 MNQ Option Wall 補真正 out-of-sample、至少 12 個月且保持 MNQ/RTH 限定。",
                "把 2026 短樣本候選鎖參數後，留出完全未看過的月份/合約做 holdout，不再用同一段資料挑選與驗證。",
                "ENQ/NQ/GC/MGC/ZL 先建立 canonical 1m source，再複製相同矩陣；沒有資料時不產生假 PF。",
            ],
        },
    }


def _md_current(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 合約 | 模型 | 狀態 | n | P&L | PF | 14t PF | WF | MC loss | 判讀 |",
        "|---|---|---|---:|---:|---:|---:|---|---:|---|",
    ]
    for row in rows:
        m = row["metrics"]
        reason = "；".join(str(item) for item in row.get("status_reasons", []))
        if not reason:
            reason = "長期/成本/穩健性門檻未全部通過"
        lines.append(
            f"| {row['symbol']} | {row['model']} | {row['status']} | {m['n']} | {_fmt_money(m['pnl'])} | {_fmt_pf(m['pf'])} | {_fmt_pf(m['stress_14t_pf'])} | {'PASS' if m['walk_forward_pass'] else 'FAIL'} | {_fmt_number(m['monte_carlo_loss_probability'], 3)} | {reason} |"
        )
    return lines


def _md_stability(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 合約 | family | 格點數 | strict proxy pass | 最佳 PF（n/PF/最壞年/最壞段） |",
        "|---|---|---:|---:|---|",
    ]
    for row in rows:
        best = row.get("best_pf") or {}
        lines.append(
            f"| {row['symbol']} | {row['family']} | {row['variants']} | {row['strict_proxy_passes']} | {best.get('tag','—')}（{best.get('n','—')} / {_fmt_pf(best.get('pf'))} / {_fmt_pf(best.get('worst_year_pf'))} / {_fmt_pf(best.get('worst_seg_pf'))}） |"
        )
    return lines


def _md_public(rows: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| 合約 | family | 格點數 | PASS 數 | n≥40 最佳 PF | n | segments | gate |",
        "|---|---|---:|---:|---:|---:|---|---|",
    ]
    for row in rows:
        best = row.get("best_n_ge_40") or {}
        lines.append(
            f"| {row['symbol']} | {row['family']} | {row['variants']} | {row['pass_count']} | {_fmt_pf(best.get('pf'))} | {best.get('n','—')} | {best.get('segments','—')} | {best.get('gate','—')} |"
        )
    return lines


def _markdown(report: dict[str, Any], market_root: Path, research_dir: Path) -> str:
    repo_root = Path(__file__).resolve().parents[1]
    contracts = report["contract_inventory"]
    current = report["current_production_results"]
    factor_families = report["factor_family_long_term"]
    stability = report["six_year_stability_sweeps"]
    emapmo = report["emapmo_method_risk_sweeps"]
    public = report["public_strategy_sweeps"]
    pi = report["pi_exit_and_trail"]
    decision = report["decision"]
    lines: list[str] = [
        "# ancserTPX 全模型 × 全方法 × 合約矩陣審計",
        "",
        f"Generated: `{report['generated_at']}`  ",
        f"Report version: `{report['report_version']}`",
        "",
        "> 結論先講：目前沒有任何一個模型可以被證明為「跨合約、跨 session、跨 regime、扣成本後仍持久」。MNQ 的 Option Wall 與 MBO Delta 只是最值得繼續取樣的候選，並不是可直接升級 live 的證據。",
        "",
        "## 1. 測試範圍與「ALL」的定義",
        "",
        "本報告把目前 engine 可選的 8 個模型、已完成的離散參數格點、PI 退出變體、MBO session/context、PA×MBO，以及 contract registry 全部放進同一個結果層。連續浮點參數沒有有限的「所有可能值」；因此只報告實際保存並執行過的 grid，不把沒跑過的組合補成結論。",
        "",
        "| 類別 | 實際覆蓋 |",
        "|---|---:|",
        "| 現行 production model × MNQ/MES long-term | 16 rows |",
        "| 6 年 Factor/Momentum/BetaFib stability grid | 120 variants |",
        "| EMAPMO 方法/風控 grid | 1,890 variants |",
        "| public research families | 1,432 variants |",
        "| PI SL/TP/time/trail | 36 variants |",
        "| PA × Databento MBO | 306 rows |",
        "| MBO session cells | 10 cells |",
        "",
        "各模型實際展開的 method axis：",
        "",
        "| 模型/類別 | 已測 method axis |",
        "|---|---|",
    ]
    for key, value in report["method_axis_manifest"].items():
        lines.append(f"| {key} | {value['tested']} |")
    lines += [
        "",
        "這些是各 family 自己的完整離散 grid；不是把所有 family 的所有參數硬做成一個 Cartesian product。那種做法會產生大量多重比較、把短樣本的偶然尖峰當成真 edge，而且未必符合該模型的因果參數語義。",
        "",
        "## 2. 合約覆蓋",
        "",
        "| 合約 | 狀態 | 資料/原因 | 可測模型 |",
        "|---|---|---|---|",
    ]
    for contract in contracts:
        lines.append(
            f"| {contract['symbol']} | {contract['status']} | {contract['reason']} | {', '.join(contract['models_run']) if contract['models_run'] else '—'} |"
        )
    lines += [
        "",
        "目前 canonical 1m store 的可用結論只有 MNQ 與 MES。ENQ/NQ/GC/MGC/ZL 有經濟規格，但沒有足夠的 canonical candles；因此「未測」不是 PF=0，也不是策略失敗。",
        "",
        "## 3. 現行 8 個模型的長期結果",
        "",
    ]
    lines.extend(_md_current(current))
    lines += [
        "",
        "PF 是帳面 gross after canonical commission/fees；14t PF 是額外往返滑價壓力。`FAIL_NO_EDGE` 表示目前完整接受門檻沒有通過；`COVERAGE_LIMITED` 表示數字可重現但資料不足以宣稱長期；MES Delta 的 row 另被標成實作無效，MES Option Wall 則是不適用。",
        "",
        "## 4. FACTOR 底下三個 signal family",
        "",
        "為了避免把 FACTOR mode 的名字當成單一模型，我把 `factor_signal_family` 固定其它 BEST 條件後，對 EMAPMO、momentum-reversion、icefishball 各跑 MNQ/MES 長期回放。",
        "",
        "| 合約 | family | n | P&L | PF | 14t PF | WF | MC loss | 判定 |",
        "|---|---|---:|---:|---:|---:|---|---:|---|",
    ]
    for row in factor_families:
        m = row["metrics"]
        lines.append(
            f"| {row['symbol']} | {row['family']} | {m['n']} | {_fmt_money(m['pnl'])} | {_fmt_pf(m['pf'])} | {_fmt_pf(m['stress_14t_pf'])} | {'PASS' if m['walk_forward_pass'] else 'FAIL'} | {_fmt_number(m['monte_carlo_loss_probability'], 3)} | {row['status']} |"
        )
    lines += [
        "",
        "這個補測的答案很直接：MNQ 的 momentum-reversion 與 icefishball 在帳面上已分別是 PF 0.96/0.99，成本壓力後 0.83/0.87；MES 的 momentum-reversion PF 0.97、icefishball gross 僅 1.04 但 14t 變成 0.60。只有 EMAPMO 還有薄 gross edge，不能把它解釋成所有 FACTOR 方法都有效。",
        "",
        "## 5. 6 年參數高原檢查",
        "",
        "這一層測的是 Factor、Momentum、BetaFib 的大約 6 年穩定性。strict proxy 要求最壞年度 PF>1 且最壞等時間段 PF>1。結果兩個合約、三個 family 都是 0 個通過。",
        "",
    ]
    lines.extend(_md_stability(stability))
    lines += [
        "",
        "這解釋了為什麼某些近期 preset 看起來 PF 2–3：它們可能是某個年份/某個 regime 的有效條件，但沒有跨過最壞年與最壞段。高 PF 的單格不是高原。",
        "",
        "## 6. 方法與風控的全格點結果",
        "",
        "### EMAPMO / Factor",
        "",
        "| 合約 | 格點 | WF pass | MC pass | long-term flag | 最佳 PF格點 | 最大樣本格點 |",
        "|---|---:|---:|---:|---:|---|---|",
    ]
    for row in emapmo:
        best = row.get("best_pf") or {}
        many = row.get("best_trade_count") or {}
        lines.append(
            f"| {row['symbol']} | {row['variants']} | {row['walk_forward_passes']} | {row['monte_carlo_passes']} | {row['long_term_ok_flags']} | {best.get('label','—')}（n={best.get('n','—')}, PF={_fmt_pf(best.get('pf'))}） | {many.get('label','—')}（n={many.get('n','—')}, PF={_fmt_pf(many.get('pf'))}） |"
        )
    lines += [
        "",
        "這個 grid 的價值是說明 side、PMO mode、SL rule、RR 會改變結果；但它是約 2.5 個月的 2026 視窗，所以 63/67 個 `long_term_ok` flag 只能當短樣本篩選，不能覆蓋 6 年 stability 結論。",
        "",
        "### Public research families",
        "",
    ]
    lines.extend(_md_public(public))
    lines += [
        "",
        "MNQ 只有 RSI2 1 個完整 gate PASS；MES 有 BBREV 2、INTRAMOM 8、ORB 1 等 PASS。它們不是矛盾，而是不同商品在同一個短視窗的條件性 edge；目前沒有 dual-symbol、長期、成本壓力都成立的 family。SESSFIB/ONCONT 的最高 PF 多數 n 太小，不能解讀。",
        "",
        "## 7. PI 退出、trail、time exit",
        "",
        "PI 的入場訊號是外部 QQQ/SPY rows，退出才是本地模型。36 個 exit/trail variants 都在約 6.3 個月 coverage 上測；因此先看診斷：current MNQ 76 筆中只有 2 筆到原始 TP，41 筆在到 TP 前成為 loss，trail trigger 沒有實際觸發。這表示用更複雜的 trail 不能自動修復入場 edge。",
        "",
        "| 合約 | variants | WF pass | MC pass | 三段 PF>1 變體 | current |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for row in pi:
        current_pi = row.get("current") or {}
        base = current_pi.get("baseline") or {}
        lines.append(
            f"| {row['symbol']} | {row['variants']} | {row['walk_forward_passes']} | {row['monte_carlo_passes']} | {row['all_three_walk_forward_pf_gt_1']} | {_fmt_pf(base.get('pf'))} / 14t {_fmt_pf((current_pi.get('stress_14t') or {}).get('pf'))} / {current_pi.get('status','—')} |"
        )
    lines += [
        "",
        "## 8. Databento MBO / order flow / PA",
        "",
        "MBO 的信息只有在「攻擊量與結果不一致」且位置/時間有意義時才有可解釋性。compact 1 分鐘 cache 仍可做 causal context，但不能等同逐筆 queue replay；目前 flow coverage 只有 2026-08-07 至 09-11。",
        "",
        "既有 session comparison 的核心結果：MNQ RTH n=31、PF=2.21、14t PF=1.94；MNQ ASIA/EURO/PRE 的 PF 分別約 0.83/0.47/0.41。MES 沒有一致的 all-session edge，只有 EURO gross PF 約 1.27 且 14t PF 約 0.68。這是 session/regime evidence，不是 MBO 長期證明。",
        "",
        "PA×MBO 的 306 rows 有許多高 PF 小樣本候選，但 evaluation 保持未選 holdout 後，沒有足夠交易數的可晉級結果。`nr7_breakout`、`inside_breakout` 的部分 short rows 可能是現象線索；必須在新月份鎖定條件重測。",
        "",
        "## 9. 為什麼有些 works、又為什麼 fails",
        "",
    ]
    for key in CURRENT_MODELS:
        explanation = MODEL_EXPLANATIONS[key]
        lines += [
            f"### {key} — {explanation['family']}",
            "",
            f"- 原理：{explanation['mechanism']}",
            f"- 可能有效：{explanation['works_when']}",
            f"- 失效原因：{explanation['fails_when']}",
            f"- 本次判定：{explanation['decision']}",
            "",
        ]
    lines += [
        "### Research families",
        "",
    ]
    for family in sorted(RESEARCH_FAMILY_EXPLANATIONS):
        lines.append(f"- **{family}**：{RESEARCH_FAMILY_EXPLANATIONS[family]}")
    lines += [
        "",
        "## 10. 最終判定與下一步",
        "",
        f"**總判定：** {decision['overall']}",
        "",
        "最值得保留的候選：",
        "",
    ]
    for item in decision["strongest_but_not_ready"]:
        lines.append(f"- {item}")
    lines += [
        "",
        "明確不應繼續當主模型的項目：",
        "",
    ]
    for item in decision["clear_failures"]:
        lines.append(f"- {item}")
    lines += [
        "",
        "升級前必要條件：",
        "",
    ]
    for item in decision["next_required_before_promotion"]:
        lines.append(f"- {item}")
    lines += [
        "",
        "## 11. 可追溯來源",
        "",
        f"- 現行模型審計：{_link(research_dir / 'all_model_long_term_audit_mnq_20260914.json', 'MNQ JSON')}、{_link(research_dir / 'all_model_long_term_audit_mes_20260914.json', 'MES JSON')}",
        f"- 6 年 stability：{_link(research_dir / 'stability_sweep_2026_MNQ_6y.json', 'MNQ stability')}、{_link(research_dir / 'stability_sweep_2026_MES_6y.json', 'MES stability')}",
        f"- EMAPMO grids：{_link(research_dir / 'emapmo_full_sweep_MNQ.json', 'MNQ EMAPMO')}、{_link(research_dir / 'emapmo_full_sweep_MES.json', 'MES EMAPMO')}",
        f"- Public family grids：{_link(research_dir / 'public_strategies_MNQ.json', 'MNQ public')}、{_link(research_dir / 'public_strategies_MES.json', 'MES public')}",
        f"- PI exits：{_link(research_dir / 'pi_trail_extended_study_latest.json', 'PI trail study')}",
        f"- FACTOR family long-term：{_link(research_dir / 'factor_family_long_term_audit_20260914.json', 'FACTOR family audit')}",
        f"- MBO sessions：{_link(research_dir / 'mbo_session_comparison_2026-08-15_2026-09-14.json', 'MBO session study')}",
        f"- PA×MBO：{_link(research_dir / 'price_action_databento_orderflow_study_current.json', 'PA/MBO study')}",
        f"- 模型 registry / contract economics：{_link(repo_root / 'backend' / 'db' / 'models.py', 'models.py')}",
        f"- Backtest dispatch：{_link(repo_root / 'backend' / 'backtest' / 'engine.py', 'backtest engine')}",
        f"- Current handoff：{_link(repo_root / 'docs' / 'HANDOFF.md', 'HANDOFF.md')}",
        "",
        "本報告為 research-only aggregation；沒有修改 preset、canonical candles、backtest/live engine 或啟動 live。",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--market-root",
        default=os.environ.get("ANCER_MARKET_DATA_ROOT", str(DEFAULT_MARKET_ROOT)),
        help="market-data workspace root",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="JSON output path; Markdown is written next to it",
    )
    args = parser.parse_args()
    market_root = Path(args.market_root).resolve()
    research_dir = market_root / "derived" / "research"
    out_path = (
        Path(args.out).resolve()
        if args.out
        else research_dir / "exhaustive_model_matrix_20260914.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report = _report_data(market_root, research_dir)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_path.with_suffix(".md")
    md_path.write_text(_markdown(report, market_root, research_dir), encoding="utf-8")
    print(f"wrote {out_path}")
    print(f"wrote {md_path}")
    print(f"current rows: {len(report['current_production_results'])}")
    print(f"contracts: {len(report['contract_inventory'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
