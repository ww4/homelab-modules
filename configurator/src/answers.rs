//! The answers file — what a TUI, a form, or an agent hands to `generate`.
//! Secrets are never in it: they come via --secret, files or environment.
//!
//! `generate` writes a copy to `<out>/answers.json`, so a later run on the
//! same directory (add a module, remove one, change a value) starts from
//! what the install already is instead of from a blank form.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

#[derive(Debug, Clone, Deserialize, Serialize)]
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub library: Option<String>,
}

impl Answers {
    /// Apply a reconfigure: `--add`, `--remove`, `--set KEY=JSON`.
    pub fn apply(&mut self, add: &[String], remove: &[String], set: &[(String, serde_json::Value)]) {
        for m in add {
            if !self.modules.contains(m) {
                self.modules.push(m.clone());
            }
        }
        self.modules.retain(|m| !remove.contains(m));
        for (k, v) in set {
            if v.is_null() {
                self.values.remove(k);
            } else {
                self.values.insert(k.clone(), v.clone());
            }
        }
    }
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct Host {
    pub name: String,
    #[serde(default = "default_system")]
    pub system: String,
    pub time_zone: String,
    /// yescrypt hash of the admin's console password, recorded by the first
    /// `generate` so a reconfigure keeps the password instead of minting a
    /// new one. A hash, not a secret.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub admin_password_hash: Option<String>,
    /// Root disk device — partitioned by disko (erased).
    pub disk: String,
    #[serde(default)]
    pub data_disks: Vec<DataDisk>,
    #[serde(default)]
    pub ssh_authorized_keys: Vec<String>,
    #[serde(default = "default_state_version")]
    pub state_version: String,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct DataDisk {
    /// Mounted at /mnt/disks/<name>.
    pub name: String,
    pub device: String,
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields, rename_all = "camelCase")]
pub struct Sops {
    /// An existing age public key for the admin; None = generate one.
    #[serde(default, skip_serializing_if = "Option::is_none")]
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

#[cfg(test)]
mod apply_tests {
    use super::*;

    #[test]
    fn apply_adds_removes_and_sets() {
        let mut a: Answers = serde_json::from_str(
            r#"{"host":{"name":"box","timeZone":"UTC","disk":"/dev/sda"},"modules":["jellyfin","glances"],"values":{"homelab.domain":"a.test"}}"#,
        )
        .unwrap();
        a.apply(
            &["paperless".into(), "jellyfin".into()],
            &["glances".into()],
            &[("homelab.adminUser".into(), "alice".into()), ("homelab.domain".into(), serde_json::Value::Null)],
        );
        assert_eq!(a.modules, vec!["jellyfin", "paperless"]);
        assert_eq!(a.values.get("homelab.adminUser").unwrap(), "alice");
        assert!(!a.values.contains_key("homelab.domain"));
        // Round-trips through JSON, so answers.json can be read back.
        let s = serde_json::to_string(&a).unwrap();
        let b: Answers = serde_json::from_str(&s).unwrap();
        assert_eq!(b.modules, a.modules);
    }
}
