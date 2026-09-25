use chrono::{DateTime, NaiveDate, Utc};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug)]
pub struct Bar {
    pub timestamp_us: i64,
    pub source: DataSource,
    pub rth_date_et: NaiveDate,
    pub topstep_trade_date_ct: NaiveDate,
    pub is_rth: bool,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: i64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DataSource {
    Databento,
    TopstepX,
}

impl DataSource {
    pub fn from_label(value: &str) -> Result<Self, String> {
        match value.to_ascii_lowercase().as_str() {
            "databento" => Ok(Self::Databento),
            "topstepx" => Ok(Self::TopstepX),
            _ => Err(format!("unsupported market-data source label: {value}")),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Databento => "databento",
            Self::TopstepX => "topstepx",
        }
    }
}

impl Bar {
    pub fn utc(&self) -> DateTime<Utc> {
        DateTime::from_timestamp_micros(self.timestamp_us)
            .expect("validated UTC timestamp in Parquet input")
    }

    pub fn utc_date(&self) -> NaiveDate {
        self.utc().date_naive()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Direction {
    Long,
    Short,
}

impl Direction {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Long => "buy",
            Self::Short => "sell",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Edge {
    Vah,
    Val,
}

impl Edge {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Vah => "vah",
            Self::Val => "val",
        }
    }
}

#[derive(Clone, Debug)]
pub struct VpSignal {
    pub direction: Direction,
    pub edge: Edge,
    pub setup: String,
    pub entry_price: f64,
    pub sl_price: f64,
    pub tp_price: f64,
    pub atr_blend: f64,
    pub profile_display_date: NaiveDate,
    pub profile_source_date: NaiveDate,
    pub poc: f64,
    pub vah: f64,
    pub val: f64,
    pub accepted_at_us: Option<i64>,
    pub signal_timestamp_us: i64,
    pub topstep_trade_date_ct: NaiveDate,
    pub rth_session_date_et: NaiveDate,
}

#[derive(Clone, Debug)]
pub struct Instrument {
    pub symbol: String,
    pub contract_id: String,
    pub point_value: f64,
    pub tick_size: f64,
    pub tick_value: f64,
    pub commission_rt: f64,
    pub fees_rt: f64,
    pub start_date_utc: NaiveDate,
    pub end_date_utc: NaiveDate,
}

#[derive(Clone, Debug)]
pub struct StrategyParams {
    pub entry_mode: String,
    pub target_mode: String,
    pub side_mode: String,
    pub value_area_pct: f64,
    pub sl_atr: f64,
    pub tp_atr: f64,
    pub confirm_bars: u32,
    pub breakout_buffer_ticks: u32,
    pub touch_tolerance_ticks: u32,
    pub reclaim_buffer_ticks: u32,
    pub max_trades_per_day: u32,
    pub min_source_candles: usize,
}

#[derive(Clone, Debug)]
pub struct ExecutionConfig {
    pub contracts: i32,
    pub max_daily_loss_usd: f64,
    pub live_edge_guard_bars: usize,
    pub entry_fill_mode: EntryFillMode,
    pub timestamp_source_policy: TimestampSourcePolicy,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum EntryFillMode {
    SignalBarClose,
    NextOneMinuteOpenAfterCompletedSignal,
}

impl EntryFillMode {
    pub fn from_config(value: &str) -> Result<Self, String> {
        match value {
            "signal_bar_close" => Ok(Self::SignalBarClose),
            "next_1m_open_after_completed_signal" => {
                Ok(Self::NextOneMinuteOpenAfterCompletedSignal)
            }
            _ => Err(format!("unsupported entry_fill mode: {value}")),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::SignalBarClose => "signal_bar_close",
            Self::NextOneMinuteOpenAfterCompletedSignal => "next_1m_open_after_completed_signal",
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TimestampSourcePolicy {
    ReferenceParity,
    DatabentoVerifiedOnly,
    AssumeAllLabelsAreIntervalStart,
}

impl TimestampSourcePolicy {
    pub fn from_config(value: &str) -> Result<Self, String> {
        match value {
            "reference_parity" => Ok(Self::ReferenceParity),
            "databento_verified_only" => Ok(Self::DatabentoVerifiedOnly),
            "assume_source_label_is_interval_start" => Ok(Self::AssumeAllLabelsAreIntervalStart),
            _ => Err(format!("unsupported timestamp_source_policy: {value}")),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::ReferenceParity => "reference_parity",
            Self::DatabentoVerifiedOnly => "databento_verified_only",
            Self::AssumeAllLabelsAreIntervalStart => "assume_source_label_is_interval_start",
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct TradeRow {
    pub reference_trade_seq: u32,
    pub symbol: String,
    pub contract_id: String,
    pub entry_time_utc: String,
    pub exit_time_utc: String,
    pub entry_time_et: String,
    pub exit_time_et: String,
    pub rth_session_date_et: String,
    pub topstep_trade_date_ct: String,
    pub direction: String,
    pub contracts: i32,
    pub entry_price: f64,
    pub exit_price: f64,
    pub sl_price: f64,
    pub tp_price: f64,
    pub original_sl_price: f64,
    pub original_tp_price: f64,
    pub gross_pnl: f64,
    pub commission: f64,
    pub fees: f64,
    pub net_pnl: f64,
    pub exit_reason: String,
    pub zone_source: String,
    pub setup: String,
    pub edge: String,
    pub quality_flags: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct DecisionTraceRow {
    pub trace_seq: u32,
    pub symbol: String,
    pub source_bar_timestamp_utc: String,
    pub signal_source: String,
    pub entry_source: String,
    pub decision_timestamp_utc: String,
    pub entry_timestamp_et: String,
    pub rth_session_date_et: String,
    pub topstep_trade_date_ct: String,
    pub direction: String,
    pub setup: String,
    pub edge: String,
    pub profile_display_date_et: String,
    pub profile_source_date_et: String,
    pub poc: f64,
    pub vah: f64,
    pub val: f64,
    pub atr_blend: f64,
    pub entry_price: f64,
    pub planned_entry_price: f64,
    pub sl_price: f64,
    pub tp_price: f64,
    pub planned_sl_price: f64,
    pub planned_tp_price: f64,
    pub breakout_accepted_at_utc: String,
    pub fill_model: String,
    pub timestamp_source_policy: String,
    pub timestamp_limitation: String,
}
