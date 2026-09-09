//! Write the flake. Every file is plain text produced from the plan; the only
//! side effects beyond the output directory are the subprocesses in keys/secrets.

pub mod nix;

use anyhow::{Context, Result};
use serde::Serialize;
use std::fs;
use std::path::Path;

use crate::keys;
use crate::plan::Plan;
use crate::schema::Source;
use crate::secrets;

#[derive(Serialize)]
pub struct Report {
    pub out: String,
    pub host: String,
    pub modules: Vec<String>,
    pub added_modules: Vec<String>,
    pub auto_values: Vec<String>,
    pub secrets: Vec<SecretReport>,
    pub dns_names: Vec<String>,
    pub files: Vec<String>,
    pub admin_key_generated: Option<String>,
    pub warnings: Vec<String>,
    pub next_steps: Vec<String>,
}

#[derive(Serialize)]
pub struct SecretReport {
    pub name: String,
    pub option: String,
    pub module: String,
    pub source: Source,
    pub file: String,
}

impl Report {
    pub fn render_text(&self) -> String {
        let mut s = format!("wrote {} for host {}\n", self.out, self.host);
        s.push_str(&format!("modules: {}\n", self.modules.join(" ")));
        if !self.added_modules.is_empty() {
            s.push_str(&format!("  added by requires: {}\n", self.added_modules.join(" ")));
        }
        if !self.auto_values.is_empty() {
            s.push_str(&format!("  enabled: {}\n", self.auto_values.join(" ")));
        }
        for sec in &self.secrets {
            s.push_str(&format!("secret {:<32} {:?}  ({})\n", sec.file, sec.source, sec.option));
        }
        if let Some(k) = &self.admin_key_generated {
            s.push_str(&format!("admin age key: {k}  — move it to ~/.config/sops/age/keys.txt and DELETE it here\n"));
        }
        for w in &self.warnings {
            s.push_str(&format!("warning: {w}\n"));
        }
        s.push_str("next:\n");
        for n in &self.next_steps {
            s.push_str(&format!("  - {n}\n"));
        }
        s
    }
}

pub fn write_all(plan: &Plan, out: &Path) -> Result<Report> {
    let host = &plan.host.name;
    let mut files: Vec<String> = Vec::new();
    let put = |files: &mut Vec<String>, rel: &str, content: String| -> Result<()> {
        let p = out.join(rel);
        if let Some(parent) = p.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&p, content).with_context(|| format!("writing {}", p.display()))?;
        files.push(rel.to_string());
        Ok(())
    };

    // Keys first: .sops.yaml must exist before any secret is encrypted.
    let km = keys::prepare(out, host, plan.admin_recipient.as_deref())?;
    put(&mut files, ".sops.yaml", keys::sops_yaml(&km, host))?;
    put(&mut files, ".gitignore", GITIGNORE.into())?;

    let mut secret_reports = Vec::new();
    let mut show_once: Vec<(String, String)> = Vec::new();
    for sp in &plan.secrets {
        let path = secrets::write_encrypted(out, sp)?;
        files.push(format!("secrets/{}.yaml", sp.name));
        secret_reports.push(SecretReport {
            name: sp.name.clone(),
            option: sp.option.clone(),
            module: sp.module.clone(),
            source: sp.source,
            file: path.strip_prefix(out).unwrap_or(&path).display().to_string(),
        });
        show_once.extend(sp.show_once.iter().cloned());
    }

    // The admin's console password (SSH keys are the intended path).
    let admin_user = plan
        .values
        .get("homelab.adminUser")
        .and_then(|v| v.as_str())
        .or_else(|| plan.schema.option("homelab.adminUser").and_then(|o| o.default.as_deref()).map(|d| d.trim_matches('"')))
        .unwrap_or("admin")
        .to_string();
    let admin_password = secrets::random_token(20);
    let admin_hash = mkpasswd(&admin_password)?;
    show_once.insert(0, (format!("console password for `{admin_user}`"), admin_password));

    put(&mut files, "flake.nix", nix::flake_nix(plan))?;
    put(&mut files, "homelab-values.nix", nix::values_nix(plan))?;
    put(&mut files, &format!("hosts/{host}/default.nix"), nix::host_nix(plan, &admin_user, &admin_hash))?;
    put(&mut files, &format!("hosts/{host}/disko.nix"), nix::disko_nix(plan))?;
    put(&mut files, &format!("hosts/{host}/hardware.nix"), nix::hardware_placeholder())?;
    put(&mut files, "README.md", readme(plan, &km))?;
    if !plan.phase2.is_empty() {
        put(&mut files, "PHASE-2.md", phase2(plan))?;
    }
    // FIRST-LOGIN.md holds plaintext: git-ignored, and created mode 600 (a
    // mode set after creation would leave a window at 644).
    let fl = out.join("FIRST-LOGIN.md");
    if fl.exists() {
        fs::remove_file(&fl)?;
    }
    secrets::write_private(&fl, &first_login(&show_once))?;
    files.push("FIRST-LOGIN.md".into());

    let mut next_steps = vec![
        "read FIRST-LOGIN.md, store what it holds, delete it".to_string(),
        format!(
            "create DNS records → this host for: {}",
            if plan.dns_names.is_empty() { "(none)".into() } else { plan.dns_names.join(", ") }
        ),
        format!(
            "install: nixos-anywhere --flake .#{host} --extra-files ./extra-files --generate-hardware-config nixos-generate-config ./hosts/{host}/hardware.nix root@<target>"
        ),
    ];
    if let Some(k) = &km.admin_key_file {
        next_steps.insert(
            0,
            format!("move {} to ~/.config/sops/age/keys.txt (it is git-ignored, but the flake dir is not a safe home for it)", k.display()),
        );
    }
    if show_once.iter().any(|(label, _)| label.contains("digest")) {
        next_steps.push(
            "SSO: add one entry per app to homelab.authelia.oidcClients using the client_secret digests in FIRST-LOGIN.md (client_id, redirect_uris — see the authelia module header)"
                .into(),
        );
    }
    if plan.phase2.is_empty() {
        next_steps.push("commit; every later change is a PR to this repo".into());
    } else {
        next_steps.push("after first boot: PHASE-2.md".into());
    }

    Ok(Report {
        out: out.display().to_string(),
        host: host.clone(),
        modules: plan.modules.clone(),
        added_modules: plan.added_modules.clone(),
        auto_values: plan.auto_values.clone(),
        secrets: secret_reports,
        dns_names: plan.dns_names.clone(),
        files,
        admin_key_generated: km.admin_key_file.as_ref().map(|p| p.display().to_string()),
        warnings: plan.warnings.clone(),
        next_steps,
    })
}

// gitignore has no inline comments: a trailing `# …` is part of the pattern.
const GITIGNORE: &str = "\
# Never commit these.
# the admin's age private key, if one was generated here
keys/
# the host's SSH private key (installed once by nixos-anywhere)
extra-files/
# plaintext show-once credentials
FIRST-LOGIN.md
result
";

fn mkpasswd(plain: &str) -> Result<String> {
    use std::io::Write;
    use std::process::{Command, Stdio};
    let mut child = Command::new("mkpasswd")
        .args(["-m", "yescrypt", "--stdin"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .context("running mkpasswd (is it on PATH?)")?;
    child.stdin.take().unwrap().write_all(plain.as_bytes())?;
    let out = child.wait_with_output()?;
    if !out.status.success() {
        anyhow::bail!("mkpasswd failed");
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

fn readme(plan: &Plan, km: &keys::KeyMaterial) -> String {
    let host = &plan.host.name;
    let mut s = format!(
        "# {host} — a homelab built from homelab-modules\n\n\
         Generated by homelab-configure. This repository is the private half of the\n\
         split: your values, your secrets (encrypted), your hardware. The\n\
         implementation is the `homelab-modules` flake input.\n\n\
         ## Install (the one supported path)\n\n\
         Boot the target into any Linux with SSH as root (the NixOS installer ISO\n\
         works), then from this directory:\n\n\
         ```sh\n\
         nixos-anywhere --flake .#{host} \\\n  \
           --extra-files ./extra-files \\\n  \
           --generate-hardware-config nixos-generate-config ./hosts/{host}/hardware.nix \\\n  \
           root@<target-ip>\n\
         ```\n\n\
         That partitions `{disk}` per `hosts/{host}/disko.nix` (**erasing it**),\n\
         installs, and places the pre-generated SSH host key so sops can decrypt on\n\
         the first boot. Commit `hosts/{host}/hardware.nix` afterwards.\n\n\
         ## Before installing\n\n\
         - `FIRST-LOGIN.md` — show-once credentials. Store them, then delete the file.\n\
         - DNS: point these names at the host (its LAN or tailnet address):\n",
        disk = plan.host.disk
    );
    if plan.dns_names.is_empty() {
        s.push_str("  (none — no vhost modules chosen)\n");
    }
    for n in &plan.dns_names {
        s.push_str(&format!("  - `{n}`\n"));
    }
    s.push_str("\n## Secrets\n\n");
    s.push_str(&format!(
        "Encrypted with sops to two age recipients (see `.sops.yaml`): the host key\n\
         (`{host_rec}`) and the admin key (`{admin_rec}`). Edit with `sops secrets/<name>.yaml`\n\
         (needs the admin private key in `~/.config/sops/age/keys.txt`).\n\n",
        host_rec = km.host_recipient,
        admin_rec = km.admin_recipient
    ));
    s.push_str("| file | for | class |\n|---|---|---|\n");
    for sp in &plan.secrets {
        s.push_str(&format!(
            "| `secrets/{}.yaml` | `{}` | {:?} |\n",
            sp.name, sp.option, sp.source
        ));
    }
    if !plan.phase2.is_empty() {
        s.push_str("\nSome values only exist after a service has run once — see `PHASE-2.md`.\n");
    }
    s.push_str("\n## Layout\n\n");
    s.push_str(&format!(
        "```\n\
         flake.nix            inputs + nixosConfigurations.{host}\n\
         homelab-values.nix   every homelab.* value + the sops declarations\n\
         hosts/{host}/{pad}  host file (users, ssh, sops), disko.nix, hardware.nix\n\
         secrets/             sops-encrypted files\n\
         extra-files/         the host SSH key (git-ignored; used once, at install)\n\
         keys/                a generated admin age key, if any (git-ignored)\n\
         ```\n\n\
         Change something: edit, `nix build .#nixosConfigurations.{host}.config.system.build.toplevel`,\n\
         commit, `nixos-rebuild switch --flake .#{host}` on the host (or your GitOps of choice).\n",
        pad = " ".repeat(12usize.saturating_sub(host.len()))
    ));
    s
}

fn first_login(show_once: &[(String, String)]) -> String {
    let mut s = String::from(
        "# FIRST LOGIN — show-once credentials\n\n\
         Everything below exists ONLY here in plaintext. Move it to your password\n\
         manager and delete this file. It is git-ignored, but that is not storage.\n\n",
    );
    for (label, value) in show_once {
        s.push_str(&format!("- **{label}**\n  `{value}`\n"));
    }
    s
}

fn phase2(plan: &Plan) -> String {
    let mut s = String::from(
        "# PHASE 2 — after the first boot\n\n\
         These values are minted by a service the first time it runs, so the\n\
         flake could not carry them. Each secret below was encrypted with a\n\
         `CHANGEME` placeholder so the system evaluates and boots; the service\n\
         that reads it will fail loudly until the real value is in place.\n\n\
         Log in to the service, copy its API key (Settings → General), then:\n\n",
    );
    for item in &plan.phase2 {
        s.push_str(&format!("- {item}\n"));
    }
    s.push_str("\nThen rebuild. Editing a sops file needs the admin age key in `~/.config/sops/age/keys.txt`.\n");
    s
}
