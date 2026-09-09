//! The answers file — what a TUI, a form, or an agent hands to `generate`.
//! Secrets are never in it: they come via --secret, files or environment.

use serde::Deserialize;
use std::collections::BTreeMap;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct Answers {
    pub host: Host,
    /// Library modules to import; `requires` are added automatically.
    pub modules: Vec<String>,
    /// homelab.* option values, keyed by full dotted option path.
    #[serde(default)]
    pub values: BTreeMap<String, serde_json::Value>,
    #[serde(default)]
    pub sops: Sops,
    /// Flake reference for the homelab-modules input.
    #[serde(default)]
    pub library: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct Host {
    pub name: String,
    #[serde(default = "default_system")]
    pub system: String,
    pub time_zone: String,
    /// Root disk device — partitioned by disko (erased).
    pub disk: String,
    #[serde(default)]
    pub data_disks: Vec<DataDisk>,
    #[serde(default)]
    pub ssh_authorized_keys: Vec<String>,
    #[serde(default = "default_state_version")]
    pub state_version: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataDisk {
    /// Mounted at /mnt/disks/<name>.
    pub name: String,
    pub device: String,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct Sops {
    /// An existing age public key for the admin; None = generate one.
    pub admin_recipient: Option<String>,
}

fn default_system() -> String {
    "x86_64-linux".into()
}
fn default_state_version() -> String {
    "26.05".into()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn minimal_answers_parse_with_defaults() {
        let a: Answers = serde_json::from_str(
            r#"{"host":{"name":"box","timeZone":"UTC","disk":"/dev/sda"},"modules":["jellyfin"]}"#,
        )
        .unwrap();
        assert_eq!(a.host.system, "x86_64-linux");
        assert_eq!(a.host.state_version, "26.05");
        assert!(a.sops.admin_recipient.is_none());
    }

    #[test]
    fn unknown_fields_are_rejected() {
        let r: Result<Answers, _> = serde_json::from_str(
            r#"{"host":{"name":"box","timeZone":"UTC","disk":"/dev/sda","bogus":1},"modules":[]}"#,
        );
        assert!(r.is_err());
    }
}
