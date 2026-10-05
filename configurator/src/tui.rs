//! `tui` — the installer's front end, shaped like Ubuntu Server's subiquity:
//! one question per screen, a linear Back / Continue flow, every screen says
//! what it is for, and the last one runs the install itself and shows the
//! addresses to open. Nothing here knows Nix; the screens fill an answers
//! file and call `generate` and `install` as child processes, exactly as a
//! person would at the shell.
//!
//! Keys: ↑↓ or Tab/Shift-Tab move between rows and the buttons; Enter edits
//! a field (Enter keeps, Esc cancels); Space toggles a choice; Enter on
//! Continue goes forward after the screen's checks, Back goes back. The
//! focused row is drawn in reverse video with a › marker — a plain
//! background colour is invisible on a VGA text console.

use anyhow::{Context, Result};
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::ExecutableCommand;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
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

// ---------------------------------------------------------------- kits

/// A kit is a module list with a name: the first real question, so a
/// first-time user picks a whole homelab in one keystroke. The three bigger
/// kits are the canned profiles the VM test installs (modules only; their
/// example values are not taken).
struct Kit {
    name: &'static str,
    blurb: &'static str,
    modules: Vec<String>,
}

fn kits() -> Vec<Kit> {
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
        Kit { name: "Blank", blurb: "Nothing chosen but the foundation. Pick modules yourself on the Modules screen.", modules: vec![] },
    ]
}

// ---------------------------------------------------------------- screens

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Step {
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

const STEPS: [Step; 10] = [Step::Welcome, Step::Kit, Step::Storage, Step::Profile, Step::Ssh, Step::Domain, Step::Extras, Step::Review, Step::Install, Step::Done];

impl Step {
    fn title(self) -> &'static str {
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
    fn index(self) -> usize {
        STEPS.iter().position(|s| *s == self).unwrap_or(0)
    }
}

#[derive(Clone, Copy, PartialEq, Eq)]
enum Role {
    Unused,
    System,
    Data,
    Parity,
}

impl Role {
    fn next(self) -> Role {
        match self {
            Role::Unused => Role::System,
            Role::System => Role::Data,
            Role::Data => Role::Parity,
            Role::Parity => Role::Unused,
        }
    }
    fn label(self) -> &'static str {
        match self {
            Role::Unused => "not used",
            Role::System => "SYSTEM",
            Role::Data => "data",
            Role::Parity => "parity",
        }
    }
}

/// One text field on a screen.
struct Field {
    key: &'static str,
    label: &'static str,
    help: &'static str,
    value: String,
    masked: bool,
}

impl Field {
    fn new(key: &'static str, label: &'static str, help: &'static str, value: &str) -> Field {
        Field { key, label, help, value: value.into(), masked: false }
    }
    fn masked(mut self) -> Field {
        self.masked = true;
        self
    }
    fn shown(&self) -> String {
        if self.value.is_empty() {
            "—".into()
        } else if self.masked {
            "•".repeat(self.value.chars().count())
        } else {
            self.value.clone()
        }
    }
}

struct ModuleRow {
    name: String,
    description: String,
    chosen: bool,
    locked: bool,
}

/// A module's supply secret, asked on the Domain screen (the Cloudflare
/// token) or with it (a VPN account for the media box).
struct SecretRow {
    option: String,
    keys: String,
    /// Typed value, shaped by guides::shape into the file the module reads.
    typed: String,
    /// Where the value was written (mode 600), once it was.
    file: Option<PathBuf>,
    verified: Option<Result<String, String>>,
}

/// What the install child process reports, read by the event loop.
struct Progress {
    lines: Mutex<Vec<String>>,
    done: AtomicBool,
    exit: AtomicI32,
}

struct App<'a> {
    schema: &'a Schema,
    step: Step,
    /// Focused row on the current screen; rows beyond the fields are the
    /// buttons (Back, Continue).
    focus: usize,
    editing: Option<String>,
    status: String,
    status_is_error: bool,
    // Welcome
    ram_mib: u64,
    address: String,
    internet: Option<bool>,
    live_usb: bool,
    // Kit
    kits: Vec<Kit>,
    kit: usize,
    // Storage
    disks: Vec<Disk>,
    roles: Vec<Role>,
    disk_fallback: Field,
    // Profile
    profile: Vec<Field>,
    admin_hash: Option<String>,
    // SSH
    github_user: Field,
    pasted_key: Field,
    keys: Vec<String>,
    // Domain
    domain: Vec<Field>,
    secrets: Vec<SecretRow>,
    /// A second Continue accepts a soft warning (no key, no password, an
    /// unverified value).
    continue_unverified: bool,
    // Extras
    modules: Vec<ModuleRow>,
    values: BTreeMap<String, String>,
    // Review / Install
    out_answers: PathBuf,
    out_dir: PathBuf,
    exe_prefix: Vec<String>,
    progress: Option<Arc<Progress>>,
    install_report: Vec<String>,
    scroll: usize,
    quit: bool,
}

/// What the focus can rest on: the screen's rows, then the two buttons.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Row {
    Field(usize),
    Disk(usize),
    Kit(usize),
    Key(usize),
    Secret(usize),
    Module(usize),
    Value(usize),
    Back,
    Continue,
}

/// `exe_prefix`: the global flags (`--catalog`, `--options`) the child
/// `generate`/`install` must get too.
pub fn run(schema: &Schema, profile: Option<&Path>, out_answers: &Path, out_dir: &Path, exe_prefix: Vec<String>) -> Result<i32> {
    let mut app = App::new(schema, out_answers, out_dir, exe_prefix).with_network();
    if let Some(p) = profile {
        let text = std::fs::read_to_string(p).with_context(|| format!("reading {}", p.display()))?;
        let a: Answers = serde_json::from_str(&text).with_context(|| format!("parsing {}", p.display()))?;
        app.load(&a);
    } else if out_answers.exists() {
        // A second run on the same directory: pick up where it left off.
        if let Ok(text) = std::fs::read_to_string(out_answers) {
            if let Ok(a) = serde_json::from_str::<Answers>(&text) {
                app.load(&a);
                app.status = format!("answers loaded from {}", out_answers.display());
            }
        }
    }

    enable_raw_mode()?;
    io::stdout().execute(EnterAlternateScreen)?;
    let backend = ratatui::backend::CrosstermBackend::new(io::stdout());
    let mut terminal = Terminal::new(backend)?;
    let result = app.event_loop(&mut terminal);
    disable_raw_mode()?;
    io::stdout().execute(LeaveAlternateScreen)?;
    result?;

    // The install's own report, so it stays in the scrollback after the form closes.
    for l in &app.install_report {
        println!("{l}");
    }
    Ok(0)
}

fn write_answers(a: &Answers, path: &Path) -> Result<()> {
    std::fs::write(path, serde_json::to_string_pretty(a)? + "\n").with_context(|| format!("writing {}", path.display()))
}

impl<'a> App<'a> {
    fn new(schema: &'a Schema, out_answers: &Path, out_dir: &Path, exe_prefix: Vec<String>) -> Self {
        let mut modules: Vec<ModuleRow> = schema
            .catalog
            .iter()
            .filter(|(n, _)| n.as_str() != "options")
            .map(|(n, m)| ModuleRow { name: n.clone(), description: m.description.clone(), chosen: FOUNDATION_ALWAYS.contains(&n.as_str()), locked: FOUNDATION_ALWAYS.contains(&n.as_str()) })
            .collect();
        modules.sort_by(|a, b| (!a.locked, &a.name).cmp(&(!b.locked, &b.name)));
        // The disk the live system runs from is not a choice at all.
        let disks: Vec<Disk> = disks::list().into_iter().filter(|d| !d.in_use).collect();
        let roles = vec![Role::Unused; disks.len()];
        let live_usb = Path::new("/iso").exists() || Path::new("/nix/.ro-store").exists();
        let mut app = App {
            schema,
            step: Step::Welcome,
            focus: 0,
            editing: None,
            status: String::new(),
            status_is_error: false,
            ram_mib: crate::plan::machine_ram_mib(),
            address: String::new(),
            internet: None,
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
                Field::new("timeZone", "Time zone", "An IANA name such as America/New_York or Europe/Berlin: backups and alerts are scheduled in it.", "UTC"),
            ],
            admin_hash: None,
            github_user: Field::new("githubUser", "Import keys from GitHub", "Type your GitHub username and press Enter: the public keys on your GitHub account are added to the list below (the way Ubuntu's installer imports an SSH identity). Nothing already in the list is removed.", ""),
            pasted_key: Field::new("pasteKey", "Paste a public key", "A line from a file such as ~/.ssh/id_ed25519.pub, starting with ssh-ed25519, ssh-rsa or ecdsa-. It is checked before it is added.", ""),
            keys: Vec::new(),
            domain: vec![
                Field::new("homelab.domain", "Domain", "Your domain, like example.com, with nothing in front of it. Every app gets a name under it (recipes.example.com) and a real certificate. For this release the domain's DNS must be at Cloudflare.", ""),
                Field::new("homelab.acme.email", "Email for certificates", "Let's Encrypt sends certificate notices here. Any address you read.", ""),
            ],
            secrets: Vec::new(),
            continue_unverified: false,
            modules,
            values: BTreeMap::new(),
            out_answers: out_answers.to_path_buf(),
            out_dir: out_dir.to_path_buf(),
            exe_prefix,
            progress: None,
            install_report: Vec::new(),
            scroll: 0,
            quit: false,
        };
        app.apply_kit();
        app
    }

    /// The Welcome screen's network line, decided once at start.
    fn with_network(mut self) -> Self {
        let (a, i) = probe_network();
        self.address = a;
        self.internet = i;
        self
    }

    /// Prefill from an answers file (a profile, or a second run).
    fn load(&mut self, a: &Answers) {
        self.admin_hash = a.host.admin_password_hash.clone();
        for f in &mut self.profile {
            match f.key {
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
            if let Some(v) = self.values.get(f.key) {
                f.value = v.clone();
            }
        }
        // Disks: match the answers' devices to what is attached.
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
        // Modules: the kit whose list matches, else Blank + the explicit set.
        let chosen: std::collections::BTreeSet<&str> = a.modules.iter().map(|s| s.as_str()).collect();
        self.kit = self.kits.len() - 1;
        for (i, k) in self.kits.iter().enumerate() {
            let set: std::collections::BTreeSet<&str> = k.modules.iter().map(|s| s.as_str()).collect();
            if !set.is_empty() && set == chosen {
                self.kit = i;
            }
        }
        for m in &mut self.modules {
            if chosen.contains(m.name.as_str()) {
                m.chosen = true;
            }
        }
        self.refresh_secrets();
    }

    // ------------------------------------------------------------ model

    fn apply_kit(&mut self) {
        let k = &self.kits[self.kit];
        for m in &mut self.modules {
            if !m.locked {
                m.chosen = k.modules.contains(&m.name);
            }
        }
        self.refresh_secrets();
    }

    fn chosen(&self) -> Vec<String> {
        self.modules.iter().filter(|m| m.chosen).map(|m| m.name.clone()).collect()
    }

    /// The closure the plan will use, so later screens show what the
    /// install will really carry.
    fn closed(&self) -> Vec<String> {
        self.schema.close_over_requires(&self.chosen()).map(|(m, _)| m).unwrap_or_else(|_| self.chosen())
    }

    fn values_json(&self) -> BTreeMap<String, serde_json::Value> {
        let mut v: BTreeMap<String, serde_json::Value> = self.values.iter().filter(|(_, v)| !v.trim().is_empty()).map(|(k, v)| (k.clone(), parse_value(v))).collect();
        for f in &self.domain {
            if !f.value.trim().is_empty() {
                v.insert(f.key.to_string(), serde_json::Value::String(f.value.trim().to_string()));
            }
        }
        let admin = self.field("adminUser");
        if !admin.is_empty() {
            v.insert("homelab.adminUser".into(), serde_json::Value::String(admin));
        }
        v
    }

    /// The supply secrets the chosen modules need, by the same rule
    /// generate applies (a nullable secret behind an off switch is not asked).
    fn refresh_secrets(&mut self) {
        let closed = self.closed();
        let values = self.values_json();
        let mut wanted: Vec<(String, String)> = Vec::new();
        for m in &closed {
            if let Some(meta) = self.schema.catalog.get(m) {
                for s in &meta.secrets {
                    if s.source == Source::Supply && s.option.starts_with("homelab.") && !crate::plan::skip_secret(self.schema, &closed, &values, s) && !wanted.iter().any(|(o, _)| o == &s.option) {
                        wanted.push((s.option.clone(), s.keys.join(", ")));
                    }
                }
            }
        }
        let mut next: Vec<SecretRow> = Vec::new();
        for (option, keys) in wanted {
            if let Some(old) = self.secrets.iter_mut().find(|r| r.option == option) {
                next.push(SecretRow { option: old.option.clone(), keys, typed: std::mem::take(&mut old.typed), file: old.file.take(), verified: old.verified.take() });
            } else {
                next.push(SecretRow { option, keys, typed: String::new(), file: None, verified: None });
            }
        }
        // The Cloudflare token first: it is the one everybody needs.
        next.sort_by_key(|r| if r.option == "homelab.acme.credentialsFile" { 0 } else { 1 });
        self.secrets = next;
    }

    /// Required values the kit leaves open beyond domain, email and admin.
    fn open_values(&self) -> Vec<(String, String, String)> {
        let mods = self.closed();
        let secret_opts: Vec<String> = self.secrets.iter().map(|r| r.option.clone()).collect();
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

    fn memory_verdict(&self) -> (u64, String, bool) {
        let need = crate::plan::memory_need(self.schema, &self.closed());
        let short = self.ram_mib != 0 && need > self.ram_mib;
        (need, crate::plan::memory_verdict(need, self.ram_mib), short)
    }

    fn system_disk(&self) -> Option<String> {
        if self.disks.is_empty() {
            let v = self.disk_fallback.value.trim();
            return if v.is_empty() { None } else { Some(v.to_string()) };
        }
        self.disks.iter().zip(&self.roles).find(|(_, r)| **r == Role::System).map(|(d, _)| d.id.display().to_string())
    }

    fn data_disks(&self) -> Vec<serde_json::Value> {
        let mut out = Vec::new();
        let mut n = 0;
        for (d, r) in self.disks.iter().zip(&self.roles) {
            match r {
                Role::Data => {
                    n += 1;
                    out.push(serde_json::json!({ "name": format!("d{n}"), "device": d.id.display().to_string() }));
                }
                Role::Parity => out.push(serde_json::json!({ "name": "parity", "device": d.id.display().to_string() })),
                _ => {}
            }
        }
        out
    }

    fn field(&self, key: &str) -> String {
        self.profile.iter().find(|f| f.key == key).map(|f| f.value.trim().to_string()).unwrap_or_default()
    }

    fn answers(&self) -> Answers {
        let typed = self.field("password");
        let admin_hash = if typed.is_empty() { self.admin_hash.clone() } else { crate::emit::mkpasswd(&typed).ok() };
        let json = serde_json::json!({
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

    // ------------------------------------------------------------ rows

    fn rows(&self) -> Vec<Row> {
        let mut r = Vec::new();
        match self.step {
            Step::Welcome => {}
            Step::Kit => r.extend((0..self.kits.len()).map(Row::Kit)),
            Step::Storage => {
                if self.disks.is_empty() {
                    r.push(Row::Field(0));
                } else {
                    r.extend((0..self.disks.len()).map(Row::Disk));
                }
            }
            Step::Profile => r.extend((0..self.profile.len()).map(Row::Field)),
            Step::Ssh => {
                r.push(Row::Field(0));
                r.push(Row::Field(1));
                r.extend((0..self.keys.len()).map(Row::Key));
            }
            Step::Domain => {
                r.extend((0..self.domain.len()).map(Row::Field));
                r.extend((0..self.secrets.len()).map(Row::Secret));
            }
            Step::Extras => {
                r.extend((0..self.open_values().len()).map(Row::Value));
                r.extend((0..self.modules.len()).map(Row::Module));
            }
            Step::Review | Step::Install | Step::Done => {}
        }
        if !matches!(self.step, Step::Welcome | Step::Install | Step::Done) {
            r.push(Row::Back);
        }
        r.push(Row::Continue);
        r
    }

    fn focused(&self) -> Row {
        let rows = self.rows();
        rows[self.focus.min(rows.len() - 1)]
    }

    fn move_focus(&mut self, d: i32) {
        let n = self.rows().len() as i32;
        self.focus = ((self.focus as i32 + d).rem_euclid(n)) as usize;
    }

    fn set_status(&mut self, s: impl Into<String>, err: bool) {
        self.status = s.into();
        self.status_is_error = err;
    }

    // ------------------------------------------------------------ keys

    fn event_loop(&mut self, terminal: &mut Terminal<ratatui::backend::CrosstermBackend<io::Stdout>>) -> Result<()> {
        loop {
            terminal.draw(|f| self.draw(f))?;
            if event::poll(Duration::from_millis(120))? {
                if let Event::Key(key) = event::read()? {
                    if key.kind != KeyEventKind::Press {
                        continue;
                    }
                    self.on_key(key);
                }
            }
            if self.step == Step::Install {
                self.poll_install();
            }
            if self.quit {
                return Ok(());
            }
        }
    }

    fn on_key(&mut self, key: KeyEvent) {
        if let Some(buf) = &mut self.editing {
            match key.code {
                KeyCode::Esc => {
                    self.editing = None;
                    self.set_status("edit cancelled", false);
                }
                KeyCode::Enter => {
                    let text = buf.clone();
                    self.editing = None;
                    self.commit_edit(text);
                }
                KeyCode::Backspace => {
                    buf.pop();
                }
                KeyCode::Tab | KeyCode::BackTab | KeyCode::Up | KeyCode::Down => {
                    self.set_status("finish this field first: Enter keeps what you typed, Esc throws it away", true);
                }
                KeyCode::Char(c) => buf.push(c),
                _ => {}
            }
            return;
        }
        if self.step == Step::Install {
            if key.code == KeyCode::Char('q') && key.modifiers.contains(KeyModifiers::CONTROL) {
                self.quit = true;
            }
            return;
        }
        match key.code {
            KeyCode::Up | KeyCode::BackTab => self.move_focus(-1),
            KeyCode::Down | KeyCode::Tab => self.move_focus(1),
            KeyCode::PageUp => self.scroll = self.scroll.saturating_sub(10),
            KeyCode::PageDown => self.scroll += 10,
            KeyCode::Esc => self.back(),
            KeyCode::Char('q') if key.modifiers.contains(KeyModifiers::CONTROL) => self.quit = true,
            KeyCode::Char(' ') => self.toggle(),
            KeyCode::Enter => match self.focused() {
                Row::Continue => self.forward(),
                Row::Back => self.back(),
                Row::Field(_) | Row::Secret(_) | Row::Value(_) => self.start_edit(),
                _ => self.toggle(),
            },
            _ => {}
        }
    }

    /// Space (or Enter on a choice): pick a kit, cycle a disk role, drop a
    /// key, toggle a module.
    fn toggle(&mut self) {
        match self.focused() {
            Row::Kit(i) => {
                self.kit = i;
                self.apply_kit();
                let (_, verdict, _) = self.memory_verdict();
                self.set_status(format!("{}: {}", self.kits[i].name, verdict), false);
            }
            Row::Disk(i) => {
                let r = self.roles[i].next();
                if r == Role::System {
                    for other in self.roles.iter_mut() {
                        if *other == Role::System {
                            *other = Role::Unused;
                        }
                    }
                }
                self.roles[i] = r;
                let msg = match r {
                    Role::System => "the system goes here; the whole disk is erased",
                    Role::Data => "storage for your files, pooled under /mnt/media (erased); a parity disk can protect it",
                    Role::Parity => "holds parity for the data disks so one can die without loss; at least as large as the largest data disk",
                    Role::Unused => "left alone",
                };
                self.set_status(format!("{}: {msg}", self.disks[i].kernel), false);
            }
            Row::Key(i) => {
                self.keys.remove(i);
                self.set_status("key removed", false);
                let n = self.rows().len();
                self.focus = self.focus.min(n - 1);
            }
            Row::Module(i) => {
                if self.modules[i].locked {
                    self.set_status("part of the foundation: always on", false);
                } else {
                    self.modules[i].chosen = !self.modules[i].chosen;
                    self.refresh_secrets();
                    let (_, verdict, _) = self.memory_verdict();
                    self.set_status(verdict, false);
                }
            }
            Row::Continue => self.forward(),
            Row::Back => self.back(),
            _ => self.start_edit(),
        }
    }

    fn start_edit(&mut self) {
        let current = match (self.step, self.focused()) {
            (Step::Storage, Row::Field(0)) => self.disk_fallback.value.clone(),
            (Step::Profile, Row::Field(i)) => self.profile[i].value.clone(),
            (Step::Ssh, Row::Field(0)) => self.github_user.value.clone(),
            (Step::Ssh, Row::Field(1)) => self.pasted_key.value.clone(),
            (Step::Domain, Row::Field(i)) => self.domain[i].value.clone(),
            (Step::Domain, Row::Secret(i)) => self.secrets[i].typed.clone(),
            (Step::Extras, Row::Value(i)) => self.open_values().get(i).map(|v| v.1.clone()).unwrap_or_default(),
            _ => return,
        };
        self.editing = Some(current);
        self.set_status("type, then Enter to keep · Esc to cancel", false);
    }

    fn commit_edit(&mut self, text: String) {
        let text = text.trim().to_string();
        match (self.step, self.focused()) {
            (Step::Storage, Row::Field(0)) => self.disk_fallback.value = text,
            (Step::Profile, Row::Field(i)) => {
                let key = self.profile[i].key;
                if key == "name" && !text.is_empty() && !(text.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') && text.chars().next().map(|c| c.is_ascii_alphanumeric()).unwrap_or(false)) {
                    self.set_status("a machine name is letters, digits and dashes, starting with a letter or digit", true);
                    return;
                }
                if key == "adminUser" && !text.is_empty() && !(text.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_') && text.chars().next().map(|c| c.is_ascii_lowercase()).unwrap_or(false)) {
                    self.set_status("a username is lowercase letters, digits, dashes and underscores, starting with a letter", true);
                    return;
                }
                if key == "timeZone" && !text.is_empty() && Path::new("/etc/zoneinfo").exists() && !Path::new("/etc/zoneinfo").join(&text).exists() {
                    self.set_status(format!("no time zone named {text}: try Region/City, e.g. America/Chicago"), true);
                    return;
                }
                self.profile[i].value = text;
                self.set_status("kept", false);
            }
            (Step::Ssh, Row::Field(0)) => {
                self.github_user.value = text.clone();
                if text.is_empty() {
                    return;
                }
                match fetch_github_keys(&text) {
                    Ok(keys) if keys.is_empty() => self.set_status(format!("github.com/{text} has no public keys"), true),
                    Ok(keys) => {
                        let mut added = 0;
                        for k in keys {
                            if !self.keys.iter().any(|e| same_key(e, &k)) {
                                self.keys.push(k);
                                added += 1;
                            }
                        }
                        self.set_status(format!("{added} key(s) added from github.com/{text}; {} in the list", self.keys.len()), false);
                    }
                    Err(e) => self.set_status(format!("could not fetch keys: {e}"), true),
                }
            }
            (Step::Ssh, Row::Field(1)) => {
                if text.is_empty() {
                    return;
                }
                match validate_key(&text) {
                    Ok(k) => {
                        if self.keys.iter().any(|e| same_key(e, &k)) {
                            self.set_status("that key is already in the list", true);
                        } else {
                            self.keys.push(k);
                            self.pasted_key.value.clear();
                            self.set_status(format!("key added; {} in the list", self.keys.len()), false);
                        }
                    }
                    Err(e) => self.set_status(e, true),
                }
            }
            (Step::Domain, Row::Field(i)) => {
                let key = self.domain[i].key;
                if key == "homelab.domain" && !text.is_empty() && !valid_domain(&text) {
                    self.set_status("a domain looks like example.com: lowercase labels joined by dots, nothing in front, no path", true);
                    return;
                }
                if key == "homelab.acme.email" && !text.is_empty() && !(text.contains('@') && text.rsplit('@').next().map(|d| d.contains('.')).unwrap_or(false)) {
                    self.set_status("an email address looks like you@example.com", true);
                    return;
                }
                self.domain[i].value = text;
                self.refresh_secrets();
                self.set_status("kept", false);
            }
            (Step::Domain, Row::Secret(i)) => {
                if text.is_empty() {
                    return;
                }
                let opt = self.secrets[i].option.clone();
                let dir = self.out_answers.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| PathBuf::from(".")).join(".secrets");
                let file = dir.join(crate::secrets::secret_name(&opt));
                let content = crate::guides::shape(&opt, &text);
                let written = std::fs::create_dir_all(&dir).map_err(|e| e.to_string()).and_then(|_| {
                    let _ = std::fs::remove_file(&file);
                    crate::secrets::write_private(&file, &content).map_err(|e| e.to_string())
                });
                match written {
                    Ok(()) => {
                        let values: BTreeMap<String, String> = self.values_json().into_iter().map(|(k, v)| (k, value_text(&v))).collect();
                        let verified = crate::guides::verify(&opt, &content, &values);
                        let msg = match &verified {
                            Some(Ok(m)) => m.clone(),
                            Some(Err(m)) => format!("NOT verified: {m}"),
                            None => format!("saved to {} (mode 600)", file.display()),
                        };
                        let err = matches!(verified, Some(Err(_)));
                        let row = &mut self.secrets[i];
                        row.typed = text;
                        row.file = Some(file);
                        row.verified = verified;
                        self.set_status(msg, err);
                    }
                    Err(e) => self.set_status(format!("could not write the secret file: {e}"), true),
                }
            }
            (Step::Extras, Row::Value(i)) => {
                if let Some((name, _, _)) = self.open_values().get(i).cloned() {
                    if text.is_empty() {
                        self.values.remove(&name);
                    } else {
                        self.values.insert(name, text);
                    }
                    self.refresh_secrets();
                }
            }
            _ => {}
        }
    }

    /// A soft warning: the first Continue shows it, the second accepts it.
    fn soft_block(&mut self, msg: &str) -> bool {
        if self.continue_unverified {
            self.continue_unverified = false;
            return false;
        }
        self.set_status(format!("{msg} (press Continue again to accept)"), true);
        self.continue_unverified = true;
        true
    }

    /// The screen's checks, then the next screen.
    fn forward(&mut self) {
        match self.step {
            Step::Welcome | Step::Kit => {}
            Step::Storage => {
                if self.system_disk().is_none() {
                    self.set_status("choose the disk the system goes on: move to it and press Space until it says SYSTEM", true);
                    return;
                }
                let parity = self.roles.iter().any(|r| *r == Role::Parity);
                let data = self.roles.iter().any(|r| *r == Role::Data);
                if parity && !data {
                    self.set_status("a parity disk protects data disks; mark at least one disk as data, or set the parity disk to data", true);
                    return;
                }
                for (want, when) in [("mergerfs-pools", data), ("snapraid", parity)] {
                    if when && !self.chosen().iter().any(|m| m == want) {
                        if let Some(m) = self.modules.iter_mut().find(|m| m.name == want) {
                            m.chosen = true;
                        }
                    }
                }
                self.refresh_secrets();
            }
            Step::Profile => {
                if self.field("name").is_empty() {
                    self.set_status("the machine needs a name", true);
                    return;
                }
                if self.field("adminUser").is_empty() {
                    self.set_status("the admin needs a username", true);
                    return;
                }
                if self.field("password") != self.field("password2") {
                    self.set_status("the two passwords differ", true);
                    return;
                }
                if self.field("password").is_empty() && self.admin_hash.is_none() && self.soft_block("no password typed: one will be minted and written to FIRST-LOGIN.md on the new system") {
                    return;
                }
            }
            Step::Ssh => {
                if self.keys.is_empty() && self.soft_block("no SSH key: you will only be able to log in at the machine's own screen") {
                    return;
                }
            }
            Step::Domain => {
                if self.closed().iter().any(|m| m == "acme") {
                    if self.domain.iter().any(|f| f.value.trim().is_empty()) {
                        self.set_status("the domain and the email are both needed", true);
                        return;
                    }
                    if let Some(missing) = self.secrets.iter().find(|r| r.file.is_none()) {
                        self.set_status(format!("{} is still empty: move to it, press Enter, type the value", missing.option), true);
                        return;
                    }
                    if self.secrets.iter().any(|r| matches!(r.verified, Some(Err(_)))) && self.soft_block("a value did not verify (the red line); fix it, or go on anyway") {
                        return;
                    }
                }
            }
            Step::Extras => {
                if let Some((name, _, _)) = self.open_values().iter().find(|(_, v, _)| v.trim().is_empty()).cloned() {
                    self.set_status(format!("{name} has no default and needs a value"), true);
                    return;
                }
            }
            Step::Review => {
                self.start_install();
                return;
            }
            Step::Install => return,
            Step::Done => {
                self.quit = true;
                return;
            }
        }
        let i = self.step.index();
        self.step = STEPS[(i + 1).min(STEPS.len() - 1)];
        self.focus = 0;
        self.scroll = 0;
        self.continue_unverified = false;
        self.set_status("", false);
    }

    fn back(&mut self) {
        if matches!(self.step, Step::Welcome | Step::Install | Step::Done) {
            return;
        }
        let i = self.step.index();
        self.step = STEPS[i.saturating_sub(1)];
        self.focus = 0;
        self.scroll = 0;
        self.continue_unverified = false;
        self.set_status("", false);
    }

    // ------------------------------------------------------------ install

    /// Write the answers, then run generate and (on the live USB) install
    /// as child processes, streaming their output into the Install screen.
    fn start_install(&mut self) {
        let answers = self.answers();
        if let Err(e) = write_answers(&answers, &self.out_answers) {
            self.set_status(format!("{e}"), true);
            return;
        }
        let exe = match std::env::current_exe() {
            Ok(e) => e,
            Err(e) => {
                self.set_status(format!("{e}"), true);
                return;
            }
        };
        let mut gen: Vec<String> = self.exe_prefix.clone();
        gen.extend(["generate".to_string(), "--answers".into(), self.out_answers.display().to_string(), "--out".into(), self.out_dir.display().to_string()]);
        for r in &self.secrets {
            if let Some(f) = &r.file {
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
        self.focus = 0;
        self.scroll = 0;
        self.set_status("", false);
    }

    fn poll_install(&mut self) {
        let Some(p) = &self.progress else { return };
        if p.done.load(Ordering::SeqCst) {
            let code = p.exit.load(Ordering::SeqCst);
            self.install_report = p.lines.lock().unwrap().clone();
            self.step = Step::Done;
            self.focus = 0;
            self.scroll = 0;
            self.progress = None;
            if code == 0 {
                self.set_status("", false);
            } else {
                self.set_status(format!("it stopped with exit {code}; the lines above say where"), true);
            }
        }
    }

    // ------------------------------------------------------------ draw

    fn draw(&self, f: &mut Frame) {
        let outer = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Min(5), Constraint::Length(2)])
            .split(f.area());
        // Title line: step n of N, like the installer's own progress.
        let n = STEPS.len() - 2; // Install and Done are not counted
        let shown = self.step.index().min(n - 1) + 1;
        let title = Line::from(vec![
            Span::styled(format!(" {} ", self.step.title()), Style::default().add_modifier(Modifier::BOLD | Modifier::REVERSED)),
            Span::raw(format!("  step {shown} of {n}  ·  homelab installer")),
        ]);
        f.render_widget(Paragraph::new(title), outer[0]);

        let body = Layout::default().direction(Direction::Vertical).constraints([Constraint::Min(3), Constraint::Length(3)]).split(outer[1]);
        match self.step {
            Step::Welcome => self.draw_welcome(f, body[0]),
            Step::Kit => self.draw_kit(f, body[0]),
            Step::Storage => self.draw_storage(f, body[0]),
            Step::Profile => self.draw_profile(f, body[0]),
            Step::Ssh => self.draw_ssh(f, body[0]),
            Step::Domain => self.draw_domain(f, body[0]),
            Step::Extras => self.draw_extras(f, body[0]),
            Step::Review => self.draw_review(f, body[0]),
            Step::Install => self.draw_install(f, body[0]),
            Step::Done => self.draw_done(f, body[0]),
        }
        self.draw_buttons(f, body[1]);

        // Footer: the edit line, or the status, or the key help.
        let footer: Line = match &self.editing {
            Some(buf) => {
                let masked = matches!((self.step, self.focused()), (Step::Profile, Row::Field(2)) | (Step::Profile, Row::Field(3)) | (Step::Domain, Row::Secret(_)));
                let shown = if masked { "•".repeat(buf.chars().count()) } else { buf.clone() };
                Line::from(vec![Span::styled(" edit: ", Style::default().fg(Color::Yellow).add_modifier(Modifier::BOLD)), Span::raw(shown), Span::styled("▏", Style::default().fg(Color::Yellow)), Span::raw("   Enter keeps · Esc cancels")])
            }
            None if !self.status.is_empty() => Line::from(Span::styled(format!(" {}", self.status), if self.status_is_error { Style::default().fg(Color::Red).add_modifier(Modifier::BOLD) } else { Style::default().fg(Color::Green) })),
            None => Line::from(" ↑↓ or Tab: move · Space: choose · Enter: edit, or press the button · Esc: back · Ctrl-Q: quit to the shell"),
        };
        f.render_widget(Paragraph::new(footer).wrap(Wrap { trim: true }), outer[2]);
    }

    fn focus_style(&self, row: Row) -> Style {
        if self.focused() == row && self.editing.is_none() {
            Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD)
        } else {
            Style::default()
        }
    }

    fn marker(&self, row: Row) -> &'static str {
        if self.focused() == row { "› " } else { "  " }
    }

    fn draw_buttons(&self, f: &mut Frame, area: Rect) {
        let mut spans = vec![Span::raw("  ")];
        if self.rows().contains(&Row::Back) {
            spans.push(Span::styled("[ Back ]", self.focus_style(Row::Back)));
            spans.push(Span::raw("   "));
        }
        let label = match self.step {
            Step::Review => if self.live_usb { "[ Install ]" } else { "[ Write the configuration ]" },
            Step::Install => "[ working... ]",
            Step::Done => "[ Close ]",
            _ => "[ Continue ]",
        };
        spans.push(Span::styled(label, self.focus_style(Row::Continue)));
        let hint = match self.step {
            Step::Review if self.live_usb => "   Install erases the disks listed above.",
            Step::Done => "   Then type `reboot` and pull the USB stick out as the screen goes dark.",
            _ => "",
        };
        spans.push(Span::raw(hint));
        f.render_widget(Paragraph::new(Line::from(spans)).block(Block::default().borders(Borders::TOP)), area);
    }

    /// A screen: an intro paragraph, a list of rows, a help box for the
    /// focused row.
    fn split(area: Rect, intro_lines: u16, help_lines: u16) -> (Rect, Rect, Rect) {
        let v = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(intro_lines), Constraint::Min(3), Constraint::Length(help_lines)])
            .split(area);
        (v[0], v[1], v[2])
    }

    fn intro(&self, f: &mut Frame, area: Rect, text: &str) {
        f.render_widget(Paragraph::new(text).wrap(Wrap { trim: true }), area);
    }

    fn help(&self, f: &mut Frame, area: Rect, title: &str, text: &str) {
        f.render_widget(Paragraph::new(text).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(format!(" {title} "))), area);
    }

    fn field_line(&self, row: Row, label: &str, shown: String) -> Line<'static> {
        Line::from(vec![Span::styled(format!("{}{:<24}", self.marker(row), label), self.focus_style(row).add_modifier(Modifier::BOLD)), Span::styled(shown, self.focus_style(row))])
    }

    fn draw_welcome(&self, f: &mut Frame, area: Rect) {
        let gb = |m: u64| format!("{:.1} GB", m as f64 / 1024.0);
        let net = match (self.address.is_empty(), self.internet) {
            (true, _) => "no network address yet: plug in a cable (wired is automatic), then press Ctrl-Q and start again".to_string(),
            (false, Some(true)) => format!("network {} · internet reachable", self.address),
            (false, Some(false)) => format!("network {} · internet NOT reachable: the install downloads a few gigabytes and needs it", self.address),
            (false, None) => format!("network {}", self.address),
        };
        let text = Text::from(vec![
            Line::from("This installs a complete, self-hosted homelab on this machine — a media server, apps, backups and monitoring — from a public module library, in one pass."),
            Line::from(""),
            Line::from("What happens next: you pick a kit, choose the disk to install on (it is erased), set your name and password, give it a domain at Cloudflare with an API token, and it installs. Nothing is written until the last screen says Install."),
            Line::from(""),
            Line::from("You will need: a domain whose DNS is at Cloudflare and an API token for it, an email address, and twenty to forty minutes, most of it waiting."),
            Line::from(""),
            Line::from(vec![Span::styled("This machine  ", Style::default().add_modifier(Modifier::BOLD)), Span::raw(format!("{} of RAM · {} disk(s) available · {}", if self.ram_mib == 0 { "unknown".to_string() } else { gb(self.ram_mib) }, self.disks.len(), net))]),
            Line::from(""),
            Line::from(if self.live_usb { "Running from the installer. The shell is still there behind this: Ctrl-Q." } else { "Not running from the installer: the last screen writes the configuration and prints the install command instead of running it." }),
        ]);
        f.render_widget(Paragraph::new(text).wrap(Wrap { trim: true }), area);
    }

    fn draw_kit(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 3, 5);
        self.intro(f, i, "One choice sets the whole module list; the Modules screen later lets you adjust it. The number is the memory the kit needs on this machine: each module's figure plus a gigabyte for the system itself.");
        let mut lines = Vec::new();
        for (idx, k) in self.kits.iter().enumerate() {
            let closed = self.schema.close_over_requires(&k.modules).map(|(m, _)| m).unwrap_or_else(|_| k.modules.clone());
            let need = crate::plan::memory_need(self.schema, &closed);
            let fits = self.ram_mib == 0 || need <= self.ram_mib;
            let row = Row::Kit(idx);
            lines.push(Line::from(vec![
                Span::styled(format!("{}({}) {:<16}", self.marker(row), if self.kit == idx { "*" } else { " " }, k.name), self.focus_style(row)),
                Span::styled(format!(" ~{} GB ", (need + 511) / 1024), Style::default().fg(if fits { Color::Green } else { Color::Red }).add_modifier(Modifier::BOLD)),
                Span::raw(k.blurb),
            ]));
        }
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), l);
        let (_, verdict, _) = self.memory_verdict();
        let k = &self.kits[self.kit];
        self.help(f, h, &format!("{} (chosen)", k.name), &format!("{verdict}\nmodules: {}", if k.modules.is_empty() { "none until you pick them".to_string() } else { k.modules.join(" ") }));
    }

    fn draw_storage(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 4, 6);
        self.intro(f, i, "Choose the disk the system goes on: move to it and press Space until it says SYSTEM. The whole disk is erased. Any other disk can be marked data (storage for your files, pooled) or parity (protects the data disks); Space again cycles the role. The USB stick you booted from is not listed and cannot be chosen.");
        if self.disks.is_empty() {
            let row = Row::Field(0);
            f.render_widget(Paragraph::new(self.field_line(row, self.disk_fallback.label, self.disk_fallback.shown())), l);
            self.help(f, h, self.disk_fallback.label, self.disk_fallback.help);
            return;
        }
        let mut lines = Vec::new();
        for (idx, (d, r)) in self.disks.iter().zip(&self.roles).enumerate() {
            let row = Row::Disk(idx);
            let role_style = Style::default().add_modifier(Modifier::BOLD).fg(match r { Role::System => Color::Yellow, Role::Data => Color::Green, Role::Parity => Color::Cyan, Role::Unused => Color::Reset });
            lines.push(Line::from(vec![
                Span::styled(format!("{}[{:<8}] ", self.marker(row), r.label()), self.focus_style(row).patch(role_style)),
                Span::styled(format!("{:<9}{:>9}  {:<5} {}", d.kernel, d.size_human(), d.transport, d.model), self.focus_style(row)),
            ]));
        }
        f.render_widget(Paragraph::new(lines), l);
        let help = match self.focused() {
            Row::Disk(i) => format!("{}\nSYSTEM: the operating system and every app's data live here (erased). data: a storage disk, erased and pooled under /mnt/media with any other data disk. parity: SnapRAID parity for the data disks, at least as large as the largest one. not used: left alone.", self.disks[i].id.display()),
            _ => "Continue needs one SYSTEM disk.".to_string(),
        };
        self.help(f, h, "about this disk", &help);
    }

    fn draw_profile(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 2, 5);
        self.intro(f, i, "The machine's name and the account you will log in with. Move to a line, press Enter to type, Enter again to keep it.");
        let lines: Vec<Line> = self.profile.iter().enumerate().map(|(idx, fld)| self.field_line(Row::Field(idx), fld.label, fld.shown())).collect();
        f.render_widget(Paragraph::new(lines), l);
        let (title, text) = match self.focused() {
            Row::Field(i) => {
                let fld = &self.profile[i];
                let extra = if fld.key == "password" && fld.value.is_empty() && self.admin_hash.is_some() { " (the password from the last run is kept unless you type a new one)" } else { "" };
                (fld.label, format!("{}{extra}", fld.help))
            }
            _ => ("Continue", "Checks that the name is valid and the two passwords match.".to_string()),
        };
        self.help(f, h, title, &text);
    }

    fn draw_ssh(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 3, 5);
        self.intro(f, i, "SSH is how you reach the machine from another computer without sitting at it. Add the public keys that may log in as the admin: import them from GitHub, or paste one. Space on a key removes it. Skipping is allowed; then only the machine's own screen works.");
        let mut lines = vec![self.field_line(Row::Field(0), self.github_user.label, self.github_user.shown()), self.field_line(Row::Field(1), self.pasted_key.label, self.pasted_key.shown()), Line::from("")];
        lines.push(Line::from(Span::styled(format!("  keys that may log in ({}):", self.keys.len()), Style::default().add_modifier(Modifier::UNDERLINED))));
        for (idx, k) in self.keys.iter().enumerate() {
            let row = Row::Key(idx);
            lines.push(Line::from(Span::styled(format!("{}{}", self.marker(row), key_summary(k)), self.focus_style(row))));
        }
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), l);
        let (title, text) = match self.focused() {
            Row::Field(0) => (self.github_user.label, self.github_user.help.to_string()),
            Row::Field(1) => (self.pasted_key.label, self.pasted_key.help.to_string()),
            Row::Key(i) => ("this key", format!("{}\nSpace removes it.", self.keys[i])),
            _ => ("Continue", "The keys go into the admin account's authorized_keys on the new system.".to_string()),
        };
        self.help(f, h, title, &text);
    }

    fn draw_domain(&self, f: &mut Frame, area: Rect) {
        let needs = self.closed().iter().any(|m| m == "acme");
        let rows_n = (self.domain.len() + self.secrets.len()) as u16 + 1;
        let (i, l, h) = Self::split(area, 4, area.height.saturating_sub(4 + rows_n).max(6));
        self.intro(f, i, if needs {
            "Every app gets a name under your domain and a real certificate, so browsers trust it. For this release the domain's DNS must be at Cloudflare (register there, or move a domain's nameservers there). The token is checked against your domain the moment you enter it. A kit with a VPN asks for that account here too."
        } else {
            "Nothing you chose needs a domain. Continue."
        });
        let mut lines: Vec<Line> = self.domain.iter().enumerate().map(|(idx, fld)| self.field_line(Row::Field(idx), fld.label, fld.shown())).collect();
        for (idx, r) in self.secrets.iter().enumerate() {
            let row = Row::Secret(idx);
            let label = match r.option.as_str() {
                "homelab.acme.credentialsFile" => "Cloudflare API token".to_string(),
                "homelab.arrStack.vpnEnvFile" => "VPN account".to_string(),
                "homelab.backup.remote.environmentFile" => "Offsite backup account".to_string(),
                "homelab.meshagent.mshFile" => "MeshCentral agent file".to_string(),
                o => o.rsplit('.').next().unwrap_or(o).to_string(),
            };
            let (state, style) = match (&r.file, &r.verified) {
                (None, _) => ("— (press Enter and type it)".to_string(), Style::default()),
                (Some(_), Some(Ok(m))) => (format!("ok: {m}"), Style::default().fg(Color::Green)),
                (Some(_), Some(Err(m))) => (format!("FAILED: {m}"), Style::default().fg(Color::Red)),
                (Some(p), None) => (format!("saved: {}", p.display()), Style::default().fg(Color::Green)),
            };
            lines.push(Line::from(vec![Span::styled(format!("{}{:<24}", self.marker(row), label), self.focus_style(row).add_modifier(Modifier::BOLD)), Span::styled(state, self.focus_style(row).patch(style))]));
        }
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), l);
        let values: BTreeMap<String, String> = self.values_json().into_iter().map(|(k, v)| (k, value_text(&v))).collect();
        let (title, text) = match self.focused() {
            Row::Field(i) => (self.domain[i].label.to_string(), self.domain[i].help.to_string()),
            Row::Secret(i) => {
                let r = &self.secrets[i];
                match crate::guides::for_option(&r.option, &values) {
                    Some(g) => (g.title.to_string(), format!("{}\n\nthe file must carry: {}", g.steps, r.keys)),
                    None => (r.option.clone(), format!("Press Enter and type the value. The file must carry: {}", r.keys)),
                }
            }
            _ => ("Continue".to_string(), "Needs the domain, the email and every value on this screen.".to_string()),
        };
        self.help(f, h, &title, &text);
    }

    fn draw_extras(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 2, 5);
        let (_, verdict, short) = self.memory_verdict();
        self.intro(f, i, &format!("The kit's modules, to adjust if you like: Space turns one on or off. {verdict}"));
        let open = self.open_values();
        let mut lines = Vec::new();
        for (idx, (name, val, _)) in open.iter().enumerate() {
            let row = Row::Value(idx);
            lines.push(Line::from(vec![Span::styled(format!("{}! {:<40}", self.marker(row), name), self.focus_style(row).fg(Color::Red).add_modifier(Modifier::BOLD)), Span::styled(if val.is_empty() { "— (needs a value)".to_string() } else { val.clone() }, self.focus_style(row))]));
        }
        // Keep the focused module on screen.
        let visible = l.height.saturating_sub(open.len() as u16) as usize;
        let focus_idx = match self.focused() { Row::Module(i) => i, _ => 0 };
        let start = if visible == 0 { 0 } else { focus_idx.saturating_sub(visible.saturating_sub(1)) };
        for (idx, m) in self.modules.iter().enumerate().skip(start) {
            let row = Row::Module(idx);
            let mark = if m.locked { "■" } else if m.chosen { "x" } else { " " };
            lines.push(Line::from(vec![
                Span::styled(format!("{}[{mark}] {:<22}", self.marker(row), m.name), self.focus_style(row).add_modifier(if m.locked { Modifier::DIM } else { Modifier::BOLD })),
                Span::styled(m.description.clone(), self.focus_style(row)),
            ]));
        }
        f.render_widget(Paragraph::new(lines), l);
        let (title, text) = match self.focused() {
            Row::Value(i) => (open[i].0.clone(), open[i].2.clone()),
            Row::Module(i) => {
                let m = &self.modules[i];
                let (mem, req) = self.schema.catalog.get(&m.name).map(|c| (c.memory, c.requires.join(" "))).unwrap_or_default();
                (m.name.clone(), format!("{}\nmemory ~{mem} MiB{}{}", m.description, if req.is_empty() { String::new() } else { format!(" · needs: {req}") }, if m.locked { " · foundation, always on" } else { "" }))
            }
            _ => ("Continue".to_string(), if short { "The machine is short of memory for this set: drop a module, or go on and expect swapping.".to_string() } else { "Everything chosen fits this machine.".to_string() }),
        };
        self.help(f, h, &title, &text);
    }

    fn draw_review(&self, f: &mut Frame, area: Rect) {
        let a = self.answers();
        let closed = self.closed();
        let (_, verdict, short) = self.memory_verdict();
        let erased: Vec<String> = std::iter::once(a.host.disk.clone()).chain(a.host.data_disks.iter().map(|d| d.device.clone())).filter(|d| !d.is_empty()).collect();
        let domain = self.domain.iter().find(|f| f.key == "homelab.domain").map(|f| f.value.clone()).unwrap_or_default();
        let bold = Style::default().add_modifier(Modifier::BOLD);
        let mut lines = vec![
            Line::from(vec![Span::styled("Machine   ", bold), Span::raw(format!("{} · time zone {} · admin `{}`{}", a.host.name, a.host.time_zone, self.field("adminUser"), if self.field("password").is_empty() && self.admin_hash.is_none() { " · password minted into FIRST-LOGIN.md" } else { "" }))]),
            Line::from(vec![Span::styled("Kit       ", bold), Span::raw(format!("{} · {} module(s): {}", self.kits[self.kit].name, closed.len(), closed.join(" ")))]),
            Line::from(vec![Span::styled("Memory    ", bold), Span::styled(verdict, Style::default().fg(if short { Color::Red } else { Color::Green }))]),
            Line::from(vec![Span::styled("SSH keys  ", bold), Span::raw(if self.keys.is_empty() { "none (console login only)".to_string() } else { self.keys.iter().map(|k| key_summary(k)).collect::<Vec<_>>().join("; ") })]),
            Line::from(vec![Span::styled("Domain    ", bold), Span::raw(if domain.is_empty() { "—".to_string() } else { format!("{domain} · {} secret file(s) saved", self.secrets.iter().filter(|r| r.file.is_some()).count()) })]),
            Line::from(""),
            Line::from(vec![Span::styled("ERASED    ", Style::default().fg(Color::Red).add_modifier(Modifier::BOLD)), Span::styled(erased.join("  "), Style::default().fg(Color::Red))]),
            Line::from(format!("          system on {} · {} data disk(s)", if a.host.disk.is_empty() { "—" } else { &a.host.disk }, a.host.data_disks.len())),
            Line::from(""),
            Line::from(format!("answers → {}    configuration → {}", self.out_answers.display(), self.out_dir.display())),
            Line::from(""),
            Line::from(if self.live_usb { "Install writes the configuration, checks it, partitions the disks, downloads the system onto the new disk and installs it. Twenty to forty minutes on a home connection; the screen shows what it is doing." } else { "Write the configuration, then run the install command it prints (or nixos-anywhere from another machine)." }),
        ];
        if !self.status.is_empty() && self.status_is_error {
            lines.push(Line::from(""));
            lines.push(Line::styled(self.status.clone(), Style::default().fg(Color::Red)));
        }
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
    }

    fn draw_install(&self, f: &mut Frame, area: Rect) {
        let lines = self.progress.as_ref().map(|p| p.lines.lock().unwrap().clone()).unwrap_or_default();
        let h = area.height.saturating_sub(2) as usize;
        let start = lines.len().saturating_sub(h);
        let text: Vec<Line> = lines[start..].iter().map(|l| Line::from(l.clone())).collect();
        f.render_widget(Paragraph::new(text).block(Block::default().borders(Borders::ALL).title(format!(" installing · {} lines so far · do not turn the machine off ", lines.len()))), area);
    }

    fn draw_done(&self, f: &mut Frame, area: Rect) {
        // Success: the install's own report, from "installed <host>" on.
        // Failure: the last screenful, which is where the error is.
        let screenful = area.height.saturating_sub(5) as usize;
        let start = if self.status_is_error {
            self.install_report.len().saturating_sub(screenful)
        } else {
            self.install_report.iter().rposition(|l| l.starts_with("installed ") || l.starts_with("wrote ")).unwrap_or(self.install_report.len().saturating_sub(screenful))
        };
        let mut lines: Vec<Line> = Vec::new();
        if self.status_is_error {
            lines.push(Line::styled(self.status.clone(), Style::default().fg(Color::Red).add_modifier(Modifier::BOLD)));
            lines.push(Line::from("Press Close; the full output is in the terminal scrollback, and `homelab-configure tui` opens this form again with your answers in it."));
            lines.push(Line::from(""));
        }
        lines.extend(self.install_report[start..].iter().map(|l| Line::from(l.clone())));
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }).block(Block::default().borders(Borders::ALL).title(" what happened ")), area);
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
fn validate_key(text: &str) -> Result<String, String> {
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
fn same_key(a: &str, b: &str) -> bool {
    let core = |s: &str| s.split_whitespace().take(2).collect::<Vec<_>>().join(" ");
    core(a) == core(b)
}

/// `ssh-ed25519 …Abcd (comment)` — the key as a person recognises it.
fn key_summary(k: &str) -> String {
    let mut parts = k.split_whitespace();
    let kind = parts.next().unwrap_or("");
    let blob = parts.next().unwrap_or("");
    let comment: Vec<&str> = parts.collect();
    let tail = if blob.len() > 12 { &blob[blob.len() - 12..] } else { blob };
    format!("{kind} …{tail}{}", if comment.is_empty() { String::new() } else { format!("  ({})", comment.join(" ")) })
}

fn valid_domain(d: &str) -> bool {
    let labels: Vec<&str> = d.split('.').collect();
    labels.len() >= 2
        && labels.iter().all(|l| !l.is_empty() && l.len() <= 63 && l.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-') && !l.starts_with('-') && !l.ends_with('-'))
        && labels.last().map(|t| t.chars().all(|c| c.is_ascii_alphabetic()) && t.len() >= 2).unwrap_or(false)
}

fn fetch_github_keys(user: &str) -> Result<Vec<String>> {
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
/// answers — shown on the Welcome screen, decided once at start.
fn probe_network() -> (String, Option<bool>) {
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

fn value_text(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// A typed value: JSON when it parses (lists, objects, numbers, booleans),
/// a string otherwise.
fn parse_value(text: &str) -> serde_json::Value {
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

    #[test]
    fn editor_text_becomes_json_or_string() {
        assert_eq!(parse_value("[1,2]"), serde_json::json!([1, 2]));
        assert_eq!(parse_value("true"), serde_json::json!(true));
        assert_eq!(parse_value("example.com"), serde_json::json!("example.com"));
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

    #[test]
    fn domains_are_checked() {
        assert!(valid_domain("example.com"));
        assert!(valid_domain("my-lab.example.co.uk"));
        assert!(!valid_domain("Example.com"));
        assert!(!valid_domain("https://example.com"));
        assert!(!valid_domain("localhost"));
    }
}
