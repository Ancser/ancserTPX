use crate::model::Bar;
use std::collections::VecDeque;

#[derive(Clone, Copy)]
struct FiveMinuteBar {
    high: f64,
    low: f64,
    close: f64,
    _volume: i64,
}

pub struct AtrBlend {
    history: VecDeque<FiveMinuteBar>,
    current_bucket: Option<i64>,
    current: Option<FiveMinuteBar>,
}

impl AtrBlend {
    pub fn new() -> Self {
        Self {
            history: VecDeque::with_capacity(400),
            current_bucket: None,
            current: None,
        }
    }

    pub fn update(&mut self, bar: &Bar) {
        const FIVE_MINUTE_MICROS: i64 = 5 * 60 * 1_000_000;
        let bucket = bar.timestamp_us.div_euclid(FIVE_MINUTE_MICROS);
        if self.current_bucket == Some(bucket) {
            if let Some(current) = self.current.as_mut() {
                current.high = current.high.max(bar.high);
                current.low = current.low.min(bar.low);
                current.close = bar.close;
                current._volume += bar.volume;
            }
            return;
        }

        if let Some(completed) = self.current.take() {
            self.history.push_back(completed);
            if self.history.len() > 400 {
                self.history.pop_front();
            }
        }
        self.current_bucket = Some(bucket);
        self.current = Some(FiveMinuteBar {
            high: bar.high,
            low: bar.low,
            close: bar.close,
            _volume: bar.volume,
        });
    }

    fn atr(&self, length: usize) -> Option<f64> {
        let minimum = 7.max(length / 2);
        if self.history.len() < minimum {
            return None;
        }
        let skip = self.history.len().saturating_sub(length);
        let segment: Vec<FiveMinuteBar> = self.history.iter().skip(skip).copied().collect();
        if segment.is_empty() {
            return None;
        }
        let mut sum = 0.0;
        for (index, bar) in segment.iter().enumerate() {
            let previous_close = if index > 0 {
                segment[index - 1].close
            } else {
                bar.close
            };
            let tr = (bar.high - bar.low)
                .max((bar.high - previous_close).abs())
                .max((bar.low - previous_close).abs());
            sum += tr;
        }
        Some(sum / segment.len() as f64)
    }

    pub fn value(&self) -> Option<f64> {
        let atr14 = self.atr(14)?;
        if !atr14.is_finite() || atr14 <= 0.0 {
            return None;
        }
        let atr50 = self.atr(50).unwrap_or(atr14);
        Some((atr14 + atr50) / 2.0)
    }
}
