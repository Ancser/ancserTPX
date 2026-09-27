use std::collections::{BTreeMap, HashSet, VecDeque};

use chrono::{DateTime, Duration, Timelike, Utc};
use serde::{Deserialize, Serialize};

use crate::clock::topstep_trade_date;
use crate::exit::{ExitPolicy, PiExitConfig, Side, resolve_pi_exit};

const HISTORY_TOLERANCE_MINUTES: i64 = 2;
const MAX_SIGNAL_QUEUE: usize = 32;
const ATR_HISTORY_LIMIT: usize = 400;

#[derive(Clone, Debug, Deserialize)]
pub struct PiReplayFixture {
    pub schema: String,
    pub params: PiParams,
    pub marks: Vec<PiMark>,
    pub bars: Vec<PiBar>,
    #[serde(default)]
    pub trace_atr: bool,
}

#[derive(Clone, Debug, Deserialize)]
pub struct PiParams {
    pub future: String,
    pub contract_size: u32,
    pub tick_size: f64,
    pub timeframe_minutes: u32,
    pub sl_atr: f64,
    pub rr_ratio: f64,
    pub max_trades_per_day: u32,
    pub long_only: bool,
    pub long_kinds: Vec<String>,
    pub short_kinds: Vec<String>,
    pub short_levels: Option<Vec<i32>>,
    pub short_sl: f64,
    pub max_signal_age_minutes: i64,
    pub side_mode: String,
    pub exit: PiExitConfig,
}

#[derive(Clone, Debug, Deserialize)]
pub struct PiMark {
    pub ts: String,
    pub message_id: String,
    pub equity: String,
    pub future: String,
    pub direction: i8,
    pub kind: String,
    pub size: String,
    pub level: Option<i32>,
    pub pos: Option<String>,
}

#[derive(Clone, Debug, Deserialize)]
pub struct PiBar {
    pub ts: String,
    pub open: f64,
    pub high: f64,
    pub low: f64,
    pub close: f64,
    pub volume: i64,
}

#[derive(Clone, Debug, Serialize)]
pub struct PushAttempt {
    pub mark_id: String,
    pub ts: String,
    pub future: String,
    pub kind: String,
    pub level: Option<i32>,
    pub accepted: bool,
    pub reason: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct SourceAttempt {
    pub mark_id: String,
    pub bar_ts: String,
    pub accepted: bool,
    pub reason: String,
}

#[derive(Clone, Debug, Serialize)]
pub struct PiDecision {
    pub message_id: String,
    pub signal_ts: String,
    pub bar_ts: String,
    pub zone_id: String,
    pub trade_date: String,
    pub equity: String,
    pub future: String,
    pub kind: String,
    pub level: Option<i32>,
    pub size: String,
    pub direction: String,
    pub quantity: u32,
    pub entry: f64,
    pub stop_loss: f64,
    pub take_profit: f64,
    pub atr_blend: f64,
    pub risk_width: f64,
    pub reason: String,
    pub exit_policy: ExitPolicy,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct PiAtrBracket {
    pub entry: f64,
    pub stop_loss: f64,
    pub take_profit: f64,
    pub risk_width: f64,
}

#[derive(Clone, Debug, Serialize)]
pub struct PiReplayReport {
    pub schema: String,
    pub marks_seen: usize,
    pub bars_seen: usize,
    pub push_attempts: Vec<PushAttempt>,
    pub source_attempts: Vec<SourceAttempt>,
    pub decisions: Vec<PiDecision>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub atr_trace: Vec<PiAtrTracePoint>,
}

#[derive(Clone, Debug, Serialize)]
pub struct PiAtrTracePoint {
    pub bar_index: usize,
    pub current_bucket_start_utc: String,
    pub current_open: f64,
    pub current_high: f64,
    pub current_low: f64,
    pub current_close: f64,
    pub current_volume: i128,
    pub completed_bars: usize,
    pub atr14: Option<f64>,
    pub atr50: Option<f64>,
    pub atr_blend: Option<f64>,
}

#[derive(Clone, Debug)]
struct TimedMark {
    timestamp: DateTime<Utc>,
    source: PiMark,
}

#[derive(Clone, Debug)]
struct QueuedMark {
    mark: TimedMark,
}

#[derive(Clone, Debug)]
struct AggregateBar {
    bucket: DateTime<Utc>,
    open: f64,
    high: f64,
    low: f64,
    close: f64,
    volume: i128,
}

pub fn replay(fixture: PiReplayFixture) -> Result<PiReplayReport, String> {
    if fixture.schema != "ancsertpx.live-runtime-pi-replay.v1" {
        return Err("unsupported PI replay fixture schema".to_owned());
    }
    if fixture.params.tick_size <= 0.0
        || fixture.params.timeframe_minutes == 0
        || fixture.params.contract_size == 0
    {
        return Err("PI fixture contains invalid tick, timeframe, or quantity".to_owned());
    }

    let mut marks = fixture
        .marks
        .into_iter()
        .map(|source| {
            let timestamp = DateTime::parse_from_rfc3339(&source.ts)
                .map_err(|error| format!("invalid PI mark timestamp {}: {error}", source.ts))?
                .with_timezone(&Utc);
            Ok(TimedMark { timestamp, source })
        })
        .collect::<Result<Vec<_>, String>>()?;
    marks.sort_by_key(|mark| mark.timestamp);

    let trace_atr = fixture.trace_atr;
    let mut state = PiReplayState::new(fixture.params, marks.len());
    let input_mark_count = marks.len();
    for bar in fixture.bars {
        let timestamp = DateTime::parse_from_rfc3339(&bar.ts)
            .map_err(|error| format!("invalid PI bar timestamp {}: {error}", bar.ts))?
            .with_timezone(&Utc);
        state.roll_bar(timestamp, &bar)?;
        if trace_atr {
            state.record_atr_trace();
        }
        state.drain_history(timestamp, &marks);
        state.evaluate_queue(timestamp, &bar)?;
    }

    Ok(PiReplayReport {
        schema: "ancsertpx.live-runtime-pi-replay.result.v1".to_owned(),
        marks_seen: input_mark_count,
        bars_seen: state.bars_seen,
        push_attempts: state.push_attempts,
        source_attempts: state.source_attempts,
        decisions: state.decisions,
        atr_trace: state.atr_trace,
    })
}

struct PiReplayState {
    params: PiParams,
    completed_bars: VecDeque<AggregateBar>,
    current_bar: Option<AggregateBar>,
    history_index: usize,
    queue: VecDeque<QueuedMark>,
    seen: HashSet<String>,
    daily_signal_count: BTreeMap<String, u32>,
    push_attempts: Vec<PushAttempt>,
    source_attempts: Vec<SourceAttempt>,
    decisions: Vec<PiDecision>,
    atr_trace: Vec<PiAtrTracePoint>,
    bars_seen: usize,
}

impl PiReplayState {
    fn new(params: PiParams, mark_count: usize) -> Self {
        Self {
            params,
            completed_bars: VecDeque::with_capacity(ATR_HISTORY_LIMIT),
            current_bar: None,
            history_index: 0,
            queue: VecDeque::with_capacity(MAX_SIGNAL_QUEUE),
            seen: HashSet::with_capacity(mark_count),
            daily_signal_count: BTreeMap::new(),
            push_attempts: Vec::new(),
            source_attempts: Vec::new(),
            decisions: Vec::new(),
            atr_trace: Vec::new(),
            bars_seen: 0,
        }
    }

    fn roll_bar(&mut self, timestamp: DateTime<Utc>, bar: &PiBar) -> Result<(), String> {
        self.bars_seen += 1;
        let minute = timestamp
            .with_second(0)
            .and_then(|value| value.with_nanosecond(0))
            .ok_or_else(|| format!("invalid bar timestamp {}", bar.ts))?;
        let minute_of_day = minute.hour() * 60 + minute.minute();
        let bucket_minute = minute_of_day - minute_of_day % self.params.timeframe_minutes;
        let bucket = minute
            .date_naive()
            .and_hms_opt(bucket_minute / 60, bucket_minute % 60, 0)
            .ok_or_else(|| format!("invalid aggregate bucket for {}", bar.ts))?
            .and_utc();

        match self.current_bar.as_mut() {
            None => {
                self.current_bar = Some(AggregateBar {
                    bucket,
                    open: bar.open,
                    high: bar.high,
                    low: bar.low,
                    close: bar.close,
                    volume: i128::from(bar.volume),
                });
            }
            Some(current) if current.bucket != bucket => {
                let completed = self.current_bar.replace(AggregateBar {
                    bucket,
                    open: bar.open,
                    high: bar.high,
                    low: bar.low,
                    close: bar.close,
                    volume: i128::from(bar.volume),
                });
                if let Some(completed) = completed {
                    if self.completed_bars.len() == ATR_HISTORY_LIMIT {
                        self.completed_bars.pop_front();
                    }
                    self.completed_bars.push_back(completed);
                }
            }
            Some(current) => {
                current.high = current.high.max(bar.high);
                current.low = current.low.min(bar.low);
                current.close = bar.close;
                current.volume += i128::from(bar.volume);
            }
        }
        Ok(())
    }

    fn record_atr_trace(&mut self) {
        let Some(current) = self.current_bar.as_ref() else {
            return;
        };
        self.atr_trace.push(PiAtrTracePoint {
            bar_index: self.bars_seen - 1,
            current_bucket_start_utc: current.bucket.to_rfc3339(),
            current_open: current.open,
            current_high: current.high,
            current_low: current.low,
            current_close: current.close,
            current_volume: current.volume,
            completed_bars: self.completed_bars.len(),
            atr14: self.atr(14),
            atr50: self.atr(50),
            atr_blend: self.atr_blend(),
        });
    }

    fn drain_history(&mut self, now: DateTime<Utc>, marks: &[TimedMark]) {
        let tolerance = Duration::minutes(HISTORY_TOLERANCE_MINUTES);
        while let Some(mark) = marks.get(self.history_index) {
            if mark.timestamp > now + tolerance {
                break;
            }
            self.history_index += 1;
            if now - mark.timestamp <= tolerance {
                self.push(mark.clone());
            }
        }
    }

    fn push(&mut self, mark: TimedMark) {
        let level = mark.source.level.or_else(|| parse_level(&mark.source.size));
        let seen_key = format!(
            "{}:{}:{}",
            mark.source.message_id,
            mark.source.kind,
            level.map_or_else(|| "None".to_owned(), |value| value.to_string())
        );

        let (accepted, reason) = if self.seen.contains(&seen_key) {
            (false, "duplicate")
        } else if !self.params.future.is_empty() && mark.source.future != self.params.future {
            (false, "wrong_future")
        } else if self.params.long_only && mark.source.direction <= 0 {
            (false, "long_only")
        } else if mark.source.direction < 0
            && mark.source.kind == "紫圈"
            && self.params.short_levels.as_ref().is_some_and(|levels| {
                level.is_none_or(|source_level| !levels.contains(&source_level))
            })
        {
            (false, "short_level_not_selected")
        } else {
            let allowed = if mark.source.direction > 0 {
                &self.params.long_kinds
            } else {
                &self.params.short_kinds
            };
            if !allowed.contains(&mark.source.kind) {
                (false, "kind_not_selected")
            } else {
                (true, "queued")
            }
        };

        self.push_attempts.push(PushAttempt {
            mark_id: mark.source.message_id.clone(),
            ts: mark.source.ts.clone(),
            future: mark.source.future.clone(),
            kind: mark.source.kind.clone(),
            level,
            accepted,
            reason: reason.to_owned(),
        });
        if !accepted {
            return;
        }
        self.seen.insert(seen_key);
        if self.seen.len() > 4000 {
            self.seen.clear();
        }
        if self.queue.len() == MAX_SIGNAL_QUEUE {
            self.queue.pop_front();
        }
        self.queue.push_back(QueuedMark { mark });
    }

    fn evaluate_queue(&mut self, now: DateTime<Utc>, bar: &PiBar) -> Result<(), String> {
        while let Some(queued) = self.queue.pop_front() {
            let mark = queued.mark;
            let age_minutes = (now - mark.timestamp).num_milliseconds() as f64 / 60_000.0;
            let rejection = if age_minutes > self.params.max_signal_age_minutes as f64 {
                Some("signal_too_old")
            } else if age_minutes < -2.0 {
                Some("signal_from_future")
            } else if self.params.long_only && mark.source.direction <= 0 {
                Some("long_only")
            } else {
                None
            };
            if let Some(reason) = rejection {
                self.record_source_rejection(&mark, bar, reason);
                continue;
            }

            let side = if mark.source.direction > 0 {
                Side::Buy
            } else {
                Side::Sell
            };
            let Some(atr_blend) = self.atr_blend() else {
                self.record_source_rejection(&mark, bar, "atr_not_warm");
                continue;
            };
            let trade_date = topstep_trade_date(now).to_string();
            let count = self
                .daily_signal_count
                .get(&trade_date)
                .copied()
                .unwrap_or(0);
            if self.params.max_trades_per_day > 0 && count >= self.params.max_trades_per_day {
                self.record_source_rejection(&mark, bar, "daily_signal_cap");
                continue;
            }
            if (self.params.side_mode.eq_ignore_ascii_case("long_only") && side != Side::Buy)
                || (self.params.side_mode.eq_ignore_ascii_case("short_only") && side != Side::Sell)
            {
                self.record_source_rejection(&mark, bar, "side_mode");
                continue;
            }

            let Some(bracket) = atr_bracket(bar.close, atr_blend, side, &self.params) else {
                self.record_source_rejection(&mark, bar, "rounded_bracket_invalid");
                continue;
            };

            let reason = format!(
                "PI | PI {} {}/{}{}",
                mark.source.equity,
                mark.source.kind,
                mark.source.size,
                mark.source
                    .pos
                    .as_deref()
                    .filter(|value| !value.is_empty())
                    .map(|value| format!("/{value}"))
                    .unwrap_or_default()
            );
            let exit_policy = resolve_pi_exit(&self.params.exit, side);
            let zone_id = format!("PI:{}:{}", trade_date, python_iso_timestamp(now));
            self.source_attempts.push(SourceAttempt {
                mark_id: mark.source.message_id.clone(),
                bar_ts: bar.ts.clone(),
                accepted: true,
                reason: "signal_created".to_owned(),
            });
            self.decisions.push(PiDecision {
                message_id: mark.source.message_id,
                signal_ts: python_datetime_str(mark.timestamp),
                bar_ts: bar.ts.clone(),
                zone_id,
                trade_date: trade_date.clone(),
                equity: mark.source.equity,
                future: mark.source.future,
                kind: mark.source.kind,
                level: mark.source.level.or_else(|| parse_level(&mark.source.size)),
                size: mark.source.size,
                direction: match side {
                    Side::Buy => "buy",
                    Side::Sell => "sell",
                }
                .to_owned(),
                quantity: self.params.contract_size,
                entry: bracket.entry,
                stop_loss: bracket.stop_loss,
                take_profit: bracket.take_profit,
                atr_blend,
                risk_width: bracket.risk_width,
                reason,
                exit_policy,
            });
            self.daily_signal_count.insert(trade_date, count + 1);
            break;
        }
        Ok(())
    }

    fn record_source_rejection(&mut self, mark: &TimedMark, bar: &PiBar, reason: &str) {
        self.source_attempts.push(SourceAttempt {
            mark_id: mark.source.message_id.clone(),
            bar_ts: bar.ts.clone(),
            accepted: false,
            reason: reason.to_owned(),
        });
    }

    fn atr_blend(&self) -> Option<f64> {
        let atr14 = self.atr(14)?;
        if atr14 <= 0.0 {
            return None;
        }
        let atr50 = self.atr(50).unwrap_or(atr14);
        Some((atr14 + atr50) / 2.0)
    }

    fn atr(&self, length: usize) -> Option<f64> {
        let minimum = 7.max(length / 2);
        if self.completed_bars.len() < minimum {
            return None;
        }
        let start = self.completed_bars.len().saturating_sub(length);
        let segment = self.completed_bars.iter().skip(start).collect::<Vec<_>>();
        if segment.is_empty() {
            return None;
        }
        let mut total = 0.0;
        for (index, bar) in segment.iter().enumerate() {
            let previous_close = if index == 0 {
                bar.close
            } else {
                segment[index - 1].close
            };
            total += (bar.high - bar.low)
                .max((bar.high - previous_close).abs())
                .max((bar.low - previous_close).abs());
        }
        Some(total / segment.len() as f64)
    }
}

pub fn atr_bracket(
    close: f64,
    atr_blend: f64,
    side: Side,
    params: &PiParams,
) -> Option<PiAtrBracket> {
    let mut risk_width = atr_blend;
    if side == Side::Sell && params.sl_atr > 0.0 {
        risk_width *= params.short_sl / params.sl_atr;
    }
    let risk = risk_width * params.sl_atr;
    let entry = round_to_tick(close, params.tick_size);
    let (stop_loss, take_profit) = match side {
        Side::Buy => (
            round_to_tick(entry - risk, params.tick_size),
            round_to_tick(entry + risk * params.rr_ratio, params.tick_size),
        ),
        Side::Sell => (
            round_to_tick(entry + risk, params.tick_size),
            round_to_tick(entry - risk * params.rr_ratio, params.tick_size),
        ),
    };
    if entry == stop_loss || entry == take_profit {
        return None;
    }
    Some(PiAtrBracket {
        entry,
        stop_loss,
        take_profit,
        risk_width,
    })
}

pub fn round_to_tick(price: f64, tick_size: f64) -> f64 {
    if tick_size <= 0.0 {
        return price;
    }
    (price / tick_size).round_ties_even() * tick_size
}

pub fn parse_level(size: &str) -> Option<i32> {
    let raw = size.strip_prefix("Level ")?;
    raw.parse().ok()
}

pub fn python_iso_timestamp(timestamp: DateTime<Utc>) -> String {
    timestamp.format("%Y-%m-%dT%H:%M:%S%.f+00:00").to_string()
}

pub fn python_datetime_str(timestamp: DateTime<Utc>) -> String {
    timestamp.format("%Y-%m-%d %H:%M:%S%.f+00:00").to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tick_rounding_matches_python_ties_to_even() {
        assert_eq!(round_to_tick(100.125, 0.25), 100.0);
        assert_eq!(round_to_tick(100.375, 0.25), 100.5);
        assert_eq!(round_to_tick(100.625, 0.25), 100.5);
    }

    #[test]
    fn source_level_falls_back_to_legacy_size_only() {
        assert_eq!(parse_level("Level 2"), Some(2));
        assert_eq!(parse_level("Large"), None);
    }
}
