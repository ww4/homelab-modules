//! `install` — the local path: you are sitting at the machine, booted from a
//! live USB, and the generated flake is on it. nixos-anywhere is for
//! installing *another* machine over SSH; this runs disko and nixos-install
//! here, and then does the two things a live-USB install forgets: it writes
//! this machine's hardware.nix first, and it carries the flake directory
//! (admin key included) onto the new system, because the live USB's
//! filesystem is RAM and is gone at reboot.
//!
//! Format first, then install: `disko-install` builds the whole system into
//! the live USB's store before it touches a disk, and that store is RAM — a
//! 4 GB box ran out before partitioning (QEMU rehearsal, 2026-10-02). Running
//! `disko` and then `nixos-install --root /mnt` makes nixos-install download
//! straight into the target disk's store instead.
//!
//! Erases every disk the answers name. Refuses without `--yes` or a typed
//! confirmation of the host name.

use anyhow::{anyhow, bail, Context, Result};
use serde::Serialize;
use std::fs;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::answers::Answers;
use crate::schema::Schema;

#[derive(Serialize)]
pub struct InstallReport {
    pub host: String,
    pub erased: Vec<String>,
    pub hardware_config: String,
    pub flake_copied_to: String,
    pub dry_run: bool,
    /// The addresses the chosen modules answer at, once DNS points there.
    pub urls: Vec<String>,
    pub admin_user: String,
    /// What `dns` did after the install (None: no Cloudflare token on hand).
    pub dns: Option<String>,
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
        if !self.dry_run {
            s.push_str(&format!(
                "\nWhen it is back up (give it a few minutes the first time):\n  \
                 log in at its screen as `{}` with the password you typed (or the one in FIRST-LOGIN.md)\n  \
                 open, from any computer on your network: {}\n",
                self.admin_user,
                if self.urls.is_empty() { "—".to_string() } else { self.urls.join("  ") }
            ));
            match &self.dns {
                Some(d) => s.push_str(&format!("  DNS: {d}")),
                None => s.push_str("  DNS: those names need records → this machine's address; run `homelab-configure dns` on it with a Cloudflare token, or add them at your DNS provider\n"),
            }
        }
        s
    }
}

pub fn run(schema: &Schema, dir: &Path, host: Option<&str>, yes: bool, dry_run: bool, keep_at: &str) -> Result<InstallReport> {
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

    // ⚠️ LAST CHECK BEFORE ANYTHING IS DESTROYED, AND IT IS NOT A DUPLICATE
    // OF THE PICKER'S. The picker refuses to offer a disk this system is
    // running from, and once that refusal missed one: a stick written with
    // Ventoy holds the ISO as a file behind device-mapper, so none of its
    // partitions are mounted and it read as free. disko wrote a new partition
    // table onto the installer's own boot stick before mkfs refused.
    //
    // The answers can also be older than the machine they are run on, or
    // written for another box entirely. So this asks again, here, with
    // nothing between it and the erase.
    let live: Vec<crate::disks::Disk> = crate::disks::list();
    for dev in &erased {
        let real = std::fs::canonicalize(dev).unwrap_or_else(|_| PathBuf::from(dev));
        let kernel = real.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if let Some(d) = live.iter().find(|d| d.kernel == kernel || d.id == real || d.id.as_os_str() == dev.as_str()) {
            if d.in_use {
                bail!(
                    "refusing to erase {dev}: this machine is running from it. \
                     That is the disk the installer itself booted from, and erasing it would \
                     destroy the installer mid-install. Choose a different disk, or boot from \
                     other media."
                );
            }
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
    // 1. Partition, format and mount under /mnt. The generated disko.nix
    //    already names the devices the answers chose, so nothing is mapped
    //    on the command line.
    let mut disko = Command::new("disko");
    disko.args(["--mode", "destroy,format,mount", "--yes-wipe-all-disks", "--flake", &flake_ref]);
    // 2. Install into /mnt: nixos-install builds against the target store, so
    //    the closure is downloaded onto the new disk, not into live-USB RAM.
    //    The bootloader (systemd-boot, EFI variables) is written by it too.
    let mut install = Command::new("nixos-install");
    install.args(["--flake", &flake_ref, "--root", "/mnt", "--no-root-passwd", "--no-channel-copy"]);
    if dry_run {
        eprintln!("would run: {disko:?}");
        eprintln!("would run: {install:?}");
        eprintln!("would copy: {}/extra-files/etc/ssh → /mnt/etc/ssh, {} → /mnt{keep_at}", dir.display(), dir.display());
    } else {
        let status = disko.status().context("running disko (is disko on PATH?)")?;
        if !status.success() {
            bail!("disko failed (exit {})", status.code().unwrap_or(1));
        }
        // disko formats the layout's swap but does not activate it; the
        // install is exactly when a small box needs it (the final system
        // build was OOM-killed at 4 GB without swap). Best effort.
        if let Ok(o) = Command::new("blkid").args(["-t", "TYPE=swap", "-o", "device"]).output() {
            for dev in String::from_utf8_lossy(&o.stdout).lines().map(str::trim).filter(|d| !d.is_empty()) {
                let _ = Command::new("swapon").arg(dev).status();
            }
        }
        let status = install.status().context("running nixos-install (is it on PATH?)")?;
        if !status.success() {
            bail!("nixos-install failed (exit {})", status.code().unwrap_or(1));
        }
        // 3. What the live USB would otherwise lose: the pre-generated host
        //    key (the secrets are encrypted to it) and the flake itself.
        copy_tree(&dir.join("extra-files/etc/ssh"), Path::new("/mnt/etc/ssh"))?;
        copy_tree(&dir, &Path::new("/mnt").join(keep_at.trim_start_matches('/')))?;
    }

    let (_, names) = crate::dns::names(schema, &answers).unwrap_or_default();
    let urls: Vec<String> = names.iter().map(|n| format!("https://{n}")).collect();
    let admin_user = answers.values.get("homelab.adminUser").and_then(|v| v.as_str()).unwrap_or("admin").to_string();
    // 4. DNS, when the TUI was given a Cloudflare token: the records point at
    //    the address this machine has now, which is the one it boots with on
    //    a home network. Not fatal — the install is done either way.
    let token = dir.join(".secrets").join(crate::secrets::secret_name("homelab.acme.credentialsFile"));
    let dns = if !dry_run && token.exists() {
        Some(match crate::dns::run(schema, &dir, None, Some(&token), false) {
            Ok(r) => r.render_text(),
            Err(e) => format!("not done ({e}); run `homelab-configure dns {keep_at}` on the new system\n"),
        })
    } else {
        None
    };

    Ok(InstallReport {
        host,
        erased,
        hardware_config: hw.display().to_string(),
        flake_copied_to: keep_at.to_string(),
        dry_run,
        urls,
        admin_user,
        dns,
    })
}

/// `cp -a src/. dst/` — modes (the 0600 host key) and ownership kept.
fn copy_tree(src: &Path, dst: &Path) -> Result<()> {
    fs::create_dir_all(dst).with_context(|| format!("creating {}", dst.display()))?;
    let status = Command::new("cp")
        .arg("-a")
        .arg(format!("{}/.", src.display()))
        .arg(dst)
        .status()
        .with_context(|| format!("copying {} to {}", src.display(), dst.display()))?;
    if !status.success() {
        bail!("copying {} to {} failed", src.display(), dst.display());
    }
    Ok(())
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
