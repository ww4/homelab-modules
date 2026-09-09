//! The schema = the library's catalog (what each module is, needs and claims)
//! joined with the option docs (every homelab.* option's type, description,
//! default). Both are generated from the library at build time; nothing here
//! is hand-maintained.

use anyhow::{anyhow, Context, Result};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, VecDeque};

pub const EMBEDDED_CATALOG: &str = include_str!(concat!(env!("OUT_DIR"), "/catalog.json"));
pub const EMBEDDED_OPTIONS: &str = include_str!(concat!(env!("OUT_DIR"), "/options.json"));

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct ModuleMeta {
    pub description: String,
    /// "import" or the homelab.* enable option that gates the module.
    pub enable: String,
    /// homelab.* option prefixes the module reads.
    pub options: Vec<String>,
    pub requires: Vec<String>,
    pub vhosts: Vec<String>,
    pub secrets: Vec<SecretMeta>,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SecretMeta {
    /// The homelab.*File option to point at the file (or a `<manual: …>` note).
    pub option: String,
    pub keys: Vec<String>,
    pub owner: String,
    pub source: Source,
}

#[derive(Debug, Clone, Copy, Deserialize, Serialize, PartialEq, Eq)]
#[serde(rename_all = "kebab-case")]
pub enum Source {
    Generate,
    Supply,
    FirstBoot,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct OptionDoc {
    pub name: String,
    #[serde(rename = "type")]
    pub type_: String,
    pub description: Option<String>,
    pub has_default: bool,
    pub default: Option<String>,
    pub example: Option<String>,
}

impl OptionDoc {
    pub fn nullable(&self) -> bool {
        self.type_.starts_with("null or ")
    }
    /// `homelab.pools.<name>.branches`, `homelab.x.pairs.*.mirror` — fields of
    /// a submodule entry. Required per entry, checked by nix at eval; never a
    /// top-level value on their own.
    pub fn submodule_member(&self) -> bool {
        self.name.contains(".<") || self.name.contains(".*")
    }
    pub fn required(&self) -> bool {
        !self.has_default && !self.submodule_member()
    }
    /// Default rendered by nix as a literal expression; strip the quotes of a
    /// plain string default so callers can use the value.
    pub fn default_str(&self) -> Option<String> {
        let d = self.default.as_ref()?;
        Some(d.trim_matches('"').to_string())
    }
}

pub struct Schema {
    pub catalog: BTreeMap<String, ModuleMeta>,
    pub options: Vec<OptionDoc>,
}

impl Schema {
    pub fn parse(catalog: &str, options: &str) -> Result<Schema> {
        let catalog: BTreeMap<String, ModuleMeta> =
            serde_json::from_str(catalog).context("parsing catalog JSON")?;
        let options: Vec<OptionDoc> =
            serde_json::from_str(options).context("parsing options JSON")?;
        Ok(Schema { catalog, options })
    }

    pub fn module(&self, name: &str) -> Result<&ModuleMeta> {
        self.catalog
            .get(name)
            .ok_or_else(|| anyhow!("unknown module `{name}` (see `schema` for the list)"))
    }

    pub fn option(&self, name: &str) -> Option<&OptionDoc> {
        self.options.iter().find(|o| o.name == name)
    }

    /// Options under a prefix: `homelab.arrStack` covers `homelab.arrStack.*`;
    /// an exact leaf name (`homelab.domain`) covers itself.
    pub fn options_under(&self, prefix: &str) -> Vec<&OptionDoc> {
        let dotted = format!("{prefix}.");
        self.options
            .iter()
            .filter(|o| o.name == prefix || o.name.starts_with(&dotted))
            .collect()
    }

    /// All options a set of modules reads, deduplicated, in schema order.
    pub fn options_for_modules<'a>(&'a self, modules: &[String]) -> Vec<&'a OptionDoc> {
        let mut seen = BTreeSet::new();
        let mut out = Vec::new();
        for m in modules {
            if let Some(meta) = self.catalog.get(m) {
                for p in &meta.options {
                    for o in self.options_under(p) {
                        if seen.insert(o.name.clone()) {
                            out.push(o);
                        }
                    }
                }
            }
        }
        out
    }

    /// Close a module list over `requires`. Returns (ordered closure, the ones
    /// that were added). Order: as requested, dependencies first.
    pub fn close_over_requires(&self, requested: &[String]) -> Result<(Vec<String>, Vec<String>)> {
        let mut result: Vec<String> = Vec::new();
        let mut added = Vec::new();
        let mut queue: VecDeque<(String, bool)> =
            requested.iter().map(|m| (m.clone(), false)).collect();
        while let Some((m, implied)) = queue.pop_front() {
            let meta = self.module(&m)?;
            for r in &meta.requires {
                if !result.contains(r) {
                    queue.push_back((r.clone(), true));
                }
            }
            if !result.contains(&m) {
                // Dependencies go in before the module that needs them.
                let deps_done = meta.requires.iter().all(|r| result.contains(r));
                if deps_done {
                    if implied && !requested.contains(&m) {
                        added.push(m.clone());
                    }
                    result.push(m);
                } else {
                    queue.push_back((m, implied));
                }
            }
        }
        Ok((result, added))
    }

    pub fn question_set(&self, modules: &[String]) -> Result<QuestionSet> {
        let selected: Vec<String> = if modules.is_empty() {
            self.catalog.keys().cloned().collect()
        } else {
            self.close_over_requires(modules)?.0
        };
        let mut mods = Vec::new();
        for name in &selected {
            let meta = self.module(name)?;
            let secret_options: BTreeSet<&str> =
                meta.secrets.iter().map(|s| s.option.as_str()).collect();
            let options = self
                .options_for_modules(std::slice::from_ref(name))
                .into_iter()
                .map(|o| QuestionOption {
                    name: o.name.clone(),
                    type_: o.type_.clone(),
                    description: o.description.clone(),
                    required: o.required() && !secret_options.contains(o.name.as_str()),
                    default: o.default.clone(),
                    example: o.example.clone(),
                    secret: secret_options.contains(o.name.as_str()),
                })
                .collect();
            mods.push(QuestionModule {
                name: name.clone(),
                description: meta.description.clone(),
                enable: meta.enable.clone(),
                requires: meta.requires.clone(),
                vhosts: meta.vhosts.clone(),
                options,
                secrets: meta.secrets.clone(),
            });
        }
        Ok(QuestionSet {
            host: host_questions(),
            modules: mods,
        })
    }
}

/// The non-module answers every flake needs.
fn host_questions() -> Vec<HostQuestion> {
    let q = |name: &str, type_: &str, required: bool, description: &str| HostQuestion {
        name: name.into(),
        type_: type_.into(),
        required,
        description: description.into(),
    };
    vec![
        q("host.name", "string", true, "Hostname (also the nixosConfigurations attribute)."),
        q("host.system", "string", false, "Nix system; default x86_64-linux."),
        q("host.timeZone", "string", true, "time.timeZone, e.g. Europe/Amsterdam."),
        q("host.disk", "string", true, "Root disk device, e.g. /dev/sda or /dev/disk/by-id/…; partitioned by disko (ERASED on install)."),
        q("host.dataDisks", "list of {name, device}", false, "Extra disks, each formatted ext4 and mounted at /mnt/disks/<name> (ERASED on install)."),
        q("host.sshAuthorizedKeys", "list of string", false, "SSH public keys for the admin user. Strongly recommended."),
        q("host.stateVersion", "string", false, "system.stateVersion; default 26.05."),
        q("sops.adminRecipient", "string", false, "Your existing age public key (age1…). Omit to have one generated into keys/."),
        q("library", "string", false, "Flake reference for the homelab-modules input; default is the library's own home."),
    ]
}

#[derive(Serialize)]
pub struct QuestionSet {
    pub host: Vec<HostQuestion>,
    pub modules: Vec<QuestionModule>,
}

#[derive(Serialize)]
pub struct HostQuestion {
    pub name: String,
    #[serde(rename = "type")]
    pub type_: String,
    pub required: bool,
    pub description: String,
}

#[derive(Serialize)]
pub struct QuestionModule {
    pub name: String,
    pub description: String,
    pub enable: String,
    pub requires: Vec<String>,
    pub vhosts: Vec<String>,
    pub options: Vec<QuestionOption>,
    pub secrets: Vec<SecretMeta>,
}

#[derive(Serialize)]
pub struct QuestionOption {
    pub name: String,
    #[serde(rename = "type")]
    pub type_: String,
    pub description: Option<String>,
    pub required: bool,
    pub default: Option<String>,
    pub example: Option<String>,
    /// Set through a secret, not a value.
    pub secret: bool,
}

impl QuestionSet {
    pub fn render_text(&self) -> String {
        let mut s = String::new();
        s.push_str("HOST\n");
        for q in &self.host {
            s.push_str(&format!(
                "  {:<26} {}{}  {}\n",
                q.name,
                q.type_,
                if q.required { " (required)" } else { "" },
                q.description
            ));
        }
        for m in &self.modules {
            s.push_str(&format!("\n{}  — {}\n", m.name, m.description));
            if m.enable != "import" {
                s.push_str(&format!("  enabled by: {}\n", m.enable));
            }
            if !m.requires.is_empty() {
                s.push_str(&format!("  requires: {}\n", m.requires.join(", ")));
            }
            if !m.vhosts.is_empty() {
                s.push_str(&format!("  vhosts: {}\n", m.vhosts.join(", ")));
            }
            for o in &m.options {
                if o.secret {
                    continue;
                }
                s.push_str(&format!(
                    "  {:<44} {}{}\n",
                    o.name,
                    o.type_,
                    if o.required { " (required)" } else { "" }
                ));
            }
            for sec in &m.secrets {
                s.push_str(&format!(
                    "  secret {:<37} {:?} keys: {}\n",
                    sec.option,
                    sec.source,
                    sec.keys.join(", ")
                ));
            }
        }
        s
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn schema() -> Schema {
        let catalog = r#"{
          "acme": {"description":"a","enable":"import","options":["homelab.acme"],"requires":[],"vhosts":[],"secrets":[
             {"option":"homelab.acme.credentialsFile","keys":["TOKEN"],"owner":"root","source":"supply"}]},
          "nginx-access": {"description":"n","enable":"import","options":[],"requires":[],"vhosts":[],"secrets":[]},
          "jellyfin": {"description":"j","enable":"import","options":["homelab.domain"],"requires":["acme","nginx-access"],"vhosts":["jellyfin"],"secrets":[]},
          "monitoring": {"description":"m","enable":"homelab.monitoring.enable","options":["homelab.monitoring"],"requires":["acme"],"vhosts":["grafana"],"secrets":[]}
        }"#;
        let options = r#"[
          {"name":"homelab.domain","type":"string","description":"d","hasDefault":false,"default":null,"example":null},
          {"name":"homelab.acme.email","type":"string","description":"e","hasDefault":false,"default":null,"example":null},
          {"name":"homelab.acme.credentialsFile","type":"string","description":"c","hasDefault":false,"default":null,"example":null},
          {"name":"homelab.acme.dnsProvider","type":"string","description":"p","hasDefault":true,"default":"\"cloudflare\"","example":null},
          {"name":"homelab.monitoring.enable","type":"boolean","description":"en","hasDefault":true,"default":"false","example":null},
          {"name":"homelab.monitoring.pairs.*.mirror","type":"string","description":"m","hasDefault":false,"default":null,"example":null}
        ]"#;
        Schema::parse(catalog, options).unwrap()
    }

    #[test]
    fn closure_adds_dependencies_first() {
        let s = schema();
        let (all, added) = s.close_over_requires(&["jellyfin".into()]).unwrap();
        assert_eq!(all, vec!["acme", "nginx-access", "jellyfin"]);
        assert_eq!(added, vec!["acme", "nginx-access"]);
    }

    #[test]
    fn closure_keeps_requested_order_and_dedups() {
        let s = schema();
        let (all, added) = s
            .close_over_requires(&["acme".into(), "monitoring".into(), "jellyfin".into()])
            .unwrap();
        assert_eq!(all, vec!["acme", "monitoring", "nginx-access", "jellyfin"]);
        assert_eq!(added, vec!["nginx-access"]);
    }

    #[test]
    fn unknown_module_is_an_error() {
        assert!(schema().close_over_requires(&["nope".into()]).is_err());
    }

    #[test]
    fn required_excludes_secret_options_and_defaults() {
        let s = schema();
        let q = s.question_set(&["acme".into()]).unwrap();
        let acme = &q.modules[0];
        let by_name = |n: &str| acme.options.iter().find(|o| o.name == n).unwrap();
        assert!(by_name("homelab.acme.email").required);
        assert!(!by_name("homelab.acme.credentialsFile").required);
        assert!(by_name("homelab.acme.credentialsFile").secret);
        assert!(!by_name("homelab.acme.dnsProvider").required);
    }

    #[test]
    fn submodule_members_are_never_top_level_required() {
        let s = schema();
        let q = s.question_set(&["monitoring".into()]).unwrap();
        let mon = q.modules.iter().find(|m| m.name == "monitoring").unwrap();
        let pairs = mon.options.iter().find(|o| o.name.contains(".*.")).unwrap();
        assert!(!pairs.required);
    }

    #[test]
    fn options_under_prefix_and_leaf() {
        let s = schema();
        assert_eq!(s.options_under("homelab.acme").len(), 3);
        assert_eq!(s.options_under("homelab.domain").len(), 1);
        assert_eq!(s.options_under("homelab.dom").len(), 0);
    }
}
