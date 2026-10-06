//! The installer's model: the questions, their order, what each answer has
//! to look like, and what happens when they are all in. Both front ends —
//! the console wizard (`tui`) and the browser one (`web`) — drive this and
//! nothing else, so the two cannot drift apart, and an answers file written
//! by either is the same file the headless `generate` takes.
//!
//! The shape follows Ubuntu Server's installer: one question per screen,
//! Back and Continue, a check at each step that says what is wrong in one
//! sentence, and soft warnings a second Continue accepts.

use anyhow::{Context, Result};
use serde_json::json;
use std::collections::BTreeMap;
use std::io::{self, BufRead};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicI32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::answers::Answers;
use crate::disks::{self, Disk};
use crate::plan::FOUNDATION_ALWAYS;
use crate::schema::{Schema, Source};

// ---------------------------------------------------------------- steps

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Step {
    Welcome,
    Kit,
    Storage,
    Profile,
    Ssh,
    Domain,
    Extras,
    Review,
    Install,
    Done,
}

pub const STEPS: [Step; 10] = [
    Step::Welcome,
    Step::Kit,
    Step::Storage,
    Step::Profile,
    Step::Ssh,
    Step::Domain,
    Step::Extras,
    Step::Review,
    Step::Install,
    Step::Done,
];

/// The steps a person counts: Install and Done are not questions.
pub const QUESTION_STEPS: usize = 8;

impl Step {
    pub fn title(self) -> &'static str {
        match self {
            Step::Welcome => "Welcome",
            Step::Kit => "What should this machine be?",
            Step::Storage => "Storage",
            Step::Profile => "Profile",
            Step::Ssh => "SSH access",
            Step::Domain => "Domain and certificates",
            Step::Extras => "Modules",
            Step::Review => "Review",
            Step::Install => "Installing",
            Step::Done => "Finished",
        }
    }
    pub fn id(self) -> &'static str {
        match self {
            Step::Welcome => "welcome",
            Step::Kit => "kit",
            Step::Storage => "storage",
            Step::Profile => "profile",
            Step::Ssh => "ssh",
            Step::Domain => "domain",
            Step::Extras => "extras",
            Step::Review => "review",
            Step::Install => "install",
            Step::Done => "done",
        }
    }
    pub fn index(self) -> usize {
        STEPS.iter().position(|s| *s == self).unwrap_or(0)
    }
    /// What the screen is for, in one paragraph.
    pub fn intro(self, live_usb: bool) -> &'static str {
        match self {
            Step::Welcome => "This installs a complete, self-hosted homelab on this machine — a media server, apps, backups and monitoring — from a public module library, in one pass. You pick a kit, choose the disk to install on (it is erased), set your name and password, give it a domain at Cloudflare with an API token, and it installs. Nothing is written until the last screen says Install.",
            Step::Kit => "One choice sets the whole module list; the Modules screen later lets you adjust it. The number is the memory the kit needs on this machine: each module's figure plus a gigabyte for the system itself.",
            Step::Storage => "Choose the disk the system goes on; the whole disk is erased. Any other disk can be marked data (storage for your files, pooled) or parity (protects the data disks). The USB stick you booted from is not listed and cannot be chosen.",
            Step::Profile => "The machine's name and the account you will log in with.",
            Step::Ssh => "SSH is how you reach the machine from another computer without sitting at it. Add the public keys that may log in as the admin: import them from GitHub, or paste one. Skipping is allowed; then only the machine's own screen works.",
            Step::Domain => "Every app gets a name under your domain and a real certificate, so browsers trust it. For this release the domain's DNS must be at Cloudflare (register there, or move a domain's nameservers there). Each box below is one line of a credentials file; the Cloudflare token is checked against your domain the moment you save it.",
            Step::Extras => "Everything here is already set the way the kit wants it, and those answers are good ones: if you have no preference, press Continue. The list is every module in the library, with the kit's choices ticked — tick another to add it, untick one to leave it out. Anything that needed a value from you was asked on an earlier screen.",
            Step::Review => {
                if live_usb {
                    "Everything you have chosen. Install writes the configuration, checks it, partitions the disks, downloads the system onto the new disk and installs it: twenty to forty minutes on a home connection."
                } else {
                    "Everything you have chosen. This machine is not the installer, so the configuration is written and the install command printed rather than run."
                }
            }
            Step::Install => "Working. Do not turn the machine off.",
            Step::Done => "What happened.",
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Role {
    Unused,
    System,
    Data,
    Parity,
}

impl Role {
    /// The next role when Space is pressed. `system_taken` is true when
    /// ANOTHER disk is already the system disk: then SYSTEM comes last
    /// rather than first, so walking a second disk to `data` cannot
    /// silently steal it from the first (the console flow the first hardware run hit).
    pub fn next(self, system_taken: bool) -> Role {
        match (self, system_taken) {
            (Role::Unused, false) => Role::System,
            (Role::Unused, true) => Role::Data,
            (Role::System, _) => Role::Data,
            (Role::Data, _) => Role::Parity,
            (Role::Parity, true) => Role::System,
            (Role::Parity, false) => Role::Unused,
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Role::Unused => "not used",
            Role::System => "SYSTEM",
            Role::Data => "data",
            Role::Parity => "parity",
        }
    }
    pub fn id(self) -> &'static str {
        match self {
            Role::Unused => "unused",
            Role::System => "system",
            Role::Data => "data",
            Role::Parity => "parity",
        }
    }
    pub fn from_id(s: &str) -> Role {
        match s {
            "system" => Role::System,
            "data" => Role::Data,
            "parity" => Role::Parity,
            _ => Role::Unused,
        }
    }
    /// What choosing this role does, for the help box.
    pub fn help(self) -> &'static str {
        match self {
            Role::System => "the operating system and every app's data live here; the whole disk is erased",
            Role::Data => "storage for your files, erased and pooled under /mnt/media with any other data disk",
            Role::Parity => "holds parity for the data disks so one can die without loss; at least as large as the largest data disk",
            Role::Unused => "left alone",
        }
    }
}

/// One editable line on a screen. `choices` turns it into a pick-list; with
/// `other_ok` the reader can still type something the list does not have.
#[derive(Clone)]
pub struct Field {
    pub key: String,
    pub label: String,
    pub help: String,
    pub value: String,
    pub masked: bool,
    pub choices: Vec<(String, String)>,
    pub other_ok: bool,
}

/// The time zones nearly everybody wants, the United States first because
/// that is where the readers are, then a handful of common others. Any IANA
/// name works: the list is a shortcut, not a limit.
pub const COMMON_TIMEZONES: [(&str, &str); 13] = [
    ("America/New_York", "US Eastern - New York"),
    ("America/Chicago", "US Central - Chicago"),
    ("America/Denver", "US Mountain - Denver"),
    ("America/Phoenix", "US Arizona - no daylight saving"),
    ("America/Los_Angeles", "US Pacific - Los Angeles"),
    ("America/Anchorage", "US Alaska - Anchorage"),
    ("Pacific/Honolulu", "US Hawaii - Honolulu"),
    ("America/Toronto", "Canada Eastern - Toronto"),
    ("Europe/London", "UK - London"),
    ("Europe/Berlin", "Central Europe - Berlin"),
    ("Australia/Sydney", "Australia - Sydney"),
    ("Asia/Tokyo", "Japan - Tokyo"),
    ("UTC", "UTC - no local time"),
];

impl Field {
    fn new(key: &str, label: &str, help: &str, value: &str) -> Field {
        Field { key: key.into(), label: label.into(), help: help.into(), value: value.into(), masked: false, choices: Vec::new(), other_ok: true }
    }
    fn masked(mut self) -> Field {
        self.masked = true;
        self
    }
    fn choices(mut self, c: &[(&str, &str)], other_ok: bool) -> Field {
        self.choices = c.iter().map(|(v, l)| (v.to_string(), l.to_string())).collect();
        self.other_ok = other_ok;
        self
    }
    /// The next value in the list, for a front end that cycles rather than
    /// drops down (the console).
    pub fn next_choice(&self) -> Option<String> {
        if self.choices.is_empty() {
            return None;
        }
        let at = self.choices.iter().position(|(v, _)| *v == self.value);
        Some(self.choices[at.map(|i| (i + 1) % self.choices.len()).unwrap_or(0)].0.clone())
    }
    /// What a list value is called on screen.
    pub fn label_of(&self, value: &str) -> String {
        self.choices.iter().find(|(v, _)| v == value).map(|(_, l)| l.clone()).unwrap_or_else(|| value.to_string())
    }
    /// What to show in the value column (a masked value never leaves as text).
    pub fn shown(&self) -> String {
        if self.value.is_empty() {
            "—".into()
        } else if self.masked {
            "•".repeat(self.value.chars().count().min(32))
        } else {
            self.value.clone()
        }
    }
}

pub struct Kit {
    pub name: &'static str,
    pub blurb: &'static str,
    pub modules: Vec<String>,
}

pub fn kits() -> Vec<Kit> {
    let profile = |json: &str| -> Vec<String> {
        serde_json::from_str::<serde_json::Value>(json)
            .ok()
            .and_then(|v| v["modules"].as_array().map(|a| a.iter().filter_map(|m| m.as_str().map(String::from)).collect()))
            .unwrap_or_default()
    };
    vec![
        Kit {
            name: "Starter",
            blurb: "Movies and shows (Jellyfin), a recipe app (Tandoor), nightly backups, monitoring with alerts to your phone. The smallest real homelab; add more later.",
            modules: ["acme", "nginx-access", "jellyfin", "tandoor", "backup", "monitoring", "ntfy", "alertmanager-ntfy"].iter().map(|s| s.to_string()).collect(),
        },
        Kit { name: "Media box", blurb: "The Starter plus the whole media pipeline: *arr apps behind a VPN, audiobooks, music, a disk pool with parity.", modules: profile(include_str!("../profiles/media-box.json")) },
        Kit { name: "Docs and forge", blurb: "Documents, photos, notes, passwords, a git forge, single sign-on: the office half.", modules: profile(include_str!("../profiles/docs-forge.json")) },
        Kit { name: "Everything", blurb: "Every module in the library. Needs a domain, a VPN account, a Backblaze account and a few disks.", modules: profile(include_str!("../profiles/everything.json")) },
        Kit { name: "Minimal", blurb: "Just the foundation: a machine that boots and is yours, serving nothing yet.", modules: vec![] },
        Kit { name: "Custom", blurb: "Whatever you tick on the Modules screen. Picking this changes nothing; it is what the kit says when your list matches none of the above.", modules: vec![CUSTOM.to_string()] },
    ]
}

/// The marker of the kit that is not a list: "whatever is ticked".
pub const CUSTOM: &str = "__custom";

/// Which kit a ticked set amounts to. Two sets of modules are discounted on
/// both sides: the foundation every install has, and anything the wizard
/// turned on by itself — marking a data disk brings in mergerfs-pools, and
/// that is not the reader choosing a different kit.
pub fn kit_for(chosen: &std::collections::BTreeSet<&str>, auto: &std::collections::BTreeSet<&str>, kits: &[Kit]) -> usize {
    let strip = |set: &std::collections::BTreeSet<&str>| -> std::collections::BTreeSet<String> {
        set.iter().filter(|n| !FOUNDATION_ALWAYS.contains(*n) && !auto.contains(*n)).map(|n| n.to_string()).collect()
    };
    let mine = strip(chosen);
    for (i, k) in kits.iter().enumerate() {
        if k.modules.iter().any(|m| m == CUSTOM) {
            continue;
        }
        let theirs: std::collections::BTreeSet<&str> = k.modules.iter().map(|s| s.as_str()).collect();
        if strip(&theirs) == mine {
            return i;
        }
    }
    kits.len() - 1
}

pub struct ModuleRow {
    pub name: String,
    pub description: String,
    pub chosen: bool,
    pub locked: bool,
    pub memory: u64,
}

/// A secret file as a form: one row per variable the module reads.
pub struct SecretRow {
    pub option: String,
    /// Field keys that are `homelab.*` options rather than lines of the file.
    pub option_fields: Vec<String>,
    pub title: String,
    pub steps: String,
    /// One per variable; empty when the value is a path to a file the user has.
    pub fields: Vec<Field>,
    pub optional: Vec<bool>,
    /// The module wants a path to an existing file (the MeshCentral .msh).
    pub path_only: bool,
    pub path: String,
    pub saved: Option<PathBuf>,
    pub verified: Option<Result<String, String>>,
    pub skipped: bool,
}

impl SecretRow {
    /// What this secret is called on screen.
    pub fn short(&self) -> String {
        match self.option.as_str() {
            "homelab.acme.credentialsFile" => "Cloudflare API token".into(),
            "homelab.arrStack.vpnEnvFile" => "VPN account".into(),
            "homelab.backup.remote.environmentFile" => "Offsite backup account".into(),
            "homelab.meshagent.mshFile" => "MeshCentral agent file".into(),
            o => o.rsplit('.').next().unwrap_or(o).to_string(),
        }
    }
    pub fn state(&self) -> String {
        if self.skipped {
            return "skipped — set it later and rebuild".into();
        }
        match (&self.saved, &self.verified) {
            (None, _) => "— not set".into(),
            (Some(_), Some(Ok(m))) => format!("ok: {m}"),
            (Some(_), Some(Err(m))) => format!("FAILED: {m}"),
            (Some(p), None) => format!("saved: {}", p.display()),
        }
    }
    pub fn filled(&self) -> bool {
        self.skipped || self.saved.is_some()
    }
}

/// What a running install reports.
pub struct Progress {
    pub lines: Mutex<Vec<String>>,
    pub done: AtomicBool,
    pub exit: AtomicI32,
}

/// A refused Continue: `soft` means a second Continue accepts it.
pub struct Blocked {
    pub message: String,
    pub soft: bool,
}

pub struct Wizard {
    pub schema: &'static Schema,
    pub step: Step,
    /// The last thing that happened, and whether it was a complaint.
    pub status: Option<(String, bool)>,
    pub ram_mib: u64,
    pub network: Arc<Mutex<(String, Option<bool>)>>,
    pub live_usb: bool,
    pub kits: Vec<Kit>,
    pub kit: usize,
    pub disks: Vec<Disk>,
    pub roles: Vec<Role>,
    pub disk_fallback: Field,
    pub profile: Vec<Field>,
    pub admin_hash: Option<String>,
    pub github_user: Field,
    pub pasted_key: Field,
    pub keys: Vec<String>,
    pub domain: Vec<Field>,
    pub secrets: Vec<SecretRow>,
    pub modules: Vec<ModuleRow>,
    pub values: BTreeMap<String, String>,
    pub out_answers: PathBuf,
    pub out_dir: PathBuf,
    pub exe_prefix: Vec<String>,
    pub progress: Option<Arc<Progress>>,
    pub install_report: Vec<String>,
    /// A soft warning has been shown once; the next Continue accepts it.
    pub warned: bool,
    /// The code a browser must present, and where it can reach us.
    pub pairing: String,
    pub web_port: u16,
    /// Set when a browser has driven this wizard, so the console says so.
    pub web_seen: Option<String>,
    /// The browser that holds the form. A second one is shown a warning and
    /// has to take over deliberately; the first then sees that it lost it.
    pub controller: Option<String>,
    /// Modules the wizard turned on by itself (a data disk brings mergerfs,
    /// a parity disk brings snapraid). They are not the reader's choices, so
    /// they must not turn the kit into "Custom".
    pub auto_modules: std::collections::BTreeSet<String>,
    /// What a check for a newer installer found: (newest commit, newer?).
    pub update: Option<(String, bool)>,
    /// Set when a newer installer has been fetched: the front end restores
    /// the terminal and hands the process over to it.
    pub relaunch: Option<String>,
}

impl Wizard {
    pub fn new(schema: &'static Schema, out_answers: &Path, out_dir: &Path, exe_prefix: Vec<String>, web_port: u16) -> Wizard {
        let mut modules: Vec<ModuleRow> = schema
            .catalog
            .iter()
            .filter(|(n, _)| n.as_str() != "options")
            .map(|(n, m)| ModuleRow {
                name: n.clone(),
                description: m.description.clone(),
                chosen: FOUNDATION_ALWAYS.contains(&n.as_str()),
                locked: FOUNDATION_ALWAYS.contains(&n.as_str()),
                memory: m.memory,
            })
            .collect();
        modules.sort_by(|a, b| (!a.locked, &a.name).cmp(&(!b.locked, &b.name)));
        // The disk the live system runs from is not a choice at all.
        let disks: Vec<Disk> = disks::list().into_iter().filter(|d| !d.in_use).collect();
        let roles = vec![Role::Unused; disks.len()];
        let live_usb = Path::new("/iso").exists() || Path::new("/nix/.ro-store").exists();
        let mut w = Wizard {
            schema,
            step: Step::Welcome,
            status: None,
            ram_mib: crate::plan::machine_ram_mib(),
            network: Arc::new(Mutex::new((String::new(), None))),
            live_usb,
            kits: kits(),
            kit: 0,
            disks,
            roles,
            disk_fallback: Field::new("disk", "System disk (by path)", "No disks were found under /dev/disk/by-id. Type the device the system should go on, e.g. /dev/sda — it is ERASED by the install.", ""),
            profile: vec![
                Field::new("name", "Machine name", "One word, letters, digits and dashes: how the machine is called on the network and in its own configuration.", "homelab"),
                Field::new("adminUser", "Admin username", "The account you log in with, at the machine's screen and over SSH. It can use sudo.", "admin"),
                Field::new("password", "Password", "What you type to log in as the admin. Pick a good one and write it down: there is no reset email. Leave both empty to have one minted into FIRST-LOGIN.md.", "").masked(),
                Field::new("password2", "Confirm password", "The same password again.", "").masked(),
                Field::new("timeZone", "Time zone", "Where this machine is: backups and alerts are scheduled in local time. Pick from the list, or type any IANA name (Region/City).", "America/New_York").choices(&COMMON_TIMEZONES, true),
            ],
            admin_hash: None,
            github_user: Field::new("githubUser", "Import keys from GitHub", "A GitHub username: the public keys on that account are added to the list below, the way Ubuntu's installer imports an SSH identity. Nothing already in the list is removed.", ""),
            pasted_key: Field::new("pasteKey", "Paste a public key", "A line from a file such as ~/.ssh/id_ed25519.pub, starting with ssh-ed25519, ssh-rsa or ecdsa-. It is checked before it is added.", ""),
            keys: Vec::new(),
            domain: vec![
                Field::new("homelab.domain", "Domain", "Your domain, like example.com, with nothing in front of it. Every app gets a name under it (recipes.example.com) and a real certificate.", ""),
                Field::new("homelab.acme.email", "Email for certificates", "Let's Encrypt sends certificate notices here. Any address you read.", ""),
            ],
            secrets: Vec::new(),
            modules,
            values: BTreeMap::new(),
            out_answers: out_answers.to_path_buf(),
            out_dir: out_dir.to_path_buf(),
            exe_prefix,
            progress: None,
            install_report: Vec::new(),
            warned: false,
            pairing: crate::secrets::random_token(16).to_uppercase().chars().filter(|c| c.is_ascii_alphanumeric()).take(6).collect(),
            web_port,
            web_seen: None,
            controller: None,
            auto_modules: Default::default(),
            update: None,
            relaunch: None,
        };
        if w.pairing.len() < 6 {
            w.pairing = format!("{:0>6}", w.pairing);
        }
        w.apply_kit();
        w
    }

    /// Keep the network line current: DHCP is often still negotiating when
    /// the installer starts, and a cable plugged in later must be noticed
    /// without a restart.
    pub fn watch_network(&self) {
        let shared = self.network.clone();
        std::thread::spawn(move || loop {
            let probe = probe_network();
            *shared.lock().unwrap() = probe;
            std::thread::sleep(Duration::from_secs(3));
        });
    }

    pub fn network(&self) -> (String, Option<bool>) {
        self.network.lock().unwrap().clone()
    }

    /// The commit this binary was built from, or "dirty" from a work tree.
    pub fn version() -> &'static str {
        option_env!("HOMELAB_REV").unwrap_or("dirty")
    }

    /// Where the pairing code and the step live across a relaunch, beside
    /// the answers the new process reads back.
    fn session_path(&self) -> PathBuf {
        self.out_answers.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| PathBuf::from(".")).join(".installer-session.json")
    }

    /// Keep the code across an update: a new code would lock the browser out
    /// of its own install.
    pub fn restore_session(&mut self) {
        if let Ok(t) = std::fs::read_to_string(self.session_path()) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&t) {
                if let Some(c) = v["pairing"].as_str() {
                    self.pairing = c.to_string();
                }
                if let Some(list) = v["skipped"].as_array() {
                    let skipped: Vec<String> = list.iter().filter_map(|s| s.as_str().map(String::from)).collect();
                    for r in &mut self.secrets {
                        if skipped.contains(&r.option) {
                            r.skipped = true;
                        }
                    }
                }
            }
        }
    }

    pub fn save_session(&self) {
        let skipped: Vec<&str> = self.secrets.iter().filter(|r| r.skipped).map(|r| r.option.as_str()).collect();
        let _ = std::fs::write(self.session_path(), json!({ "pairing": self.pairing, "skipped": skipped }).to_string());
    }

    /// Ask the mirror what the newest installer is. Cheap: one small request.
    pub fn check_update(&mut self) -> Result<String, String> {
        let out = Command::new("curl")
            .args(["-fsS", "--max-time", "15", "https://api.github.com/repos/ww4/homelab-modules/commits/main"])
            .output()
            .map_err(|e| format!("could not reach GitHub: {e}"))?;
        if !out.status.success() {
            return Err("could not reach GitHub to check for an update".into());
        }
        let v: serde_json::Value = serde_json::from_slice(&out.stdout).map_err(|_| "GitHub returned something unexpected".to_string())?;
        let sha = v["sha"].as_str().unwrap_or("").to_string();
        if sha.is_empty() {
            return Err("GitHub returned no commit".into());
        }
        let mine = Self::version();
        if mine == "dirty" {
            self.update = Some((sha, false));
            return Ok("this installer was built from a work tree, so there is nothing to compare it with".into());
        }
        let newer = !sha.starts_with(mine) && !mine.starts_with(&sha[..7.min(sha.len())]);
        self.update = Some((sha.clone(), newer));
        Ok(if newer {
            format!("a newer installer is available ({})", &sha[..7.min(sha.len())])
        } else {
            "this is the newest installer".into()
        })
    }

    /// Fetch the newest installer and hand this process over to it, keeping
    /// the answers, the saved secrets and the pairing code. Never during an
    /// install: the install is a child of this process.
    pub fn apply_update(&mut self) -> Result<String, String> {
        if self.progress.is_some() || self.step == Step::Install {
            return Err("not while the install is running".into());
        }
        let answers = self.answers();
        std::fs::write(&self.out_answers, serde_json::to_string_pretty(&answers).map_err(|e| e.to_string())? + "\n").map_err(|e| e.to_string())?;
        self.save_session();
        let out = Command::new("nix")
            .args([
                "build",
                "--refresh",
                "--no-write-lock-file",
                "--max-jobs",
                "0",
                "--no-link",
                "--print-out-paths",
                "github:ww4/homelab-modules?dir=configurator#default",
            ])
            .output()
            .map_err(|e| format!("could not run nix: {e}"))?;
        if !out.status.success() {
            return Err(format!("could not fetch the newer installer: {}", String::from_utf8_lossy(&out.stderr).lines().last().unwrap_or("").trim()));
        }
        let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
        let exe = format!("{path}/bin/homelab-configure");
        if !Path::new(&exe).exists() {
            return Err(format!("{exe}: not there after the fetch"));
        }
        Ok(exe)
    }

    pub fn say(&mut self, msg: impl Into<String>, err: bool) {
        self.status = Some((msg.into(), err));
    }

    // ------------------------------------------------------------ loading

    /// Prefill from an answers file (a profile, or a second run).
    pub fn load(&mut self, a: &Answers) {
        self.admin_hash = a.host.admin_password_hash.clone();
        for f in &mut self.profile {
            match f.key.as_str() {
                "name" => f.value = a.host.name.clone(),
                "timeZone" => f.value = a.host.time_zone.clone(),
                "adminUser" => {
                    if let Some(u) = a.values.get("homelab.adminUser").and_then(|v| v.as_str()) {
                        f.value = u.to_string();
                    }
                }
                _ => {}
            }
        }
        self.keys = a.host.ssh_authorized_keys.clone();
        for (k, v) in &a.values {
            self.values.insert(k.clone(), value_text(v));
        }
        for f in &mut self.domain {
            if let Some(v) = self.values.get(&f.key) {
                f.value = v.clone();
            }
        }
        for (i, d) in self.disks.iter().enumerate() {
            let id = d.id.display().to_string();
            if id == a.host.disk {
                self.roles[i] = Role::System;
            } else if let Some(dd) = a.host.data_disks.iter().find(|dd| dd.device == id) {
                self.roles[i] = if dd.name == "parity" { Role::Parity } else { Role::Data };
            }
        }
        if self.disks.is_empty() {
            self.disk_fallback.value = a.host.disk.clone();
        }
        let chosen: std::collections::BTreeSet<&str> = a.modules.iter().map(|s| s.as_str()).collect();
        for m in &mut self.modules {
            m.chosen = m.locked || chosen.contains(m.name.as_str());
        }
        // A relaunch brings back modules but not why they are on. The disks
        // say: data disks mean mergerfs was added for them, a parity disk
        // means snapraid was.
        let has_data = a.host.data_disks.iter().any(|d| d.name != "parity");
        let has_parity = a.host.data_disks.iter().any(|d| d.name == "parity");
        for (name, implied) in [("mergerfs-pools", has_data), ("snapraid", has_parity)] {
            if implied && chosen.contains(name) {
                self.auto_modules.insert(name.to_string());
            }
        }
        self.kit = self.chosen_kit();
        self.refresh_secrets();
    }

    // ------------------------------------------------------------ model

    pub fn apply_kit(&mut self) {
        let wanted = self.kits[self.kit].modules.clone();
        if wanted.iter().any(|m| m == CUSTOM) {
            return; // Custom is a description of the list, not a list.
        }
        self.auto_modules.clear();
        for m in &mut self.modules {
            if !m.locked {
                m.chosen = wanted.contains(&m.name);
            }
        }
        self.refresh_secrets();
    }

    /// Which kit the ticked modules amount to, ignoring the foundation that
    /// every install has: the honest answer after a reload, a relaunch, or a
    /// visit to the Modules screen. Custom when it matches none.
    pub fn chosen_kit(&self) -> usize {
        let mine: std::collections::BTreeSet<&str> = self.modules.iter().filter(|m| m.chosen && !m.locked).map(|m| m.name.as_str()).collect();
        let auto: std::collections::BTreeSet<&str> = self.auto_modules.iter().map(|s| s.as_str()).collect();
        kit_for(&mine, &auto, &self.kits)
    }

    pub fn set_kit(&mut self, i: usize) {
        if i < self.kits.len() {
            self.kit = i;
            self.apply_kit();
            let (_, verdict, _) = self.memory();
            let name = self.kits[i].name;
            self.say(format!("{name}: {verdict}"), false);
        }
    }

    pub fn chosen(&self) -> Vec<String> {
        self.modules.iter().filter(|m| m.chosen).map(|m| m.name.clone()).collect()
    }

    /// The closure the plan will use, so later screens show what the install
    /// will really carry.
    pub fn closed(&self) -> Vec<String> {
        self.schema.close_over_requires(&self.chosen()).map(|(m, _)| m).unwrap_or_else(|_| self.chosen())
    }

    pub fn values_json(&self) -> BTreeMap<String, serde_json::Value> {
        let mut v: BTreeMap<String, serde_json::Value> = self.values.iter().filter(|(_, v)| !v.trim().is_empty()).map(|(k, v)| (k.clone(), parse_value(v))).collect();
        for f in &self.domain {
            if !f.value.trim().is_empty() {
                v.insert(f.key.clone(), serde_json::Value::String(f.value.trim().to_string()));
            }
        }
        let admin = self.field("adminUser");
        if !admin.is_empty() {
            v.insert("homelab.adminUser".into(), serde_json::Value::String(admin));
        }
        v
    }

    fn values_text(&self) -> BTreeMap<String, String> {
        self.values_json().into_iter().map(|(k, v)| (k, value_text(&v))).collect()
    }

    /// The supply secrets the chosen modules need, by the same rule generate
    /// applies (a nullable secret behind an off switch is not asked), each as
    /// a form of the variables its file must carry.
    pub fn refresh_secrets(&mut self) {
        let closed = self.closed();
        let values = self.values_json();
        let text = self.values_text();
        let mut wanted: Vec<(String, Vec<String>)> = Vec::new();
        for m in &closed {
            if let Some(meta) = self.schema.catalog.get(m) {
                for s in &meta.secrets {
                    if s.source == Source::Supply && s.option.starts_with("homelab.") && !crate::plan::skip_secret(self.schema, &closed, &values, s) && !wanted.iter().any(|(o, _)| o == &s.option) {
                        wanted.push((s.option.clone(), s.keys.clone()));
                    }
                }
            }
        }
        let mut next: Vec<SecretRow> = Vec::new();
        for (option, keys) in wanted {
            let guide = crate::guides::for_option(&option, &text);
            let spec = crate::guides::fields(&option, &text);
            let mut fields: Vec<Field> = Vec::new();
            let mut optional: Vec<bool> = Vec::new();
            for f in &spec {
                let mut fld = Field::new(&f.var, &f.label, &f.help, "");
                fld.masked = f.masked;
                fld.choices = f.choices.clone();
                fld.other_ok = f.choices.is_empty();
                // An option field answers a homelab.* option: show what is set.
                if f.is_option {
                    fld.value = self.values.get(&f.var).cloned().unwrap_or_default();
                }
                fields.push(fld);
                optional.push(f.optional);
            }
            let option_fields: Vec<String> = spec.iter().filter(|f| f.is_option).map(|f| f.var.clone()).collect();
            // Carry over what was typed for a variable that still exists.
            let old = self.secrets.iter_mut().find(|r| r.option == option);
            let (mut saved, mut verified, mut skipped, mut path) = (None, None, false, String::new());
            if let Some(o) = old {
                for f in &mut fields {
                    if let Some(prev) = o.fields.iter().find(|p| p.key == f.key) {
                        if !f.key.starts_with("homelab.") {
                            f.value = prev.value.clone();
                        }
                    }
                }
                saved = o.saved.take();
                verified = o.verified.take();
                skipped = o.skipped;
                path = std::mem::take(&mut o.path);
            }
            next.push(SecretRow {
                option_fields,
                title: guide.as_ref().map(|g| g.title.to_string()).unwrap_or_else(|| option.clone()),
                steps: guide.as_ref().map(|g| g.steps.to_string()).unwrap_or_else(|| format!("The file must carry: {}", keys.join(", "))),
                path_only: fields.is_empty(),
                option,
                fields,
                optional,
                path,
                saved,
                verified,
                skipped,
            });
        }
        // The Cloudflare token first: it is the one everybody needs.
        next.sort_by_key(|r| if r.option == "homelab.acme.credentialsFile" { 0 } else { 1 });
        self.secrets = next;
    }

    /// Required values the kit leaves open beyond domain, email and admin.
    pub fn open_values(&self) -> Vec<(String, String, String)> {
        let mods = self.closed();
        // Anything a credential form already asks for — the file itself and
        // the options that form owns, like the VPN provider — is not asked
        // again here.
        let mut secret_opts: Vec<String> = self.secrets.iter().map(|r| r.option.clone()).collect();
        for r in &self.secrets {
            secret_opts.extend(r.option_fields.iter().cloned());
        }
        let fixed = ["homelab.domain", "homelab.acme.email", "homelab.adminUser"];
        let mut rows: Vec<(String, String, String)> = self
            .schema
            .options_for_modules(&mods)
            .into_iter()
            .filter(|o| o.required() && !o.name.ends_with("File") && !secret_opts.contains(&o.name) && !fixed.contains(&o.name.as_str()))
            .map(|o| (o.name.clone(), self.values.get(&o.name).cloned().unwrap_or_default(), o.description.clone().unwrap_or_default()))
            .collect();
        rows.sort();
        rows
    }

    pub fn memory(&self) -> (u64, String, bool) {
        let need = crate::plan::memory_need(self.schema, &self.closed());
        let short = self.ram_mib != 0 && need > self.ram_mib;
        (need, crate::plan::memory_verdict(need, self.ram_mib), short)
    }

    pub fn system_disk(&self) -> Option<String> {
        if self.disks.is_empty() {
            let v = self.disk_fallback.value.trim();
            return if v.is_empty() { None } else { Some(v.to_string()) };
        }
        self.disks.iter().zip(&self.roles).find(|(_, r)| **r == Role::System).map(|(d, _)| d.id.display().to_string())
    }

    pub fn data_disks(&self) -> Vec<serde_json::Value> {
        let mut out = Vec::new();
        let mut n = 0;
        for (d, r) in self.disks.iter().zip(&self.roles) {
            match r {
                Role::Data => {
                    n += 1;
                    out.push(json!({ "name": format!("d{n}"), "device": d.id.display().to_string() }));
                }
                Role::Parity => out.push(json!({ "name": "parity", "device": d.id.display().to_string() })),
                _ => {}
            }
        }
        out
    }

    pub fn erased(&self) -> Vec<String> {
        std::iter::once(self.system_disk().unwrap_or_default())
            .chain(self.data_disks().into_iter().map(|d| d["device"].as_str().unwrap_or_default().to_string()))
            .filter(|d| !d.is_empty())
            .collect()
    }

    pub fn field(&self, key: &str) -> String {
        self.profile.iter().find(|f| f.key == key).map(|f| f.value.trim().to_string()).unwrap_or_default()
    }

    pub fn answers(&self) -> Answers {
        let typed = self.field("password");
        let admin_hash = if typed.is_empty() { self.admin_hash.clone() } else { crate::emit::mkpasswd(&typed).ok() };
        let json = json!({
            "host": {
                "name": self.field("name"),
                "timeZone": self.field("timeZone"),
                "disk": self.system_disk().unwrap_or_default(),
                "dataDisks": self.data_disks(),
                "sshAuthorizedKeys": self.keys,
                "adminPasswordHash": admin_hash,
            },
            "modules": self.chosen(),
            "values": self.values_json(),
        });
        serde_json::from_value(json).expect("answers shape")
    }

    // ------------------------------------------------------------ edits

    pub fn cycle_disk(&mut self, i: usize) {
        if i >= self.disks.len() {
            return;
        }
        let taken = self.roles.iter().enumerate().any(|(j, r)| j != i && *r == Role::System);
        self.set_disk_role(i, self.roles[i].next(taken));
    }

    pub fn set_disk_role(&mut self, i: usize, r: Role) {
        if i >= self.disks.len() {
            return;
        }
        let mut moved_from = None;
        if r == Role::System {
            for (j, other) in self.roles.iter_mut().enumerate() {
                if j != i && *other == Role::System {
                    *other = Role::Unused;
                    moved_from = Some(j);
                }
            }
        }
        self.roles[i] = r;
        let msg = match moved_from {
            Some(j) => format!("{} is the system disk now; {} is no longer used", self.disks[i].kernel, self.disks[j].kernel),
            None => format!("{}: {}", self.disks[i].kernel, r.help()),
        };
        self.say(msg, false);
    }

    /// The system disk typed by hand (no /dev/disk/by-id on this machine).
    pub fn set_disk_path(&mut self, text: &str) {
        self.disk_fallback.value = text.trim().to_string();
    }

    pub fn set_profile(&mut self, key: &str, text: &str) -> Result<(), String> {
        let text = text.trim().to_string();
        if key == "name" && !text.is_empty() && !valid_hostname(&text) {
            return Err("a machine name is letters, digits and dashes, starting with a letter or digit".into());
        }
        if key == "adminUser" && !text.is_empty() && !valid_username(&text) {
            return Err("a username is lowercase letters, digits, dashes and underscores, starting with a letter".into());
        }
        if key == "timeZone" && !text.is_empty() && Path::new("/etc/zoneinfo").exists() && !Path::new("/etc/zoneinfo").join(&text).exists() {
            return Err(format!("no time zone named {text}: try Region/City, e.g. America/Chicago"));
        }
        if let Some(f) = self.profile.iter_mut().find(|f| f.key == key) {
            f.value = text;
            Ok(())
        } else {
            Err(format!("no field {key}"))
        }
    }

    pub fn import_github_keys(&mut self, user: &str) -> Result<String, String> {
        let user = user.trim().to_string();
        self.github_user.value = user.clone();
        if user.is_empty() {
            return Ok(String::new());
        }
        match fetch_github_keys(&user) {
            Ok(keys) if keys.is_empty() => Err(format!("github.com/{user} has no public keys")),
            Ok(keys) => {
                let mut added = 0;
                for k in keys {
                    if !self.keys.iter().any(|e| same_key(e, &k)) {
                        self.keys.push(k);
                        added += 1;
                    }
                }
                Ok(format!("{added} key(s) added from github.com/{user}; {} in the list", self.keys.len()))
            }
            Err(e) => Err(format!("could not fetch keys: {e}")),
        }
    }

    pub fn add_key(&mut self, text: &str) -> Result<String, String> {
        let text = text.trim();
        if text.is_empty() {
            return Ok(String::new());
        }
        let k = validate_key(text)?;
        if self.keys.iter().any(|e| same_key(e, &k)) {
            return Err("that key is already in the list".into());
        }
        self.keys.push(k);
        self.pasted_key.value.clear();
        Ok(format!("key added; {} in the list", self.keys.len()))
    }

    pub fn remove_key(&mut self, i: usize) {
        if i < self.keys.len() {
            self.keys.remove(i);
            self.say("key removed", false);
        }
    }

    pub fn set_domain(&mut self, key: &str, text: &str) -> Result<(), String> {
        let text = text.trim().to_string();
        if key == "homelab.domain" && !text.is_empty() && !valid_domain(&text) {
            return Err("a domain looks like example.com: lowercase labels joined by dots, nothing in front, no path".into());
        }
        if key == "homelab.acme.email" && !text.is_empty() && !valid_email(&text) {
            return Err("an email address looks like you@example.com".into());
        }
        match self.domain.iter_mut().find(|f| f.key == key) {
            Some(f) => {
                f.value = text;
                self.refresh_secrets();
                Ok(())
            }
            None => Err(format!("no field {key}")),
        }
    }

    pub fn set_secret_field(&mut self, option: &str, var: &str, text: &str) -> Result<(), String> {
        let row = self.secrets.iter_mut().find(|r| r.option == option).ok_or("no such secret")?;
        if var == "__path" {
            row.path = text.trim().to_string();
            return Ok(());
        }
        let f = row.fields.iter_mut().find(|f| f.key == var).ok_or("no such field")?;
        f.value = text.trim().to_string();
        // A homelab.* answer belongs in the values, and the rest of the form
        // follows it (a provider decides which lines its file needs).
        if var.starts_with("homelab.") {
            let v = f.value.clone();
            self.set_value(var, &v);
        }
        Ok(())
    }

    /// Write the form to a 600 file next to the answers, and check it where
    /// an API allows.
    pub fn save_secret(&mut self, option: &str) -> Result<String, String> {
        let text_values = self.values_text();
        let idx = self.secrets.iter().position(|r| r.option == option).ok_or("no such secret")?;
        let (content, missing): (String, Vec<String>) = {
            let row = &self.secrets[idx];
            if row.path_only {
                let p = row.path.trim();
                if p.is_empty() {
                    return Err("give the path of the file".into());
                }
                if !Path::new(p).exists() {
                    return Err(format!("{p}: no such file on this machine"));
                }
                (String::new(), vec![])
            } else {
                let missing: Vec<String> = row
                    .fields
                    .iter()
                    .zip(&row.optional)
                    .filter(|(f, opt)| f.value.trim().is_empty() && !**opt)
                    .map(|(f, _)| f.label.clone())
                    .collect();
                let pairs: Vec<(String, String)> = row.fields.iter().filter(|f| !row.option_fields.contains(&f.key)).map(|f| (f.key.clone(), f.value.clone())).collect();
                (crate::guides::compose(&pairs), missing)
            }
        };
        if !missing.is_empty() {
            return Err(format!("still empty: {}", missing.join(", ")));
        }
        if self.secrets[idx].path_only {
            let p = self.secrets[idx].path.clone();
            let row = &mut self.secrets[idx];
            row.saved = Some(PathBuf::from(&p));
            row.skipped = false;
            row.verified = None;
            return Ok(format!("using {p}"));
        }
        let dir = self.out_answers.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| PathBuf::from(".")).join(".secrets");
        let file = dir.join(crate::secrets::secret_name(option));
        std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
        let _ = std::fs::remove_file(&file);
        crate::secrets::write_private(&file, &content).map_err(|e| e.to_string())?;
        let verified = crate::guides::verify(option, &content, &text_values);
        let row = &mut self.secrets[idx];
        row.saved = Some(file.clone());
        row.skipped = false;
        row.verified = verified;
        self.save_session();
        let row = &self.secrets[idx];
        match &row.verified {
            Some(Ok(m)) => Ok(m.clone()),
            Some(Err(m)) => Err(format!("saved, but NOT verified: {m}")),
            None => Ok(format!("saved to {} (mode 600)", file.display())),
        }
    }

    /// Leave a secret for later: the install runs, the services that need it
    /// do not work until it is set and the machine rebuilt.
    pub fn skip_secret(&mut self, option: &str) -> Result<String, String> {
        let row = self.secrets.iter_mut().find(|r| r.option == option).ok_or("no such secret")?;
        row.skipped = true;
        row.saved = None;
        row.verified = None;
        self.save_session();
        let what = match option {
            "homelab.acme.credentialsFile" => "no certificates will be issued: every address will warn, or not answer at all, until you set the token on the installed machine (/root/homelab) and rebuild",
            "homelab.arrStack.vpnEnvFile" => "the download client stays off until the VPN account is set",
            "homelab.backup.remote.environmentFile" => "there will be no offsite backup copy, only the local one",
            _ => "the services that read it will not start until it is set",
        };
        Ok(format!("skipped — {what}"))
    }

    pub fn set_value(&mut self, name: &str, text: &str) {
        let text = text.trim().to_string();
        if text.is_empty() {
            self.values.remove(name);
        } else {
            self.values.insert(name.to_string(), text);
        }
        self.refresh_secrets();
    }

    pub fn toggle_module(&mut self, name: &str) -> Result<(), String> {
        let m = self.modules.iter_mut().find(|m| m.name == name).ok_or("no such module")?;
        if m.locked {
            return Err("part of the foundation: always on".into());
        }
        m.chosen = !m.chosen;
        self.auto_modules.remove(name);   // a hand on it makes it a choice
        self.refresh_secrets();
        let (_, verdict, _) = self.memory();
        self.say(verdict, false);
        Ok(())
    }

    // ------------------------------------------------------------ flow

    /// The screen's checks. `Err(soft)` is a warning the next call accepts.
    pub fn advance(&mut self) -> Result<(), Blocked> {
        let soft = |w: &mut Wizard, msg: &str| -> Result<(), Blocked> {
            if w.warned {
                w.warned = false;
                Ok(())
            } else {
                w.warned = true;
                Err(Blocked { message: format!("{msg} (Continue again to accept)"), soft: true })
            }
        };
        let hard = |msg: &str| Blocked { message: msg.to_string(), soft: false };

        match self.step {
            Step::Welcome => {
                let (address, internet) = self.network();
                if address.is_empty() {
                    return Err(hard("no network address yet: plug in a network cable (wired is automatic); this screen keeps checking every few seconds"));
                }
                if internet == Some(false) {
                    soft(self, "the internet is not reachable from here, and the install downloads a few gigabytes")?;
                }
            }
            Step::Kit => {}
            Step::Storage => {
                if self.system_disk().is_none() {
                    return Err(hard("choose the disk the system goes on"));
                }
                let parity = self.roles.iter().any(|r| *r == Role::Parity);
                let data = self.roles.iter().any(|r| *r == Role::Data);
                if parity && !data {
                    return Err(hard("a parity disk protects data disks; mark at least one disk as data, or set the parity disk to data"));
                }
                for (want, when) in [("mergerfs-pools", data), ("snapraid", parity)] {
                    if when {
                        if let Some(m) = self.modules.iter_mut().find(|m| m.name == want) {
                            if !m.chosen {
                                self.auto_modules.insert(want.to_string());
                            }
                            m.chosen = true;
                        }
                    }
                }
                self.refresh_secrets();
            }
            Step::Profile => {
                if self.field("name").is_empty() {
                    return Err(hard("the machine needs a name"));
                }
                if self.field("adminUser").is_empty() {
                    return Err(hard("the admin needs a username"));
                }
                if self.field("password") != self.field("password2") {
                    return Err(hard("the two passwords differ"));
                }
                if self.field("password").is_empty() && self.admin_hash.is_none() {
                    soft(self, "no password typed: one will be minted and written to FIRST-LOGIN.md on the new system")?;
                }
            }
            Step::Ssh => {
                if self.keys.is_empty() {
                    soft(self, "no SSH key: you will only be able to log in at the machine's own screen")?;
                }
            }
            Step::Domain => {
                if self.closed().iter().any(|m| m == "acme") {
                    if self.domain.iter().any(|f| f.value.trim().is_empty()) {
                        return Err(hard("the domain and the email are both needed"));
                    }
                    if let Some(r) = self.secrets.iter().find(|r| !r.filled()) {
                        return Err(hard(&format!("{} is not set: fill it in, or press Skip on it", r.short())));
                    }
                    if self.secrets.iter().any(|r| matches!(r.verified, Some(Err(_)))) {
                        soft(self, "a value did not verify (the red line); fix it, or go on anyway")?;
                    }
                }
            }
            Step::Extras => {
                if let Some((name, _, _)) = self.open_values().iter().find(|(_, v, _)| v.trim().is_empty()).cloned() {
                    return Err(hard(&format!("{name} has no default and needs a value")));
                }
            }
            Step::Review => {
                self.start_install().map_err(|e| hard(&e))?;
                return Ok(());
            }
            Step::Install => return Ok(()),
            Step::Done => return Ok(()),
        }
        let i = self.step.index();
        self.step = STEPS[(i + 1).min(STEPS.len() - 1)];
        self.warned = false;
        self.status = None;
        Ok(())
    }

    pub fn back(&mut self) {
        if matches!(self.step, Step::Welcome | Step::Install | Step::Done) {
            return;
        }
        self.step = STEPS[self.step.index().saturating_sub(1)];
        self.warned = false;
        self.status = None;
    }

    // ------------------------------------------------------------ install

    /// Write the answers, then run generate and (on the live USB) install as
    /// child processes, streaming their output into the Install screen.
    pub fn start_install(&mut self) -> Result<(), String> {
        let answers = self.answers();
        std::fs::write(&self.out_answers, serde_json::to_string_pretty(&answers).map_err(|e| e.to_string())? + "\n").map_err(|e| format!("{}: {e}", self.out_answers.display()))?;
        let exe = std::env::current_exe().map_err(|e| e.to_string())?;
        let mut gen: Vec<String> = self.exe_prefix.clone();
        gen.extend(["generate".to_string(), "--answers".into(), self.out_answers.display().to_string(), "--out".into(), self.out_dir.display().to_string()]);
        for r in &self.secrets {
            if let Some(f) = &r.saved {
                gen.push("--secret".into());
                gen.push(format!("{}=@{}", r.option, f.display()));
            }
        }
        let install: Option<Vec<String>> = if self.live_usb {
            let mut v = vec![exe.display().to_string()];
            v.extend(self.exe_prefix.clone());
            v.extend(["install".to_string(), self.out_dir.display().to_string(), "--yes".into()]);
            Some(v)
        } else {
            None
        };
        let progress = Arc::new(Progress { lines: Mutex::new(Vec::new()), done: AtomicBool::new(false), exit: AtomicI32::new(0) });
        let p = progress.clone();
        std::thread::spawn(move || {
            let push = |p: &Progress, s: String| p.lines.lock().unwrap().push(s);
            push(&p, "== generate: writing and checking the configuration".into());
            let code = stream(&p, Command::new(&exe).args(&gen));
            if code != 0 {
                push(&p, format!("generate failed (exit {code}); the lines above say what is missing"));
                p.exit.store(code, Ordering::SeqCst);
                p.done.store(true, Ordering::SeqCst);
                return;
            }
            if let Some(inst) = install {
                push(&p, "== install: erasing the marked disks, then downloading the system onto the new one".into());
                let mut c = Command::new("sudo");
                c.arg("-n").args(&inst);
                let code = stream(&p, &mut c);
                p.exit.store(code, Ordering::SeqCst);
            }
            p.done.store(true, Ordering::SeqCst);
        });
        self.progress = Some(progress);
        self.step = Step::Install;
        self.status = None;
        Ok(())
    }

    /// Move to Done when the child has finished. Called from both front ends.
    pub fn poll_install(&mut self) {
        let Some(p) = &self.progress else { return };
        if p.done.load(Ordering::SeqCst) {
            let code = p.exit.load(Ordering::SeqCst);
            self.install_report = p.lines.lock().unwrap().clone();
            self.step = Step::Done;
            self.progress = None;
            if code != 0 {
                self.say(format!("it stopped with exit {code}; the lines above say where"), true);
            }
        }
    }

    pub fn progress_lines(&self) -> Vec<String> {
        match &self.progress {
            Some(p) => p.lines.lock().unwrap().clone(),
            None => self.install_report.clone(),
        }
    }

    /// The tail a person wants on the Done screen: the install's own report
    /// when it worked, the last lines when it did not.
    pub fn done_tail(&self, screenful: usize) -> Vec<String> {
        let failed = self.status.as_ref().map(|(_, e)| *e).unwrap_or(false);
        let start = if failed {
            self.install_report.len().saturating_sub(screenful)
        } else {
            self.install_report
                .iter()
                .rposition(|l| l.starts_with("installed ") || l.starts_with("wrote "))
                .unwrap_or(self.install_report.len().saturating_sub(screenful))
        };
        self.install_report[start..].to_vec()
    }

    // ------------------------------------------------------------ api

    /// Everything a front end needs to draw the current screen.
    pub fn state_json(&self) -> serde_json::Value {
        let (address, internet) = self.network();
        let (need, verdict, short) = self.memory();
        // A password that came back as a hash has no text, but it is set: the
        // box shows dots rather than looking empty. ONLY that one — a masked
        // credential box must look empty until its own value is there, or a
        // skipped Cloudflare token appears to be filled in.
        let kept_password = self.admin_hash.is_some();
        let field = |f: &Field| json!({
            "key": f.key, "label": f.label, "help": f.help,
            "value": if f.masked { String::new() } else { f.value.clone() },
            "set": !f.value.is_empty() || (kept_password && (f.key == "password" || f.key == "password2")),
            "masked": f.masked, "other_ok": f.other_ok,
            "choices": f.choices.iter().map(|(v, l)| json!({ "value": v, "label": l })).collect::<Vec<_>>(),
        });
        json!({
            "step": self.step.id(),
            "title": self.step.title(),
            "intro": self.step.intro(self.live_usb),
            "index": self.step.index().min(QUESTION_STEPS - 1) + 1,
            "of": QUESTION_STEPS,
            "status": self.status.as_ref().map(|(m, e)| json!({ "message": m, "error": e })),
            "machine": {
                "ram_mib": self.ram_mib,
                "address": address,
                "internet": internet,
                "live_usb": self.live_usb,
                "disks": self.disks.len(),
            },
            "memory": { "need_mib": need, "verdict": verdict, "short": short },
            "kit": self.chosen_kit(),
            "kits": self.kits.iter().enumerate().map(|(i, k)| {
                let closed = self.schema.close_over_requires(&k.modules).map(|(m, _)| m).unwrap_or_else(|_| k.modules.clone());
                let n = crate::plan::memory_need(self.schema, &closed);
                let custom = k.modules.iter().any(|m| m == CUSTOM);
                let need = if custom { crate::plan::memory_need(self.schema, &self.closed()) } else { n };
                json!({
                    "name": k.name, "blurb": k.blurb,
                    "need_gb": (need + 511) / 1024,
                    "fits": self.ram_mib == 0 || need <= self.ram_mib,
                    "chosen": i == self.chosen_kit(),
                    "modules": if custom { self.closed() } else { k.modules.clone() },
                })
            }).collect::<Vec<_>>(),
            "disks": self.disks.iter().zip(&self.roles).map(|(d, r)| json!({
                "id": d.id.display().to_string(), "kernel": d.kernel, "size": d.size_human(),
                "model": d.model, "transport": d.transport, "role": r.id(), "role_label": r.label(),
            })).collect::<Vec<_>>(),
            "disk_fallback": field(&self.disk_fallback),
            "profile": self.profile.iter().map(&field).collect::<Vec<_>>(),
            "ssh": { "github_user": self.github_user.value, "keys": self.keys.iter().map(|k| json!({ "summary": key_summary(k), "full": k })).collect::<Vec<_>>() },
            "domain": self.domain.iter().map(&field).collect::<Vec<_>>(),
            "secrets": self.secrets.iter().map(|r| json!({
                "option": r.option, "short": r.short(), "title": r.title, "steps": r.steps,
                "path_only": r.path_only, "path": r.path,
                "fields": r.fields.iter().zip(&r.optional).map(|(f, opt)| {
                    let mut v = field(f);
                    v["optional"] = json!(opt);
                    v["is_option"] = json!(r.option_fields.contains(&f.key));
                    v
                }).collect::<Vec<_>>(),
                "state": r.state(), "filled": r.filled(), "skipped": r.skipped,
                "verified": r.verified.as_ref().map(|v| match v { Ok(m) => json!({ "ok": true, "message": m }), Err(m) => json!({ "ok": false, "message": m }) }),
            })).collect::<Vec<_>>(),
            "values": self.open_values().iter().map(|(n, v, d)| json!({ "name": n, "value": v, "description": d })).collect::<Vec<_>>(),
            "modules": self.modules.iter().map(|m| json!({ "name": m.name, "description": m.description, "chosen": m.chosen, "locked": m.locked, "memory": m.memory })).collect::<Vec<_>>(),
            "review": {
                "host": self.field("name"), "time_zone": self.field("timeZone"), "admin": self.field("adminUser"),
                "minted_password": self.field("password").is_empty() && self.admin_hash.is_none(),
                "kit": self.kits[self.kit].name, "modules": self.closed(),
                "keys": self.keys.iter().map(|k| key_summary(k)).collect::<Vec<_>>(),
                "domain": self.domain.iter().find(|f| f.key == "homelab.domain").map(|f| f.value.clone()).unwrap_or_default(),
                "erased": self.erased(),
                "skipped_secrets": self.secrets.iter().filter(|r| r.skipped).map(|r| r.short()).collect::<Vec<_>>(),
                "out_dir": self.out_dir.display().to_string(),
            },
            "install": { "lines": self.progress_lines(), "running": self.progress.is_some() },
            "done": { "report": self.done_tail(24) },
            "web": { "port": self.web_port, "code": self.pairing, "seen": self.web_seen },
            "version": Self::version(),
            "update": self.update.as_ref().map(|(sha, newer)| json!({ "newest": sha, "newer": newer, "restarting": self.relaunch.is_some() })),
        })
    }
}

// ---------------------------------------------------------------- helpers

/// Run a command, pushing each output line to the progress; the exit code.
fn stream(p: &Progress, cmd: &mut Command) -> i32 {
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped()).stdin(Stdio::null());
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => {
            p.lines.lock().unwrap().push(format!("could not start {:?}: {e}", cmd.get_program()));
            return 1;
        }
    };
    let out = child.stdout.take();
    let err = child.stderr.take();
    std::thread::scope(|s| {
        if let Some(r) = out {
            s.spawn(|| {
                for line in io::BufReader::new(r).lines().map_while(Result::ok) {
                    p.lines.lock().unwrap().push(line);
                }
            });
        }
        if let Some(e) = err {
            s.spawn(|| {
                for line in io::BufReader::new(e).lines().map_while(Result::ok) {
                    p.lines.lock().unwrap().push(line);
                }
            });
        }
    });
    child.wait().map(|s| s.code().unwrap_or(1)).unwrap_or(1)
}

/// `type base64 [comment]` with a plausible base64 blob; the normalised key.
pub fn validate_key(text: &str) -> Result<String, String> {
    let mut parts = text.split_whitespace();
    let kind = parts.next().unwrap_or("");
    let blob = parts.next().unwrap_or("");
    let comment: Vec<&str> = parts.collect();
    let kinds = ["ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com"];
    if !kinds.contains(&kind) {
        return Err(format!("a public key starts with one of {}; this starts with `{kind}`", kinds[..3].join(", ")));
    }
    if blob.len() < 40 || !blob.chars().all(|c| c.is_ascii_alphanumeric() || c == '+' || c == '/' || c == '=') || blob.len() % 4 != 0 {
        return Err("the part after the key type should be one long base64 string (copy the whole line from the .pub file)".into());
    }
    let mut out = format!("{kind} {blob}");
    if !comment.is_empty() {
        out.push(' ');
        out.push_str(&comment.join(" "));
    }
    Ok(out)
}

/// Two keys are the same when type and blob match (comments differ freely).
pub fn same_key(a: &str, b: &str) -> bool {
    let core = |s: &str| s.split_whitespace().take(2).collect::<Vec<_>>().join(" ");
    core(a) == core(b)
}

/// `ssh-ed25519 …Abcd (comment)` — the key as a person recognises it.
pub fn key_summary(k: &str) -> String {
    let mut parts = k.split_whitespace();
    let kind = parts.next().unwrap_or("");
    let blob = parts.next().unwrap_or("");
    let comment: Vec<&str> = parts.collect();
    let tail = if blob.len() > 12 { &blob[blob.len() - 12..] } else { blob };
    format!("{kind} ...{tail}{}", if comment.is_empty() { String::new() } else { format!("  ({})", comment.join(" ")) })
}

pub fn valid_domain(d: &str) -> bool {
    let labels: Vec<&str> = d.split('.').collect();
    labels.len() >= 2
        && labels.iter().all(|l| !l.is_empty() && l.len() <= 63 && l.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-') && !l.starts_with('-') && !l.ends_with('-'))
        && labels.last().map(|t| t.chars().all(|c| c.is_ascii_alphabetic()) && t.len() >= 2).unwrap_or(false)
}

pub fn valid_email(e: &str) -> bool {
    e.contains('@') && e.rsplit('@').next().map(|d| d.contains('.')).unwrap_or(false) && !e.contains(' ')
}

pub fn valid_hostname(h: &str) -> bool {
    h.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') && h.chars().next().map(|c| c.is_ascii_alphanumeric()).unwrap_or(false)
}

pub fn valid_username(u: &str) -> bool {
    u.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_') && u.chars().next().map(|c| c.is_ascii_lowercase()).unwrap_or(false)
}

pub fn fetch_github_keys(user: &str) -> Result<Vec<String>> {
    if user.is_empty() || !user.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
        anyhow::bail!("not a GitHub username");
    }
    let out = Command::new("curl")
        .args(["-fsSL", "--max-time", "15", &format!("https://github.com/{user}.keys")])
        .output()
        .context("running curl")?;
    if !out.status.success() {
        anyhow::bail!("github.com/{user}.keys: {}", String::from_utf8_lossy(&out.stderr).trim());
    }
    Ok(String::from_utf8_lossy(&out.stdout)
        .lines()
        .map(str::trim)
        .filter(|l| l.starts_with("ssh-") || l.starts_with("ecdsa-") || l.starts_with("sk-"))
        .map(|l| format!("{l} {user}@github"))
        .collect())
}

/// The machine's address toward the internet, and whether the binary cache
/// answers.
pub fn probe_network() -> (String, Option<bool>) {
    let address = Command::new("ip")
        .args(["-4", "route", "get", "1.1.1.1"])
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).to_string())
        .and_then(|t| t.split_whitespace().skip_while(|w| *w != "src").nth(1).map(String::from))
        .unwrap_or_default();
    if address.is_empty() {
        return (address, None);
    }
    let ok = Command::new("curl").args(["-fsS", "--max-time", "5", "-o", "/dev/null", "https://cache.nixos.org/nix-cache-info"]).status().map(|s| s.success()).unwrap_or(false);
    (address, Some(ok))
}

pub fn value_text(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// A typed value: JSON when it parses (lists, objects, numbers, booleans), a
/// string otherwise.
pub fn parse_value(text: &str) -> serde_json::Value {
    let t = text.trim();
    if t.starts_with('[') || t.starts_with('{') || t == "true" || t == "false" || t == "null" || t.parse::<f64>().is_ok() {
        serde_json::from_str(t).unwrap_or_else(|_| serde_json::Value::String(t.to_string()))
    } else {
        serde_json::Value::String(t.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The bug from the first hardware run: Starter was picked, a data disk
    /// marked, and step 2 then read "Custom" — because marking the disk had
    /// ticked mergerfs-pools.
    #[test]
    fn a_disk_added_module_does_not_make_the_kit_custom() {
        let kits = kits();
        let starter = kits.iter().position(|k| k.name == "Starter").expect("a Starter kit");
        let mut mine: std::collections::BTreeSet<&str> = kits[starter].modules.iter().map(|s| s.as_str()).collect();
        let none = Default::default();
        assert_eq!(kit_for(&mine, &none, &kits), starter);

        // The storage step adds mergerfs-pools for the data disk.
        mine.insert("mergerfs-pools");
        assert_ne!(kit_for(&mine, &none, &kits), starter, "without the auto set it reads Custom — the bug");
        let auto: std::collections::BTreeSet<&str> = ["mergerfs-pools"].into_iter().collect();
        assert_eq!(kit_for(&mine, &auto, &kits), starter, "discounting it reads Starter again");

        // A module the reader ticks themselves is still a different list.
        assert!(!mine.contains("forgejo"));
        mine.insert("forgejo");
        assert_ne!(kit_for(&mine, &auto, &kits), starter);
    }

    /// A wizard over a small catalogue holding the Starter modules plus the
    /// two the storage step adds.
    fn test_wizard() -> Wizard {
        let catalog: String = {
            let mut m = serde_json::Map::new();
            for name in ["acme", "nginx-access", "jellyfin", "tandoor", "backup", "monitoring", "ntfy", "alertmanager-ntfy", "mergerfs-pools", "snapraid"] {
                m.insert(
                    name.to_string(),
                    json!({"description": name, "enable": "import", "options": [], "requires": [], "vhosts": [], "secrets": []}),
                );
            }
            serde_json::Value::Object(m).to_string()
        };
        let schema = Box::leak(Box::new(Schema::parse(&catalog, "[]").expect("a schema")));
        let dir = std::env::temp_dir().join(format!("hl-kit-test-{}", std::process::id()));
        Wizard::new(schema, &dir.join("answers.json"), &dir, vec![], 0)
    }

    /// Two disks the roles can be set on, so the storage step has something
    /// to act on without a real /dev/disk/by-id.
    fn give_it_disks(w: &mut Wizard) {
        w.disks = vec![
            crate::disks::Disk { id: "/dev/disk/by-id/a".into(), kernel: "sda".into(), size_bytes: 1 << 40, model: "a".into(), transport: "sata".into(), in_use: false },
            crate::disks::Disk { id: "/dev/disk/by-id/b".into(), kernel: "sdb".into(), size_bytes: 1 << 40, model: "b".into(), transport: "sata".into(), in_use: false },
        ];
        w.roles = vec![Role::Unused, Role::Unused];
    }

    /// The whole path a reader walks: pick Starter, mark a system disk and a
    /// data disk, Continue. Step 2 must still read Starter.
    #[test]
    fn starter_survives_the_storage_step() {
        let mut w = test_wizard();
        let starter = w.kits.iter().position(|k| k.name == "Starter").expect("a Starter kit");
        w.set_kit(starter);
        assert_eq!(w.chosen_kit(), starter, "picking it reads back");

        give_it_disks(&mut w);
        w.set_disk_role(0, Role::System);
        w.set_disk_role(1, Role::Data);
        w.step = Step::Storage;
        assert!(w.advance().is_ok(), "the storage step accepts a system and a data disk");

        assert!(w.modules.iter().any(|m| m.name == "mergerfs-pools" && m.chosen), "the data disk brought the pool in");
        assert_eq!(w.chosen_kit(), starter, "and the kit still reads Starter, not Custom");
    }

    /// The in-place updater relaunches the installer and reads the answers
    /// back. The answers record the modules and the disks but not why a
    /// module is on, so the kit has to be inferred from the disks again.
    #[test]
    fn starter_survives_a_relaunch() {
        let mut first = test_wizard();
        let starter = first.kits.iter().position(|k| k.name == "Starter").expect("a Starter kit");
        first.set_kit(starter);
        give_it_disks(&mut first);
        first.set_disk_role(0, Role::System);
        first.set_disk_role(1, Role::Data);
        first.step = Step::Storage;
        assert!(first.advance().is_ok(), "the storage step accepts it");
        let saved = first.answers();
        assert!(saved.modules.iter().any(|m| m == "mergerfs-pools"), "the answers carry the pool");

        let mut again = test_wizard();
        give_it_disks(&mut again);
        again.load(&saved);
        assert_eq!(again.chosen_kit(), starter, "the relaunched installer still reads Starter");
        assert_eq!(again.kit, starter, "and step 2 shows it");
    }

    /// And a kit that names those modules itself must still match when the
    /// disks brought them in: the auto set is discounted on both sides.
    #[test]
    fn a_kit_that_names_mergerfs_still_matches() {
        let kits = kits();
        let Some(i) = kits.iter().position(|k| k.modules.iter().any(|m| m == "mergerfs-pools")) else {
            return;
        };
        let mine: std::collections::BTreeSet<&str> = kits[i].modules.iter().map(|s| s.as_str()).collect();
        let auto: std::collections::BTreeSet<&str> = ["mergerfs-pools"].into_iter().collect();
        assert_eq!(kit_for(&mine, &auto, &kits), i);
    }

    #[test]
    fn editor_text_becomes_json_or_string() {
        assert_eq!(parse_value("[1,2]"), json!([1, 2]));
        assert_eq!(parse_value("true"), json!(true));
        assert_eq!(parse_value("example.com"), json!("example.com"));
    }

    #[test]
    fn keys_are_validated_and_deduplicated() {
        let k = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEJwHH00HOg12ICIjLwVzHYpCMs/vtOexwok8DV1D6Co me@laptop";
        assert!(validate_key(k).is_ok());
        assert!(validate_key("hello").is_err());
        assert!(validate_key("ssh-ed25519 notbase64!").is_err());
        assert!(same_key(k, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEJwHH00HOg12ICIjLwVzHYpCMs/vtOexwok8DV1D6Co other@github"));
        assert!(key_summary(k).ends_with("(me@laptop)"));
    }

    /// Walking a second disk to `data` must not take SYSTEM off the first
    /// on the way past it.
    /// The Kit screen must say what the ticked modules amount to, not what
    /// was clicked once: after a reload or an in-place update the answers
    /// come back as a module list and nothing else.
    #[test]
    fn the_kit_is_derived_from_the_modules() {
        let ks = kits();
        let starter: std::collections::BTreeSet<&str> = ks[0].modules.iter().map(|s| s.as_str()).collect();
        // The foundation is in every install and must not break the match.
        let mut with_foundation = starter.clone();
        for f in FOUNDATION_ALWAYS {
            with_foundation.insert(f);
        }
        let without: std::collections::BTreeSet<&str> = with_foundation.iter().copied().filter(|n| !FOUNDATION_ALWAYS.contains(n)).collect();
        assert_eq!(without, starter);
        // Custom is last and is not a module list.
        assert_eq!(ks.last().unwrap().name, "Custom");
        assert!(ks.last().unwrap().modules.iter().any(|m| m == CUSTOM));
        assert_eq!(ks[ks.len() - 2].name, "Minimal");
        assert!(ks[ks.len() - 2].modules.is_empty());
    }

    #[test]
    fn the_role_cycle_does_not_steal_the_system_disk() {
        // Nothing is the system disk yet: the first press offers it.
        assert_eq!(Role::Unused.next(false), Role::System);
        assert_eq!(Role::System.next(false), Role::Data);
        assert_eq!(Role::Data.next(false), Role::Parity);
        assert_eq!(Role::Parity.next(false), Role::Unused);
        // Another disk holds it: data comes first, SYSTEM last and deliberate.
        assert_eq!(Role::Unused.next(true), Role::Data);
        assert_eq!(Role::Data.next(true), Role::Parity);
        assert_eq!(Role::Parity.next(true), Role::System);
    }

    #[test]
    fn inputs_are_checked() {
        assert!(valid_domain("example.com"));
        assert!(valid_domain("my-lab.example.co.uk"));
        assert!(!valid_domain("Example.com"));
        assert!(!valid_domain("https://example.com"));
        assert!(!valid_domain("localhost"));
        assert!(valid_email("me@example.com"));
        assert!(!valid_email("me@localhost"));
        assert!(valid_hostname("homelab-1"));
        assert!(!valid_hostname("-lab"));
        assert!(valid_username("admin"));
        assert!(!valid_username("Admin"));
    }
}
