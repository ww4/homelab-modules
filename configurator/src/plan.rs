//! Resolve answers against the schema into a complete, checked plan. Every
//! problem is collected and reported together (exit 2), not one at a time.

use anyhow::Result;
use std::collections::{BTreeMap, BTreeSet};

use crate::answers::{Answers, Host};
use crate::schema::{Schema, SecretMeta, Source};
use crate::secrets::{self, SecretPlan, Supplied};
use crate::Rejected;

pub const DEFAULT_LIBRARY: &str = "git+https://git.rosemaryacres.com/ww4/homelab-modules.git"; // leak-scan-ok: the library's own home

/// Foundation modules — present from the first run, never offered as choices.
/// `system` and `boot` are added to every plan; they read no values.
pub const FOUNDATION_ALWAYS: &[&str] = &["system", "boot"];
/// Foundation modules the user must choose AND configure before the first
/// install, with the reason the plan gives when one is missing.
pub const FOUNDATION_REQUIRED: &[(&str, &str)] = &[(
    "backup",
    "the restore path must exist before there is anything to restore — add `backup` with \
     `homelab.backup.paths` (the state directories of your services) and \
     `homelab.backup.local.repository`",
)];
/// Modules a reconfigure refuses to remove, with the reason.
pub const NEVER_REMOVE: &[(&str, &str)] = &[
    ("system", "foundation: every host needs it"),
    ("boot", "foundation: every host needs it"),
    ("backup", "foundation: a machine with no backup is a decision to make by hand, not with --remove"),
    ("mergerfs-pools", "foundation: the pool shape is decided at install; changing it later is a data migration, not a reconfigure"),
];
/// Modules whose absence is allowed but warned about on a fresh install.
pub const FOUNDATION_DEFAULT_ON: &[(&str, &str)] = &[
    ("monitoring", "a box with no alerting is a box whose first failure is silent"),
    ("ntfy", "without a notification channel the alerts have nowhere to go"),
];

pub struct Plan<'a> {
    pub schema: &'a Schema,
    pub host: &'a Host,
    /// Final ordered module list (dependencies first).
    pub modules: Vec<String>,
    /// Modules the user did not name but `requires` pulled in.
    pub added_modules: Vec<String>,
    /// Modules present in the previous answers and absent now (a reconfigure).
    pub removed_modules: Vec<String>,
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
    /// Rough resident memory the module set needs, MiB, base system included.
    pub memory_mib: u64,
}

impl<'a> Plan<'a> {
    /// `previous`: the answers the output directory was last generated from
    /// (a reconfigure), or None for a fresh install. `existing_secrets`: the
    /// sops secret names whose files already exist in the output; those are
    /// kept, never re-minted.
    pub fn build(
        schema: &'a Schema,
        answers: &'a Answers,
        supplied: &Supplied,
        previous: Option<&Answers>,
        existing_secrets: &BTreeSet<String>,
    ) -> Result<Plan<'a>, Rejected> {
        let mut problems = Vec::new();
        let mut warnings = Vec::new();

        if answers.modules.is_empty() {
            problems.push("modules: choose at least one module".into());
        }
        // The foundation modules nobody chooses: added to every plan.
        let mut requested = answers.modules.clone();
        for f in FOUNDATION_ALWAYS {
            if schema.catalog.contains_key(*f) && !requested.iter().any(|m| m == f) {
                requested.push((*f).to_string());
            }
        }
        for (f, why) in FOUNDATION_REQUIRED {
            // On a reconfigure that drops it, the --remove refusal below is the
            // message; on a fresh install (or one that never had it), this is.
            let had_it = previous.map(|p| p.modules.iter().any(|m| m == f)).unwrap_or(false);
            if schema.catalog.contains_key(*f) && !requested.iter().any(|m| m == f) && !had_it {
                problems.push(format!("modules: `{f}` is a foundation module — {why}"));
            }
        }
        if previous.is_none() {
            for (f, why) in FOUNDATION_DEFAULT_ON {
                if schema.catalog.contains_key(*f) && !requested.iter().any(|m| m == f) {
                    warnings.push(format!("modules: `{f}` is not chosen — {why}"));
                }
            }
        }
        let (modules, mut added_modules) = match schema.close_over_requires(&requested) {
            Ok(x) => x,
            Err(e) => {
                problems.push(format!("modules: {e}"));
                (Vec::new(), Vec::new())
            }
        };
        // The foundation set counts as added: the user never named it.
        for f in FOUNDATION_ALWAYS {
            if modules.iter().any(|m| m == f) && !answers.modules.iter().any(|m| m == f) && !added_modules.iter().any(|m| m == f) {
                added_modules.push((*f).to_string());
            }
        }

        // A reconfigure: what was there before and is not now.
        let mut removed_modules = Vec::new();
        if let Some(prev) = previous {
            let prev_closed = schema
                .close_over_requires(&prev.modules)
                .map(|(all, _)| all)
                .unwrap_or_else(|_| prev.modules.clone());
            for m in &prev_closed {
                if modules.contains(m) {
                    continue;
                }
                if let Some((_, why)) = NEVER_REMOVE.iter().find(|(n, _)| n == m) {
                    problems.push(format!("--remove {m}: refused — {why}"));
                    continue;
                }
                let dependents: Vec<&String> = modules
                    .iter()
                    .filter(|other| schema.catalog.get(*other).map(|meta| meta.requires.contains(m)).unwrap_or(false))
                    .collect();
                if !dependents.is_empty() {
                    problems.push(format!(
                        "--remove {m}: refused — still required by {}",
                        dependents.iter().map(|d| d.as_str()).collect::<Vec<_>>().join(", ")
                    ));
                    continue;
                }
                removed_modules.push(m.clone());
            }
            if removed_modules.iter().any(|m| m == "authelia") {
                let orphans: Vec<String> = answers
                    .values
                    .keys()
                    .filter(|k| k.to_lowercase().contains("oidc"))
                    .cloned()
                    .collect();
                if !orphans.is_empty() {
                    warnings.push(format!(
                        "removing authelia orphans the SSO wiring behind {}: those apps will fail to log in until you unset them",
                        orphans.join(", ")
                    ));
                }
            }
            for m in &removed_modules {
                warnings.push(format!(
                    "removed {m}: NixOS leaves its state in place (look under /var/lib) and its secret files stay in secrets/; delete both yourself if you mean it"
                ));
            }
        }

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
        // Values minted once per run and reused wherever the same key family
        // appears (the *arr API keys — see secrets::api_key_family).
        let mut minted = BTreeMap::new();
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
                if skip_secret(schema, &modules, &values, s) {
                    continue;
                }
                secret_options.insert(s.option.clone());
                if existing_secrets.contains(&secrets::secret_name(&s.option)) && supplied.take(&s.option).is_none() {
                    // A reconfigure keeps what is already encrypted; a value
                    // passed with --secret replaces it on purpose.
                    secret_plans.push(secrets::kept_secret(m, s, &values, schema));
                    continue;
                }
                match secrets::plan_secret(m, s, &values, schema, supplied, &mut minted) {
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

        let memory_mib = memory_need(schema, &modules);
        let ram = machine_ram_mib();
        if ram != 0 && memory_mib > ram {
            warnings.push(format!("memory: {}", memory_verdict(memory_mib, ram)));
        }
        Ok(Plan {
            schema,
            host: &answers.host,
            modules,
            memory_mib,
            added_modules,
            removed_modules,
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

/// A nullable secret is one the module works without. Two shapes are
/// skipped: OIDC client secrets when the SSO provider is absent (null = no
/// SSO wiring), and a secret whose option group has its own `enable` switch
/// that is off — `homelab.backup.remote.environmentFile` is only read when
/// `homelab.backup.remote.enable` is true.
fn skip_secret(
    schema: &Schema,
    modules: &[String],
    values: &BTreeMap<String, serde_json::Value>,
    s: &SecretMeta,
) -> bool {
    let nullable = schema.option(&s.option).map(|o| o.nullable()).unwrap_or(false);
    if !nullable {
        return false;
    }
    if s.option.to_lowercase().contains("oidc") && !modules.iter().any(|m| m == "authelia") {
        return true;
    }
    if let Some((group, _)) = s.option.rsplit_once('.') {
        let gate = format!("{group}.enable");
        if let Some(o) = schema.option(&gate) {
            let on = values
                .get(&gate)
                .and_then(|v| v.as_bool())
                .or_else(|| o.default_str().map(|d| d == "true"))
                .unwrap_or(false);
            return !on;
        }
    }
    false
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

    fn schema() -> Schema {
        let catalog = r#"{
          "system": {"description":"s","enable":"import","options":[],"requires":[],"vhosts":[],"secrets":[]},
          "boot": {"description":"b","enable":"import","options":[],"requires":[],"vhosts":[],"secrets":[]},
          "acme": {"description":"a","enable":"import","options":["homelab.acme"],"requires":[],"vhosts":[],"secrets":[
             {"option":"homelab.acme.credentialsFile","keys":["TOKEN"],"owner":"root","source":"supply"}]},
          "nginx-access": {"description":"n","enable":"import","options":[],"requires":[],"vhosts":[],"secrets":[]},
          "jellyfin": {"description":"j","enable":"import","options":["homelab.domain"],"requires":["acme","nginx-access"],"vhosts":["jellyfin"],"secrets":[]},
          "backup": {"description":"k","enable":"import","options":["homelab.backup"],"requires":[],"vhosts":[],"secrets":[
             {"option":"homelab.backup.passwordFile","keys":["<passphrase>"],"owner":"root","source":"generate"}]},
          "authelia": {"description":"au","enable":"homelab.authelia.enable","options":["homelab.authelia"],"requires":["acme"],"vhosts":["auth"],"secrets":[]}
        }"#;
        let options = r#"[
          {"name":"homelab.domain","type":"string","description":"d","hasDefault":false,"default":null,"example":null},
          {"name":"homelab.acme.email","type":"string","description":"e","hasDefault":false,"default":null,"example":null},
          {"name":"homelab.acme.credentialsFile","type":"string","description":"c","hasDefault":false,"default":null,"example":null},
          {"name":"homelab.backup.paths","type":"list of string","description":"p","hasDefault":false,"default":null,"example":null},
          {"name":"homelab.backup.passwordFile","type":"string","description":"pw","hasDefault":false,"default":null,"example":null},
          {"name":"homelab.authelia.enable","type":"boolean","description":"en","hasDefault":true,"default":"false","example":null}
        ]"#;
        Schema::parse(catalog, options).unwrap()
    }

    fn answers(modules: &[&str]) -> Answers {
        let mut a: Answers = serde_json::from_str(
            r#"{"host":{"name":"box","timeZone":"UTC","disk":"/dev/sda"},"modules":[],"values":{"homelab.domain":"a.test","homelab.acme.email":"a@a.test","homelab.backup.paths":["/var/lib/x"]}}"#,
        )
        .unwrap();
        a.modules = modules.iter().map(|m| m.to_string()).collect();
        a
    }

    fn supplied() -> Supplied {
        let mut s = Supplied::default();
        s.by_option.insert("homelab.acme.credentialsFile".into(), "TOKEN=x\n".into());
        s
    }

    #[test]
    fn foundation_is_added_and_backup_is_demanded() {
        let s = schema();
        let a = answers(&["jellyfin"]);
        let err = Plan::build(&s, &a, &supplied(), None, &BTreeSet::new()).err().unwrap();
        assert!(err.0.iter().any(|p| p.contains("`backup` is a foundation module")), "{:?}", err.0);

        let a = answers(&["jellyfin", "backup"]);
        let p = Plan::build(&s, &a, &supplied(), None, &BTreeSet::new()).unwrap();
        assert!(p.modules.contains(&"system".to_string()) && p.modules.contains(&"boot".to_string()));
        assert!(p.added_modules.contains(&"system".to_string()));
        assert!(p.removed_modules.is_empty());
    }

    #[test]
    fn removal_is_checked_against_requires_and_the_foundation() {
        let s = schema();
        let prev = answers(&["jellyfin", "backup"]);
        // Removing acme while jellyfin still needs it: refused.
        let mut a = answers(&["jellyfin", "backup"]);
        a.values.clear();
        let a = { let mut b = answers(&["jellyfin", "backup"]); b.modules = vec!["jellyfin".into(), "backup".into()]; b };
        // acme is pulled in by requires either way; simulate an explicit removal of backup.
        let mut no_backup = a.clone();
        no_backup.modules.retain(|m| m != "backup");
        let err = Plan::build(&s, &no_backup, &supplied(), Some(&prev), &BTreeSet::new()).err().unwrap();
        assert!(err.0.iter().any(|p| p.contains("--remove backup: refused")), "{:?}", err.0);

        // Removing jellyfin: allowed, with the state warning; acme stays (nothing else needs it, but it was requested by nothing — it is dropped too).
        let mut no_jf = a.clone();
        no_jf.modules.retain(|m| m != "jellyfin");
        let p = Plan::build(&s, &no_jf, &supplied(), Some(&prev), &BTreeSet::new()).unwrap();
        assert!(p.removed_modules.contains(&"jellyfin".to_string()));
        assert!(p.warnings.iter().any(|w| w.contains("removed jellyfin")));
    }

    #[test]
    fn existing_secrets_are_kept_not_reminted() {
        let s = schema();
        let a = answers(&["jellyfin", "backup"]);
        let mut existing = BTreeSet::new();
        existing.insert("backup-password".to_string());
        let p = Plan::build(&s, &a, &supplied(), Some(&a), &existing).unwrap();
        let bk = p.secrets.iter().find(|x| x.option == "homelab.backup.passwordFile").unwrap();
        assert!(bk.kept);
        assert!(bk.show_once.is_empty());
        let acme = p.secrets.iter().find(|x| x.option == "homelab.acme.credentialsFile").unwrap();
        assert!(!acme.kept, "a supplied value replaces the file");
    }

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

/// MiB this machine has (`MemTotal` in /proc/meminfo); 0 when unreadable.
pub fn machine_ram_mib() -> u64 {
    std::fs::read_to_string("/proc/meminfo")
        .ok()
        .and_then(|t| t.lines().find(|l| l.starts_with("MemTotal:")).and_then(|l| l.split_whitespace().nth(1)).and_then(|k| k.parse::<u64>().ok()))
        .map(|kib| kib / 1024)
        .unwrap_or(0)
}

/// The base system (kernel, systemd, journald, the container runtime, page
/// cache the services need to be usable) before any module is counted.
pub const BASE_MEMORY_MIB: u64 = 1024;

/// Rough resident memory a module set needs, MiB: the catalog figures plus
/// the base. The caller passes the closed set (requires included).
pub fn memory_need(schema: &crate::schema::Schema, modules: &[String]) -> u64 {
    BASE_MEMORY_MIB + modules.iter().filter_map(|m| schema.catalog.get(m)).map(|m| m.memory).sum::<u64>()
}

/// One line for a person: what the set needs against what the box has.
/// Headroom of a quarter is the line between "runs" and "runs well".
pub fn memory_verdict(need_mib: u64, ram_mib: u64) -> String {
    let gb = |m: u64| format!("{:.1}", m as f64 / 1024.0);
    if ram_mib == 0 {
        return format!("needs about {} GB of RAM (base system included); this machine's RAM is unknown", gb(need_mib));
    }
    if need_mib > ram_mib {
        format!("needs about {} GB of RAM; this machine has {} GB — SHORT by {} GB: drop a module or add memory", gb(need_mib), gb(ram_mib), gb(need_mib - ram_mib))
    } else if need_mib * 5 > ram_mib * 4 {
        format!("needs about {} GB of RAM; this machine has {} GB — fits, with little to spare", gb(need_mib), gb(ram_mib))
    } else {
        format!("needs about {} GB of RAM; this machine has {} GB — fits", gb(need_mib), gb(ram_mib))
    }
}
