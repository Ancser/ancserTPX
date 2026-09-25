use crate::model::Bar;
use chrono::NaiveDate;
use std::collections::BTreeMap;

#[derive(Clone, Debug)]
pub struct Profile {
    pub display_date: NaiveDate,
    pub source_date: NaiveDate,
    pub poc_tick: i64,
    pub vah_tick: i64,
    pub val_tick: i64,
    pub high_100_tick: i64,
    pub low_100_tick: i64,
    pub source_candles: usize,
    pub source_volume: i64,
    pub value_area_pct: f64,
}

impl Profile {
    pub fn poc(&self, tick_size: f64) -> f64 {
        self.poc_tick as f64 * tick_size
    }
    pub fn vah(&self, tick_size: f64) -> f64 {
        self.vah_tick as f64 * tick_size
    }
    pub fn val(&self, tick_size: f64) -> f64 {
        self.val_tick as f64 * tick_size
    }
}

#[derive(Clone, Copy)]
struct ProfileBar {
    high: f64,
    low: f64,
    close: f64,
    volume: i64,
}

fn round_tick(price: f64, tick_size: f64) -> i64 {
    (price / tick_size).round_ties_even() as i64
}

fn make_profile(
    source_date: NaiveDate,
    display_date: NaiveDate,
    bars: &[ProfileBar],
    tick_size: f64,
    value_area_pct: f64,
    min_source_candles: usize,
) -> Option<Profile> {
    if bars.len() < min_source_candles {
        return None;
    }

    let mut price_volume = BTreeMap::<i64, i64>::new();
    for bar in bars {
        if bar.volume <= 0 {
            continue;
        }
        let high_tick = round_tick(bar.high, tick_size);
        let low_tick = round_tick(bar.low, tick_size);
        if high_tick <= low_tick {
            *price_volume
                .entry(round_tick(bar.close, tick_size))
                .or_default() += bar.volume;
            continue;
        }
        let num_ticks = high_tick - low_tick + 1;
        let volume_per_tick = bar.volume / num_ticks;
        for tick in low_tick..=high_tick {
            *price_volume.entry(tick).or_default() += volume_per_tick;
        }
    }
    if price_volume.is_empty() {
        return None;
    }

    let profile_volume: i64 = price_volume.values().sum();
    let max_volume = *price_volume.values().max()?;
    let poc_candidates: Vec<i64> = price_volume
        .iter()
        .filter_map(|(tick, volume)| (*volume == max_volume).then_some(*tick))
        .collect();
    let poc_tick = *poc_candidates.get(poc_candidates.len() / 2)?;

    let ticks: Vec<i64> = price_volume.keys().copied().collect();
    let poc_index = ticks.binary_search(&poc_tick).ok()?;
    let target_volume = profile_volume as f64 * value_area_pct;
    let mut cumulative = *price_volume.get(&poc_tick)?;
    let mut upper = poc_index;
    let mut lower = poc_index;

    while (cumulative as f64) < target_volume {
        let can_up = upper + 1 < ticks.len();
        let can_down = lower > 0;
        if !can_up && !can_down {
            break;
        }
        let mut volume_up = 0_i64;
        if can_up {
            volume_up += *price_volume.get(&ticks[upper + 1]).unwrap_or(&0);
            if upper + 2 < ticks.len() {
                volume_up += *price_volume.get(&ticks[upper + 2]).unwrap_or(&0);
            }
        }
        let mut volume_down = 0_i64;
        if can_down {
            volume_down += *price_volume.get(&ticks[lower - 1]).unwrap_or(&0);
            if lower >= 2 {
                volume_down += *price_volume.get(&ticks[lower - 2]).unwrap_or(&0);
            }
        }

        if !can_down || (can_up && volume_up >= volume_down) {
            upper += 1;
            cumulative += *price_volume.get(&ticks[upper]).unwrap_or(&0);
        } else if !can_up || (can_down && volume_down > volume_up) {
            lower -= 1;
            cumulative += *price_volume.get(&ticks[lower]).unwrap_or(&0);
        }
    }

    Some(Profile {
        display_date,
        source_date,
        poc_tick,
        vah_tick: ticks[upper],
        val_tick: ticks[lower],
        high_100_tick: *ticks.last()?,
        low_100_tick: *ticks.first()?,
        source_candles: bars.len(),
        source_volume: profile_volume,
        value_area_pct,
    })
}

pub struct PriorRthTracker {
    tick_size: f64,
    value_area_pct: f64,
    min_source_candles: usize,
    current_date: Option<NaiveDate>,
    current_bars: Vec<ProfileBar>,
    levels: Option<Profile>,
}

impl PriorRthTracker {
    pub fn new(tick_size: f64, value_area_pct: f64, min_source_candles: usize) -> Self {
        Self {
            tick_size,
            value_area_pct,
            min_source_candles,
            current_date: None,
            current_bars: Vec::with_capacity(400),
            levels: None,
        }
    }

    pub fn current_date(&self) -> Option<NaiveDate> {
        self.current_date
    }

    pub fn update(&mut self, bar: &Bar) -> Option<Profile> {
        if !bar.is_rth {
            return self.levels.clone();
        }

        let current_date = bar.rth_date_et;
        match self.current_date {
            None => {
                self.current_date = Some(current_date);
            }
            Some(previous_date) if current_date != previous_date => {
                self.levels = make_profile(
                    previous_date,
                    current_date,
                    &self.current_bars,
                    self.tick_size,
                    self.value_area_pct,
                    self.min_source_candles,
                );
                self.current_date = Some(current_date);
                self.current_bars.clear();
                if let Some(levels) = self.levels.as_mut() {
                    levels.display_date = current_date;
                }
            }
            _ => {}
        }

        self.current_bars.push(ProfileBar {
            high: bar.high,
            low: bar.low,
            close: bar.close,
            volume: bar.volume,
        });
        self.levels.clone()
    }
}
