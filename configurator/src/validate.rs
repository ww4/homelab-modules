//! Prove the generated flake: `nix eval` the toplevel derivation (every
//! option and type error surfaces; nothing is built), or `nix build` it.
//! Flakes only see tracked files, so the directory is a git repo first.

use anyhow::{anyhow, Context, Result};
use serde::Serialize;
use std::fs;
use std::path::Path;
use std::process::Command;

#[derive(Serialize)]
pub struct Validation {
    pub mode: &'static str,
    pub ok: bool,
    pub host: String,
    /// The toplevel .drv (eval) or store path (build) on success.
    pub result: Option<String>,
    /// Tail of nix's stderr on failure.
    pub error: Option<String>,
}

impl Validation {
    pub fn render_text(&self) -> String {
        match (&self.ok, &self.result, &self.error) {
            (true, Some(r), _) => format!("validation ({}): OK — {}\n", self.mode, r),
            (_, _, Some(e)) => format!("validation ({}): FAILED\n{}\n", self.mode, e),
            _ => format!("validation ({}): FAILED\n", self.mode),
        }
    }
}

pub fn ensure_git(dir: &Path) -> Result<()> {
    if !dir.join(".git").exists() {
        let st = Command::new("git")
            .current_dir(dir)
            .args(["init", "-q"])
            .status()
            .context("git init")?;
        if !st.success() {
            return Err(anyhow!("git init failed in {}", dir.display()));
        }
    }
    let st = Command::new("git")
        .current_dir(dir)
        .args(["add", "-A"])
        .status()
        .context("git add")?;
    if !st.success() {
        return Err(anyhow!("git add failed in {}", dir.display()));
    }
    Ok(())
}

pub fn run(dir: &Path, host: &str, build: bool, library_override: Option<&str>) -> Result<Validation> {
    ensure_git(dir)?;
    let attr = format!(".#nixosConfigurations.{host}.config.system.build.toplevel");
    let mut cmd = Command::new("nix");
    cmd.current_dir(dir);
    if build {
        cmd.args(["build", "--no-link", "--print-out-paths"]).arg(&attr);
    } else {
        cmd.args(["eval", "--raw"]).arg(format!("{attr}.drvPath"));
    }
    if let Some(lib) = library_override {
        let lib = if lib.starts_with("path:") { lib.to_string() } else { format!("path:{lib}") };
        cmd.args(["--override-input", "homelab-modules", &lib, "--no-write-lock-file"]);
    }
    let out = cmd.output().context("running nix (is it on PATH?)")?;
    let mode = if build { "build" } else { "eval" };
    if out.status.success() {
        Ok(Validation {
            mode,
            ok: true,
            host: host.into(),
            result: Some(String::from_utf8_lossy(&out.stdout).trim().to_string()),
            error: None,
        })
    } else {
        let stderr = String::from_utf8_lossy(&out.stderr);
        // Keep the useful end; eval traces are long.
        let lines: Vec<&str> = stderr.lines().filter(|l| !l.starts_with("evaluation warning")).collect();
        let tail: Vec<&str> = lines.iter().rev().take(40).rev().copied().collect();
        Ok(Validation {
            mode,
            ok: false,
            host: host.into(),
            result: None,
            error: Some(tail.join("\n")),
        })
    }
}

/// The single host in a generated flake (hosts/<name>/).
pub fn only_host(dir: &Path) -> Result<String> {
    let mut hosts: Vec<String> = fs::read_dir(dir.join("hosts"))
        .with_context(|| format!("{} has no hosts/ directory", dir.display()))?
        .filter_map(|e| e.ok())
        .filter(|e| e.path().is_dir())
        .map(|e| e.file_name().to_string_lossy().to_string())
        .collect();
    match hosts.len() {
        1 => Ok(hosts.remove(0)),
        0 => Err(anyhow!("no hosts under {}/hosts", dir.display())),
        _ => Err(anyhow!("several hosts ({}) — pass --host", hosts.join(", "))),
    }
}
