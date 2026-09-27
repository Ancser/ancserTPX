use serde::{Deserialize, Serialize};
use serde_json::Value;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct RobinhoodCapabilityReport {
    pub provider: String,
    pub transport: String,
    pub options_tools_discovered: bool,
    pub live_ready: bool,
    pub discovered_tool_names: Vec<String>,
    pub blockers: Vec<String>,
}

pub fn capability_report(
    tool_manifest: Option<&Value>,
    authenticated: bool,
) -> RobinhoodCapabilityReport {
    let mut names = tool_manifest
        .and_then(|manifest| manifest.get("tools"))
        .and_then(Value::as_array)
        .map(|tools| {
            tools
                .iter()
                .filter_map(|tool| tool.get("name").and_then(Value::as_str))
                .map(str::to_owned)
                .collect::<Vec<_>>()
        })
        .unwrap_or_default();
    names.sort();
    names.dedup();
    let options_tools_discovered = names
        .iter()
        .any(|name| name.to_ascii_lowercase().contains("option"));
    let mut blockers = Vec::new();
    if !authenticated {
        blockers.push("robinhood_agentic_account_authentication_required".to_owned());
    }
    if tool_manifest.is_none() || !options_tools_discovered {
        blockers.push("robinhood_options_tool_schema_discovery_required".to_owned());
    } else {
        blockers.push("robinhood_option_tool_schema_mapping_unverified".to_owned());
    }
    blockers.push("robinhood_live_adapter_not_implemented_for_unverified_tool_schemas".to_owned());
    RobinhoodCapabilityReport {
        provider: "Robinhood Agentic Trading MCP".to_owned(),
        transport: "https://agent.robinhood.com/mcp/trading".to_owned(),
        options_tools_discovered,
        live_ready: false,
        discovered_tool_names: names,
        blockers,
    }
}
