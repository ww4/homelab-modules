//! `dns` — the DNS records the chosen modules need, created at Cloudflare
//! with the same token ACME uses. Runs on the installed machine (the
//! address it points the names at is the one it has then — the tailnet
//! address once Tailscale has joined), or anywhere with `--ip`.
//!
//! Every homelab vhost is an A record → one address, proxied OFF: the
//! network gate is the perimeter and Cloudflare's proxy would sit outside
//! it. Idempotent — an existing record with the same content is left alone,
//! a different one is updated.

use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use std::fs;
use std::path::Path;
use std::process::Command;

use crate::answers::Answers;
use crate::plan::resolve_placeholder;
use crate::schema::Schema;

#[derive(Serialize)]
pub struct DnsReport {
    pub zone: String,
    pub address: String,
    pub created: Vec<String>,
    pub updated: Vec<String>,
    pub unchanged: Vec<String>,
    pub dry_run: bool,
}

impl DnsReport {
    pub fn render_text(&self) -> String {
        let mut s = format!("zone {} → {}{}\n", self.zone, self.address, if self.dry_run { " (dry run)" } else { "" });
        for (label, list) in [("created", &self.created), ("updated", &self.updated), ("unchanged", &self.unchanged)] {
            if !list.is_empty() {
                s.push_str(&format!("  {label}: {}\n", list.join(", ")));
            }
        }
        s
    }
}

/// The names the chosen modules claim, as `<sub>.<domain>`.
pub fn names(schema: &Schema, answers: &Answers) -> Result<(String, Vec<String>)> {
    let domain = answers
        .values
        .get("homelab.domain")
        .and_then(|v| v.as_str())
        .ok_or_else(|| anyhow!("homelab.domain is not set in the answers"))?
        .to_string();
    let (modules, _) = schema.close_over_requires(&answers.modules)?;
    let values: std::collections::BTreeMap<String, serde_json::Value> = answers.values.clone();
    let mut out = Vec::new();
    for m in &modules {
        if let Some(meta) = schema.catalog.get(m) {
            for v in &meta.vhosts {
                let sub = resolve_placeholder(schema, &values, v);
                let name = format!("{sub}.{domain}");
                if !out.contains(&name) {
                    out.push(name);
                }
            }
        }
    }
    Ok((domain, out))
}

pub fn run(schema: &Schema, dir: &Path, ip: Option<&str>, token_file: Option<&Path>, dry_run: bool) -> Result<DnsReport> {
    let text = fs::read_to_string(dir.join("answers.json")).with_context(|| format!("{} has no answers.json", dir.display()))?;
    let answers: Answers = serde_json::from_str(&text).context("parsing answers.json")?;
    let (domain, names) = names(schema, &answers)?;

    let address = match ip {
        Some(a) => a.to_string(),
        None => detect_address()?,
    };
    let token = read_token(token_file)?;

    let zone_id = cf_get(&token, &format!("zones?name={domain}"))?["result"]
        .as_array()
        .and_then(|a| a.first())
        .and_then(|z| z["id"].as_str())
        .map(String::from)
        .ok_or_else(|| anyhow!("the token sees no zone named {domain}"))?;

    let mut report = DnsReport { zone: domain.clone(), address: address.clone(), created: vec![], updated: vec![], unchanged: vec![], dry_run };
    for name in names {
        let existing = cf_get(&token, &format!("zones/{zone_id}/dns_records?type=A&name={name}"))?;
        let rec = existing["result"].as_array().and_then(|a| a.first()).cloned();
        match rec {
            Some(r) if r["content"].as_str() == Some(address.as_str()) => report.unchanged.push(name),
            Some(r) => {
                if !dry_run {
                    let id = r["id"].as_str().unwrap_or_default();
                    cf_send(&token, "PATCH", &format!("zones/{zone_id}/dns_records/{id}"), &serde_json::json!({ "content": address, "proxied": false }))?;
                }
                report.updated.push(name);
            }
            None => {
                if !dry_run {
                    cf_send(&token, "POST", &format!("zones/{zone_id}/dns_records"), &serde_json::json!({ "type": "A", "name": name, "content": address, "proxied": false, "ttl": 1, "comment": "homelab-configure" }))?;
                }
                report.created.push(name);
            }
        }
    }
    Ok(report)
}

/// The tailnet address if Tailscale is up, else the address of the default
/// route — what a record created from the installed box should point at.
fn detect_address() -> Result<String> {
    if let Ok(o) = Command::new("tailscale").args(["ip", "-4"]).output() {
        if o.status.success() {
            let s = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if !s.is_empty() {
                return Ok(s);
            }
        }
    }
    let o = Command::new("ip").args(["-4", "route", "get", "1.1.1.1"]).output().context("running ip")?;
    let text = String::from_utf8_lossy(&o.stdout);
    text.split_whitespace()
        .skip_while(|w| *w != "src")
        .nth(1)
        .map(String::from)
        .ok_or_else(|| anyhow!("could not detect this machine's address; pass --ip"))
}

/// The ACME token: the file named, else the sops-materialised path on an
/// installed system, else the typed-value file the TUI wrote.
fn read_token(explicit: Option<&Path>) -> Result<String> {
    let candidates: Vec<std::path::PathBuf> = match explicit {
        Some(p) => vec![p.to_path_buf()],
        None => vec!["/run/secrets/acme-credentials".into(), ".secrets/acme-credentials".into()],
    };
    for p in &candidates {
        if let Ok(text) = fs::read_to_string(p) {
            if let Some(t) = text.lines().find_map(|l| l.strip_prefix("CLOUDFLARE_DNS_API_TOKEN=")) {
                return Ok(t.trim().trim_matches('"').to_string());
            }
        }
    }
    bail!("no CLOUDFLARE_DNS_API_TOKEN found in {:?}; pass --token-file", candidates)
}

fn cf_get(token: &str, path: &str) -> Result<serde_json::Value> {
    let o = Command::new("curl")
        .args(["-fsS", "--max-time", "20", "-H", &format!("Authorization: Bearer {token}"), &format!("https://api.cloudflare.com/client/v4/{path}")])
        .output()
        .context("running curl")?;
    if !o.status.success() {
        bail!("Cloudflare GET {path} failed: {}", String::from_utf8_lossy(&o.stderr).trim());
    }
    Ok(serde_json::from_slice(&o.stdout).context("Cloudflare returned non-JSON")?)
}

fn cf_send(token: &str, method: &str, path: &str, body: &serde_json::Value) -> Result<serde_json::Value> {
    let o = Command::new("curl")
        .args(["-fsS", "--max-time", "20", "-X", method, "-H", &format!("Authorization: Bearer {token}"), "-H", "Content-Type: application/json", "--data", &body.to_string(), &format!("https://api.cloudflare.com/client/v4/{path}")])
        .output()
        .context("running curl")?;
    if !o.status.success() {
        bail!("Cloudflare {method} {path} failed: {}", String::from_utf8_lossy(&o.stderr).trim());
    }
    let v: serde_json::Value = serde_json::from_slice(&o.stdout).context("Cloudflare returned non-JSON")?;
    if v["success"].as_bool() != Some(true) {
        bail!("Cloudflare {method} {path}: {}", v["errors"]);
    }
    Ok(v)
}
