use crate::model::{
    Bar, DataSource, DecisionTraceRow, Direction, EntryFillMode, ExecutionConfig, Instrument,
    StrategyParams, TimestampSourcePolicy, TradeRow, VpSignal,
};
use crate::strategy::{VolumeProfileStrategy, round_price};
use chrono::{DateTime, NaiveDate, SecondsFormat, Utc};
use chrono_tz::America::New_York;
use serde_json::Value;
use std::collections::{HashMap, HashSet};

#[derive(Clone)]
struct Position {
    signal: VpSignal,
    signal_bar: Bar,
    entry_bar: Bar,
    entry_price: f64,
    planned_entry_price: f64,
    planned_sl_price: f64,
    planned_tp_price: f64,
}

#[derive(Clone)]
struct PendingEntry {
    signal: VpSignal,
    signal_bar: Bar,
}

#[derive(Clone)]
struct ClosedTrade {
    position: Position,
    exit_bar: Bar,
    exit_price: f64,
    exit_reason: String,
}

#[derive(Debug)]
pub struct SimulationResult {
    pub rows: Vec<TradeRow>,
    pub traces: Vec<DecisionTraceRow>,
    pub total_input_bars: usize,
    pub total_signals: usize,
    pub signals_without_next_bar: usize,
    pub signals_filtered_by_source_policy: usize,
    pub source_excluded_rth_sessions: usize,
    pub warmup_or_out_of_window_trades: usize,
    pub forced_end_exits: usize,
}

pub struct Simulator {
    instrument: Instrument,
    params: StrategyParams,
    execution: ExecutionConfig,
    quality_flags: HashMap<String, Vec<String>>,
}

impl Simulator {
    pub fn new(
        instrument: Instrument,
        params: StrategyParams,
        execution: ExecutionConfig,
        quality_flags_json: &Value,
    ) -> Self {
        let mut quality_flags = HashMap::new();
        if let Some(entries) = quality_flags_json
            .get(&instrument.symbol)
            .and_then(Value::as_object)
        {
            for (entry_date, value) in entries {
                let values: Vec<String> = value
                    .as_array()
                    .into_iter()
                    .flatten()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect();
                quality_flags.insert(format!("{}|{}", instrument.symbol, entry_date), values);
            }
        }
        Self {
            instrument,
            params,
            execution,
            quality_flags,
        }
    }

    pub fn run(&self, bars: &[Bar]) -> Result<SimulationResult, Box<dyn std::error::Error>> {
        let mut strategy =
            VolumeProfileStrategy::new(self.instrument.tick_size, self.params.clone())?;
        let mut position: Option<Position> = None;
        let mut closed: Vec<ClosedTrade> = Vec::new();
        let mut daily_pnl = HashMap::<NaiveDate, f64>::new();
        let mut total_signals = 0_usize;
        let mut signals_without_next_bar = 0_usize;
        let mut signals_filtered_by_source_policy = 0_usize;
        let mut forced_end_exits = 0_usize;
        let source_excluded_sessions: HashSet<NaiveDate> = bars
            .iter()
            .filter(|bar| {
                bar.is_rth
                    && bar.source != DataSource::Databento
                    && bar.rth_date_et >= self.instrument.start_date_utc
                    && bar.rth_date_et <= self.instrument.end_date_utc
            })
            .map(|bar| bar.rth_date_et)
            .collect();
        let mut pending_entry: Option<PendingEntry> = None;

        for (index, bar) in bars.iter().enumerate() {
            strategy.observe(bar);
            let remaining = bars.len().saturating_sub(index + 1);
            let near_data_end = remaining < self.execution.live_edge_guard_bars;

            let utc_date = bar.utc_date();
            if daily_pnl.get(&utc_date).copied().unwrap_or(0.0)
                <= -self.execution.max_daily_loss_usd
            {
                pending_entry = None;
                if let Some(open) = position.take() {
                    close_trade(
                        open,
                        *bar,
                        bar.close,
                        "flatten",
                        &self.instrument,
                        self.execution.contracts,
                        &mut daily_pnl,
                        &mut closed,
                    );
                    strategy.notify_trade_closed();
                }
                continue;
            }

            if in_flatten_window(bar) {
                pending_entry = None;
                if let Some(open) = position.take() {
                    close_trade(
                        open,
                        *bar,
                        bar.close,
                        "flatten",
                        &self.instrument,
                        self.execution.contracts,
                        &mut daily_pnl,
                        &mut closed,
                    );
                    strategy.notify_trade_closed();
                }
                continue;
            }

            if let Some(open) = position.take() {
                if let Some((exit_price, reason)) = bracket_exit(*bar, &open) {
                    close_trade(
                        open,
                        *bar,
                        exit_price,
                        reason,
                        &self.instrument,
                        self.execution.contracts,
                        &mut daily_pnl,
                        &mut closed,
                    );
                    strategy.notify_trade_closed();
                } else {
                    position = Some(open);
                    continue;
                }
            }

            if let Some(pending) = pending_entry.take() {
                if !valid_next_bar_fill(pending.signal_bar, *bar) {
                    signals_without_next_bar += 1;
                    continue;
                }
                if !source_policy_allows(
                    pending.signal_bar,
                    *bar,
                    self.execution.timestamp_source_policy,
                    &source_excluded_sessions,
                ) {
                    signals_filtered_by_source_policy += 1;
                    continue;
                }
                let open = position_for_next_bar(
                    pending.signal,
                    pending.signal_bar,
                    *bar,
                    self.instrument.tick_size,
                );
                if let Some((exit_price, reason)) = bracket_exit(*bar, &open) {
                    close_trade(
                        open,
                        *bar,
                        exit_price,
                        reason,
                        &self.instrument,
                        self.execution.contracts,
                        &mut daily_pnl,
                        &mut closed,
                    );
                    strategy.notify_trade_closed();
                } else {
                    position = Some(open);
                }
                continue;
            }

            if near_data_end || !bar.is_rth {
                continue;
            }

            if self.execution.timestamp_source_policy
                == TimestampSourcePolicy::DatabentoVerifiedOnly
                && (bar.source != DataSource::Databento
                    || source_excluded_sessions.contains(&bar.rth_date_et))
            {
                continue;
            }

            if let Some(signal) = strategy.evaluate(bar) {
                total_signals += 1;
                match self.execution.entry_fill_mode {
                    EntryFillMode::SignalBarClose => {
                        let open = position_for_signal_close(signal, *bar);
                        if entry_bar_stop_hit(*bar, &open) {
                            let exit_price = open.signal.sl_price;
                            close_trade(
                                open,
                                *bar,
                                exit_price,
                                "sl",
                                &self.instrument,
                                self.execution.contracts,
                                &mut daily_pnl,
                                &mut closed,
                            );
                            strategy.notify_trade_closed();
                        } else {
                            position = Some(open);
                        }
                    }
                    EntryFillMode::NextOneMinuteOpenAfterCompletedSignal => {
                        let Some(next_bar) = bars.get(index + 1) else {
                            signals_without_next_bar += 1;
                            continue;
                        };
                        if !source_policy_allows(
                            *bar,
                            *next_bar,
                            self.execution.timestamp_source_policy,
                            &source_excluded_sessions,
                        ) {
                            signals_filtered_by_source_policy += 1;
                            continue;
                        }
                        if valid_next_bar_fill(*bar, *next_bar) {
                            pending_entry = Some(PendingEntry {
                                signal,
                                signal_bar: *bar,
                            });
                        } else {
                            signals_without_next_bar += 1;
                        }
                    }
                }
            }
        }

        if let Some(open) = position.take() {
            let last = *bars
                .last()
                .ok_or("cannot flatten at end of an empty input")?;
            forced_end_exits += 1;
            close_trade(
                open,
                last,
                last.close,
                "flatten",
                &self.instrument,
                self.execution.contracts,
                &mut daily_pnl,
                &mut closed,
            );
            strategy.notify_trade_closed();
        }

        closed.sort_by_key(|trade| trade.position.entry_bar.timestamp_us);
        let start = self.instrument.start_date_utc;
        let end = self.instrument.end_date_utc;
        let all_count = closed.len();
        let mut rows = Vec::new();
        let mut traces = Vec::new();
        for trade in closed {
            let entry_date_utc = trade.position.entry_bar.utc_date();
            if entry_date_utc < start || entry_date_utc > end {
                continue;
            }
            let sequence = rows.len() as u32 + 1;
            let flags_key = format!(
                "{}|{}",
                self.instrument.symbol, trade.position.entry_bar.rth_date_et
            );
            let flags = self
                .quality_flags
                .get(&flags_key)
                .cloned()
                .unwrap_or_default();
            rows.push(to_trade_row(
                sequence,
                &trade,
                &self.instrument,
                self.execution.contracts,
                &flags,
            )?);
            traces.push(to_trace_row(
                sequence,
                &trade.position,
                &self.instrument.symbol,
                self.execution.entry_fill_mode,
                self.execution.timestamp_source_policy,
            ));
        }

        let included_trade_count = traces.len();
        Ok(SimulationResult {
            rows,
            traces,
            total_input_bars: bars.len(),
            total_signals,
            signals_without_next_bar,
            signals_filtered_by_source_policy,
            source_excluded_rth_sessions: source_excluded_sessions.len(),
            warmup_or_out_of_window_trades: all_count.saturating_sub(included_trade_count),
            forced_end_exits,
        })
    }
}

fn in_flatten_window(bar: &Bar) -> bool {
    let local = bar.utc().with_timezone(&New_York);
    let minutes = local.hour() as u16 * 60 + local.minute() as u16;
    (15 * 60 + 45..18 * 60).contains(&minutes)
}

fn valid_next_bar_fill(signal_bar: Bar, entry_bar: Bar) -> bool {
    entry_bar.is_rth
        && signal_bar.rth_date_et == entry_bar.rth_date_et
        && entry_bar.timestamp_us - signal_bar.timestamp_us == 60_000_000
        && !in_flatten_window(&entry_bar)
}

fn source_policy_allows(
    signal_bar: Bar,
    entry_bar: Bar,
    policy: TimestampSourcePolicy,
    source_excluded_sessions: &HashSet<NaiveDate>,
) -> bool {
    match policy {
        TimestampSourcePolicy::ReferenceParity => false,
        TimestampSourcePolicy::DatabentoVerifiedOnly => {
            signal_bar.source == DataSource::Databento
                && entry_bar.source == DataSource::Databento
                && !source_excluded_sessions.contains(&signal_bar.rth_date_et)
        }
        TimestampSourcePolicy::AssumeAllLabelsAreIntervalStart => true,
    }
}

fn position_for_signal_close(signal: VpSignal, bar: Bar) -> Position {
    Position {
        planned_entry_price: signal.entry_price,
        planned_sl_price: signal.sl_price,
        planned_tp_price: signal.tp_price,
        entry_price: signal.entry_price,
        signal,
        signal_bar: bar,
        entry_bar: bar,
    }
}

fn position_for_next_bar(
    mut signal: VpSignal,
    signal_bar: Bar,
    entry_bar: Bar,
    tick_size: f64,
) -> Position {
    let planned_entry_price = signal.entry_price;
    let planned_sl_price = signal.sl_price;
    let planned_tp_price = signal.tp_price;
    let risk_distance = (planned_entry_price - planned_sl_price).abs();
    let reward_distance = (planned_tp_price - planned_entry_price).abs();
    let entry_price = round_price(entry_bar.open, tick_size);
    let (sl_price, tp_price) = match signal.direction {
        Direction::Long => (
            round_price(entry_price - risk_distance, tick_size),
            round_price(entry_price + reward_distance, tick_size),
        ),
        Direction::Short => (
            round_price(entry_price + risk_distance, tick_size),
            round_price(entry_price - reward_distance, tick_size),
        ),
    };
    signal.entry_price = entry_price;
    signal.sl_price = sl_price;
    signal.tp_price = tp_price;
    Position {
        signal,
        signal_bar,
        entry_bar,
        entry_price,
        planned_entry_price,
        planned_sl_price,
        planned_tp_price,
    }
}

fn entry_bar_stop_hit(bar: Bar, position: &Position) -> bool {
    match position.signal.direction {
        Direction::Long => bar.low <= position.signal.sl_price,
        Direction::Short => bar.high >= position.signal.sl_price,
    }
}

fn bracket_exit(bar: Bar, position: &Position) -> Option<(f64, &'static str)> {
    let sl = position.signal.sl_price;
    let tp = position.signal.tp_price;
    match position.signal.direction {
        Direction::Long => {
            let hit_sl = bar.low <= sl;
            let hit_tp = bar.high >= tp;
            match (hit_sl, hit_tp) {
                (true, true) => {
                    if (bar.open - sl).abs() <= (bar.open - tp).abs() {
                        Some((sl, "sl"))
                    } else {
                        Some((tp, "tp"))
                    }
                }
                (true, false) => Some((sl, "sl")),
                (false, true) => Some((tp, "tp")),
                (false, false) => None,
            }
        }
        Direction::Short => {
            let hit_sl = bar.high >= sl;
            let hit_tp = bar.low <= tp;
            match (hit_sl, hit_tp) {
                (true, true) => {
                    if (bar.open - sl).abs() <= (bar.open - tp).abs() {
                        Some((sl, "sl"))
                    } else {
                        Some((tp, "tp"))
                    }
                }
                (true, false) => Some((sl, "sl")),
                (false, true) => Some((tp, "tp")),
                (false, false) => None,
            }
        }
    }
}

fn close_trade(
    position: Position,
    exit_bar: Bar,
    exit_price: f64,
    exit_reason: &str,
    instrument: &Instrument,
    contracts: i32,
    daily_pnl: &mut HashMap<NaiveDate, f64>,
    closed: &mut Vec<ClosedTrade>,
) {
    let gross = match position.signal.direction {
        Direction::Long => {
            (exit_price - position.entry_price) * instrument.point_value * contracts as f64
        }
        Direction::Short => {
            (position.entry_price - exit_price) * instrument.point_value * contracts as f64
        }
    };
    let commission = instrument.commission_rt * contracts as f64;
    let fees = instrument.fees_rt * contracts as f64;
    let net = gross - commission - fees;
    *daily_pnl.entry(exit_bar.utc_date()).or_default() += net;
    closed.push(ClosedTrade {
        position,
        exit_bar,
        exit_price,
        exit_reason: exit_reason.to_owned(),
    });
}

fn utc_iso(timestamp_us: i64) -> String {
    DateTime::<Utc>::from_timestamp_micros(timestamp_us)
        .expect("validated UTC timestamp")
        .to_rfc3339_opts(SecondsFormat::AutoSi, false)
}

fn et_iso(timestamp_us: i64) -> String {
    DateTime::<Utc>::from_timestamp_micros(timestamp_us)
        .expect("validated UTC timestamp")
        .with_timezone(&New_York)
        .to_rfc3339_opts(SecondsFormat::AutoSi, false)
}

fn to_trade_row(
    sequence: u32,
    trade: &ClosedTrade,
    instrument: &Instrument,
    contracts: i32,
    quality_flags: &[String],
) -> Result<TradeRow, Box<dyn std::error::Error>> {
    let gross_pnl = match trade.position.signal.direction {
        Direction::Long => {
            (trade.exit_price - trade.position.entry_price)
                * instrument.point_value
                * contracts as f64
        }
        Direction::Short => {
            (trade.position.entry_price - trade.exit_price)
                * instrument.point_value
                * contracts as f64
        }
    };
    let commission = instrument.commission_rt * contracts as f64;
    let fees = instrument.fees_rt * contracts as f64;
    let net_pnl = gross_pnl - commission - fees;
    Ok(TradeRow {
        reference_trade_seq: sequence,
        symbol: instrument.symbol.clone(),
        contract_id: instrument.contract_id.clone(),
        entry_time_utc: utc_iso(trade.position.entry_bar.timestamp_us),
        exit_time_utc: utc_iso(trade.exit_bar.timestamp_us),
        entry_time_et: et_iso(trade.position.entry_bar.timestamp_us),
        exit_time_et: et_iso(trade.exit_bar.timestamp_us),
        rth_session_date_et: trade.position.entry_bar.rth_date_et.to_string(),
        topstep_trade_date_ct: trade.position.entry_bar.topstep_trade_date_ct.to_string(),
        direction: trade.position.signal.direction.as_str().to_owned(),
        contracts,
        entry_price: trade.position.entry_price,
        exit_price: trade.exit_price,
        sl_price: trade.position.signal.sl_price,
        tp_price: trade.position.signal.tp_price,
        original_sl_price: trade.position.planned_sl_price,
        original_tp_price: trade.position.planned_tp_price,
        gross_pnl,
        commission,
        fees,
        net_pnl,
        exit_reason: trade.exit_reason.clone(),
        zone_source: "volume_profile".to_owned(),
        setup: trade.position.signal.setup.clone(),
        edge: trade.position.signal.edge.as_str().to_owned(),
        quality_flags: serde_json::to_string(quality_flags)?,
    })
}

fn to_trace_row(
    sequence: u32,
    position: &Position,
    symbol: &str,
    fill_mode: EntryFillMode,
    source_policy: TimestampSourcePolicy,
) -> DecisionTraceRow {
    let signal = &position.signal;
    let signal_available_at_us = match fill_mode {
        EntryFillMode::SignalBarClose => signal.signal_timestamp_us,
        EntryFillMode::NextOneMinuteOpenAfterCompletedSignal => {
            signal.signal_timestamp_us + 60_000_000
        }
    };
    let (fill_model, timestamp_limitation) = match (fill_mode, source_policy) {
        (EntryFillMode::SignalBarClose, _) => (
            "market immediate at rounded signal-bar close; entry-bar SL-only check".to_owned(),
            "Uses the canonical source timestamp as the Python reference does; bar-end availability is not asserted by the source manifest.".to_owned(),
        ),
        (
            EntryFillMode::NextOneMinuteOpenAfterCompletedSignal,
            TimestampSourcePolicy::DatabentoVerifiedOnly,
        ) => (
            "market at next contiguous 1m RTH bar open; planned SL/TP distances re-anchored to fill".to_owned(),
            "Databento OHLCV ts_event marks interval start; signal availability is modeled at source timestamp + 1m. Vendor publication/processing latency is not modeled.".to_owned(),
        ),
        (
            EntryFillMode::NextOneMinuteOpenAfterCompletedSignal,
            TimestampSourcePolicy::AssumeAllLabelsAreIntervalStart,
        ) => (
            "market at next contiguous 1m RTH bar open; planned SL/TP distances re-anchored to fill; all source labels assumed interval-start".to_owned(),
            "TopstepX t interval-edge semantics are absent from the official API reference; this sensitivity assumes interval-start labels and immediate next-bar-open execution.".to_owned(),
        ),
        (
            EntryFillMode::NextOneMinuteOpenAfterCompletedSignal,
            TimestampSourcePolicy::ReferenceParity,
        ) => (
            "invalid source policy for causal next-bar fill".to_owned(),
            "causal next-bar fill requires an explicit timestamp source policy".to_owned(),
        ),
    };
    DecisionTraceRow {
        trace_seq: sequence,
        symbol: symbol.to_owned(),
        source_bar_timestamp_utc: utc_iso(position.signal_bar.timestamp_us),
        signal_source: position.signal_bar.source.as_str().to_owned(),
        entry_source: position.entry_bar.source.as_str().to_owned(),
        decision_timestamp_utc: utc_iso(signal_available_at_us),
        entry_timestamp_et: et_iso(position.entry_bar.timestamp_us),
        rth_session_date_et: signal.rth_session_date_et.to_string(),
        topstep_trade_date_ct: signal.topstep_trade_date_ct.to_string(),
        direction: signal.direction.as_str().to_owned(),
        setup: signal.setup.clone(),
        edge: signal.edge.as_str().to_owned(),
        profile_display_date_et: signal.profile_display_date.to_string(),
        profile_source_date_et: signal.profile_source_date.to_string(),
        poc: signal.poc,
        vah: signal.vah,
        val: signal.val,
        atr_blend: signal.atr_blend,
        entry_price: position.entry_price,
        planned_entry_price: position.planned_entry_price,
        sl_price: signal.sl_price,
        tp_price: signal.tp_price,
        planned_sl_price: position.planned_sl_price,
        planned_tp_price: position.planned_tp_price,
        breakout_accepted_at_utc: signal.accepted_at_us.map(utc_iso).unwrap_or_default(),
        fill_model,
        timestamp_source_policy: source_policy.as_str().to_owned(),
        timestamp_limitation,
    }
}

use chrono::Timelike;
