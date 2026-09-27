use crate::core::{Direction, EntryIntent};
use ancsertpx_vp_replay::model::{Bar, Direction as VpDirection, StrategyParams};
use ancsertpx_vp_replay::strategy::VolumeProfileStrategy;
use chrono::{DateTime, Utc};

pub struct VpBreakoutAdapter {
    strategy: VolumeProfileStrategy,
    tick_size: f64,
    size: i32,
    observed_bars: usize,
}

impl VpBreakoutAdapter {
    pub fn new(
        tick_size: f64,
        size: i32,
        params: StrategyParams,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        if !tick_size.is_finite() || tick_size <= 0.0 || size <= 0 {
            return Err("VP adapter requires a positive tick size and size".into());
        }
        Ok(Self {
            strategy: VolumeProfileStrategy::new(tick_size, params)?,
            tick_size,
            size,
            observed_bars: 0,
        })
    }

    pub fn warmup(&mut self, bars: &[Bar]) {
        for bar in bars {
            self.strategy.observe(bar);
            self.observed_bars += 1;
        }
    }

    pub fn on_completed_bar(&mut self, bar: &Bar) -> Option<EntryIntent> {
        self.strategy.observe(bar);
        self.observed_bars += 1;
        let signal = self.strategy.evaluate(bar)?;
        let signal_time = DateTime::<Utc>::from_timestamp_micros(signal.signal_timestamp_us)?;
        Some(EntryIntent {
            strategy_id: format!("vp_breakout_retest_atr_{}", signal.edge.as_str()),
            signal_time_utc: signal_time,
            direction: match signal.direction {
                VpDirection::Long => Direction::Long,
                VpDirection::Short => Direction::Short,
            },
            entry_price: signal.entry_price,
            stop_price: signal.sl_price,
            target_price: signal.tp_price,
            tick_size: self.tick_size,
            size: self.size,
            reason: signal.setup,
        })
    }

    pub fn observed_bars(&self) -> usize {
        self.observed_bars
    }
}

pub fn demo_params() -> StrategyParams {
    StrategyParams {
        entry_mode: "breakout".to_owned(),
        target_mode: "atr".to_owned(),
        side_mode: "all".to_owned(),
        value_area_pct: 0.80,
        sl_atr: 1.0,
        tp_atr: 1.5,
        confirm_bars: 1,
        breakout_buffer_ticks: 1,
        touch_tolerance_ticks: 2,
        reclaim_buffer_ticks: 0,
        max_trades_per_day: 1,
        min_source_candles: 10,
    }
}
