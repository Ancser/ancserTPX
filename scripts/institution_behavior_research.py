"""Institution/liquidity behavior research.

This script converts "hunter / smart money / sweep" ideas into measurable
market microstructure proxies available from the current 1m OHLCV data.

It does not claim to observe real institutional intent.  It measures repeatable
behavior around liquidity pools, session opens, volume spikes, and volume
profile voids, then validates predictive value with walk-forward ML.

Outputs:
  data/machinelearning/institution_research/latest.json
  data/machinelearning/institution_research/features.csv
  data/machinelearning/institution_research/event_stats.csv
  data/machinelearning/institution_research/report.md

Run:
  python -m scripts.institution_behavior_research
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import TimeSeriesSplit
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

from backend.backtest.engine import _topstep_trade_date
from backend.data import candle_store


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "data" / "machinelearning" / "institution_research"
LATEST_JSON = OUT_DIR / "latest.json"
FEATURES_CSV = OUT_DIR / "features.csv"
EVENT_STATS_CSV = OUT_DIR / "event_stats.csv"
STRATEGY_SCORES_CSV = OUT_DIR / "strategy_scores.csv"
REGIME_STATS_CSV = OUT_DIR / "regime_stats.csv"
REPORT_MD = OUT_DIR / "report.md"

TICK = 0.25
MNQ_TICK_VALUE = 0.50
ROUND_TURN_COST = 2.48
NY = ZoneInfo("America/New_York")


def _utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _session_for(ts: datetime) -> tuple[str, datetime]:
    ts = _utc(ts)
    d = ts.date()
    tod = ts.time()
    if tod >= time(22, 0) or tod < time(7, 0):
        start_day = d if tod >= time(22, 0) else d - timedelta(days=1)
        return "ASIA", datetime.combine(start_day, time(22, 0), tzinfo=timezone.utc)
    if time(7, 0) <= tod < time(11, 0):
        return "EURO", datetime.combine(d, time(7, 0), tzinfo=timezone.utc)
    if time(11, 0) <= tod < time(13, 30):
        return "PRE", datetime.combine(d, time(11, 0), tzinfo=timezone.utc)
    if time(13, 30) <= tod < time(20, 0):
        return "RTH", datetime.combine(d, time(13, 30), tzinfo=timezone.utc)
    return "AH", datetime.combine(d, time(20, 0), tzinfo=timezone.utc)


def _round_tick(price: float) -> float:
    return round(float(price) / TICK) * TICK


def _safe_div(a, b):
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(np.asarray(b) != 0, np.asarray(a) / np.asarray(b), np.nan)


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _future_max(series: pd.Series, horizon: int) -> pd.Series:
    return series.shift(-1).iloc[::-1].rolling(horizon, min_periods=1).max().iloc[::-1]


def _future_min(series: pd.Series, horizon: int) -> pd.Series:
    return series.shift(-1).iloc[::-1].rolling(horizon, min_periods=1).min().iloc[::-1]


def _future_last(series: pd.Series, horizon: int) -> pd.Series:
    return series.shift(-horizon)


def load_frame() -> pd.DataFrame:
    candles = sorted(candle_store.load("MNQ", 1), key=lambda c: c.timestamp)
    rows = []
    for c in candles:
        ts = _utc(c.timestamp)
        session, session_start = _session_for(ts)
        ny = ts.astimezone(NY)
        rows.append(
            {
                "timestamp": ts,
                "open": float(c.open),
                "high": float(c.high),
                "low": float(c.low),
                "close": float(c.close),
                "volume": float(c.volume or 0),
                "trade_date": _topstep_trade_date(ts),
                "session": session,
                "session_key": f"{session}:{session_start.isoformat()}",
                "session_start": session_start,
                "ny_hour": ny.hour,
                "ny_minute": ny.hour * 60 + ny.minute,
                "weekday": ny.weekday(),
                "month": ny.month,
            }
        )
    df = pd.DataFrame(rows)
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def add_basic_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ret_1"] = out["close"].diff()
    out["ret_1_ticks"] = out["ret_1"] / TICK
    out["range"] = out["high"] - out["low"]
    out["range_ticks"] = out["range"] / TICK
    out["body"] = out["close"] - out["open"]
    out["body_ticks"] = out["body"] / TICK
    out["upper_wick"] = out["high"] - out[["open", "close"]].max(axis=1)
    out["lower_wick"] = out[["open", "close"]].min(axis=1) - out["low"]
    out["upper_wick_ratio"] = out["upper_wick"] / out["range"].replace(0, np.nan)
    out["lower_wick_ratio"] = out["lower_wick"] / out["range"].replace(0, np.nan)
    out["direction_bar"] = np.sign(out["close"] - out["open"])
    out["delta_proxy"] = out["direction_bar"] * out["volume"]

    for n in (5, 15, 20, 30, 60, 240):
        out[f"ret_{n}"] = out["close"].diff(n)
        out[f"ret_{n}_ticks"] = out[f"ret_{n}"] / TICK
        out[f"rv_{n}"] = out["ret_1"].rolling(n).std()
        out[f"range_mean_{n}"] = out["range"].rolling(n).mean()
        out[f"volume_mean_{n}"] = out["volume"].rolling(n).mean()
        out[f"volume_std_{n}"] = out["volume"].rolling(n).std()
        out[f"volume_z_{n}"] = (out["volume"] - out[f"volume_mean_{n}"]) / out[f"volume_std_{n}"].replace(0, np.nan)

    for n in (9, 21, 50, 200):
        ema = out["close"].ewm(span=n, adjust=False).mean()
        out[f"ema_{n}"] = ema
        out[f"ema_{n}_dist_ticks"] = (out["close"] - ema) / TICK
        out[f"ema_{n}_slope_5"] = ema.diff(5) / TICK

    out["ema_9_21_spread"] = (out["ema_9"] - out["ema_21"]) / TICK
    out["ema_21_50_spread"] = (out["ema_21"] - out["ema_50"]) / TICK
    out["rsi_14"] = _rsi(out["close"], 14)

    low14 = out["low"].rolling(14).min()
    high14 = out["high"].rolling(14).max()
    out["stoch_k"] = 100 * (out["close"] - low14) / (high14 - low14).replace(0, np.nan)
    out["stoch_d"] = out["stoch_k"].rolling(3).mean()
    out["kdj_j"] = 3 * out["stoch_k"] - 2 * out["stoch_d"]

    ema12 = out["close"].ewm(span=12, adjust=False).mean()
    ema26 = out["close"].ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macd_signal"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macd_hist"] = out["macd"] - out["macd_signal"]

    mid20 = out["close"].rolling(20).mean()
    std20 = out["close"].rolling(20).std()
    out["bb_z_20"] = (out["close"] - mid20) / std20.replace(0, np.nan)

    prev_close = out["close"].shift(1)
    tr = pd.concat(
        [
            out["high"] - out["low"],
            (out["high"] - prev_close).abs(),
            (out["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr_14"] = tr.rolling(14).mean()
    out["atr_60"] = tr.rolling(60).mean()
    out["vol_regime"] = pd.qcut(out["atr_60"].rank(method="first"), 5, labels=False, duplicates="drop")
    return out


def _profile_levels(group: pd.DataFrame, bin_size: float = 1.0) -> dict:
    if group.empty:
        return {}
    typical = ((group["high"] + group["low"] + group["close"]) / 3.0).to_numpy()
    vol = group["volume"].to_numpy()
    bins = np.round(typical / bin_size) * bin_size
    prof: dict[float, float] = {}
    for price, volume in zip(bins, vol):
        prof[float(price)] = prof.get(float(price), 0.0) + float(volume)
    if not prof:
        return {}
    items = sorted(prof.items())
    prices = np.array([p for p, _ in items])
    vols = np.array([v for _, v in items])
    poc = float(prices[int(np.argmax(vols))])
    total = float(vols.sum())
    order = np.argsort(vols)[::-1]
    take = []
    acc = 0.0
    for idx in order:
        take.append(prices[idx])
        acc += vols[idx]
        if acc >= total * 0.70:
            break
    vah = float(max(take))
    val = float(min(take))
    low_cut = np.quantile(vols, 0.20) if len(vols) > 4 else vols.min()
    high_cut = np.quantile(vols, 0.80) if len(vols) > 4 else vols.max()
    lvns = prices[vols <= low_cut]
    hvns = prices[vols >= high_cut]
    return {
        "poc": poc,
        "vah": vah,
        "val": val,
        "lvns": [float(x) for x in lvns],
        "hvns": [float(x) for x in hvns],
        "max_vol": float(vols.max()),
        "profile": {str(float(p)): float(v) for p, v in prof.items()},
    }


def add_profile_and_levels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    day = out.groupby("trade_date").agg(
        day_high=("high", "max"),
        day_low=("low", "min"),
        day_open=("open", "first"),
        day_close=("close", "last"),
        day_volume=("volume", "sum"),
    )
    day["day_range"] = day["day_high"] - day["day_low"]
    prev_day = day.shift(1).add_prefix("prev_")
    out = out.join(prev_day, on="trade_date")

    session = out.groupby("session_key").agg(
        sess_high=("high", "max"),
        sess_low=("low", "min"),
        sess_open=("open", "first"),
        sess_close=("close", "last"),
        sess_volume=("volume", "sum"),
        session=("session", "first"),
    )
    session["sess_range"] = session["sess_high"] - session["sess_low"]
    prev_session = session.shift(1).add_prefix("prev_")
    out = out.join(prev_session, on="session_key")

    out["prior_session_high"] = out.groupby("session_key")["high"].cummax().shift(1)
    out["prior_session_low"] = out.groupby("session_key")["low"].cummin().shift(1)
    out["session_cum_volume"] = out.groupby("session_key")["volume"].cumsum()
    out["session_cum_delta_proxy"] = out.groupby("session_key")["delta_proxy"].cumsum()
    out["minute_in_session"] = (
        (out["timestamp"] - out["session_start"]).dt.total_seconds() / 60.0
    )

    profiles = {date: _profile_levels(g) for date, g in out.groupby("trade_date")}
    dates = sorted(profiles.keys())
    prev_profile = {dates[i]: profiles.get(dates[i - 1], {}) if i > 0 else {} for i in range(len(dates))}

    poc = []
    vah = []
    val = []
    near_lvn = []
    near_hvn = []
    bin_vol_pct = []
    for _, row in out.iterrows():
        prof = prev_profile.get(row["trade_date"], {})
        price = float(row["close"])
        poc.append(prof.get("poc", np.nan))
        vah.append(prof.get("vah", np.nan))
        val.append(prof.get("val", np.nan))
        lvns = prof.get("lvns", [])
        hvns = prof.get("hvns", [])
        near_lvn.append(min([abs(price - x) for x in lvns], default=np.nan))
        near_hvn.append(min([abs(price - x) for x in hvns], default=np.nan))
        b = str(float(round(price / 1.0) * 1.0))
        max_vol = prof.get("max_vol", np.nan)
        bvol = prof.get("profile", {}).get(b, np.nan)
        bin_vol_pct.append(bvol / max_vol if max_vol and not np.isnan(max_vol) else np.nan)

    out["prev_poc"] = poc
    out["prev_vah"] = vah
    out["prev_val"] = val
    out["dist_prev_poc_ticks"] = (out["close"] - out["prev_poc"]) / TICK
    out["dist_prev_vah_ticks"] = (out["close"] - out["prev_vah"]) / TICK
    out["dist_prev_val_ticks"] = (out["close"] - out["prev_val"]) / TICK
    out["nearest_prev_lvn_ticks"] = np.array(near_lvn) / TICK
    out["nearest_prev_hvn_ticks"] = np.array(near_hvn) / TICK
    out["prev_profile_bin_vol_pct"] = bin_vol_pct
    out["profile_void_score"] = (1.0 - out["prev_profile_bin_vol_pct"]).clip(lower=0, upper=1)
    return out


def add_liquidity_events(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    eps = TICK

    out["sweep_prev_high"] = (
        (out["high"] > out["prev_day_high"] + eps) & (out["close"] < out["prev_day_high"])
    )
    out["sweep_prev_low"] = (
        (out["low"] < out["prev_day_low"] - eps) & (out["close"] > out["prev_day_low"])
    )
    out["break_prev_high_close"] = out["close"] > out["prev_day_high"]
    out["break_prev_low_close"] = out["close"] < out["prev_day_low"]
    out["sweep_session_high"] = (
        (out["high"] > out["prior_session_high"] + eps) & (out["close"] < out["prior_session_high"])
    )
    out["sweep_session_low"] = (
        (out["low"] < out["prior_session_low"] - eps) & (out["close"] > out["prior_session_low"])
    )

    highs = out["high"].to_numpy()
    lows = out["low"].to_numpy()
    eq_high_count = np.zeros(len(out))
    eq_low_count = np.zeros(len(out))
    dist_eq_high = np.full(len(out), np.nan)
    dist_eq_low = np.full(len(out), np.nan)
    lookback = 120
    tol = 2 * TICK
    for i in range(len(out)):
        lo = max(0, i - lookback)
        if i <= lo:
            continue
        hwin = highs[lo:i]
        lwin = lows[lo:i]
        hlevel = np.nanmax(hwin)
        llevel = np.nanmin(lwin)
        eq_high_count[i] = np.sum(np.abs(hwin - hlevel) <= tol)
        eq_low_count[i] = np.sum(np.abs(lwin - llevel) <= tol)
        dist_eq_high[i] = (hlevel - out.at[i, "close"]) / TICK
        dist_eq_low[i] = (out.at[i, "close"] - llevel) / TICK

    out["eq_high_count_120"] = eq_high_count
    out["eq_low_count_120"] = eq_low_count
    out["dist_eq_high_ticks"] = dist_eq_high
    out["dist_eq_low_ticks"] = dist_eq_low

    out["dist_round25_ticks"] = np.abs(out["close"] - (np.round(out["close"] / 25.0) * 25.0)) / TICK
    out["dist_round50_ticks"] = np.abs(out["close"] - (np.round(out["close"] / 50.0) * 50.0)) / TICK

    def closeness(dist_ticks, scale=24):
        return np.exp(-np.clip(dist_ticks, 0, 10_000) / scale)

    dist_prev_high_up = (out["prev_day_high"] - out["close"]) / TICK
    dist_prev_low_down = (out["close"] - out["prev_day_low"]) / TICK
    dist_sess_high_up = (out["prior_session_high"] - out["close"]) / TICK
    dist_sess_low_down = (out["close"] - out["prior_session_low"]) / TICK

    out["liquidity_up_score"] = (
        closeness(dist_prev_high_up)
        + closeness(dist_sess_high_up)
        + closeness(out["dist_eq_high_ticks"])
        + (out["eq_high_count_120"] / 6.0).clip(0, 2)
        + closeness(out["dist_round25_ticks"], scale=8)
        + out["volume_z_60"].clip(lower=0, upper=4).fillna(0) / 4
        + out["profile_void_score"].fillna(0)
    )
    out["liquidity_down_score"] = (
        closeness(dist_prev_low_down)
        + closeness(dist_sess_low_down)
        + closeness(out["dist_eq_low_ticks"])
        + (out["eq_low_count_120"] / 6.0).clip(0, 2)
        + closeness(out["dist_round25_ticks"], scale=8)
        + out["volume_z_60"].clip(lower=0, upper=4).fillna(0) / 4
        + out["profile_void_score"].fillna(0)
    )
    out["hunter_pressure"] = out[["liquidity_up_score", "liquidity_down_score"]].max(axis=1)

    out["ny_open_0_15"] = (out["session"] == "RTH") & (out["ny_minute"] >= 570) & (out["ny_minute"] < 585)
    out["ny_open_15_30"] = (out["session"] == "RTH") & (out["ny_minute"] >= 585) & (out["ny_minute"] < 600)
    out["ny_open_30_60"] = (out["session"] == "RTH") & (out["ny_minute"] >= 600) & (out["ny_minute"] < 630)

    open15 = (
        out[out["ny_open_0_15"]]
        .groupby("session_key")
        .agg(open15_high=("high", "max"), open15_low=("low", "min"), open15_volume=("volume", "sum"))
    )
    out = out.join(open15, on="session_key")
    after15 = (out["session"] == "RTH") & (out["ny_minute"] >= 585)
    out["sweep_open15_high"] = after15 & (out["high"] > out["open15_high"] + eps) & (out["close"] < out["open15_high"])
    out["sweep_open15_low"] = after15 & (out["low"] < out["open15_low"] - eps) & (out["close"] > out["open15_low"])
    out["break_open15_high_close"] = after15 & (out["close"] > out["open15_high"])
    out["break_open15_low_close"] = after15 & (out["close"] < out["open15_low"])
    return out


def add_labels(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for h in (5, 15, 30, 60):
        out[f"future_high_{h}"] = _future_max(out["high"], h)
        out[f"future_low_{h}"] = _future_min(out["low"], h)
        out[f"future_close_{h}"] = _future_last(out["close"], h)
        out[f"future_ret_{h}_ticks"] = (out[f"future_close_{h}"] - out["close"]) / TICK
        out[f"mfe_up_{h}_ticks"] = (out[f"future_high_{h}"] - out["close"]) / TICK
        out[f"mfe_down_{h}_ticks"] = (out["close"] - out[f"future_low_{h}"]) / TICK
        out[f"long_quality_{h}"] = out[f"mfe_up_{h}_ticks"] - out[f"mfe_down_{h}_ticks"]
        out[f"short_quality_{h}"] = out[f"mfe_down_{h}_ticks"] - out[f"mfe_up_{h}_ticks"]
        out[f"target_up_{h}"] = (out[f"future_ret_{h}_ticks"] >= 12).astype(int)
        out[f"target_down_{h}"] = (out[f"future_ret_{h}_ticks"] <= -12).astype(int)
    return out


@dataclass
class EventStat:
    event: str
    count: int
    short_rev_30: float
    long_rev_30: float
    mean_ret_30: float
    mean_abs_ret_30: float
    mean_mfe_up_30: float
    mean_mfe_down_30: float
    median_hunter_pressure: float
    note: str


def compute_event_stats(df: pd.DataFrame) -> list[dict]:
    events = {
        "sweep_prev_high": "掃前日高後收回：理論 short hunter/fade",
        "sweep_prev_low": "掃前日低後收回：理論 long hunter/fade",
        "sweep_session_high": "掃 session 內部高後收回",
        "sweep_session_low": "掃 session 內部低後收回",
        "sweep_open15_high": "9:30 前15m高被掃後收回",
        "sweep_open15_low": "9:30 前15m低被掃後收回",
        "break_open15_high_close": "9:30 前15m高收盤突破",
        "break_open15_low_close": "9:30 前15m低收盤跌破",
        "volume_spike_z3": "volume_z_60 > 3",
        "profile_void": "前日 volume profile 低成交區",
        "high_hunter_pressure": "綜合 liquidity/hunter score 前10%",
    }
    work = df.copy()
    work["volume_spike_z3"] = work["volume_z_60"] > 3
    work["profile_void"] = work["profile_void_score"] > 0.8
    hp_cut = work["hunter_pressure"].quantile(0.90)
    work["high_hunter_pressure"] = work["hunter_pressure"] >= hp_cut

    rows: list[dict] = []
    for event, note in events.items():
        mask = work[event].fillna(False).astype(bool)
        sub = work[mask].copy()
        if sub.empty:
            continue
        short_rev = float((sub["short_quality_30"] > 20).mean())
        long_rev = float((sub["long_quality_30"] > 20).mean())
        rows.append(
            EventStat(
                event=event,
                count=int(len(sub)),
                short_rev_30=round(short_rev, 4),
                long_rev_30=round(long_rev, 4),
                mean_ret_30=round(float(sub["future_ret_30_ticks"].mean()), 3),
                mean_abs_ret_30=round(float(sub["future_ret_30_ticks"].abs().mean()), 3),
                mean_mfe_up_30=round(float(sub["mfe_up_30_ticks"].mean()), 3),
                mean_mfe_down_30=round(float(sub["mfe_down_30_ticks"].mean()), 3),
                median_hunter_pressure=round(float(sub["hunter_pressure"].median()), 3),
                note=note,
            ).__dict__
        )
    return rows


def run_ml(df: pd.DataFrame) -> dict:
    feature_cols = [
        "range_ticks", "body_ticks", "upper_wick_ratio", "lower_wick_ratio",
        "ret_5_ticks", "ret_15_ticks", "ret_30_ticks", "ret_60_ticks",
        "rv_15", "rv_60", "volume_z_20", "volume_z_60", "session_cum_delta_proxy",
        "ema_9_dist_ticks", "ema_21_dist_ticks", "ema_50_dist_ticks",
        "ema_9_21_spread", "ema_21_50_spread", "ema_9_slope_5", "ema_21_slope_5",
        "rsi_14", "stoch_k", "kdj_j", "macd_hist", "bb_z_20",
        "atr_14", "atr_60", "dist_prev_poc_ticks", "dist_prev_vah_ticks", "dist_prev_val_ticks",
        "nearest_prev_lvn_ticks", "nearest_prev_hvn_ticks", "profile_void_score",
        "eq_high_count_120", "eq_low_count_120", "dist_eq_high_ticks", "dist_eq_low_ticks",
        "dist_round25_ticks", "liquidity_up_score", "liquidity_down_score", "hunter_pressure",
        "minute_in_session", "ny_minute", "weekday", "month",
    ]
    data = df.dropna(subset=["target_up_30", "future_ret_30_ticks"]).copy()
    data = data.iloc[300:].copy()
    X = data[feature_cols].replace([np.inf, -np.inf], np.nan)
    y = data["target_up_30"].astype(int)
    returns = data["future_ret_30_ticks"].to_numpy()

    splitter = TimeSeriesSplit(n_splits=5)
    fold_rows = []
    importances: list[pd.Series] = []
    for fold, (train_idx, test_idx) in enumerate(splitter.split(X), start=1):
        X_train, X_test = X.iloc[train_idx], X.iloc[test_idx]
        y_train, y_test = y.iloc[train_idx], y.iloc[test_idx]
        if y_train.nunique() < 2 or y_test.nunique() < 2:
            continue
        clf = GradientBoostingClassifier(
            n_estimators=160,
            learning_rate=0.03,
            max_depth=2,
            min_samples_leaf=80,
            subsample=0.75,
            random_state=100 + fold,
        )
        pipe = make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), clf)
        pipe.fit(X_train, y_train)
        prob = pipe.predict_proba(X_test)[:, 1]
        auc = roc_auc_score(y_test, prob)
        cutoff = np.quantile(prob, 0.90)
        top = prob >= cutoff
        bottom = prob <= np.quantile(prob, 0.10)
        test_returns = returns[test_idx]
        fold_rows.append(
            {
                "fold": fold,
                "auc": round(float(auc), 4),
                "test_n": int(len(test_idx)),
                "top_decile_hit": round(float(y_test.to_numpy()[top].mean()), 4),
                "base_hit": round(float(y_test.mean()), 4),
                "top_decile_ret_ticks": round(float(np.nanmean(test_returns[top])), 3),
                "bottom_decile_ret_ticks": round(float(np.nanmean(test_returns[bottom])), 3),
            }
        )
        gb = pipe.named_steps["gradientboostingclassifier"]
        importances.append(pd.Series(gb.feature_importances_, index=feature_cols))

    if importances:
        imp = pd.concat(importances, axis=1).fillna(0)
        imp_mean = imp.mean(axis=1).sort_values(ascending=False)
        top_features = [
            {"feature": name, "importance": round(float(val), 6)}
            for name, val in imp_mean.head(20).items()
        ]
    else:
        top_features = []
    aucs = [r["auc"] for r in fold_rows]
    return {
        "target": "future 30m close return >= +12 ticks",
        "folds": fold_rows,
        "mean_auc": round(float(np.mean(aucs)), 4) if aucs else None,
        "min_auc": round(float(np.min(aucs)), 4) if aucs else None,
        "top_features": top_features,
        "feature_count": len(feature_cols),
    }


def _max_drawdown(equity: list[float]) -> float:
    peak = 0.0
    dd = 0.0
    for value in equity:
        peak = max(peak, value)
        dd = max(dd, peak - value)
    return dd


def _filtered_indices(indices: np.ndarray, cooldown_bars: int) -> list[int]:
    picked: list[int] = []
    last = -10**9
    for idx in indices.tolist():
        if idx - last >= cooldown_bars:
            picked.append(int(idx))
            last = int(idx)
    return picked


def _simulate_one(
    opens: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    idx: int,
    entry: float,
    direction: int,
    stop_ticks: int,
    target_ticks: int,
    hold_bars: int,
    start_offset: int,
) -> tuple[float, int, bool]:
    start = idx + start_offset
    if start >= len(opens):
        return 0.0, 0, False
    stop_price = entry - direction * stop_ticks * TICK
    target_price = entry + direction * target_ticks * TICK
    end = min(len(opens) - 1, start + hold_bars - 1)
    ambiguous = False

    for j in range(start, end + 1):
        if direction > 0:
            hit_stop = lows[j] <= stop_price
            hit_target = highs[j] >= target_price
            if hit_stop and hit_target:
                ambiguous = True
                ticks = -stop_ticks
                break
            if hit_stop:
                ticks = -stop_ticks
                break
            if hit_target:
                ticks = target_ticks
                break
        else:
            hit_stop = highs[j] >= stop_price
            hit_target = lows[j] <= target_price
            if hit_stop and hit_target:
                ambiguous = True
                ticks = -stop_ticks
                break
            if hit_stop:
                ticks = -stop_ticks
                break
            if hit_target:
                ticks = target_ticks
                break
    else:
        ticks = direction * (closes[end] - entry) / TICK

    pnl = ticks * MNQ_TICK_VALUE - ROUND_TURN_COST
    return float(pnl), int(round(float(ticks))), ambiguous


def score_sweep_strategies(df: pd.DataFrame) -> list[dict]:
    """Score resting-liquidity and reject-after-sweep rules with conservative fills."""

    work = df.copy()
    hp90 = work["hunter_pressure"].quantile(0.90)
    hp75 = work["hunter_pressure"].quantile(0.75)
    rules = [
        ("rest_prev_high", "resting", -1, "prev_day_high", work["high"] >= work["prev_day_high"]),
        ("rest_prev_low", "resting", 1, "prev_day_low", work["low"] <= work["prev_day_low"]),
        ("rest_open15_high", "resting", -1, "open15_high", work["break_open15_high_close"] | work["sweep_open15_high"]),
        ("rest_open15_low", "resting", 1, "open15_low", work["break_open15_low_close"] | work["sweep_open15_low"]),
        ("reject_prev_high", "reject", -1, None, work["sweep_prev_high"]),
        ("reject_prev_low", "reject", 1, None, work["sweep_prev_low"]),
        ("reject_open15_high", "reject", -1, None, work["sweep_open15_high"]),
        ("reject_open15_low", "reject", 1, None, work["sweep_open15_low"]),
        ("reject_session_high", "reject", -1, None, work["sweep_session_high"]),
        ("reject_session_low", "reject", 1, None, work["sweep_session_low"]),
    ]
    sessions = ["ALL", "RTH", "ASIA", "EURO"]
    filters = ["none", "vol_z2", "profile_void", "hunter_p75", "hunter_p90", "rsi_extreme", "ema_stretch", "ny_930_1030"]
    stop_ticks_list = [12, 20, 30, 40, 60]
    target_ticks_list = [20, 30, 40, 60, 80, 120]
    hold_bars_list = [15, 30, 60]

    opens = work["open"].to_numpy(float)
    highs = work["high"].to_numpy(float)
    lows = work["low"].to_numpy(float)
    closes = work["close"].to_numpy(float)
    session_arr = work["session"].to_numpy(str)
    ny_min = work["ny_minute"].to_numpy(float)
    high_s = pd.Series(highs)
    low_s = pd.Series(lows)
    close_s = pd.Series(closes)
    future = {}
    for hold_bars in hold_bars_list:
        future[("rest_high", hold_bars)] = high_s.iloc[::-1].rolling(hold_bars, min_periods=1).max().iloc[::-1].to_numpy()
        future[("rest_low", hold_bars)] = low_s.iloc[::-1].rolling(hold_bars, min_periods=1).min().iloc[::-1].to_numpy()
        future[("rest_exit", hold_bars)] = close_s.shift(-(hold_bars - 1)).to_numpy()
        future[("reject_high", hold_bars)] = high_s.shift(-1).iloc[::-1].rolling(hold_bars, min_periods=1).max().iloc[::-1].to_numpy()
        future[("reject_low", hold_bars)] = low_s.shift(-1).iloc[::-1].rolling(hold_bars, min_periods=1).min().iloc[::-1].to_numpy()
        future[("reject_exit", hold_bars)] = close_s.shift(-hold_bars).to_numpy()
    rows: list[dict] = []

    for rule, trigger, direction, level_col, base_mask in rules:
        base = base_mask.fillna(False).to_numpy(bool)
        if trigger == "resting" and level_col:
            levels = work[level_col].to_numpy(float)
            base = base & np.isfinite(levels)
        else:
            levels = np.full(len(work), np.nan)

        for session_name in sessions:
            if session_name == "ALL":
                session_mask = np.ones(len(work), dtype=bool)
            else:
                session_mask = session_arr == session_name

            for filter_name in filters:
                filt = np.ones(len(work), dtype=bool)
                if filter_name == "vol_z2":
                    filt = (work["volume_z_60"] >= 2).fillna(False).to_numpy(bool)
                elif filter_name == "profile_void":
                    filt = (work["profile_void_score"] >= 0.70).fillna(False).to_numpy(bool)
                elif filter_name == "hunter_p75":
                    filt = (work["hunter_pressure"] >= hp75).fillna(False).to_numpy(bool)
                elif filter_name == "hunter_p90":
                    filt = (work["hunter_pressure"] >= hp90).fillna(False).to_numpy(bool)
                elif filter_name == "rsi_extreme":
                    if direction < 0:
                        filt = (work["rsi_14"] >= 65).fillna(False).to_numpy(bool)
                    else:
                        filt = (work["rsi_14"] <= 35).fillna(False).to_numpy(bool)
                elif filter_name == "ema_stretch":
                    if direction < 0:
                        filt = (work["ema_21_dist_ticks"] >= 24).fillna(False).to_numpy(bool)
                    else:
                        filt = (work["ema_21_dist_ticks"] <= -24).fillna(False).to_numpy(bool)
                elif filter_name == "ny_930_1030":
                    filt = ((session_arr == "RTH") & (ny_min >= 570) & (ny_min < 630))

                mask = base & session_mask & filt
                raw_indices = np.flatnonzero(mask)
                if len(raw_indices) < 25:
                    continue

                for hold_bars in hold_bars_list:
                    indices = _filtered_indices(raw_indices, cooldown_bars=max(10, min(hold_bars, 30)))
                    if len(indices) < 25:
                        continue
                    for stop_ticks in stop_ticks_list:
                        for target_ticks in target_ticks_list:
                            idx_arr = np.asarray(indices, dtype=int)
                            if trigger == "resting":
                                entry = levels[idx_arr]
                                max_high = future[("rest_high", hold_bars)][idx_arr]
                                min_low = future[("rest_low", hold_bars)][idx_arr]
                                exit_close = future[("rest_exit", hold_bars)][idx_arr]
                            else:
                                valid_next = idx_arr + 1 < len(opens)
                                idx_arr = idx_arr[valid_next]
                                if len(idx_arr) < 25:
                                    continue
                                entry = opens[idx_arr + 1]
                                max_high = future[("reject_high", hold_bars)][idx_arr]
                                min_low = future[("reject_low", hold_bars)][idx_arr]
                                exit_close = future[("reject_exit", hold_bars)][idx_arr]
                            valid = (
                                np.isfinite(entry)
                                & np.isfinite(max_high)
                                & np.isfinite(min_low)
                                & np.isfinite(exit_close)
                            )
                            if int(valid.sum()) < 25:
                                continue
                            entry = entry[valid]
                            max_high = max_high[valid]
                            min_low = min_low[valid]
                            exit_close = exit_close[valid]
                            if direction > 0:
                                stop_hit = (entry - min_low) / TICK >= stop_ticks
                                target_hit = (max_high - entry) / TICK >= target_ticks
                                expiry_ticks = (exit_close - entry) / TICK
                            else:
                                stop_hit = (max_high - entry) / TICK >= stop_ticks
                                target_hit = (entry - min_low) / TICK >= target_ticks
                                expiry_ticks = (entry - exit_close) / TICK
                            ambiguous = int(np.sum(stop_hit & target_hit))
                            tick_result = np.where(
                                stop_hit,
                                -float(stop_ticks),
                                np.where(target_hit, float(target_ticks), expiry_ticks),
                            )
                            arr = tick_result * MNQ_TICK_VALUE - ROUND_TURN_COST
                            wins = arr[arr > 0]
                            losses = arr[arr < 0]
                            total_gain = float(wins.sum())
                            total_loss = float(losses.sum())
                            pf = total_gain / abs(total_loss) if total_loss < 0 else math.inf
                            equity = np.cumsum(arr).tolist()
                            max_dd = _max_drawdown(equity)
                            avg_loss = float(losses.mean()) if len(losses) else 0.0
                            tail_loss = float(np.quantile(losses, 0.05)) if len(losses) else 0.0
                            profit = float(arr.sum())
                            score = (
                                profit
                                + min(pf, 4.0) * 300.0
                                - max_dd * 1.5
                                - abs(total_loss) * 0.08
                                - max(0, len(arr) - 300) * 2.0
                            )
                            rows.append(
                                {
                                    "rule": rule,
                                    "trigger": trigger,
                                    "direction": "long" if direction > 0 else "short",
                                    "session": session_name,
                                    "filter": filter_name,
                                    "hold_bars": hold_bars,
                                    "stop_ticks": stop_ticks,
                                    "target_ticks": target_ticks,
                                    "trades": int(len(arr)),
                                    "pnl": round(profit, 2),
                                    "max_dd": round(float(max_dd), 2),
                                    "profit_factor": round(float(pf), 4) if math.isfinite(pf) else 999.0,
                                    "win_rate": round(float((arr > 0).mean()), 4),
                                    "expectancy": round(float(arr.mean()), 3),
                                    "total_loss": round(total_loss, 2),
                                    "total_gain": round(total_gain, 2),
                                    "avg_win": round(float(wins.mean()), 3) if len(wins) else 0.0,
                                    "avg_loss": round(avg_loss, 3),
                                    "tail_loss_5pct": round(tail_loss, 3),
                                    "ambiguous": int(ambiguous),
                                    "score": round(float(score), 2),
                                }
                            )

    rows.sort(key=lambda r: (r["score"], r["pnl"], r["profit_factor"]), reverse=True)
    return rows


def compute_regime_stats(df: pd.DataFrame) -> list[dict]:
    rows: list[dict] = []
    fields = [
        ("session", "session"),
        ("month", "month"),
        ("weekday", "weekday"),
        ("vol_regime", "vol_regime"),
    ]
    sweep_cols = ["sweep_prev_high", "sweep_prev_low", "sweep_open15_high", "sweep_open15_low"]
    for group_name, col in fields:
        for value, sub in df.groupby(col, dropna=True):
            if len(sub) < 100:
                continue
            sweep_count = int(sub[sweep_cols].fillna(False).any(axis=1).sum())
            rows.append(
                {
                    "group": group_name,
                    "value": str(value),
                    "bars": int(len(sub)),
                    "mean_ret_30": round(float(sub["future_ret_30_ticks"].mean()), 3),
                    "abs_ret_30": round(float(sub["future_ret_30_ticks"].abs().mean()), 3),
                    "mean_range": round(float(sub["range_ticks"].mean()), 3),
                    "mean_volume_z60": round(float(sub["volume_z_60"].mean()), 3),
                    "mean_hunter_pressure": round(float(sub["hunter_pressure"].mean()), 3),
                    "sweep_count": sweep_count,
                    "sweep_rate": round(float(sweep_count / len(sub)), 4),
                }
            )
    rows.sort(key=lambda r: (r["group"], r["value"]))
    return rows


def build_findings(event_stats: list[dict], ml: dict, strategy_scores: list[dict] | None = None) -> list[str]:
    findings = []
    by_event = {r["event"]: r for r in event_stats}
    for event in ("sweep_prev_high", "sweep_prev_low", "sweep_open15_high", "sweep_open15_low"):
        row = by_event.get(event)
        if not row:
            continue
        findings.append(
            f"{event}: n={row['count']}, mean30={row['mean_ret_30']} ticks, "
            f"shortRev={row['short_rev_30']}, longRev={row['long_rev_30']}"
        )
    if ml.get("mean_auc") is not None:
        verdict = "可疑訊號" if ml["mean_auc"] < 0.55 else "有弱訊號" if ml["mean_auc"] < 0.60 else "有可研究訊號"
        findings.append(f"ML walk-forward mean AUC={ml['mean_auc']} min={ml['min_auc']} => {verdict}")
    if strategy_scores:
        best = strategy_scores[0]
        findings.append(
            "best_rule="
            f"{best['rule']} {best['trigger']} {best['direction']} "
            f"{best['session']} {best['filter']} "
            f"TP{best['target_ticks']} SL{best['stop_ticks']} H{best['hold_bars']} "
            f"pnl={best['pnl']} dd={best['max_dd']} pf={best['profit_factor']} "
            f"loss={best['total_loss']}"
        )
    return findings


def write_outputs(
    df: pd.DataFrame,
    event_stats: list[dict],
    ml: dict,
    strategy_scores: list[dict],
    regime_stats: list[dict],
) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    export_cols = [
        "timestamp", "open", "high", "low", "close", "volume", "trade_date", "session",
        "ny_hour", "ny_minute", "range_ticks", "volume_z_60", "rsi_14", "macd_hist",
        "profile_void_score", "liquidity_up_score", "liquidity_down_score", "hunter_pressure",
        "sweep_prev_high", "sweep_prev_low", "sweep_session_high", "sweep_session_low",
        "sweep_open15_high", "sweep_open15_low", "future_ret_30_ticks",
        "mfe_up_30_ticks", "mfe_down_30_ticks",
    ]
    df[export_cols].to_csv(FEATURES_CSV, index=False)
    pd.DataFrame(event_stats).to_csv(EVENT_STATS_CSV, index=False)
    pd.DataFrame(strategy_scores).to_csv(STRATEGY_SCORES_CSV, index=False)
    pd.DataFrame(regime_stats).to_csv(REGIME_STATS_CSV, index=False)

    findings = build_findings(event_stats, ml, strategy_scores)
    latest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "data": {
            "bars": int(len(df)),
            "first": df["timestamp"].min().isoformat(),
            "last": df["timestamp"].max().isoformat(),
        },
        "findings": findings,
        "event_stats": event_stats,
        "strategy_scores": strategy_scores[:40],
        "regime_stats": regime_stats,
        "ml": ml,
        "files": {
            "features_csv": str(FEATURES_CSV),
            "event_stats_csv": str(EVENT_STATS_CSV),
            "strategy_scores_csv": str(STRATEGY_SCORES_CSV),
            "regime_stats_csv": str(REGIME_STATS_CSV),
            "report_md": str(REPORT_MD),
        },
    }
    LATEST_JSON.write_text(json.dumps(latest, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Institution / Hunter / Sweep Research",
        "",
        f"Generated: {latest['created_at']}",
        f"Data: {latest['data']['bars']} 1m bars, {latest['data']['first']} .. {latest['data']['last']}",
        "",
        "## 核心限制",
        "",
        "目前只有 1m OHLCV，沒有 CME MBO / Level 2 / iceberg / true aggressive order flow。"
        "所以這份研究測的是 institution 行為的 proxy，不是直接觀察真實大戶意圖。",
        "",
        "## 初步結論",
        "",
    ]
    lines += [f"- {x}" for x in findings]
    lines += [
        "",
        "## Event Stats",
        "",
        "| event | n | mean30 ticks | shortRev30 | longRev30 | MFE up | MFE down | note |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for row in event_stats:
        lines.append(
            f"| {row['event']} | {row['count']} | {row['mean_ret_30']} | "
            f"{row['short_rev_30']} | {row['long_rev_30']} | "
            f"{row['mean_mfe_up_30']} | {row['mean_mfe_down_30']} | {row['note']} |"
        )
    lines += [
        "",
        "## Rule Scores",
        "",
        "| rule | trigger | session | filter | trades | pnl | dd | pf | win | loss | TP | SL | H |",
        "|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in strategy_scores[:25]:
        lines.append(
            f"| {row['rule']} | {row['trigger']} | {row['session']} | {row['filter']} | "
            f"{row['trades']} | {row['pnl']} | {row['max_dd']} | {row['profit_factor']} | "
            f"{row['win_rate']} | {row['total_loss']} | {row['target_ticks']} | "
            f"{row['stop_ticks']} | {row['hold_bars']} |"
        )
    lines += [
        "",
        "## Regime Stats",
        "",
        "| group | value | bars | mean30 | abs30 | range | hunter | sweep rate |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in regime_stats:
        lines.append(
            f"| {row['group']} | {row['value']} | {row['bars']} | {row['mean_ret_30']} | "
            f"{row['abs_ret_30']} | {row['mean_range']} | {row['mean_hunter_pressure']} | "
            f"{row['sweep_rate']} |"
        )
    lines += [
        "",
        "## ML Walk-Forward",
        "",
        f"Target: {ml.get('target')}",
        f"Mean AUC: {ml.get('mean_auc')} / Min AUC: {ml.get('min_auc')}",
        "",
        "| fold | AUC | base hit | top decile hit | top decile ret ticks | bottom decile ret ticks |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in ml.get("folds", []):
        lines.append(
            f"| {row['fold']} | {row['auc']} | {row['base_hit']} | {row['top_decile_hit']} | "
            f"{row['top_decile_ret_ticks']} | {row['bottom_decile_ret_ticks']} |"
        )
    lines += ["", "## Top Features", ""]
    for row in ml.get("top_features", [])[:20]:
        lines.append(f"- {row['feature']}: {row['importance']}")
    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    df = load_frame()
    df = add_basic_features(df)
    df = add_profile_and_levels(df)
    df = add_liquidity_events(df)
    df = add_labels(df)
    event_stats = compute_event_stats(df)
    ml = run_ml(df)
    strategy_scores = score_sweep_strategies(df)
    regime_stats = compute_regime_stats(df)
    write_outputs(df, event_stats, ml, strategy_scores, regime_stats)
    print(f"wrote {LATEST_JSON}")
    print(f"features {FEATURES_CSV}")
    print(f"strategy scores {STRATEGY_SCORES_CSV}")
    print(f"report {REPORT_MD}")


if __name__ == "__main__":
    main()
