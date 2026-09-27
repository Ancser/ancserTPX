use crate::core::RuntimeConfig;
use std::path::Path;

pub fn load_runtime_config(
    path: impl AsRef<Path>,
) -> Result<RuntimeConfig, Box<dyn std::error::Error>> {
    let text = std::fs::read_to_string(path)?;
    let config: RuntimeConfig = serde_json::from_str(&text)?;
    validate_runtime_config(&config)?;
    Ok(config)
}

pub fn validate_runtime_config(config: &RuntimeConfig) -> Result<(), Box<dyn std::error::Error>> {
    if !config.tick_size.is_finite() || config.tick_size <= 0.0 {
        return Err("tick_size must be a finite positive value".into());
    }
    if !config.tick_value.is_finite() || config.tick_value <= 0.0 {
        return Err("tick_value must be a finite positive value".into());
    }
    if config.contract_size <= 0 {
        return Err("contract_size must be positive".into());
    }
    if config.max_signal_age_seconds < 0 {
        return Err("max_signal_age_seconds must be non-negative".into());
    }
    if config.max_market_staleness_seconds < 0 {
        return Err("max_market_staleness_seconds must be non-negative".into());
    }
    if !config.max_entry_reference_distance_points.is_finite()
        || config.max_entry_reference_distance_points <= 0.0
    {
        return Err("max_entry_reference_distance_points must be finite and positive".into());
    }
    Ok(())
}
