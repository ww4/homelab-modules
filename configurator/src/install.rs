//! `install` — the local path: you are sitting at the machine, booted from a
//! live USB, and the generated flake is on it. nixos-anywhere is for
//! installing *another* machine over SSH; this runs disko and nixos-install
//! here, through disko's own `disko-install`, and then does the two things a
//! live-USB install forgets: it writes this machine's hardware.nix first,
//! and it carries the flake directory (admin key included) onto the new
//! system, because the live USB's filesystem is RAM and is gone at reboot.
//!
//! Erases every disk the answers name. Refuses without `--yes` or a typed
//! confirmation of the host name.

use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use std::fs;
use std::io::{self, BufRead, Write};
use std::path::Path;
use std::process::Command;

use crate::answers::Answers;

#[derive(Serialize)]
pub struct InstallReport {
    pub host: String,
    pub erased: Vec<String>,
    pub hardware_config: String,
    pub flake_copied_to: String,
    pub dry_run: bool,
}

impl InstallReport {
    pub fn render_text(&self) -> String {
        let mut s = String::new();
        if self.dry_run {
            s.push_str("dry run — nothing was written\n");
        } else {
            s.push_str(&format!("installed {} — remove the USB stick and reboot\n", self.host));
        }
        s.push_str(&format!("erased: {}\n", self.erased.join(", ")));
        s.push_str(&format!("hardware config: {}\n", self.hardware_config));
        s.push_str(&format!(
            "the flake (your values, secrets, and keys/) is at {} on the new system\n  \
             — move keys/admin-age-key.txt to a machine that is not this one, then delete it there\n  \
             — FIRST-LOGIN.md is in it too: read, store, delete\n",
            self.flake_copied_to
        ));
        s
    }
}

pub fn run(dir: &Path, host: Option<&str>, yes: bool, dry_run: bool, keep_at: &str) -> Result<InstallReport> {
    let dir = dir.canonicalize().with_context(|| format!("{} does not exist", dir.display()))?;
    let answers_path = dir.join("answers.json");
    let text = fs::read_to_string(&answers_path).with_context(|| format!("{} is not a generated flake (no answers.json)", dir.display()))?;
    let answers: Answers = serde_json::from_str(&text).context("parsing answers.json")?;
    let host = match host {
        Some(h) => h.to_string(),
        None => answers.host.name.clone(),
    };
    if !dir.join("extra-files/etc/ssh/ssh_host_ed25519_key").exists() {
        bail!("{}/extra-files/etc/ssh has no host key — generate wrote it; without it the new system cannot decrypt its secrets", dir.display());
    }

    let mut erased = vec![answers.host.disk.clone()];
    erased.extend(answers.host.data_disks.iter().map(|d| d.device.clone()));
    for dev in &erased {
        if !Path::new(dev).exists() {
            bail!("{dev}: no such device on this machine (the answers were written for a different box, or the disk is not attached)");
        }
    }

    if !dry_run {
        if unsafe { libc_geteuid() } != 0 {
            bail!("install needs root (run it from the live USB as root)");
        }
        if !yes {
            confirm(&host, &erased)?;
        }
    }

    // This machine's hardware, before the install reads the flake.
    let hw = dir.join("hosts").join(&host).join("hardware.nix");
    if !dry_run {
        let out = Command::new("nixos-generate-config")
            .args(["--no-filesystems", "--show-hardware-config"])
            .output()
            .context("running nixos-generate-config (is this a NixOS live system?)")?;
        if !out.status.success() {
            bail!("nixos-generate-config failed: {}", String::from_utf8_lossy(&out.stderr));
        }
        fs::write(&hw, &out.stdout).with_context(|| format!("writing {}", hw.display()))?;
        if dir.join(".git").exists() {
            // An untracked file is invisible to a flake; make sure it is seen.
            let _ = Command::new("git").current_dir(&dir).args(["add", "-A"]).status();
        }
    }

    let flake_ref = format!("{}#{host}", dir.display());
    let mut cmd = Command::new("disko-install");
    cmd.arg("--flake")
        .arg(&flake_ref)
        .args(["--mode", "format", "--write-efi-boot-entries"]);
    // disko-install maps every disko disk by name on the command line; the
    // generated layout calls the system disk `main` and each data disk by its
    // answers name.
    cmd.args(["--disk", "main", &answers.host.disk]);
    for d in &answers.host.data_disks {
        cmd.args(["--disk", &d.name, &d.device]);
    }
    cmd.arg("--extra-files")
        .arg(dir.join("extra-files/etc/ssh"))
        .arg("/etc/ssh")
        .arg("--extra-files")
        .arg(&dir)
        .arg(keep_at);
    if dry_run {
        cmd.arg("--dry-run");
    }
    let status = cmd.status().context("running disko-install (is disko on PATH?)")?;
    if !status.success() {
        bail!("disko-install failed (exit {})", status.code().unwrap_or(1));
    }

    Ok(InstallReport {
        host,
        erased,
        hardware_config: hw.display().to_string(),
        flake_copied_to: keep_at.to_string(),
        dry_run,
    })
}

fn confirm(host: &str, erased: &[String]) -> Result<()> {
    eprintln!("This ERASES every byte on:");
    for d in erased {
        eprintln!("  {d}");
    }
    eprint!("and installs {host} on them. Type the host name to continue: ");
    io::stderr().flush()?;
    let mut line = String::new();
    io::stdin().lock().read_line(&mut line)?;
    if line.trim() != host {
        return Err(anyhow!("not confirmed; nothing was touched"));
    }
    Ok(())
}

#[allow(non_snake_case)]
unsafe fn libc_geteuid() -> u32 {
    // Avoid a libc crate dependency for one call: /proc is always there on the
    // live system this runs on.
    fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| s.lines().find(|l| l.starts_with("Uid:")).and_then(|l| l.split_whitespace().nth(2).and_then(|u| u.parse().ok())))
        .unwrap_or(u32::MAX)
}
