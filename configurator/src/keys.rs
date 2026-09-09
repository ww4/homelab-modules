//! Key material: the admin's age key and the target's SSH host key, from
//! which sops-nix derives the host's age recipient. Both are produced by the
//! tools that define their formats (age-keygen, ssh-keygen, ssh-to-age).
//!
//! The host key is generated HERE so its recipient can be in .sops.yaml before
//! the machine exists; nixos-anywhere installs it via --extra-files, and the
//! first activation can decrypt.

use anyhow::{anyhow, bail, Context, Result};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

pub struct KeyMaterial {
    pub admin_recipient: String,
    /// Where a fresh admin key was written (None if the recipient was given).
    pub admin_key_file: Option<PathBuf>,
    pub host_recipient: String,
}

pub fn prepare(out: &Path, host: &str, admin_recipient: Option<&str>) -> Result<KeyMaterial> {
    let (admin_recipient, admin_key_file) = match admin_recipient {
        Some(r) => {
            if !r.starts_with("age1") {
                bail!("sops.adminRecipient must be an age public key (age1…), got `{r}`");
            }
            (r.to_string(), None)
        }
        None => {
            let dir = out.join("keys");
            fs::create_dir_all(&dir)?;
            let file = dir.join("admin-age-key.txt");
            if file.exists() {
                // --force re-run: keep the key the user may already have copied.
                let text = fs::read_to_string(&file)?;
                let rec = text
                    .lines()
                    .find_map(|l| l.strip_prefix("# public key: "))
                    .ok_or_else(|| anyhow!("{} has no `# public key:` line", file.display()))?;
                (rec.trim().to_string(), Some(file))
            } else {
                let out = Command::new("age-keygen")
                    .arg("-o")
                    .arg(&file)
                    .output()
                    .context("running age-keygen (is age on PATH?)")?;
                if !out.status.success() {
                    bail!("age-keygen failed: {}", String::from_utf8_lossy(&out.stderr));
                }
                // age-keygen reports the recipient on stderr.
                let text = String::from_utf8_lossy(&out.stderr);
                let rec = text
                    .lines()
                    .find_map(|l| l.trim().strip_prefix("Public key:"))
                    .map(|s| s.trim().to_string())
                    .ok_or_else(|| anyhow!("age-keygen printed no `Public key:` line"))?;
                (rec, Some(file))
            }
        }
    };

    // SSH host key → extra-files/etc/ssh/, mode 600, outside git.
    let ssh_dir = out.join("extra-files/etc/ssh");
    fs::create_dir_all(&ssh_dir)?;
    let host_key_file = ssh_dir.join("ssh_host_ed25519_key");
    if !host_key_file.exists() {
        let status = Command::new("ssh-keygen")
            .args(["-q", "-t", "ed25519", "-N", "", "-C"])
            .arg(format!("root@{host}"))
            .arg("-f")
            .arg(&host_key_file)
            .status()
            .context("running ssh-keygen (is openssh on PATH?)")?;
        if !status.success() {
            bail!("ssh-keygen failed");
        }
    }
    let pub_path = ssh_dir.join("ssh_host_ed25519_key.pub");
    let out_ = Command::new("ssh-to-age")
        .arg("-i")
        .arg(&pub_path)
        .output()
        .context("running ssh-to-age (is it on PATH?)")?;
    if !out_.status.success() {
        bail!("ssh-to-age failed: {}", String::from_utf8_lossy(&out_.stderr));
    }
    let host_recipient = String::from_utf8_lossy(&out_.stdout).trim().to_string();
    if !host_recipient.starts_with("age1") {
        bail!("ssh-to-age returned `{host_recipient}`");
    }

    Ok(KeyMaterial {
        admin_recipient,
        admin_key_file,
        host_recipient,
    })
}

/// The flake's sops config: every secret encrypted to both recipients, so the
/// box decrypts at activation and the admin can edit.
pub fn sops_yaml(k: &KeyMaterial, host: &str) -> String {
    format!(
        "# sops creation rules — who can decrypt secrets/*. The host key is what the\n\
         # machine decrypts with at activation; the admin key is what you edit with\n\
         # (`sops secrets/<name>.yaml`).\n\
         keys:\n  \
           - &host_{host} {}\n  \
           - &admin {}\n\
         creation_rules:\n  \
           - path_regex: secrets/[^/]+\\.(yaml|json|env)$\n    \
             key_groups:\n      \
               - age:\n          \
                   - *host_{host}\n          \
                   - *admin\n",
        k.host_recipient, k.admin_recipient
    )
    .replace("host_-", "host_")
}
