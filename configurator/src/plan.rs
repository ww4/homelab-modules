//! Resolve answers against the schema into a complete, checked plan. Every
//! problem is collected and reported together (exit 2), not one at a time.

use anyhow::Result;
use std::collections::{BTreeMap, BTreeSet};

use crate::answers::{Answers, Host};
use crate::schema::{Schema, SecretMeta, Source};
use crate::secrets::{self, SecretPlan, Supplied};
use crate::Rejected;

pub const DEFAULT_LIBRARY: &str = "git+https://git.rosemaryacres.com/ww4/homelab-modules.git"; // leak-scan-ok: the library's own home

pub struct Plan<'a> {
    pub schema: &'a Schema,
    pub host: &'a Host,
    /// Final ordered module list (dependencies first).
    pub modules: Vec<String>,
    /// Modules the user did not name but `requires` pulled in.
    pub added_modules: Vec<String>,
    /// homelab.* values to emit, including enable flags set automatically.
    pub values: BTreeMap<String, serde_json::Value>,
    pub auto_values: Vec<String>,
    pub secrets: Vec<SecretPlan>,
    /// First-boot / manual items the flake cannot settle by itself.
    pub phase2: Vec<String>,
    /// `<vhost>.<domain>` names the chosen modules claim.
    pub dns_names: Vec<String>,
    pub library_url: String,
    pub admin_recipient: Option<String>,
    pub warnings: Vec<String>,
}

impl<'a> Plan<'a> {
    pub fn build(
        schema: &'a Schema,
        answers: &'a Answers,
        supplied: &Supplied,
    ) -> Result<Plan<'a>, Rejected> {
        let mut problems = Vec::new();
        let mut warnings = Vec::new();

        if answers.modules.is_empty() {
            problems.push("modules: choose at least one module".into());
        }
        let (modules, added_modules) = match schema.close_over_requires(&answers.modules) {
            Ok(x) => x,
            Err(e) => {
                problems.push(format!("modules: {e}"));
                (Vec::new(), Vec::new())
            }
        };

        // Values: only homelab.* is accepted here; everything else belongs in
        // the host file the user edits afterwards.
        let mut values = answers.values.clone();
        for k in answers.values.keys() {
            if !k.starts_with("homelab.") {
                problems.push(format!("values.{k}: only homelab.* options go here"));
            } else if schema.option(k).is_none() && !is_under_known_prefix(schema, k) {
                problems.push(format!("values.{k}: no such option"));
            }
        }

        // Enable flags for gated modules.
        let mut auto_values = Vec::new();
        for m in &modules {
            let meta = schema.module(m).expect("closed over known modules");
            if meta.enable != "import" && !values.contains_key(&meta.enable) {
                values.insert(meta.enable.clone(), serde_json::Value::Bool(true));
                auto_values.push(meta.enable.clone());
            }
        }

        // Secrets, then required-option check (secret options are satisfied by
        // secrets, not values).
        let mut secret_plans = Vec::new();
        let mut phase2 = Vec::new();
        let mut secret_options = BTreeSet::new();
        for m in &modules {
            let meta = schema.module(m).expect("closed over known modules");
            for s in &meta.secrets {
                if !s.option.starts_with("homelab.") {
                    // `<manual: …>` — not an option; only a note.
                    phase2.push(format!(
                        "{m}: {} — keys {}",
                        s.option.trim_start_matches('<').trim_end_matches('>'),
                        s.keys.join(", ")
                    ));
                    continue;
                }
                if skip_secret(schema, &modules, s) {
                    continue;
                }
                secret_options.insert(s.option.clone());
                match secrets::plan_secret(m, s, &values, schema, supplied) {
                    Ok(p) => {
                        if p.source == Source::FirstBoot {
                            phase2.push(format!(
                                "{m}: `sops secrets/{}.yaml` and replace the CHANGEME values ({})",
                                p.name,
                                s.keys.join(", ")
                            ));
                        }
                        secret_plans.push(p);
                    }
                    Err(e) => problems.push(e),
                }
            }
        }
        for o in schema.options_for_modules(&modules) {
            if o.required() && !secret_options.contains(&o.name) && !values.contains_key(&o.name)
            {
                problems.push(format!(
                    "values.{}: required ({}) — {}",
                    o.name,
                    o.type_,
                    o.description.as_deref().unwrap_or("").trim().replace('\n', " ")
                ));
            }
        }
        for s in supplied.unused(&secret_plans) {
            warnings.push(format!("--secret {s}: not needed by the chosen modules; ignored"));
        }

        // DNS names.
        let domain = values
            .get("homelab.domain")
            .and_then(|v| v.as_str())
            .unwrap_or("<domain>")
            .to_string();
        let mut dns_names = Vec::new();
        for m in &modules {
            let meta = schema.module(m).expect("closed over known modules");
            for v in &meta.vhosts {
                let sub = resolve_placeholder(schema, &values, v);
                dns_names.push(format!("{sub}.{domain}"));
            }
        }

        if answers.host.ssh_authorized_keys.is_empty() {
            warnings.push(
                "host.sshAuthorizedKeys is empty — the admin can only log in at the console with the generated password"
                    .into(),
            );
        }
        if answers.host.name.is_empty() || !answers.host.name.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
            problems.push("host.name: letters, digits and dashes only".into());
        }

        if !problems.is_empty() {
            return Err(Rejected(problems));
        }

        let library_url = answers
            .library
            .clone()
            .unwrap_or_else(|| DEFAULT_LIBRARY.to_string());

        Ok(Plan {
            schema,
            host: &answers.host,
            modules,
            added_modules,
            values,
            auto_values,
            secrets: secret_plans,
            phase2,
            dns_names,
            library_url,
            admin_recipient: answers.sops.admin_recipient.clone(),
            warnings,
        })
    }
}

/// A value key like `homelab.pools.media.mountpoint` is fine when
/// `homelab.pools` is an attrsOf option; accept anything under a known
/// option whose type is a set.
fn is_under_known_prefix(schema: &Schema, key: &str) -> bool {
    let mut parts: Vec<&str> = key.split('.').collect();
    while parts.len() > 2 {
        parts.pop();
        let prefix = parts.join(".");
        if let Some(o) = schema.option(&prefix) {
            return o.type_.contains("attribute set");
        }
    }
    false
}

/// OIDC client secrets only make sense with the SSO provider present; the
/// option is nullable and the module documents null = no SSO wiring.
fn skip_secret(schema: &Schema, modules: &[String], s: &SecretMeta) -> bool {
    let nullable = schema.option(&s.option).map(|o| o.nullable()).unwrap_or(false);
    nullable && s.option.to_lowercase().contains("oidc") && !modules.iter().any(|m| m == "authelia")
}

/// `<homelab.x.y>` → the value, else the option's default, else the text.
pub fn resolve_placeholder(
    schema: &Schema,
    values: &BTreeMap<String, serde_json::Value>,
    text: &str,
) -> String {
    let inner = match text.strip_prefix('<').and_then(|t| t.strip_suffix('>')) {
        Some(i) if i.starts_with("homelab.") => i,
        _ => return text.to_string(),
    };
    if let Some(v) = values.get(inner) {
        return match v {
            serde_json::Value::String(s) => s.clone(),
            other => other.to_string(),
        };
    }
    schema
        .option(inner)
        .and_then(|o| o.default_str())
        .unwrap_or_else(|| text.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn placeholder_resolution_order() {
        let schema = Schema::parse(
            "{}",
            r#"[{"name":"homelab.vaultwarden.subdomain","type":"string","description":null,"hasDefault":true,"default":"\"vault\"","example":null}]"#,
        )
        .unwrap();
        let mut values = BTreeMap::new();
        assert_eq!(
            resolve_placeholder(&schema, &values, "<homelab.vaultwarden.subdomain>"),
            "vault"
        );
        values.insert("homelab.vaultwarden.subdomain".into(), "keys".into());
        assert_eq!(
            resolve_placeholder(&schema, &values, "<homelab.vaultwarden.subdomain>"),
            "keys"
        );
        assert_eq!(resolve_placeholder(&schema, &values, "plain"), "plain");
        assert_eq!(resolve_placeholder(&schema, &values, "<manual: x>"), "<manual: x>");
    }
}
