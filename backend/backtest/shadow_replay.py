# ============================================================
# 文件: backend/backtest/shadow_replay.py
# 狀態: 1.0.9 新增 (P0 實盤平價層 — 影子重放)
# 目的: 1.0.8 驗屍結論的頭號死因是「執行錯配」:同窗口回測進 30 筆、
#       實盤只進 4 筆。在錯配修復並被持續監控之前,一切回測統計無意義。
#       本模組每個交易日自動:用當日實際 K 線 + 當日實盤所用的參數快照
#       重跑回測,逐筆 DIFF 實盤成交 → 吻合率日報 + 告警。
# 通過標準(1.0.9 P0): 連續 2 週 match_rate ≥ 0.9 才允許新策略上真錢。
# 關聯: → backend/api/routes.py  (/live/shadow-replay 端點 + 每日排程)
#       → data/strategy_snapshots.jsonl (參數快照庫,1.0.8)
#       → data/trades.json             (實盤逐筆記錄)
#       → docs/1.0.9_SKILL_REPORT.md   (P0 規格)
# ============================================================
"""P0 影子重放:實盤 vs 同參數回測 逐筆對賬。"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.backtest.engine import BacktestEngine, _topstep_trade_date
from backend.db.models import (
    BacktestConfig, StrategyParams,
    _extract_symbol, get_commission_rt, get_fees_rt,
)

logger = logging.getLogger(__name__)

TRADES_FILE = Path("data") / "trades.json"                 # 加料版(strategy/snapshot tag,pnl 有 bug)
TRADE_HISTORY_FILE = Path("data") / "trade_history.json"    # 1.0.9: broker 真相(準,對得上 Topstep)
SNAPSHOTS_FILE = Path("data") / "strategy_snapshots.jsonl"
REPORT_DIR = Path("data") / "shadow_replay"

# 配對容忍度 — P0 要判的是「同一個決策」(同時間±12分 + 同方向),不是逐 tick 對價。
# live 的 limit 常在回撤時以更好價成交(vs 回測假設剛好在 zone 邊界成交),故
# 價差本身是要「量測並回報」的指標,不是配對的閘門 → 價格放到 120 tick(30pt)寬鬆上限,
# 只擋掉明顯不同的單;實際價差記在 price_slip_ticks 供分析。
MATCH_TIME_MIN = 12.0
MATCH_PRICE_TICKS = 120
PASS_MATCH_RATE = 0.90    # P0 通過線

# 1.0.9: 只比對「主帳號」— 其餘帳號是跟單(follower),鏡像主帳號,忽略。
# None = 自動偵測(trade_history 中總筆數最多者);可由 set_main_account() 覆寫。
_MAIN_ACCOUNT: Optional[str] = None


def set_main_account(acct_id) -> None:
    global _MAIN_ACCOUNT
    _MAIN_ACCOUNT = str(acct_id) if acct_id is not None else None


def _parse_ts(v) -> Optional[datetime]:
    if not v:
        return None
    import re
    # broker 檔有 5 位微秒等非標準格式 → 去掉小數秒再解析
    s = re.sub(r"\.\d+", "", str(v)).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def _load_trade_history() -> List[dict]:
    if not TRADE_HISTORY_FILE.exists():
        return []
    try:
        return json.loads(TRADE_HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"shadow: read trade_history.json failed: {e}")
        return []


def main_account_id(rows: Optional[List[dict]] = None) -> Optional[str]:
    """主帳號:優先用 set_main_account 覆寫;否則自動 = broker 記錄最多的帳號。"""
    if _MAIN_ACCOUNT:
        return _MAIN_ACCOUNT
    rows = rows if rows is not None else _load_trade_history()
    counts: Dict[str, int] = {}
    for r in rows:
        a = str(r.get("account_id") or "")
        if a:
            counts[a] = counts.get(a, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _load_snapshots() -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not SNAPSHOTS_FILE.exists():
        return out
    with SNAPSHOTS_FILE.open(encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line)
                sid = rec.get("snapshot_id")
                if sid:
                    out[sid] = rec
            except Exception:
                continue
    return out


def _load_live_fills(trade_date: str, main_acct: Optional[str] = None) -> List[dict]:
    """1.0.9: 當日主帳號 broker 真相成交(準 net pnl,對得上 Topstep)。

    來源改為 trade_history.json(broker 原始),只取主帳號、當交易日的 round-trip。
    trades.json 因 exit_price/pnl 有 bug 只用於補策略 tag(不用於 pnl)。
    """
    hist = _load_trade_history()
    if not hist:
        return []
    acct = main_acct or main_account_id(hist)
    fills: List[dict] = []
    for r in hist:
        if str(r.get("account_id") or "") != str(acct):
            continue
        et = _parse_ts(r.get("entry_time"))
        if et is None or r.get("entry_price") is None:
            continue
        if _topstep_trade_date(et) != trade_date:
            continue
        pnl = r.get("pnl")            # broker net(pnl_is_net=True)
        if pnl is None:
            pnl = r.get("gross_pnl")
        fills.append({
            "entry_time": et,
            "entry_price": float(r["entry_price"]),
            "direction": str(r.get("direction") or "").lower(),
            "strategy": "trend",
            "snapshot_id": None,       # broker 檔無 tag,params 由 _day_snapshot_id 補
            "pnl": float(pnl) if pnl is not None else None,
        })
    fills.sort(key=lambda x: x["entry_time"])
    return fills


def _day_snapshot_id(trade_date: str, main_acct: Optional[str]) -> Optional[str]:
    """當日主帳號使用的 param snapshot(從 trades.json 的 tag 取,只為還原參數)。
    取當日出現最多的 snapshot_id;無則 None(交由 caller fallback 最新 snapshot)。"""
    if not TRADES_FILE.exists():
        return None
    try:
        rows = json.loads(TRADES_FILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    counts: Dict[str, int] = {}
    for r in rows:
        if main_acct is not None and str(r.get("account_id") or "") != str(main_acct):
            continue
        et = _parse_ts(r.get("entry_time"))
        if et is None or _topstep_trade_date(et) != trade_date:
            continue
        sid = r.get("param_snapshot_id")
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    return max(counts, key=counts.get) if counts else None


def _params_from_snapshot(snap: dict) -> Optional[StrategyParams]:
    raw = dict(snap.get("params") or {})
    if not raw:
        return None
    valid = set(StrategyParams.__dataclass_fields__.keys())
    kw = {k: v for k, v in raw.items() if k in valid}
    try:
        return StrategyParams(**kw)
    except Exception as e:
        logger.warning(f"shadow: snapshot params rebuild failed: {e}")
        return None


def _bt_trades_for_date(params: StrategyParams, candles: list, trade_date: str,
                        zone_timeline=None) -> List[dict]:
    cid = params.contract_id
    config = BacktestConfig(
        strategies=["trend"], initial_capital=50_000.0,
        symbol=_extract_symbol(cid), commission_rt=get_commission_rt(cid),
        fees_rt=get_fees_rt(cid),
        value_area_pct=float(getattr(params, "value_area_pct", 0.80) or 0.80),
    )
    result = BacktestEngine(config=config, strategy_params=params,
                            zone_timeline=zone_timeline,
                            record_equity=False).run(candles)
    out = []
    for t in result.trades:
        if _topstep_trade_date(t.entry_time) != trade_date:
            continue
        out.append({
            "entry_time": t.entry_time if t.entry_time.tzinfo else t.entry_time.replace(tzinfo=timezone.utc),
            "entry_price": float(t.entry_price),
            "direction": t.direction.value,
            "pnl": t.pnl,
        })
    return out


def _diff(live: List[dict], bt: List[dict], tick: float = 0.25) -> dict:
    """貪婪配對:方向相同、時間 ±MATCH_TIME_MIN、價格 ±MATCH_PRICE_TICKS。"""
    bt_free = list(range(len(bt)))
    pairs = []
    for lv in live:
        best = None
        best_cost = None
        for i in bt_free:
            b = bt[i]
            if b["direction"] != lv["direction"]:
                continue
            dt_min = abs((b["entry_time"] - lv["entry_time"]).total_seconds()) / 60.0
            dp_ticks = abs(b["entry_price"] - lv["entry_price"]) / tick
            if dt_min > MATCH_TIME_MIN or dp_ticks > MATCH_PRICE_TICKS:
                continue
            cost = dt_min + dp_ticks * 0.1
            if best_cost is None or cost < best_cost:
                best, best_cost = i, cost
        if best is not None:
            bt_free.remove(best)
            b = bt[best]
            lp = lv.get("pnl")
            bp = b.get("pnl")
            pairs.append({
                "live_time": lv["entry_time"].isoformat(),
                "bt_time": b["entry_time"].isoformat(),
                "time_offset_min": round((lv["entry_time"] - b["entry_time"]).total_seconds() / 60.0, 1),
                "price_slip_ticks": round((lv["entry_price"] - b["entry_price"]) / tick, 1),
                "direction": lv["direction"],
                # 1.0.9: 逐筆 PnL 背離 — 「進場對了但 PnL 差」才是真正的執行問題
                "live_pnl": round(lp, 2) if lp is not None else None,
                "bt_pnl": round(bp, 2) if bp is not None else None,
                "pnl_diff": (round(lp - bp, 2) if (lp is not None and bp is not None) else None),
            })
    matched = len(pairs)
    live_only = [lv for lv in live
                 if not any(p["live_time"] == lv["entry_time"].isoformat() for p in pairs)]
    bt_only = [bt[i] for i in bt_free]
    denom = max(len(live), len(bt))
    rate = (matched / denom) if denom else 1.0
    # 1.0.9: 配對後的 PnL 背離匯總 — entries 對得上但 pnl 差多少
    diffs = [p["pnl_diff"] for p in pairs if p.get("pnl_diff") is not None]
    matched_live_pnl = sum(p["live_pnl"] for p in pairs if p.get("live_pnl") is not None)
    matched_bt_pnl = sum(p["bt_pnl"] for p in pairs if p.get("bt_pnl") is not None)
    worst_pairs = sorted(
        (p for p in pairs if p.get("pnl_diff") is not None),
        key=lambda p: p["pnl_diff"],
    )[:3]
    return {
        "live_n": len(live),
        "bt_n": len(bt),
        "matched": matched,
        "match_rate": round(rate, 3),
        "matched_live_pnl": round(matched_live_pnl, 2),
        "matched_bt_pnl": round(matched_bt_pnl, 2),
        "pnl_diff_total": round(sum(diffs), 2) if diffs else 0.0,
        "worst_pnl_diffs": worst_pairs,
        "pairs": pairs,
        "live_only": [
            {"entry_time": x["entry_time"].isoformat(), "entry_price": x["entry_price"],
             "direction": x["direction"], "pnl": x.get("pnl")} for x in live_only
        ],
        "bt_only": [
            {"entry_time": x["entry_time"].isoformat(), "entry_price": x["entry_price"],
             "direction": x["direction"], "pnl": x.get("pnl")} for x in bt_only
        ],
    }


def run_shadow_replay(
    candles: list,
    trade_date: Optional[str] = None,
    zone_timeline_provider=None,
) -> dict:
    """對指定交易日做影子重放。candles 需已排序(完整歷史,供 zone 狀態)。

    zone_timeline_provider(params) -> timeline|None,由呼叫端(routes)注入
    快取 timeline 以加速;None 則引擎走 detector 慢路徑。
    """
    if not candles:
        return {"error": "no candles"}
    trade_date = trade_date or _topstep_trade_date(candles[-1].timestamp)

    # 1.0.9 BUGFIX: 不再裁剪窗口 — 之前裁成前 5 日導致 zone 偵測器暖機不足、
    # 回測當日嚴重少出單(7/3 回測 20→5,誤判為「實盤多開 10 筆」)。改用
    # 完整歷史;速度由 zone_timeline_provider(單 TF 快取 timeline)保證。
    dates_all = sorted({_topstep_trade_date(c.timestamp) for c in candles})
    if trade_date not in dates_all:
        return {"error": f"trade_date {trade_date} 不在 K 線範圍", "date": trade_date}

    # 1.0.9: 只比對主帳號(其餘為跟單);live 用 broker 真相(trade_history)
    main_acct = main_account_id()
    fills = _load_live_fills(trade_date, main_acct)
    snaps = _load_snapshots()

    # 當日主帳號用的參數快照(單一 dominant);無 tag → fallback 最新快照
    sid = _day_snapshot_id(trade_date, main_acct)
    if not sid:
        if snaps:
            sid = max(snaps.values(), key=lambda r: str(r.get("created_at") or ""))["snapshot_id"]
        elif not fills:
            return {"date": trade_date, "reports": [],
                    "note": "無主帳號成交且無參數快照 — 略過", "main_account": main_acct}

    reports = []
    snap = snaps.get(sid) if sid else None
    if sid and not snap:
        reports.append({"snapshot_id": sid, "error": "snapshot 不存在(舊庫?)"})
    else:
        params = _params_from_snapshot(snap) if snap else None
        if snap and params is None:
            reports.append({"snapshot_id": sid, "error": "params 重建失敗"})
        elif params is not None:
            timeline = zone_timeline_provider(params) if zone_timeline_provider else None
            try:
                bt = _bt_trades_for_date(params, candles, trade_date, timeline)
                d = _diff(fills, bt)
                d["snapshot_id"] = sid
                d["strategy_mode"] = snap.get("strategy_mode")
                d["exit_mode"] = snap.get("tr_exit_mode")
                d["pass"] = bool(d["match_rate"] >= PASS_MATCH_RATE)
                reports.append(d)
            except Exception as e:
                logger.exception("shadow replay backtest failed")
                reports.append({"snapshot_id": sid, "error": f"backtest: {e}"})

    day_pass = all(r.get("pass") for r in reports if "error" not in r) and any(
        "error" not in r for r in reports
    )
    payload = {
        "date": trade_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candles_last": candles[-1].timestamp.isoformat(),
        "main_account": main_acct,
        "day_pass": day_pass,
        "pass_line": PASS_MATCH_RATE,
        "reports": reports,
    }
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / f"{trade_date}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as e:
        logger.warning(f"shadow: persist failed: {e}")
    return payload


def load_recent_reports(n: int = 14) -> List[dict]:
    """近 n 份日報(P0 連續通過檢查用)。"""
    if not REPORT_DIR.exists():
        return []
    files = sorted(REPORT_DIR.glob("*.json"))[-n:]
    out = []
    for f in files:
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out
