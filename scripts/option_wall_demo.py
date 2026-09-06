"""Build a point-in-time QQQ 0DTE option-wall demo from local Databento exports.

This script does not download data.  It consumes the licensed, git-ignored CSV
exports under ``data/research/option_wall_demo/<date>/`` and publishes a small
derived JSON file for the read-only chart layer plus a static review figure.

The exposure model is deliberately labelled as a proxy:

* Black-Scholes-Merton with r=q=0 for the remaining hours of the 0DTE session.
* Calls are positive and puts negative (the Options Wall/book convention).
* Intraday Volume GEX uses cumulative consolidated volume, not dealer inventory.
* Every five-minute snapshot uses only quotes, volume and prices available by
  that timestamp.
"""
from __future__ import annotations

import argparse
import json
import math
import pickle
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from statistics import NormalDist

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Rectangle


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
UTC = timezone.utc
_NORMAL = NormalDist()


def _as_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _bsm_price(spot: float, strike: float, years: float, sigma: float, is_call: bool) -> float:
    if years <= 0 or sigma <= 0 or spot <= 0 or strike <= 0:
        return max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * years) / (sigma * root_t)
    d2 = d1 - sigma * root_t
    if is_call:
        return spot * _NORMAL.cdf(d1) - strike * _NORMAL.cdf(d2)
    return strike * _NORMAL.cdf(-d2) - spot * _NORMAL.cdf(-d1)


def _implied_vol(price: float, spot: float, strike: float, years: float, is_call: bool) -> float | None:
    intrinsic = max(spot - strike, 0.0) if is_call else max(strike - spot, 0.0)
    upper = spot if is_call else strike
    if years <= 0 or price < intrinsic - 0.02 or price <= 0 or price >= upper:
        return None
    target = max(price, intrinsic + 1e-8)
    lo, hi = 0.005, 8.0
    if _bsm_price(spot, strike, years, hi, is_call) < target:
        return None
    for _ in range(48):
        mid = (lo + hi) * 0.5
        if _bsm_price(spot, strike, years, mid, is_call) < target:
            lo = mid
        else:
            hi = mid
    sigma = (lo + hi) * 0.5
    return sigma if 0.005 <= sigma <= 8.0 else None


def _gamma(spot: float, strike: float, years: float, sigma: float) -> float:
    if years <= 0 or sigma <= 0:
        return 0.0
    root_t = math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * sigma * sigma * years) / (sigma * root_t)
    return math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi) / (spot * sigma * root_t)


def _load_mnq_bars(path: Path, session_date: str) -> pd.DataFrame:
    # The local pickle is the repository's existing Candle store.  Its price
    # coordinate is shared by MNQ/NQ; volume is intentionally not used here.
    with path.open("rb") as handle:
        bars = pickle.load(handle)
    day = datetime.fromisoformat(session_date).date()
    rows = []
    for bar in bars:
        ts = bar.timestamp
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        else:
            ts = ts.astimezone(UTC)
        if ts.date() != day or not (time(13, 30) <= ts.time() < time(20, 0)):
            continue
        rows.append(
            {
                "ts": pd.Timestamp(ts),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
            }
        )
    frame = pd.DataFrame(rows).set_index("ts").sort_index()
    if len(frame) != 390:
        raise RuntimeError(f"expected 390 MNQ RTH one-minute bars, found {len(frame)}")
    return frame


def _load_pi_signals(path: Path, session_date: str) -> list[dict]:
    out: list[dict] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("event") != "received" or row.get("equity") != "QQQ":
                continue
            if not str(row.get("ts", "")).startswith(session_date):
                continue
            out.append(
                {
                    "ts": row["ts"],
                    "received_at": row.get("received_at"),
                    "side": row.get("side"),
                    "kind": row.get("kind"),
                    "size": row.get("size"),
                    "position": row.get("pos"),
                }
            )
    return out


def _rolling_return_beta(qqq: pd.Series, mnq: pd.Series, as_of: pd.Timestamp) -> float:
    aligned = pd.concat({"qqq": qqq, "mnq": mnq}, axis=1).dropna()
    aligned = aligned.loc[aligned.index <= as_of].tail(61)
    if len(aligned) < 21:
        return 1.0
    returns = np.log(aligned).diff().dropna()
    x = returns["qqq"].to_numpy(dtype=float)
    y = returns["mnq"].to_numpy(dtype=float)
    denominator = float(np.dot(x, x))
    if denominator <= 1e-14:
        return 1.0
    return float(np.clip(np.dot(x, y) / denominator, 0.70, 1.30))


def _map_qqq_to_mnq(level: float | None, qqq_spot: float, mnq_spot: float, beta: float) -> float | None:
    if level is None or not math.isfinite(level):
        return None
    return mnq_spot * (1.0 + beta * (level / qqq_spot - 1.0))


def _gamma_price_profile(
    profile: pd.DataFrame,
    spot: float,
    years: float,
    weight_column: str = "oi",
) -> tuple[np.ndarray, np.ndarray]:
    """Return signed GEX over a fixed price grid for one position proxy.

    ``oi`` is the defensible overnight position map.  ``volume`` is accepted
    as an explicitly labelled intraday activity proxy; consolidated volume is
    unsigned and must not be mistaken for observed dealer inventory.
    """
    if weight_column not in profile.columns:
        return np.array([], dtype=float), np.array([], dtype=float)
    weights = pd.to_numeric(profile[weight_column], errors="coerce")
    active = profile[(weights > 0) & profile["iv"].notna()].copy()
    if active.empty:
        return np.array([], dtype=float), np.array([], dtype=float)
    strikes = active["strike"].to_numpy(dtype=float)
    ivs = active["iv"].to_numpy(dtype=float)
    position_weights = active[weight_column].to_numpy(dtype=float)
    signs = active["sign"].to_numpy(dtype=float)
    grid = np.linspace(spot * 0.94, spot * 1.06, 241)
    trials = grid[:, np.newaxis]
    root_t = math.sqrt(years)
    sigma_root_t = ivs[np.newaxis, :] * root_t
    d1 = (
        np.log(trials / strikes[np.newaxis, :])
        + 0.5 * ivs[np.newaxis, :] ** 2 * years
    ) / sigma_root_t
    gammas = (
        np.exp(-0.5 * d1 * d1)
        / math.sqrt(2.0 * math.pi)
        / (trials * sigma_root_t)
    )
    totals = np.sum(
        signs[np.newaxis, :] * gammas * position_weights[np.newaxis, :]
        * 100.0 * trials * trials * 0.01,
        axis=1,
    )
    return grid, totals.astype(float)


def _gamma_flip(
    profile: pd.DataFrame,
    spot: float,
    years: float,
    weight_column: str = "oi",
) -> float | None:
    grid, totals = _gamma_price_profile(profile, spot, years, weight_column)
    return _gamma_flip_from_profile(grid, totals, spot)


def _gamma_flip_from_profile(
    grid: np.ndarray,
    totals: np.ndarray,
    spot: float,
) -> float | None:
    if not len(grid):
        return None
    candidates = []
    for idx in range(1, len(grid)):
        left, right = totals[idx - 1], totals[idx]
        if left == 0:
            candidates.append(float(grid[idx - 1]))
        elif left * right < 0:
            weight = abs(left) / (abs(left) + abs(right))
            candidates.append(float(grid[idx - 1] + weight * (grid[idx] - grid[idx - 1])))
    return min(candidates, key=lambda value: abs(value - spot)) if candidates else None


def _contract_profile(
    latest_quotes: dict,
    cumulative_volume: dict[str, int],
    definitions: dict,
    open_interest: dict[str, int],
    spot: float,
    as_of: pd.Timestamp,
    expiry: pd.Timestamp,
) -> pd.DataFrame:
    years = max((expiry - as_of).total_seconds(), 1.0) / (365.0 * 24.0 * 3600.0)
    rows = []
    for symbol, quote in latest_quotes.items():
        meta = definitions.get(symbol)
        if meta is None or (as_of - quote["ts"]).total_seconds() > 125:
            continue
        bid, ask = _as_float(quote["bid"]), _as_float(quote["ask"])
        if bid is None or ask is None or bid < 0 or ask <= 0 or ask < bid:
            continue
        midpoint = (bid + ask) * 0.5
        # A wide quote is not a defensible IV observation.  The absolute arm
        # keeps cheap 0DTE options while the relative arm protects the ATM set.
        if ask - bid > max(0.25, midpoint * 0.50):
            continue
        strike = meta["strike"]
        is_call = meta["class"] == "C"
        iv = _implied_vol(midpoint, spot, strike, years, is_call)
        if iv is None:
            continue
        gamma = _gamma(spot, strike, years, iv)
        sign = 1.0 if is_call else -1.0
        oi = int(open_interest.get(symbol, 0))
        volume = int(cumulative_volume.get(symbol, 0))
        scale = gamma * 100.0 * spot * spot * 0.01
        rows.append(
            {
                "symbol": symbol,
                "strike": strike,
                "class": meta["class"],
                "sign": sign,
                "mid": midpoint,
                "iv": iv,
                "gamma": gamma,
                "oi": oi,
                "volume": volume,
                "oi_gex": sign * scale * oi,
                "volume_gex": sign * scale * volume,
            }
        )
    return pd.DataFrame(rows)


def _wall_level(grouped: pd.DataFrame, side: str, column: str) -> float | None:
    subset = grouped[grouped["class"] == side]
    if subset.empty:
        return None
    idx = subset[column].idxmax() if side == "C" else subset[column].idxmin()
    return float(subset.loc[idx, "strike"])


def _profile_by_strike(profile: pd.DataFrame) -> pd.DataFrame:
    if profile.empty:
        return pd.DataFrame()
    return (
        profile.groupby(["strike", "class"], as_index=False)[["oi_gex", "volume_gex"]]
        .sum()
        .sort_values(["strike", "class"])
    )


def build_demo(data_dir: Path, session_date: str) -> tuple[dict, pd.DataFrame, dict[str, pd.DataFrame]]:
    definitions_df = pd.read_csv(data_dir / "qqq_definition.csv.gz", compression="gzip")
    expiration = pd.to_datetime(definitions_df["expiration"], utc=True, errors="coerce")
    definitions_df = definitions_df[
        (expiration.dt.date.astype(str) == session_date)
        & definitions_df["instrument_class"].isin(["C", "P"])
    ].copy()
    definitions = {
        row.raw_symbol: {"strike": float(row.strike_price), "class": row.instrument_class}
        for row in definitions_df.itertuples()
    }

    stats = pd.read_csv(data_dir / "qqq_0dte_statistics.csv.gz", compression="gzip")
    oi_rows = stats[stats["stat_type"] == 9]
    open_interest = oi_rows.groupby("symbol")["quantity"].first().astype(int).to_dict()

    quotes = pd.read_csv(data_dir / "qqq_0dte_cbbo_1m.csv.gz", compression="gzip")
    quotes["ts"] = pd.to_datetime(quotes["ts_recv"], utc=True)
    quotes = quotes.sort_values("ts")

    volume = pd.read_csv(data_dir / "qqq_0dte_ohlcv_1m.csv.gz", compression="gzip")
    volume["ts"] = pd.to_datetime(volume["ts_event"], utc=True) + pd.Timedelta(minutes=1)
    volume = (
        volume.groupby(["ts", "symbol"], as_index=False)["volume"].sum().sort_values("ts")
    )

    qqq_bbo = pd.read_csv(data_dir / "qqq_bbo_1s.csv.gz", compression="gzip")
    qqq_bbo["ts"] = pd.to_datetime(qqq_bbo["ts_recv"], utc=True)
    qqq_bbo["mid"] = (qqq_bbo["bid_px_00"] + qqq_bbo["ask_px_00"]) * 0.5
    qqq_bbo = qqq_bbo[
        qqq_bbo["mid"].notna() & (qqq_bbo["ask_px_00"] >= qqq_bbo["bid_px_00"])
    ].sort_values("ts")
    qqq_bbo["minute_end"] = qqq_bbo["ts"].dt.floor("min") + pd.Timedelta(minutes=1)
    qqq_minute = qqq_bbo.groupby("minute_end")["mid"].last()

    mnq = _load_mnq_bars(ROOT / "data" / "store" / "MNQ_accumulated_1m.pkl", session_date)
    mnq_minute = mnq["close"].copy()
    mnq_minute.index = mnq_minute.index + pd.Timedelta(minutes=1)

    day = datetime.fromisoformat(session_date).date()
    expiry = pd.Timestamp(datetime.combine(day, time(20, 15), tzinfo=UTC))
    snapshot_times = pd.date_range(
        pd.Timestamp(datetime.combine(day, time(13, 35), tzinfo=UTC)),
        pd.Timestamp(datetime.combine(day, time(20, 0), tzinfo=UTC)),
        freq="5min",
    )

    quote_records = quotes.to_dict("records")
    volume_records = volume.to_dict("records")
    latest_quotes: dict[str, dict] = {}
    cumulative_volume: dict[str, int] = defaultdict(int)
    quote_idx = volume_idx = 0
    snapshots = []
    selected_profiles: dict[str, pd.DataFrame] = {}
    profile_targets = {
        pd.Timestamp(datetime.combine(day, time(14, 0), tzinfo=UTC)),
        pd.Timestamp(datetime.combine(day, time(17, 0), tzinfo=UTC)),
        pd.Timestamp(datetime.combine(day, time(18, 35), tzinfo=UTC)),
    }

    for as_of in snapshot_times:
        while quote_idx < len(quote_records) and quote_records[quote_idx]["ts"] <= as_of:
            row = quote_records[quote_idx]
            latest_quotes[row["symbol"]] = {
                "ts": row["ts"], "bid": row["bid_px_00"], "ask": row["ask_px_00"]
            }
            quote_idx += 1
        while volume_idx < len(volume_records) and volume_records[volume_idx]["ts"] <= as_of:
            row = volume_records[volume_idx]
            cumulative_volume[row["symbol"]] += int(row["volume"])
            volume_idx += 1

        qqq_available = qqq_minute.loc[qqq_minute.index <= as_of]
        mnq_available = mnq_minute.loc[mnq_minute.index <= as_of]
        if qqq_available.empty or mnq_available.empty:
            continue
        qqq_spot = float(qqq_available.iloc[-1])
        mnq_spot = float(mnq_available.iloc[-1])
        beta = _rolling_return_beta(qqq_minute, mnq_minute, as_of)
        profile = _contract_profile(
            latest_quotes, cumulative_volume, definitions, open_interest,
            qqq_spot, as_of, expiry,
        )
        grouped = _profile_by_strike(profile)
        if grouped.empty:
            continue

        oi_call = _wall_level(grouped, "C", "oi_gex")
        oi_put = _wall_level(grouped, "P", "oi_gex")
        volume_call = _wall_level(grouped, "C", "volume_gex")
        volume_put = _wall_level(grouped, "P", "volume_gex")
        # The book uses OI for the opening map and cumulative Volume GEX after
        # the first 30 minutes.  Preserve both source-specific walls in JSON.
        use_volume = as_of.time() >= time(14, 0) and volume_call is not None and volume_put is not None
        call_wall = volume_call if use_volume else oi_call
        put_wall = volume_put if use_volume else oi_put
        years = max((expiry - as_of).total_seconds(), 1.0) / (365.0 * 24.0 * 3600.0)
        raw_flip = _gamma_flip(profile, qqq_spot, years)
        # The simple call+/put- inventory assumption can produce a remote root
        # that jumps discontinuously as 0DTE IV observations disappear.  A
        # remote root is still retained as a quality state, but it is not drawn
        # as an actionable intraday level.
        flip = raw_flip if raw_flip is not None and abs(raw_flip / qqq_spot - 1.0) <= 0.012 else None
        call_oi = float(profile.loc[profile["class"] == "C", "oi_gex"].sum())
        put_oi = float(profile.loc[profile["class"] == "P", "oi_gex"].sum())
        denominator = call_oi + abs(put_oi)

        snapshots.append(
            {
                "as_of": as_of.isoformat().replace("+00:00", "Z"),
                "qqq_spot": qqq_spot,
                "mnq_spot": mnq_spot,
                "return_beta": beta,
                "wall_source": "volume" if use_volume else "oi",
                "valid_contracts": int(len(profile)),
                "call_wall_qqq": call_wall,
                "put_wall_qqq": put_wall,
                "gamma_flip_qqq": flip,
                "gamma_flip_quality": (
                    "stable_local" if flip is not None else
                    "remote_unstable" if raw_flip is not None else "no_root"
                ),
                "oi_call_wall_qqq": oi_call,
                "oi_put_wall_qqq": oi_put,
                "volume_call_wall_qqq": volume_call,
                "volume_put_wall_qqq": volume_put,
                "call_wall_mnq": _map_qqq_to_mnq(call_wall, qqq_spot, mnq_spot, beta),
                "put_wall_mnq": _map_qqq_to_mnq(put_wall, qqq_spot, mnq_spot, beta),
                "gamma_flip_mnq": _map_qqq_to_mnq(flip, qqq_spot, mnq_spot, beta),
                "net_oi_gex_1pct": float(profile["oi_gex"].sum()),
                "net_volume_gex_1pct": float(profile["volume_gex"].sum()),
                "gex_ratio": call_oi / denominator if denominator else None,
            }
        )
        if as_of in profile_targets:
            selected_profiles[as_of.isoformat().replace("+00:00", "Z")] = grouped

    manifest = json.loads((data_dir / "purchase_manifest.json").read_text(encoding="utf-8"))
    pi_signals = _load_pi_signals(ROOT / "data" / "logs" / "pi_live_signals.jsonl", session_date)
    payload = {
        "available": True,
        "symbol": "MNQ",
        "underlying": "QQQ",
        "date": session_date,
        "resolution": "5m snapshots from 1m option and price data",
        "source": "Databento OPRA.PILLAR + EQUS.MINI; local TopstepX MNQ 1m",
        "paid_cost_usd": manifest["total_cost"],
        "model": {
            "name": "QQQ 0DTE GEX proxy v1",
            "option_model": "Black-Scholes-Merton r=0 q=0 intraday approximation",
            "sign_assumption": "calls positive; puts negative",
            "volume_gex": "cumulative consolidated contract volume; unsigned flow proxy",
            "contract_multiplier": 100,
            "expiry_utc": expiry.isoformat().replace("+00:00", "Z"),
            "wall_schedule": "OI through 10:00 ET; cumulative Volume GEX after 10:00 ET",
            "mapping": "point-in-time QQQ/MNQ anchor with trailing 60-minute return beta",
        },
        "pi_signals": pi_signals,
        "snapshots": snapshots,
        "profiles": {
            ts: frame.replace({np.nan: None}).to_dict("records")
            for ts, frame in selected_profiles.items()
        },
    }
    return payload, mnq, selected_profiles


def _five_minute_candles(mnq: pd.DataFrame) -> pd.DataFrame:
    return mnq.resample("5min", origin="start_day", offset="30min").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()


def render_figure(payload: dict, mnq: pd.DataFrame, profiles: dict[str, pd.DataFrame], path: Path) -> None:
    snapshots = pd.DataFrame(payload["snapshots"])
    snapshots["as_of"] = pd.to_datetime(snapshots["as_of"], utc=True)
    candles = _five_minute_candles(mnq)

    plt.style.use("dark_background")
    figure = plt.figure(figsize=(16, 10), constrained_layout=True)
    grid = figure.add_gridspec(3, 3, height_ratios=[2.5, 1.0, 1.45])
    price_ax = figure.add_subplot(grid[0, :])
    gex_ax = figure.add_subplot(grid[1, :])
    profile_axes = [figure.add_subplot(grid[2, idx]) for idx in range(3)]

    width = 4.0 / (24.0 * 60.0)
    for ts, row in candles.iterrows():
        x = mdates.date2num(ts.to_pydatetime())
        rising = row["close"] >= row["open"]
        color = "#b7c1cc" if rising else "#56606b"
        price_ax.vlines(x, row["low"], row["high"], color=color, linewidth=0.7, alpha=0.9)
        body_low = min(row["open"], row["close"])
        body_height = max(abs(row["close"] - row["open"]), 0.25)
        price_ax.add_patch(Rectangle((x - width / 2, body_low), width, body_height,
                                     facecolor=color, edgecolor=color, linewidth=0.5))

    price_ax.step(snapshots["as_of"], snapshots["call_wall_mnq"], where="post",
                  color="#28d17c", linewidth=1.8, label="Call Wall (mapped)")
    price_ax.step(snapshots["as_of"], snapshots["put_wall_mnq"], where="post",
                  color="#ff5d73", linewidth=1.8, label="Put Wall (mapped)")
    if snapshots["gamma_flip_mnq"].notna().any():
        price_ax.step(snapshots["as_of"], snapshots["gamma_flip_mnq"], where="post",
                      color="#ffb547", linewidth=1.2, linestyle="--", label="Gamma Flip (mapped)")

    pi_names = {"紫圈": "PURPLE CIRCLE", "淡蓝圈": "LIGHT-BLUE CIRCLE", "青π": "CYAN π", "粉π": "PINK π"}
    for signal_index, signal in enumerate(payload["pi_signals"]):
        ts = pd.Timestamp(signal["ts"])
        candle_idx = candles.index.get_indexer([ts], method="pad")[0]
        if candle_idx < 0:
            continue
        price = float(candles.iloc[candle_idx]["close"])
        is_long = signal["side"] == "long"
        marker, color = ("^", "#36d7ff") if is_long else ("v", "#d184ff")
        price_ax.scatter(ts, price, marker=marker, s=90, color=color, edgecolor="#10151c", zorder=8)
        snap_idx = snapshots["as_of"].searchsorted(ts, side="right") - 1
        if snap_idx >= 0:
            snap = snapshots.iloc[snap_idx]
            label = (f"PI {pi_names.get(signal['kind'], 'MARK')} {signal['side'].upper()}\n"
                     f"Net OI GEX {snap['net_oi_gex_1pct']/1e9:+.2f}B")
            long_offset = 18 if signal_index % 2 else -42
            price_ax.annotate(label, (ts, price), xytext=(5, long_offset if is_long else -34),
                              textcoords="offset points", fontsize=7.5, color=color,
                              bbox=dict(boxstyle="round,pad=0.25", fc="#111821", ec=color, alpha=0.88))

    price_ax.set_title(
        f"{payload['date']} MNQ · actual QQQ 0DTE Wall/GEX demo",
        loc="left",
        fontsize=13,
    )
    price_ax.set_ylabel("MNQ price")
    price_ax.legend(loc="upper right", ncols=3, frameon=False, fontsize=8)
    price_ax.grid(alpha=0.14, linewidth=0.6)
    price_ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone(timedelta(hours=-4))))

    gex_ax.plot(snapshots["as_of"], snapshots["net_oi_gex_1pct"] / 1e9,
                color="#ffb547", linewidth=1.6, label="Net OI GEX")
    gex_ax.plot(snapshots["as_of"], snapshots["net_volume_gex_1pct"] / 1e9,
                color="#8ad8ff", linewidth=1.4, label="Net cumulative Volume GEX proxy")
    gex_ax.axhline(0, color="#8a94a3", linewidth=0.7)
    gex_ax.set_ylabel("$B per 1% QQQ move")
    gex_ax.legend(loc="upper left", frameon=False, fontsize=8)
    gex_ax.grid(alpha=0.14, linewidth=0.6)
    gex_ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=timezone(timedelta(hours=-4))))

    snapshot_lookup = snapshots.set_index("as_of")
    for axis, (ts_text, profile) in zip(profile_axes, sorted(profiles.items())):
        ts = pd.Timestamp(ts_text)
        snap = snapshot_lookup.loc[ts]
        spot = float(snap["qqq_spot"])
        view = profile[(profile["strike"] >= spot - 22) & (profile["strike"] <= spot + 22)].copy()
        calls = view[view["class"] == "C"]
        puts = view[view["class"] == "P"]
        axis.barh(calls["strike"], calls["volume_gex"] / 1e9, height=0.60,
                  color="#28d17c", alpha=0.78, label="Call")
        axis.barh(puts["strike"], puts["volume_gex"] / 1e9, height=0.60,
                  color="#ff5d73", alpha=0.78, label="Put")
        axis.axvline(0, color="#8a94a3", linewidth=0.7)
        axis.axhline(spot, color="#f1f5f9", linewidth=0.8, linestyle=":")
        axis.set_title(f"{ts.tz_convert('America/New_York'):%H:%M ET} · Volume GEX by strike", fontsize=9)
        axis.set_xlabel("$B per 1%")
        axis.set_ylabel("QQQ strike")
        axis.grid(alpha=0.10, linewidth=0.5)
    if profile_axes:
        profile_axes[0].legend(loc="lower right", frameon=False, fontsize=7)

    figure.text(
        0.005, 0.003,
        "Actual Databento OPRA/EQUS data; 0DTE only. BSM r=q=0 proxy. Calls + / puts − is an assumption; "
        "Volume GEX is cumulative volume, not observed dealer inventory. Walls mapped to MNQ point-in-time.",
        fontsize=7.5, color="#aab4c0",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(path, dpi=160, facecolor="#090d12")
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default="2026-09-01")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--output-json", type=Path)
    parser.add_argument("--output-png", type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir or ROOT / "data" / "research" / "option_wall_demo" / args.date
    output_json = args.output_json or data_dir / "derived.json"
    output_png = args.output_png or data_dir / f"option_wall_demo_{args.date.replace('-', '')}.png"
    payload, mnq, profiles = build_demo(data_dir, args.date)
    output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    render_figure(payload, mnq, profiles, output_png)
    print(json.dumps({
        "json": str(output_json), "png": str(output_png),
        "snapshots": len(payload["snapshots"]), "pi_signals": len(payload["pi_signals"]),
        "paid_cost_usd": payload["paid_cost_usd"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
