use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Side {
    Buy,
    Sell,
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum TrailMode {
    None,
    Single,
    Ladder,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct PiExitConfig {
    pub long_hold_minutes: u32,
    pub short_hold_minutes: u32,
    pub trail_enabled: bool,
    pub trail_trigger_pct: f64,
    pub trail_offset_ticks: i64,
    pub tp_ticks: i64,
    pub exit_mode: String,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct ExitPolicy {
    pub model: String,
    pub max_hold_minutes: u32,
    pub hard_tp_enabled: bool,
    pub trail_mode: TrailMode,
    pub trail_trigger_pct: f64,
    pub trail_offset_ticks: i64,
    pub trail_lock_pct: f64,
    pub ladder_trigger_r: f64,
    pub ladder_gap_r: f64,
}

pub fn resolve_pi_exit(config: &PiExitConfig, side: Side) -> ExitPolicy {
    let mut trigger_pct = config.trail_trigger_pct;
    if trigger_pct > 1.0 {
        trigger_pct /= 100.0;
    }
    trigger_pct = trigger_pct.max(0.0);

    let mut trail_mode = TrailMode::None;
    let mut trail_offset_ticks = 0;
    // Python's ladder branch is enabled only for the trend/factor models.
    // PI keeps its directional hold and the shared single-trail path.
    if config.trail_enabled && trigger_pct > 0.0 {
        trail_mode = TrailMode::Single;
        let tick_step = 5_i64;
        let floored_trigger = ((config.tp_ticks.unsigned_abs() as f64 * trigger_pct)
            / tick_step as f64)
            .floor() as i64
            * tick_step;
        let max_positive = (floored_trigger - tick_step).max(0);
        trail_offset_ticks = config
            .trail_offset_ticks
            .max(0)
            .min(config.tp_ticks.unsigned_abs().min(i64::MAX as u64) as i64)
            .min(max_positive);
    }

    ExitPolicy {
        model: "pi".to_owned(),
        max_hold_minutes: match side {
            Side::Buy => config.long_hold_minutes,
            Side::Sell => config.short_hold_minutes,
        },
        hard_tp_enabled: trail_mode != TrailMode::Ladder,
        trail_mode,
        trail_trigger_pct: trigger_pct,
        trail_offset_ticks,
        trail_lock_pct: 0.0,
        ladder_trigger_r: 2.0,
        ladder_gap_r: 2.0,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pi_exit_policy_uses_directional_hold_and_effective_trail_settings() {
        let config = PiExitConfig {
            long_hold_minutes: 0,
            short_hold_minutes: 60,
            trail_enabled: false,
            trail_trigger_pct: 0.0,
            trail_offset_ticks: 0,
            tp_ticks: 150,
            exit_mode: "tp".into(),
        };
        let long = resolve_pi_exit(&config, Side::Buy);
        let short = resolve_pi_exit(&config, Side::Sell);
        assert_eq!(long.max_hold_minutes, 0);
        assert_eq!(short.max_hold_minutes, 60);
        assert!(long.hard_tp_enabled && short.hard_tp_enabled);
        assert_eq!(long.trail_mode, TrailMode::None);
    }

    #[test]
    fn pi_does_not_inherit_factor_ladder_mode() {
        let config = PiExitConfig {
            long_hold_minutes: 0,
            short_hold_minutes: 60,
            trail_enabled: true,
            trail_trigger_pct: 0.3,
            trail_offset_ticks: 10,
            tp_ticks: 150,
            exit_mode: "ladder".into(),
        };
        let policy = resolve_pi_exit(&config, Side::Buy);
        assert_eq!(policy.trail_mode, TrailMode::Single);
        assert!(policy.hard_tp_enabled);
        assert_eq!(policy.trail_offset_ticks, 10);
    }
}
