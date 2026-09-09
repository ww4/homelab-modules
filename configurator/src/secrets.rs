//! Secret planning and minting. Three classes, from the catalog:
//!   generate   — minted here (passwords, tokens, OIDC client secrets)
//!   supply     — only the user has it (DNS token, VPN conf, .msh); --secret
//!   first-boot — exists only after a service ran once; a CHANGEME placeholder
//!                is encrypted so the flake evaluates, and PHASE-2.md lists it
//!
//! Formats the library's modules expect are produced by the tools that own
//! them (authelia for pbkdf2 digests); argon2 for Vaultwarden's ADMIN_TOKEN is
//! a plain PHC string and is computed here.

use anyhow::{anyhow, bail, Context, Result};
use argon2::password_hash::{rand_core::OsRng, PasswordHasher, SaltString};
use argon2::{Algorithm, Argon2, Params, Version};
use rand::{distributions::Alphanumeric, Rng};
use serde::Serialize;
use std::collections::BTreeMap;
use std::fs;
use std::path::PathBuf;
use std::process::Command;

use crate::schema::{Schema, SecretMeta, Source};

/// `--secret OPTION=@file` / `OPTION=env:VAR`, resolved to contents.
#[derive(Default)]
pub struct Supplied {
    pub by_option: BTreeMap<String, String>,
    used: std::cell::RefCell<Vec<String>>,
}

impl Supplied {
    pub fn take(&self, option: &str) -> Option<&String> {
        let v = self.by_option.get(option)?;
        self.used.borrow_mut().push(option.to_string());
        Some(v)
    }
    pub fn unused(&self, _plans: &[SecretPlan]) -> Vec<String> {
        let used = self.used.borrow();
        self.by_option
            .keys()
            .filter(|k| !used.contains(k))
            .cloned()
            .collect()
    }
}

pub fn parse_supplied(args: &[String]) -> Result<Supplied> {
    let mut s = Supplied::default();
    for a in args {
        let (opt, src) = a
            .split_once('=')
            .ok_or_else(|| anyhow!("--secret {a}: expected OPTION=@file or OPTION=env:VAR"))?;
        let content = if let Some(path) = src.strip_prefix('@') {
            fs::read_to_string(path).with_context(|| format!("--secret {opt}: reading {path}"))?
        } else if let Some(var) = src.strip_prefix("env:") {
            std::env::var(var).with_context(|| format!("--secret {opt}: ${var} is not set"))?
        } else {
            bail!("--secret {opt}: source must be @file or env:VAR (never the value itself — it would land in shell history)");
        };
        s.by_option.insert(opt.to_string(), content);
    }
    Ok(s)
}

#[derive(Debug, Clone, Serialize)]
pub struct SecretPlan {
    pub module: String,
    pub option: String,
    /// sops secret name = file stem under secrets/.
    pub name: String,
    pub owner: String,
    pub source: Source,
    pub keys: Vec<String>,
    /// Plaintext file content (encrypted on write; never reported).
    #[serde(skip)]
    pub content: String,
    /// Show-once values for FIRST-LOGIN.md: (label, value).
    #[serde(skip)]
    pub show_once: Vec<(String, String)>,
}

pub fn plan_secret(
    module: &str,
    meta: &SecretMeta,
    values: &BTreeMap<String, serde_json::Value>,
    schema: &Schema,
    supplied: &Supplied,
) -> std::result::Result<SecretPlan, String> {
    let name = secret_name(&meta.option);
    let owner = crate::plan::resolve_placeholder(schema, values, &meta.owner);
    let mut show_once = Vec::new();
    let content = match meta.source {
        Source::Supply => match supplied.take(&meta.option) {
            Some(c) => c.clone(),
            None => {
                return Err(format!(
                    "secret {} ({module}): must be supplied — pass `--secret {}=@<file>` with {}",
                    meta.option,
                    meta.option,
                    describe_keys(&meta.keys)
                ))
            }
        },
        Source::Generate => match supplied.take(&meta.option) {
            Some(c) => c.clone(),
            None => generate_content(module, &meta.keys, &mut show_once).map_err(|e| format!("{e:#}"))?,
        },
        Source::FirstBoot => match supplied.take(&meta.option) {
            Some(c) => c.clone(),
            None => placeholder_content(&meta.keys),
        },
    };
    Ok(SecretPlan {
        module: module.to_string(),
        option: meta.option.clone(),
        name,
        owner,
        source: meta.source,
        keys: meta.keys.clone(),
        content,
        show_once,
    })
}

/// `homelab.arrStack.vpnEnvFile` → `arr-stack-vpn-env`.
pub fn secret_name(option: &str) -> String {
    let stripped = option.strip_prefix("homelab.").unwrap_or(option);
    let stripped = stripped.strip_suffix("File").unwrap_or(stripped);
    let mut out = String::new();
    for (i, c) in stripped.chars().enumerate() {
        if c == '.' {
            out.push('-');
        } else if c.is_ascii_uppercase() {
            if i > 0 && !out.ends_with('-') {
                out.push('-');
            }
            out.push(c.to_ascii_lowercase());
        } else {
            out.push(c);
        }
    }
    out
}

fn describe_keys(keys: &[String]) -> String {
    if keys.iter().all(|k| is_env_key(k)) {
        format!("lines {}", keys.join(", "))
    } else {
        keys.join(", ")
    }
}

/// `SONARR_API_KEY`, `FIREWALL_VPN_INPUT_PORTS (optional)`, `SMTP_* (optional)`
/// → env-file line names (a `*` family is only ever optional).
fn is_env_key(k: &str) -> bool {
    let bare = k.split(' ').next().unwrap_or("");
    !bare.is_empty()
        && bare
            .chars()
            .all(|c| c.is_ascii_uppercase() || c.is_ascii_digit() || c == '_' || c == '*')
}

fn is_optional(k: &str) -> bool {
    k.contains("(optional)") || bare_key(k).contains('*')
}

fn bare_key(k: &str) -> &str {
    k.split(' ').next().unwrap_or(k)
}

pub fn random_token(len: usize) -> String {
    rand::thread_rng()
        .sample_iter(&Alphanumeric)
        .take(len)
        .map(char::from)
        .collect()
}

fn generate_content(
    module: &str,
    keys: &[String],
    show_once: &mut Vec<(String, String)>,
) -> Result<String> {
    if keys.iter().all(|k| is_env_key(k)) {
        // An env file: one generated value per non-optional key.
        let mut lines = Vec::new();
        for k in keys {
            if is_optional(k) {
                continue;
            }
            let var = bare_key(k);
            if k.contains("argon2") {
                let plain = random_token(40);
                let hash = argon2_phc(&plain)?;
                show_once.push((format!("{module}: {var} (plaintext — the env file holds the argon2 hash)"), plain));
                lines.push(format!("{var}='{hash}'"));
            } else {
                let v = random_token(32);
                show_once.push((format!("{module}: {var}"), v.clone()));
                lines.push(format!("{var}={v}"));
            }
        }
        return Ok(lines.join("\n") + "\n");
    }
    // A single-value file (password / client secret). The catalog phrases these
    // as `<what it is>`.
    let what = keys.first().map(String::as_str).unwrap_or("value");
    let plain = random_token(32);
    let what_clean = what.trim_start_matches('<').trim_end_matches('>');
    show_once.push((format!("{module}: {what_clean}"), plain.clone()));
    if what.to_lowercase().contains("oidc") {
        // The provider side wants a one-way digest of the same value.
        let digest = authelia_pbkdf2(&plain)?;
        show_once.push((
            format!("{module}: OIDC client_secret digest for homelab.authelia.oidcClients"),
            digest,
        ));
    }
    Ok(plain + "\n")
}

fn placeholder_content(keys: &[String]) -> String {
    if keys.iter().all(|k| is_env_key(k)) {
        keys.iter()
            .filter(|k| !is_optional(k))
            .map(|k| format!("{}=CHANGEME\n", bare_key(k)))
            .collect()
    } else {
        "CHANGEME\n".to_string()
    }
}

/// Vaultwarden accepts an argon2id PHC string as ADMIN_TOKEN (its own
/// `vaultwarden hash` produces the same shape). OWASP-ish parameters.
pub fn argon2_phc(plain: &str) -> Result<String> {
    let params = Params::new(65540, 3, 4, None).map_err(|e| anyhow!("argon2 params: {e}"))?;
    let hasher = Argon2::new(Algorithm::Argon2id, Version::V0x13, params);
    let salt = SaltString::generate(&mut OsRng);
    Ok(hasher
        .hash_password(plain.as_bytes(), &salt)
        .map_err(|e| anyhow!("argon2: {e}"))?
        .to_string())
}

/// `authelia crypto hash generate pbkdf2 --password …` — the digest format the
/// authelia module's oidcClients expects. Delegated to authelia itself so the
/// format can never drift from what it validates.
pub fn authelia_pbkdf2(plain: &str) -> Result<String> {
    let out = Command::new("authelia")
        .args(["crypto", "hash", "generate", "pbkdf2", "--variant", "sha512", "--password", plain])
        .output()
        .context("running `authelia crypto hash generate` (is authelia on PATH?)")?;
    if !out.status.success() {
        bail!("authelia crypto hash failed: {}", String::from_utf8_lossy(&out.stderr));
    }
    let text = String::from_utf8_lossy(&out.stdout);
    text.lines()
        .find_map(|l| l.trim().strip_prefix("Digest:").map(|d| d.trim().to_string()))
        .ok_or_else(|| anyhow!("authelia output had no `Digest:` line: {text}"))
}

/// The sops-encrypted file: `<name>: |` + the content. Written plaintext,
/// then encrypted in place by sops using the flake's own .sops.yaml — the same
/// path the user takes later to edit it.
pub fn write_encrypted(out_dir: &std::path::Path, plan: &SecretPlan) -> Result<PathBuf> {
    let dir = out_dir.join("secrets");
    fs::create_dir_all(&dir)?;
    let path = dir.join(format!("{}.yaml", plan.name));
    let mut yaml = format!("{}: |\n", plan.name);
    for line in plan.content.lines() {
        yaml.push_str("  ");
        yaml.push_str(line);
        yaml.push('\n');
    }
    write_private(&path, &yaml)?;
    let status = Command::new("sops")
        .current_dir(out_dir)
        .args(["--encrypt", "--in-place"])
        .arg(format!("secrets/{}.yaml", plan.name))
        .status()
        .context("running sops (is it on PATH?)")?;
    if !status.success() {
        let _ = fs::remove_file(&path);
        bail!("sops --encrypt failed for secrets/{}.yaml", plan.name);
    }
    Ok(path)
}

pub fn write_private(path: &std::path::Path, content: &str) -> Result<()> {
    use std::os::unix::fs::OpenOptionsExt;
    use std::io::Write;
    let mut f = fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .with_context(|| format!("writing {}", path.display()))?;
    f.write_all(content.as_bytes())?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn secret_names_are_stable_kebab() {
        assert_eq!(secret_name("homelab.arrStack.vpnEnvFile"), "arr-stack-vpn-env");
        assert_eq!(secret_name("homelab.meshagent.mshFile"), "meshagent-msh");
        assert_eq!(secret_name("homelab.acme.credentialsFile"), "acme-credentials");
        assert_eq!(secret_name("homelab.nextcloud.adminPasswordFile"), "nextcloud-admin-password");
        assert_eq!(secret_name("homelab.monitoring.grafanaOidcSecretFile"), "monitoring-grafana-oidc-secret");
    }

    #[test]
    fn env_keys_and_placeholders_are_told_apart() {
        assert!(is_env_key("SONARR_API_KEY"));
        assert!(is_env_key("FIREWALL_VPN_INPUT_PORTS (optional)"));
        assert!(is_env_key("ADMIN_TOKEN (argon2 hash)"));
        assert!(!is_env_key("<initial admin password>"));
    }

    #[test]
    fn placeholder_content_shapes() {
        assert_eq!(
            placeholder_content(&["A_KEY".into(), "B (optional)".into()]),
            "A_KEY=CHANGEME\n"
        );
        assert_eq!(placeholder_content(&["<password>".into()]), "CHANGEME\n");
    }

    #[test]
    fn generated_env_file_has_one_line_per_required_key() {
        let mut show = Vec::new();
        let c = generate_content("m", &["X".into(), "Y (optional)".into(), "SMTP_PASS (optional)".into()], &mut show).unwrap();
        assert_eq!(c.lines().count(), 1);
        assert!(c.starts_with("X="));
        assert_eq!(show.len(), 1);
    }

    #[test]
    fn vaultwarden_env_carries_the_argon2_hash_not_the_plaintext() {
        let mut show = Vec::new();
        let c = generate_content(
            "vaultwarden",
            &["ADMIN_TOKEN (argon2 hash)".into(), "SMTP_* (optional)".into()],
            &mut show,
        )
        .unwrap();
        assert!(c.starts_with("ADMIN_TOKEN='$argon2id$"), "got: {c}");
        assert_eq!(c.lines().count(), 1);
        assert_eq!(show.len(), 1);
        assert!(show[0].0.contains("plaintext"));
        assert!(!c.contains(&show[0].1), "plaintext must not be in the env file");
    }

    #[test]
    fn argon2_token_is_phc() {
        let h = argon2_phc("hunter2").unwrap();
        assert!(h.starts_with("$argon2id$v=19$m=65540,t=3,p=4$"));
    }

    #[test]
    fn supplied_requires_file_or_env() {
        assert!(parse_supplied(&["homelab.x=literal".into()]).is_err());
        std::env::set_var("HC_TEST_SECRET", "v");
        let s = parse_supplied(&["homelab.x=env:HC_TEST_SECRET".into()]).unwrap();
        assert_eq!(s.take("homelab.x").unwrap(), "v");
        assert!(s.unused(&[]).is_empty());
    }
}
