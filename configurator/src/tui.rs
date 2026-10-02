//! The terminal front end. A thin skin over the schema: it produces an
//! answers file (and the `--secret` flags for what only the user can supply),
//! then hands off to `generate`. Nothing lives only here — anything the TUI
//! can do, the headless command can do from the same answers file.
//!
//! Five screens, Tab / Shift-Tab between them:
//!   1 Host      name, time zone, disks, SSH key
//!   2 Modules   the catalog; Space toggles; foundation entries are locked
//!   3 Values    every homelab.* option the chosen modules read; Enter edits
//!   4 Secrets   file paths for what only you can supply
//!   5 Review    write answers.json (w) or write and run generate (g)

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
use crate::plan::{FOUNDATION_ALWAYS, FOUNDATION_REQUIRED};
use crate::schema::{Schema, Source};

const SCREENS: [&str; 5] = ["1 Host", "2 Modules", "3 Values", "4 Secrets", "5 Review"];

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
    host: Vec<Field>,
    host_cursor: usize,
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
            Field { key: "timeZone", label: "Time zone", help: "an IANA name, e.g. America/New_York", value: "UTC".into() },
            Field { key: "disk", label: "System disk", help: "/dev/disk/by-id/… — ERASED by the install", value: String::new() },
            Field { key: "dataDisks", label: "Data disks", help: "name=/dev/disk/by-id/…, comma-separated; each mounts at /mnt/disks/<name>", value: String::new() },
            Field { key: "sshAuthorizedKeys", label: "SSH public key", help: "your key; without it the admin can only log in at the console", value: String::new() },
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
        App {
            schema,
            screen: 0,
            host,
            host_cursor: 0,
            modules,
            module_cursor: 0,
            values: BTreeMap::new(),
            value_cursor: 0,
            secrets: BTreeMap::new(),
            secret_cursor: 0,
            editing: None,
            status: "Tab/Shift-Tab: screens · ↑↓: move · Space: toggle · Enter: edit · q: quit".into(),
            out_answers: out_answers.to_path_buf(),
            out_dir: out_dir.to_path_buf(),
            done: None,
        }
    }

    fn load(&mut self, a: &Answers) {
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
        let data_disks: Vec<serde_json::Value> = field("dataDisks")
            .split(',')
            .filter_map(|s| s.trim().split_once('='))
            .map(|(n, d)| serde_json::json!({ "name": n.trim(), "device": d.trim() }))
            .collect();
        let keys: Vec<String> = field("sshAuthorizedKeys").split_whitespace().collect::<Vec<_>>().chunks(3).map(|c| c.join(" ")).filter(|k| !k.is_empty()).collect();
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
                "disk": field("disk"),
                "dataDisks": data_disks,
                "sshAuthorizedKeys": keys,
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
            KeyCode::Char(c @ '1'..='5') if key.modifiers.is_empty() && self.screen != 2 => self.screen = (c as u8 - b'1') as usize,
            KeyCode::Up => self.move_cursor(-1),
            KeyCode::Down => self.move_cursor(1),
            KeyCode::Char(' ') if self.screen == 1 => {
                if let Some(m) = self.modules.get_mut(self.module_cursor) {
                    if !m.locked {
                        m.chosen = !m.chosen;
                    }
                }
            }
            KeyCode::Enter => self.start_edit(),
            KeyCode::Char('w') if self.screen == 4 => self.done = Some(Outcome::Written),
            KeyCode::Char('g') if self.screen == 4 => self.done = Some(Outcome::Generate),
            _ => {}
        }
    }

    fn move_cursor(&mut self, d: i32) {
        let len = match self.screen {
            0 => self.host.len(),
            1 => self.modules.len(),
            2 => self.value_rows().len(),
            3 => self.secret_metas().len(),
            _ => return,
        };
        let cur = match self.screen {
            0 => &mut self.host_cursor,
            1 => &mut self.module_cursor,
            2 => &mut self.value_cursor,
            _ => &mut self.secret_cursor,
        };
        if len == 0 {
            return;
        }
        *cur = ((*cur as i32 + d).rem_euclid(len as i32)) as usize;
    }

    fn start_edit(&mut self) {
        match self.screen {
            0 => self.editing = Some(self.host[self.host_cursor].value.clone()),
            2 => {
                if let Some((name, _, _, _)) = self.value_rows().get(self.value_cursor) {
                    self.editing = Some(self.values.get(name).cloned().unwrap_or_default());
                }
            }
            3 => {
                if let Some((opt, _)) = self.secret_metas().get(self.secret_cursor) {
                    self.editing = Some(self.secrets.get(opt).cloned().unwrap_or_default());
                }
            }
            _ => {}
        }
    }

    fn commit_edit(&mut self, text: String) {
        match self.screen {
            0 => self.host[self.host_cursor].value = text,
            2 => {
                if let Some((name, _, _, _)) = self.value_rows().get(self.value_cursor).cloned() {
                    if text.trim().is_empty() {
                        self.values.remove(&name);
                    } else {
                        self.values.insert(name, text);
                    }
                }
            }
            3 => {
                if let Some((opt, _)) = self.secret_metas().get(self.secret_cursor).cloned() {
                    self.secrets.insert(opt, text);
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
            0 => self.draw_host(f, outer[1]),
            1 => self.draw_modules(f, outer[1]),
            2 => self.draw_values(f, outer[1]),
            3 => self.draw_secrets(f, outer[1]),
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
            .map(|fld| ListItem::new(Line::from(vec![Span::styled(format!("{:<16}", fld.label), Style::default().add_modifier(Modifier::BOLD)), Span::raw(if fld.value.is_empty() { "—".to_string() } else { fld.value.clone() })])))
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
        let (list_area, help_area) = Self::split_list(area);
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
            List::new(items).block(Block::default().borders(Borders::ALL).title(" Secrets only you can supply — a file path each; never the value ")).highlight_style(Style::default().bg(Color::DarkGray)),
            list_area,
            &mut st,
        );
        let help = metas.get(self.secret_cursor).map(|(_, keys)| format!("the file must carry: {keys}")).unwrap_or_else(|| "the chosen modules need nothing supplied; everything else is minted".into());
        f.render_widget(Paragraph::new(help).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(" what the file holds ")), help_area);
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
            lines.push(Line::styled("ready — w: write answers.json · g: write and run generate", Style::default().fg(Color::Green)));
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
