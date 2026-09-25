use crate::atr::AtrBlend;
use crate::model::{Bar, Direction, Edge, StrategyParams, VpSignal};
use crate::profile::{PriorRthTracker, Profile};
use chrono::NaiveDate;
use std::collections::{HashMap, HashSet};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BreakSide {
    Up,
    Down,
}

pub struct VolumeProfileStrategy {
    tick_size: f64,
    params: StrategyParams,
    atr: AtrBlend,
    tracker: PriorRthTracker,
    levels: Option<Profile>,
    profile_day: Option<NaiveDate>,
    edge_used: HashSet<String>,
    outside_side: Option<BreakSide>,
    outside_count: u32,
    accepted_breakout: Option<BreakSide>,
    accepted_at_us: Option<i64>,
    failed_break_side: Option<BreakSide>,
    last_observed_key: Option<i64>,
    daily_trades: HashMap<NaiveDate, u32>,
}

impl VolumeProfileStrategy {
    pub fn new(tick_size: f64, params: StrategyParams) -> Result<Self, Box<dyn std::error::Error>> {
        if params.entry_mode != "breakout" {
            return Err(format!(
                "this frozen parity runner supports entry_mode=breakout, got {}",
                params.entry_mode
            )
            .into());
        }
        if params.target_mode != "atr" || params.side_mode != "all" {
            return Err(format!(
                "this frozen parity runner supports target_mode=atr and side_mode=all, got {}/{}",
                params.target_mode, params.side_mode
            )
            .into());
        }
        Ok(Self {
            tick_size,
            tracker: PriorRthTracker::new(
                tick_size,
                params.value_area_pct,
                params.min_source_candles,
            ),
            params,
            atr: AtrBlend::new(),
            levels: None,
            profile_day: None,
            edge_used: HashSet::new(),
            outside_side: None,
            outside_count: 0,
            accepted_breakout: None,
            accepted_at_us: None,
            failed_break_side: None,
            last_observed_key: None,
            daily_trades: HashMap::new(),
        })
    }

    pub fn observe(&mut self, bar: &Bar) {
        let key_seconds = bar.timestamp_us.div_euclid(1_000_000);
        if self.last_observed_key == Some(key_seconds) {
            return;
        }

        self.atr.update(bar);
        let levels = self.tracker.update(bar);
        let profile_day = self.tracker.current_date();
        if profile_day != self.profile_day {
            self.profile_day = profile_day;
            self.levels = levels;
            self.reset_profile_state();
        } else if levels.is_some() {
            self.levels = levels;
        }
        self.update_break_state(bar);
        self.last_observed_key = Some(key_seconds);
    }

    fn reset_profile_state(&mut self) {
        self.edge_used.clear();
        self.outside_side = None;
        self.outside_count = 0;
        self.accepted_breakout = None;
        self.accepted_at_us = None;
        self.failed_break_side = None;
    }

    fn edge_key(&self, edge: Edge) -> Option<String> {
        self.levels
            .as_ref()
            .map(|levels| format!("{}:{}", levels.display_date, edge.as_str()))
    }

    fn update_break_state(&mut self, bar: &Bar) {
        if !bar.is_rth {
            return;
        }
        let Some(levels) = self.levels.as_ref() else {
            return;
        };
        let vah = levels.vah(self.tick_size);
        let val = levels.val(self.tick_size);
        let breakout_buffer = self.params.breakout_buffer_ticks as f64 * self.tick_size;
        let reclaim_buffer = self.params.reclaim_buffer_ticks as f64 * self.tick_size;
        let upper = vah + breakout_buffer;
        let lower = val - breakout_buffer;
        let up_reclaim = vah - reclaim_buffer;
        let down_reclaim = val + reclaim_buffer;
        let outside = if bar.close > upper {
            Some(BreakSide::Up)
        } else if bar.close < lower {
            Some(BreakSide::Down)
        } else {
            None
        };

        if let Some(side) = outside {
            if self.outside_side == Some(side) {
                self.outside_count += 1;
            } else {
                self.outside_side = Some(side);
                self.outside_count = 1;
            }
            self.failed_break_side = None;
            if self.outside_count >= self.params.confirm_bars
                && self.accepted_breakout != Some(side)
            {
                self.accepted_breakout = Some(side);
                self.accepted_at_us = Some(bar.timestamp_us);
            }
            return;
        }

        if self.outside_side == Some(BreakSide::Up) && bar.close < up_reclaim {
            self.failed_break_side = Some(BreakSide::Up);
            self.accepted_breakout = None;
            self.accepted_at_us = None;
        } else if self.outside_side == Some(BreakSide::Down) && bar.close > down_reclaim {
            self.failed_break_side = Some(BreakSide::Down);
            self.accepted_breakout = None;
            self.accepted_at_us = None;
        }
        self.outside_side = None;
        self.outside_count = 0;
    }

    pub fn evaluate(&mut self, bar: &Bar) -> Option<VpSignal> {
        if self.last_observed_key != Some(bar.timestamp_us.div_euclid(1_000_000)) {
            self.observe(bar);
        }
        if !bar.is_rth {
            return None;
        }
        let levels = self.levels.clone()?;
        let poc = levels.poc(self.tick_size);
        let vah = levels.vah(self.tick_size);
        let val = levels.val(self.tick_size);
        if !(val < poc && poc < vah) {
            return None;
        }
        let side = self.accepted_breakout?;
        if self
            .accepted_at_us
            .is_some_and(|accepted| bar.timestamp_us <= accepted)
        {
            return None;
        }
        let touch = self.params.touch_tolerance_ticks as f64 * self.tick_size;
        let breakout = self.params.breakout_buffer_ticks as f64 * self.tick_size;
        let (direction, edge, qualifies) = match side {
            BreakSide::Up => (
                Direction::Long,
                Edge::Vah,
                bar.low <= vah + touch && bar.close > vah + breakout,
            ),
            BreakSide::Down => (
                Direction::Short,
                Edge::Val,
                bar.high >= val - touch && bar.close < val - breakout,
            ),
        };
        if !qualifies {
            return None;
        }

        self.make_signal(bar, levels, direction, edge, poc, vah, val)
    }

    fn make_signal(
        &mut self,
        bar: &Bar,
        levels: Profile,
        direction: Direction,
        edge: Edge,
        poc: f64,
        vah: f64,
        val: f64,
    ) -> Option<VpSignal> {
        if self.params.max_trades_per_day > 0
            && self
                .daily_trades
                .get(&bar.topstep_trade_date_ct)
                .copied()
                .unwrap_or(0)
                >= self.params.max_trades_per_day
        {
            return None;
        }
        let edge_key = self.edge_key(edge)?;
        if self.edge_used.contains(&edge_key) {
            return None;
        }
        let atr = self.atr.value()?;
        let entry = round_price(bar.close, self.tick_size);
        let risk_distance = self.tick_size.max(atr * self.params.sl_atr);
        let reward_distance = self.tick_size.max(atr * self.params.tp_atr);
        let raw_sl = match direction {
            Direction::Long => entry - risk_distance,
            Direction::Short => entry + risk_distance,
        };
        let raw_tp = match direction {
            Direction::Long => entry + reward_distance,
            Direction::Short => entry - reward_distance,
        };
        let sl = round_price(raw_sl, self.tick_size);
        let tp = round_price(raw_tp, self.tick_size);
        let valid = match direction {
            Direction::Long => sl < entry && entry < tp,
            Direction::Short => tp < entry && entry < sl,
        };
        if !valid {
            return None;
        }

        self.edge_used.insert(edge_key);
        *self
            .daily_trades
            .entry(bar.topstep_trade_date_ct)
            .or_default() += 1;
        Some(VpSignal {
            direction,
            edge,
            setup: "breakout_retest".to_owned(),
            entry_price: entry,
            sl_price: sl,
            tp_price: tp,
            atr_blend: atr,
            profile_display_date: levels.display_date,
            profile_source_date: levels.source_date,
            poc,
            vah,
            val,
            accepted_at_us: self.accepted_at_us,
            signal_timestamp_us: bar.timestamp_us,
            topstep_trade_date_ct: bar.topstep_trade_date_ct,
            rth_session_date_et: bar.rth_date_et,
        })
    }

    pub fn notify_trade_closed(&mut self) {
        // Matches VolumeProfileStrategy.notify_trade_closed: consume the edge
        // for this displayed RTH day, then clear only transient order state.
    }
}

pub fn round_price(price: f64, tick_size: f64) -> f64 {
    let ticks = (price / tick_size).round_ties_even();
    let rounded = ticks * tick_size;
    (rounded * 10_000.0).round_ties_even() / 10_000.0
}
