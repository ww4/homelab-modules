//! The terminal front end. A thin skin over the schema: it produces an
//! answers file (and the `--secret` flags for what only the user can supply),
//! then hands off to `generate`. Nothing lives only here — anything the TUI
//! can do, the headless command can do from the same answers file.
//!
//! Six screens, Tab / Shift-Tab between them:
//!   1 Host      name, time zone, SSH key (and disks by hand, if the picker cannot see them)
//!   2 Disks     every disk by stable id; Space cycles its role: system / data / parity
//!   3 Modules   the catalog; Space toggles; foundation entries are locked
//!   4 Values    every homelab.* option the chosen modules read; Enter edits
//!   5 Secrets   file paths for what only you can supply, or a typed value saved to a 600 file
//!   6 Review    write answers.json (w) or write and run generate (g)

use anyhow::{Context, Result};
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::ExecutableCommand;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, List, ListItem, ListState, Paragraph, Tabs, Wrap};
use ratatui::{Frame, Terminal};
use std::collections::BTreeMap;
use std::io;
use std::path::{Path, PathBuf};

use crate::answers::Answers;
use crate::disks::{self, Disk};
use crate::plan::{FOUNDATION_ALWAYS, FOUNDATION_REQUIRED};
use crate::schema::{Schema, Source};

const SCREENS: [&str; 7] = ["1 Kit", "2 Host", "3 Disks", "4 Modules", "5 Values", "6 Secrets", "7 Review"];
const KIT: usize = 0;
const HOST: usize = 1;
const DISKS: usize = 2;
const MODULES: usize = 3;
const VALUES: usize = 4;
const SECRETS: usize = 5;
const REVIEW: usize = 6;

/// A kit is a module list with a name: the first screen, so a first-time
/// user picks a whole homelab in one keystroke and only then sees the
/// module list it expands to. The three bigger kits are the canned profiles
/// the VM test installs (modules only; their example values are not taken).
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
            Role::Unused => "      ",
            Role::System => "SYSTEM",
            Role::Data => "data  ",
            Role::Parity => "parity",
        }
    }
}

struct Field {
    key: &'static str,
    label: &'static str,
    help: &'static str,
    value: String,
}

struct ModuleRow {
    name: String,
    description: String,
    chosen: bool,
    locked: bool,
    requires: Vec<String>,
}

struct App<'a> {
    schema: &'a Schema,
    screen: usize,
    kits: Vec<Kit>,
    kit_cursor: usize,
    kit: Option<usize>,
    /// The admin password hash carried over from an earlier run (load());
    /// kept unless a new password is typed.
    admin_hash: Option<String>,
    host: Vec<Field>,
    host_cursor: usize,
    disks: Vec<Disk>,
    roles: Vec<Role>,
    disk_cursor: usize,
    /// Secrets screen: editing a VALUE (written to a 600 file) rather than a path.
    editing_value: bool,
    modules: Vec<ModuleRow>,
    module_cursor: usize,
    values: BTreeMap<String, String>,
    value_cursor: usize,
    secrets: BTreeMap<String, String>,
    secret_cursor: usize,
    editing: Option<String>,
    status: String,
    out_answers: PathBuf,
    out_dir: PathBuf,
    done: Option<Outcome>,
}

enum Outcome {
    Quit,
    Written,
    Generate,
}

pub fn run(schema: &Schema, profile: Option<&Path>, out_answers: &Path, out_dir: &Path) -> Result<Option<Vec<String>>> {
    let mut app = App::new(schema, out_answers, out_dir);
    if let Some(p) = profile {
        let text = std::fs::read_to_string(p).with_context(|| format!("reading {}", p.display()))?;
        let a: Answers = serde_json::from_str(&text).with_context(|| format!("parsing {}", p.display()))?;
        app.load(&a);
    }

    enable_raw_mode()?;
    io::stdout().execute(EnterAlternateScreen)?;
    let backend = ratatui::backend::CrosstermBackend::new(io::stdout());
    let mut terminal = Terminal::new(backend)?;
    let result = app.event_loop(&mut terminal);
    disable_raw_mode()?;
    io::stdout().execute(LeaveAlternateScreen)?;
    result?;

    match app.done {
        Some(Outcome::Generate) => {
            let answers = app.answers();
            write_answers(&answers, &app.out_answers)?;
            let mut argv = vec![
                "generate".to_string(),
                "--answers".into(),
                app.out_answers.display().to_string(),
                "--out".into(),
                app.out_dir.display().to_string(),
            ];
            for (opt, path) in &app.secrets {
                if !path.trim().is_empty() {
                    argv.push("--secret".into());
                    argv.push(format!("{opt}=@{}", path.trim()));
                }
            }
            Ok(Some(argv))
        }
        Some(Outcome::Written) => {
            let answers = app.answers();
            write_answers(&answers, &app.out_answers)?;
            println!("wrote {}", app.out_answers.display());
            println!("next: homelab-configure generate --answers {} --out {}{}", app.out_answers.display(), app.out_dir.display(), app.secret_flags());
            Ok(None)
        }
        _ => Ok(None),
    }
}

fn write_answers(a: &Answers, path: &Path) -> Result<()> {
    std::fs::write(path, serde_json::to_string_pretty(a)? + "\n").with_context(|| format!("writing {}", path.display()))
}

impl<'a> App<'a> {
    fn new(schema: &'a Schema, out_answers: &Path, out_dir: &Path) -> Self {
        let host = vec![
            Field { key: "name", label: "Host name", help: "letters, digits and dashes", value: "box".into() },
            Field { key: "adminPassword", label: "Admin password", help: "what you type to log in at the machine's own screen as the admin user (and for the first login of apps that use it — FIRST-LOGIN.md says which). Leave it empty and one is minted and written to FIRST-LOGIN.md.", value: String::new() },
            Field { key: "timeZone", label: "Time zone", help: "an IANA name, e.g. America/New_York", value: "UTC".into() },
            Field { key: "disk", label: "System disk", help: "/dev/disk/by-id/… — ERASED by the install", value: String::new() },
            Field { key: "dataDisks", label: "Data disks", help: "name=/dev/disk/by-id/…, comma-separated; each mounts at /mnt/disks/<name>", value: String::new() },
            Field { key: "sshAuthorizedKeys", label: "SSH public key", help: "your key; without it the admin can only log in at the console", value: String::new() },
            Field { key: "githubUser", label: "GitHub user", help: "type a GitHub username and press Enter: its public keys are fetched into the field above (the way ssh-import-id does)", value: String::new() },
        ];
        let mut modules: Vec<ModuleRow> = schema
            .catalog
            .iter()
            .filter(|(n, _)| n.as_str() != "options")
            .map(|(n, m)| ModuleRow {
                name: n.clone(),
                description: m.description.clone(),
                chosen: FOUNDATION_ALWAYS.contains(&n.as_str()),
                locked: FOUNDATION_ALWAYS.contains(&n.as_str()),
                requires: m.requires.clone(),
            })
            .collect();
        modules.sort_by(|a, b| (!a.locked, &a.name).cmp(&(!b.locked, &b.name)));
        let disks = disks::list();
        let roles = vec![Role::Unused; disks.len()];
        App {
            schema,
            screen: 0,
            kits: kits(),
            kit_cursor: 0,
            kit: None,
            admin_hash: None,
            host,
            host_cursor: 0,
            disks,
            roles,
            disk_cursor: 0,
            editing_value: false,
            modules,
            module_cursor: 0,
            values: BTreeMap::new(),
            value_cursor: 0,
            secrets: BTreeMap::new(),
            secret_cursor: 0,
            editing: None,
            status: "Tab/Shift-Tab: screens · ↑↓: move · Space: toggle/cycle · Enter: edit · q: quit".into(),
            out_answers: out_answers.to_path_buf(),
            out_dir: out_dir.to_path_buf(),
            done: None,
        }
    }

    fn load(&mut self, a: &Answers) {
        self.admin_hash = a.host.admin_password_hash.clone();
        for f in &mut self.host {
            f.value = match f.key {
                "name" => a.host.name.clone(),
                "timeZone" => a.host.time_zone.clone(),
                "disk" => a.host.disk.clone(),
                "dataDisks" => a.host.data_disks.iter().map(|d| format!("{}={}", d.name, d.device)).collect::<Vec<_>>().join(", "),
                "sshAuthorizedKeys" => a.host.ssh_authorized_keys.join(" "),
                _ => String::new(),
            };
        }
        for m in &mut self.modules {
            if a.modules.contains(&m.name) {
                m.chosen = true;
            }
        }
        for (k, v) in &a.values {
            self.values.insert(k.clone(), value_text(v));
        }
    }

    /// Apply the kit under the cursor: its modules on, everything else off,
    /// the foundation untouched.
    fn apply_kit(&mut self) {
        let k = &self.kits[self.kit_cursor];
        for m in &mut self.modules {
            if !m.locked {
                m.chosen = k.modules.contains(&m.name);
            }
        }
        self.kit = Some(self.kit_cursor);
        self.status = format!("{}: {} module(s) chosen · Tab → the machine", k.name, self.chosen().len());
    }

    fn chosen(&self) -> Vec<String> {
        self.modules.iter().filter(|m| m.chosen).map(|m| m.name.clone()).collect()
    }

    /// The closure the plan will use, so Values and Secrets show what the
    /// whole set reads, not only the rows the user ticked.
    fn closed(&self) -> Vec<String> {
        self.schema.close_over_requires(&self.chosen()).map(|(all, _)| all).unwrap_or_else(|_| self.chosen())
    }

    fn value_rows(&self) -> Vec<(String, String, bool, String)> {
        let mods = self.closed();
        let secret_opts: Vec<String> = self.secret_metas().into_iter().map(|(o, _)| o).collect();
        let mut rows: Vec<(String, String, bool, String)> = self
            .schema
            .options_for_modules(&mods)
            .into_iter()
            .filter(|o| !o.name.ends_with("File") && !secret_opts.contains(&o.name))
            .map(|o| {
                let current = self.values.get(&o.name).cloned().unwrap_or_default();
                let shown = if current.is_empty() { o.default_str().map(|d| format!("(default {d})")).unwrap_or_default() } else { current };
                (o.name.clone(), shown, o.required() && !self.values.contains_key(&o.name), o.description.clone().unwrap_or_default())
            })
            .collect();
        rows.sort_by(|a, b| (!a.2, &a.0).cmp(&(!b.2, &b.0)));
        rows
    }

    fn secret_metas(&self) -> Vec<(String, String)> {
        let mut out = Vec::new();
        for m in self.closed() {
            if let Some(meta) = self.schema.catalog.get(&m) {
                for s in &meta.secrets {
                    if s.source == Source::Supply && s.option.starts_with("homelab.") {
                        out.push((s.option.clone(), s.keys.join(", ")));
                    }
                }
            }
        }
        out
    }

    fn secret_flags(&self) -> String {
        self.secrets
            .iter()
            .filter(|(_, p)| !p.trim().is_empty())
            .map(|(o, p)| format!(" --secret {o}=@{}", p.trim()))
            .collect()
    }

    fn answers(&self) -> Answers {
        let field = |k: &str| self.host.iter().find(|f| f.key == k).map(|f| f.value.trim().to_string()).unwrap_or_default();
        // Roles chosen on the Disks screen win over the typed fields.
        let picked_system = self.disks.iter().zip(&self.roles).find(|(_, r)| **r == Role::System).map(|(d, _)| d.id.display().to_string());
        let mut picked_data: Vec<serde_json::Value> = Vec::new();
        let mut n = 0;
        for (d, r) in self.disks.iter().zip(&self.roles) {
            match r {
                Role::Data => {
                    n += 1;
                    picked_data.push(serde_json::json!({ "name": format!("d{n}"), "device": d.id.display().to_string() }));
                }
                Role::Parity => picked_data.push(serde_json::json!({ "name": "parity", "device": d.id.display().to_string() })),
                _ => {}
            }
        }
        let system_disk = picked_system.unwrap_or_else(|| field("disk"));
        let data_disks: Vec<serde_json::Value> = if picked_data.is_empty() {
            field("dataDisks")
                .split(',')
                .filter_map(|s| s.trim().split_once('='))
                .map(|(n, d)| serde_json::json!({ "name": n.trim(), "device": d.trim() }))
                .collect()
        } else {
            picked_data
        };
        let keys = split_keys(&field("sshAuthorizedKeys"));
        // A typed password is hashed here and only the hash travels in the
        // answers; an untyped one keeps the hash from the last run, if any.
        let typed = field("adminPassword");
        let admin_hash = if typed.is_empty() { self.admin_hash.clone() } else { crate::emit::mkpasswd(&typed).ok() };
        let mut values = serde_json::Map::new();
        for (k, v) in &self.values {
            if !v.trim().is_empty() {
                values.insert(k.clone(), parse_value(v));
            }
        }
        let json = serde_json::json!({
            "host": {
                "name": field("name"),
                "timeZone": field("timeZone"),
                "disk": system_disk,
                "dataDisks": data_disks,
                "sshAuthorizedKeys": keys,
                "adminPasswordHash": admin_hash,
            },
            "modules": self.chosen(),
            "values": values,
        });
        serde_json::from_value(json).expect("answers shape")
    }

    fn event_loop(&mut self, terminal: &mut Terminal<ratatui::backend::CrosstermBackend<io::Stdout>>) -> Result<()> {
        loop {
            terminal.draw(|f| self.draw(f))?;
            if let Event::Key(key) = event::read()? {
                if key.kind != KeyEventKind::Press {
                    continue;
                }
                self.on_key(key);
                if self.done.is_some() {
                    return Ok(());
                }
            }
        }
    }

    fn on_key(&mut self, key: KeyEvent) {
        if let Some(buf) = &mut self.editing {
            match key.code {
                KeyCode::Esc => self.editing = None,
                KeyCode::Enter => {
                    let text = buf.clone();
                    self.editing = None;
                    self.commit_edit(text);
                }
                KeyCode::Backspace => {
                    buf.pop();
                }
                KeyCode::Char(c) => buf.push(c),
                _ => {}
            }
            return;
        }
        match key.code {
            KeyCode::Char('q') => self.done = Some(Outcome::Quit),
            KeyCode::Tab => self.screen = (self.screen + 1) % SCREENS.len(),
            KeyCode::BackTab => self.screen = (self.screen + SCREENS.len() - 1) % SCREENS.len(),
            KeyCode::Char(c @ '1'..='7') if key.modifiers.is_empty() && self.screen != VALUES && self.screen != HOST => self.screen = (c as u8 - b'1') as usize,
            KeyCode::Up => self.move_cursor(-1),
            KeyCode::Down => self.move_cursor(1),
            KeyCode::Char(' ') | KeyCode::Enter if self.screen == KIT => self.apply_kit(),
            KeyCode::Char(' ') if self.screen == DISKS => {
                if let Some(d) = self.disks.get(self.disk_cursor) {
                    if !d.in_use {
                        let r = self.roles[self.disk_cursor].next();
                        if r == Role::System {
                            // One system disk: demote any other.
                            for other in self.roles.iter_mut() {
                                if *other == Role::System {
                                    *other = Role::Unused;
                                }
                            }
                        }
                        self.roles[self.disk_cursor] = r;
                    }
                }
            }
            KeyCode::Char(' ') if self.screen == MODULES => {
                if let Some(m) = self.modules.get_mut(self.module_cursor) {
                    if !m.locked {
                        m.chosen = !m.chosen;
                    }
                }
            }
            KeyCode::Char('v') if self.screen == SECRETS => {
                if self.secret_metas().get(self.secret_cursor).is_some() {
                    self.editing_value = true;
                    self.editing = Some(String::new());
                }
            }
            KeyCode::Enter => self.start_edit(),
            KeyCode::Char('w') if self.screen == REVIEW => self.done = Some(Outcome::Written),
            KeyCode::Char('g') if self.screen == REVIEW => self.done = Some(Outcome::Generate),
            _ => {}
        }
    }

    fn move_cursor(&mut self, d: i32) {
        let len = match self.screen {
            KIT => self.kits.len(),
            HOST => self.host.len(),
            DISKS => self.disks.len(),
            MODULES => self.modules.len(),
            VALUES => self.value_rows().len(),
            SECRETS => self.secret_metas().len(),
            _ => return,
        };
        let cur = match self.screen {
            KIT => &mut self.kit_cursor,
            HOST => &mut self.host_cursor,
            DISKS => &mut self.disk_cursor,
            MODULES => &mut self.module_cursor,
            VALUES => &mut self.value_cursor,
            _ => &mut self.secret_cursor,
        };
        if len == 0 {
            return;
        }
        *cur = ((*cur as i32 + d).rem_euclid(len as i32)) as usize;
    }

    fn start_edit(&mut self) {
        match self.screen {
            HOST => self.editing = Some(self.host[self.host_cursor].value.clone()),
            VALUES => {
                if let Some((name, _, _, _)) = self.value_rows().get(self.value_cursor) {
                    self.editing = Some(self.values.get(name).cloned().unwrap_or_default());
                }
            }
            SECRETS => {
                if let Some((opt, _)) = self.secret_metas().get(self.secret_cursor) {
                    self.editing_value = false;
                    self.editing = Some(self.secrets.get(opt).cloned().unwrap_or_default());
                }
            }
            _ => {}
        }
    }

    fn commit_edit(&mut self, text: String) {
        match self.screen {
            HOST => {
                let key = self.host[self.host_cursor].key;
                self.host[self.host_cursor].value = text.clone();
                if key == "githubUser" && !text.trim().is_empty() {
                    match fetch_github_keys(text.trim()) {
                        Ok(keys) if keys.is_empty() => self.status = format!("github.com/{} has no public keys", text.trim()),
                        Ok(keys) => {
                            let n = keys.len();
                            if let Some(f) = self.host.iter_mut().find(|f| f.key == "sshAuthorizedKeys") {
                                f.value = keys.join(" ");
                            }
                            self.status = format!("{n} key(s) from github.com/{}", text.trim());
                        }
                        Err(e) => self.status = format!("could not fetch keys: {e}"),
                    }
                }
            }
            VALUES => {
                if let Some((name, _, _, _)) = self.value_rows().get(self.value_cursor).cloned() {
                    if text.trim().is_empty() {
                        self.values.remove(&name);
                    } else {
                        self.values.insert(name, text);
                    }
                }
            }
            SECRETS => {
                if let Some((opt, _)) = self.secret_metas().get(self.secret_cursor).cloned() {
                    if self.editing_value {
                        // A typed value goes to a 600 file next to the answers;
                        // the answers file itself never carries it.
                        self.editing_value = false;
                        let dir = self.out_answers.parent().map(|p| p.to_path_buf()).unwrap_or_else(|| PathBuf::from(".")).join(".secrets");
                        let file = dir.join(crate::secrets::secret_name(&opt));
                        // The guide knows the file's shape (a bare Cloudflare token
                        // becomes its env line) and, where an API allows, checks it.
                        let content = crate::guides::shape(&opt, &text);
                        let written = std::fs::create_dir_all(&dir)
                            .map_err(|e| e.to_string())
                            .and_then(|_| {
                                let _ = std::fs::remove_file(&file);
                                crate::secrets::write_private(&file, &content).map_err(|e| e.to_string())
                            });
                        match written {
                            Ok(()) => {
                                self.secrets.insert(opt.clone(), file.display().to_string());
                                self.status = match crate::guides::verify(&opt, &content, &self.values) {
                                    Some(Ok(m)) => format!("{m} · wrote {}", file.display()),
                                    Some(Err(m)) => format!("NOT verified: {m} · wrote {}", file.display()),
                                    None => format!("wrote {} (mode 600)", file.display()),
                                };
                            }
                            Err(e) => self.status = format!("could not write the secret file: {e}"),
                        }
                    } else {
                        self.secrets.insert(opt, text);
                    }
                }
            }
            _ => {}
        }
    }

    fn draw(&self, f: &mut Frame) {
        let outer = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(3), Constraint::Min(5), Constraint::Length(3)])
            .split(f.area());
        let tabs = Tabs::new(SCREENS.iter().map(|s| Line::from(*s)).collect::<Vec<_>>())
            .select(self.screen)
            .block(Block::default().borders(Borders::ALL).title(" homelab-configure "))
            .highlight_style(Style::default().add_modifier(Modifier::BOLD).fg(Color::Yellow));
        f.render_widget(tabs, outer[0]);
        match self.screen {
            KIT => self.draw_kit(f, outer[1]),
            HOST => self.draw_host(f, outer[1]),
            DISKS => self.draw_disks(f, outer[1]),
            MODULES => self.draw_modules(f, outer[1]),
            VALUES => self.draw_values(f, outer[1]),
            SECRETS => self.draw_secrets(f, outer[1]),
            _ => self.draw_review(f, outer[1]),
        }
        let footer = match &self.editing {
            Some(buf) => Line::from(vec![Span::styled("edit: ", Style::default().fg(Color::Yellow)), Span::raw(buf.clone()), Span::styled("▏", Style::default().fg(Color::Yellow)), Span::raw("   Enter: keep · Esc: cancel")]),
            None => Line::from(self.status.clone()),
        };
        f.render_widget(Paragraph::new(footer).block(Block::default().borders(Borders::ALL)), outer[2]);
    }

    fn split_list(area: Rect) -> (Rect, Rect) {
        let v = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Min(3), Constraint::Length(6)])
            .split(area);
        (v[0], v[1])
    }

    fn draw_host(&self, f: &mut Frame, area: Rect) {
        let (list_area, help_area) = Self::split_list(area);
        let items: Vec<ListItem> = self
            .host
            .iter()
            .map(|fld| {
                let shown = if fld.key == "adminPassword" {
                    if !fld.value.is_empty() { "•".repeat(fld.value.chars().count()) } else if self.admin_hash.is_some() { "(kept from the last run)".to_string() } else { "— (one will be minted)".to_string() }
                } else if fld.value.is_empty() {
                    "—".to_string()
                } else {
                    fld.value.clone()
                };
                ListItem::new(Line::from(vec![Span::styled(format!("{:<16}", fld.label), Style::default().add_modifier(Modifier::BOLD)), Span::raw(shown)]))
            })
            .collect();
        let mut st = ListState::default();
        st.select(Some(self.host_cursor));
        f.render_stateful_widget(
            List::new(items).block(Block::default().borders(Borders::ALL).title(" The machine ")).highlight_style(Style::default().bg(Color::DarkGray)),
            list_area,
            &mut st,
        );
        let help = &self.host[self.host_cursor].help;
        f.render_widget(Paragraph::new(help.to_string()).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(" about this field ")), help_area);
    }

    fn draw_kit(&self, f: &mut Frame, area: Rect) {
        let (list_area, help_area) = Self::split_list(area);
        let items: Vec<ListItem> = self
            .kits
            .iter()
            .enumerate()
            .map(|(i, k)| {
                let mark = if self.kit == Some(i) { "●" } else { "○" };
                ListItem::new(Line::from(vec![Span::raw(format!("{mark} ")), Span::styled(format!("{:<16}", k.name), Style::default().add_modifier(Modifier::BOLD)), Span::raw(k.blurb)]))
            })
            .collect();
        let mut st = ListState::default();
        st.select(Some(self.kit_cursor));
        f.render_stateful_widget(
            List::new(items).block(Block::default().borders(Borders::ALL).title(" What do you want this machine to be? (Space or Enter picks; change your mind any time) ")).highlight_style(Style::default().bg(Color::DarkGray)),
            list_area,
            &mut st,
        );
        let k = &self.kits[self.kit_cursor];
        let help = if k.modules.is_empty() { "No modules until you pick them on the Modules screen.".to_string() } else { format!("modules: {}", k.modules.join(" ")) };
        f.render_widget(Paragraph::new(help).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(" what it turns on ")), help_area);
    }

    fn draw_disks(&self, f: &mut Frame, area: Rect) {
        let (list_area, help_area) = Self::split_list(area);
        let items: Vec<ListItem> = self
            .disks
            .iter()
            .zip(&self.roles)
            .map(|(d, r)| {
                let style = if d.in_use { Style::default().fg(Color::DarkGray) } else { Style::default() };
                let role = if d.in_use { "in use".to_string() } else { r.label().to_string() };
                ListItem::new(Line::from(vec![
                    Span::styled(format!("[{role}] "), style.add_modifier(Modifier::BOLD).fg(match r { Role::System => Color::Yellow, Role::Data => Color::Green, Role::Parity => Color::Cyan, Role::Unused => Color::Reset })),
                    Span::styled(format!("{:<8}{:>9}  {:<5} ", d.kernel, d.size_human(), d.transport), style),
                    Span::styled(d.model.clone(), style),
                ]))
            })
            .collect();
        let mut st = ListState::default();
        st.select(Some(self.disk_cursor.min(self.disks.len().saturating_sub(1))));
        let title = if self.disks.is_empty() { " Disks — none visible under /dev/disk/by-id (type them on the Host screen) ".to_string() } else { " Disks — Space cycles: SYSTEM (erased, holds the OS) → data → parity → unused ".to_string() };
        f.render_stateful_widget(
            List::new(items).block(Block::default().borders(Borders::ALL).title(title)).highlight_style(Style::default().bg(Color::DarkGray)),
            list_area,
            &mut st,
        );
        let help = match self.disks.get(self.disk_cursor) {
            Some(d) if d.in_use => "this disk holds the running system (or a mounted filesystem) and is not offered".to_string(),
            Some(d) => format!("{}\nroles chosen here override the Host screen's typed disks; data disks become d1, d2… under /mnt/disks; parity needs the snapraid module", d.id.display()),
            None => String::new(),
        };
        f.render_widget(Paragraph::new(help).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(" about this disk ")), help_area);
    }

    fn draw_modules(&self, f: &mut Frame, area: Rect) {
        let (list_area, help_area) = Self::split_list(area);
        let items: Vec<ListItem> = self
            .modules
            .iter()
            .map(|m| {
                let mark = if m.locked { "[■]" } else if m.chosen { "[x]" } else { "[ ]" };
                let style = if m.locked { Style::default().fg(Color::DarkGray) } else { Style::default() };
                ListItem::new(Line::from(vec![Span::styled(format!("{mark} {:<20}", m.name), style.add_modifier(Modifier::BOLD)), Span::styled(m.description.clone(), style)]))
            })
            .collect();
        let mut st = ListState::default();
        st.select(Some(self.module_cursor));
        let chosen = self.chosen().len();
        f.render_stateful_widget(
            List::new(items).block(Block::default().borders(Borders::ALL).title(format!(" Modules — {chosen} chosen; ■ = foundation, always on "))).highlight_style(Style::default().bg(Color::DarkGray)),
            list_area,
            &mut st,
        );
        let m = &self.modules[self.module_cursor];
        let mut help = m.description.clone();
        if !m.requires.is_empty() {
            help.push_str(&format!("\nrequires: {} (added for you)", m.requires.join(", ")));
        }
        if let Some((_, why)) = FOUNDATION_REQUIRED.iter().find(|(n, _)| *n == m.name) {
            help.push_str(&format!("\nfoundation: {why}"));
        }
        f.render_widget(Paragraph::new(help).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(" about this module ")), help_area);
    }

    fn draw_values(&self, f: &mut Frame, area: Rect) {
        let (list_area, help_area) = Self::split_list(area);
        let rows = self.value_rows();
        let items: Vec<ListItem> = rows
            .iter()
            .map(|(name, shown, required, _)| {
                let flag = if *required { Span::styled("! ", Style::default().fg(Color::Red)) } else { Span::raw("  ") };
                ListItem::new(Line::from(vec![flag, Span::styled(format!("{:<44}", name), Style::default().add_modifier(Modifier::BOLD)), Span::raw(shown.clone())]))
            })
            .collect();
        let mut st = ListState::default();
        st.select(Some(self.value_cursor.min(rows.len().saturating_sub(1))));
        let missing = rows.iter().filter(|r| r.2).count();
        f.render_stateful_widget(
            List::new(items).block(Block::default().borders(Borders::ALL).title(format!(" Values — {missing} required still unset (!) · Enter edits · lists/objects as JSON "))).highlight_style(Style::default().bg(Color::DarkGray)),
            list_area,
            &mut st,
        );
        let help = rows.get(self.value_cursor).map(|r| r.3.clone()).unwrap_or_else(|| "no options to set for the chosen modules".into());
        f.render_widget(Paragraph::new(help).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(" about this option ")), help_area);
    }

    fn draw_secrets(&self, f: &mut Frame, area: Rect) {
        // The guide needs room: a short list, a tall help pane.
        let v = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(3 + self.secret_metas().len().max(1) as u16), Constraint::Min(8)])
            .split(area);
        let (list_area, help_area) = (v[0], v[1]);
        let metas = self.secret_metas();
        let items: Vec<ListItem> = metas
            .iter()
            .map(|(opt, _)| {
                let path = self.secrets.get(opt).cloned().unwrap_or_default();
                ListItem::new(Line::from(vec![Span::styled(format!("{:<44}", opt), Style::default().add_modifier(Modifier::BOLD)), Span::raw(if path.is_empty() { "— (file path)".to_string() } else { path })]))
            })
            .collect();
        let mut st = ListState::default();
        st.select(Some(self.secret_cursor.min(metas.len().saturating_sub(1))));
        f.render_stateful_widget(
            List::new(items).block(Block::default().borders(Borders::ALL).title(" Secrets only you can supply — Enter: a file path · v: type the value (saved to a 600 file) ")).highlight_style(Style::default().bg(Color::DarkGray)),
            list_area,
            &mut st,
        );
        let (title, help) = match metas.get(self.secret_cursor) {
            Some((opt, keys)) => match crate::guides::for_option(opt, &self.values) {
                Some(g) => (format!(" {} ", g.title), format!("{}\n\nthe file must carry: {keys}", g.steps)),
                None => (" what the file holds ".to_string(), format!("the file must carry: {keys}")),
            },
            None => (" what the file holds ".to_string(), "the chosen modules need nothing supplied; everything else is minted".to_string()),
        };
        f.render_widget(Paragraph::new(help).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(title)), help_area);
    }

    fn draw_review(&self, f: &mut Frame, area: Rect) {
        let a = self.answers();
        let closed = self.closed();
        let missing: Vec<String> = self.value_rows().into_iter().filter(|r| r.2).map(|r| r.0).collect();
        let unsupplied: Vec<String> = self.secret_metas().into_iter().filter(|(o, _)| self.secrets.get(o).map(|p| p.trim().is_empty()).unwrap_or(true)).map(|(o, _)| o).collect();
        let mut lines = vec![
            Line::from(format!("host {} · {} · system disk {} · {} data disk(s)", a.host.name, a.host.time_zone, if a.host.disk.is_empty() { "—" } else { &a.host.disk }, a.host.data_disks.len())),
            Line::from(format!("modules ({}): {}", closed.len(), closed.join(" "))),
            Line::from(""),
        ];
        if missing.is_empty() && unsupplied.is_empty() {
            lines.push(Line::styled("ready — g: write it all out (then the screen tells you the one install command) · w: write answers.json only", Style::default().fg(Color::Green)));
        } else {
            if !missing.is_empty() {
                lines.push(Line::styled(format!("required values unset: {}", missing.join(", ")), Style::default().fg(Color::Red)));
            }
            if !unsupplied.is_empty() {
                lines.push(Line::styled(format!("secrets without a file: {}", unsupplied.join(", ")), Style::default().fg(Color::Red)));
            }
            lines.push(Line::from("w still writes answers.json; generate will reject it with the full list"));
        }
        lines.push(Line::from(""));
        lines.push(Line::from(format!("answers → {}    flake → {}", self.out_answers.display(), self.out_dir.display())));
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(" Review ")), area);
    }
}

/// Several authorized_keys entries typed on one line: a new key starts at
/// every key-type token, so a key with or without a trailing comment splits
/// correctly.
fn split_keys(text: &str) -> Vec<String> {
    let mut keys: Vec<Vec<&str>> = Vec::new();
    for tok in text.split_whitespace() {
        if tok.starts_with("ssh-") || tok.starts_with("ecdsa-") || tok.starts_with("sk-") || keys.is_empty() {
            keys.push(vec![tok]);
        } else if let Some(last) = keys.last_mut() {
            last.push(tok);
        }
    }
    keys.into_iter().map(|k| k.join(" ")).filter(|k| k.split_whitespace().count() >= 2).collect()
}

/// `https://github.com/<user>.keys` — one authorized_keys line per key. curl
/// rather than an HTTP crate: it is on every live system and in the package's
/// PATH, and this is the only network call the TUI makes.
fn fetch_github_keys(user: &str) -> Result<Vec<String>> {
    if user.is_empty() || !user.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
        anyhow::bail!("not a GitHub username");
    }
    let out = std::process::Command::new("curl")
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

fn value_text(v: &serde_json::Value) -> String {
    match v {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    }
}

/// Text from the editor → JSON: valid JSON stays JSON (lists, objects,
/// numbers, booleans); anything else is a string.
fn parse_value(text: &str) -> serde_json::Value {
    let t = text.trim();
    match serde_json::from_str::<serde_json::Value>(t) {
        Ok(v) if !v.is_string() || t.starts_with('"') => v,
        _ => serde_json::Value::String(t.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn editor_text_becomes_json_or_string() {
        assert_eq!(parse_value("example.test"), serde_json::Value::String("example.test".into()));
        assert_eq!(parse_value("[\"a\",\"b\"]"), serde_json::json!(["a", "b"]));
        assert_eq!(parse_value("true"), serde_json::json!(true));
        assert_eq!(parse_value("14"), serde_json::json!(14));
        assert_eq!(parse_value("{\"k\":1}"), serde_json::json!({"k": 1}));
    }
}

#[cfg(test)]
mod key_tests {
    use super::split_keys;

    #[test]
    fn keys_split_at_type_tokens_with_or_without_comments() {
        let two = split_keys("ssh-ed25519 AAAA1 a@b ssh-rsa BBBB2");
        assert_eq!(two, vec!["ssh-ed25519 AAAA1 a@b", "ssh-rsa BBBB2"]);
        assert_eq!(split_keys("sk-ssh-ed25519@openssh.com CCCC3 you@laptop"), vec!["sk-ssh-ed25519@openssh.com CCCC3 you@laptop"]);
        assert!(split_keys("").is_empty());
    }
}
