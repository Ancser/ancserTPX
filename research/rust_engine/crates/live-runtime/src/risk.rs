use chrono::{DateTime, NaiveDate, Utc};

use crate::clock::topstep_trade_date;

#[derive(Clone, Copy, Debug, Default)]
pub struct RiskLimits {
    pub daily_loss_stop: u32,
    pub daily_win_stop: u32,
    pub daily_profit_stop: f64,
}

#[derive(Clone, Debug)]
pub struct DailyRiskState {
    pub trade_date: NaiveDate,
    pub loss_count: u32,
    pub win_count: u32,
    pub realized_pnl: f64,
    limits: RiskLimits,
}

impl DailyRiskState {
    pub fn new(now: DateTime<Utc>, limits: RiskLimits) -> Self {
        Self {
            trade_date: topstep_trade_date(now),
            loss_count: 0,
            win_count: 0,
            realized_pnl: 0.0,
            limits,
        }
    }

    pub fn record_bot_close(&mut self, now: DateTime<Utc>, pnl: f64) {
        self.roll_to(now);
        if !pnl.is_finite() || pnl.abs() < 1e-9 {
            return;
        }
        self.realized_pnl += pnl;
        if pnl > 0.0 {
            self.win_count += 1;
        } else {
            self.loss_count += 1;
        }
    }

    pub fn roll_to(&mut self, now: DateTime<Utc>) {
        let date = topstep_trade_date(now);
        if date != self.trade_date {
            self.trade_date = date;
            self.loss_count = 0;
            self.win_count = 0;
            self.realized_pnl = 0.0;
        }
    }

    pub fn lock_reasons(&self) -> Vec<&'static str> {
        let mut reasons = Vec::new();
        if self.limits.daily_loss_stop > 0 && self.loss_count >= self.limits.daily_loss_stop {
            reasons.push("daily_loss_stop");
        }
        if self.limits.daily_win_stop > 0 && self.win_count >= self.limits.daily_win_stop {
            reasons.push("daily_win_stop");
        }
        if self.limits.daily_profit_stop > 0.0 && self.realized_pnl >= self.limits.daily_profit_stop
        {
            reasons.push("daily_profit_stop");
        }
        reasons
    }

    pub fn is_locked(&self) -> bool {
        !self.lock_reasons().is_empty()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn utc(raw: &str) -> DateTime<Utc> {
        DateTime::parse_from_rfc3339(raw)
            .unwrap()
            .with_timezone(&Utc)
    }

    #[test]
    fn loss_win_profit_gates_and_topstep_roll_match_source_semantics() {
        let limits = RiskLimits {
            daily_loss_stop: 1,
            daily_win_stop: 2,
            daily_profit_stop: 100.0,
        };
        let mut state = DailyRiskState::new(utc("2026-09-25T21:59:00Z"), limits);
        state.record_bot_close(utc("2026-09-25T22:00:00Z"), 60.0);
        assert_eq!(state.win_count, 1);
        assert!(!state.is_locked());
        state.record_bot_close(utc("2026-09-25T22:10:00Z"), 40.0);
        assert_eq!(
            state.lock_reasons(),
            vec!["daily_win_stop", "daily_profit_stop"]
        );

        state.roll_to(utc("2026-09-26T22:00:00Z"));
        assert_eq!(state.loss_count, 0);
        assert_eq!(state.win_count, 0);
        assert_eq!(state.realized_pnl, 0.0);
        state.record_bot_close(utc("2026-09-26T22:01:00Z"), -1.0);
        assert_eq!(state.lock_reasons(), vec!["daily_loss_stop"]);
    }
}
