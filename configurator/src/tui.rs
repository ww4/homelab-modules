//! The console front end: a renderer and a keymap over `wizard::Wizard`,
//! nothing more. Every rule about what an answer may be lives in the model,
//! which the browser front end drives too.
//!
//! Shaped like Ubuntu Server's installer: one question per screen, Back and
//! Continue at the bottom, the focused row in reverse video with a `>`
//! marker, and editing done **in the field** with a real cursor — not on a
//! command line at the bottom of the screen.

use anyhow::{Context, Result};
use crossterm::event::{self, Event, KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::ExecutableCommand;
use ratatui::layout::{Constraint, Direction, Layout, Position, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Wrap};
use ratatui::{Frame, Terminal};
use std::io;
use std::path::Path;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use crate::answers::Answers;
use crate::schema::Schema;
use crate::wizard::{key_summary, Role, Step, Wizard};

/// Where the focus can rest. The index is into the model's lists.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Row {
    Field(usize),
    Kit(usize),
    Disk(usize),
    Key(usize),
    Secret(usize),
    SecretField(usize),
    Save,
    Skip,
    Value(usize),
    Module(usize),
    Back,
    Continue,
}

const LABEL: usize = 26;

struct Ui {
    w: Arc<Mutex<Wizard>>,
    focus: usize,
    /// Editing: which row, and the buffer being typed into it.
    editing: Option<(Row, String)>,
    /// The Domain screen opens one credential at a time as its own form.
    open_secret: Option<usize>,
    /// Rendered QR of the browser URL, kept for the URL it was made from.
    qr: Option<(String, Vec<String>)>,
    /// The rows of the current screen, refreshed once per frame and before
    /// every key. Rendering must never take the model lock twice: a std
    /// Mutex is not reentrant, and the draw path deadlocked itself.
    rows: Vec<Row>,
    quit: bool,
}

pub fn run(schema: &'static Schema, profile: Option<&Path>, out_answers: &Path, out_dir: &Path, exe_prefix: Vec<String>, web: bool, port: u16) -> Result<i32> {
    let mut wiz = Wizard::new(schema, out_answers, out_dir, exe_prefix, port);
    if let Some(p) = profile {
        let text = std::fs::read_to_string(p).with_context(|| format!("reading {}", p.display()))?;
        let a: Answers = serde_json::from_str(&text).with_context(|| format!("parsing {}", p.display()))?;
        wiz.load(&a);
    } else if out_answers.exists() {
        if let Ok(text) = std::fs::read_to_string(out_answers) {
            if let Ok(a) = serde_json::from_str::<Answers>(&text) {
                wiz.load(&a);
                wiz.say(format!("answers loaded from {}", out_answers.display()), false);
            }
        }
    }
    wiz.restore_session();
    wiz.watch_network();
    let shared = Arc::new(Mutex::new(wiz));
    if web {
        // A busy port is not fatal: the console front end is enough.
        if let Err(e) = crate::web::serve(shared.clone()) {
            shared.lock().unwrap().say(e, true);
        }
    }

    let mut ui = Ui { w: shared.clone(), focus: 0, editing: None, open_secret: None, qr: None, rows: Vec::new(), quit: false };
    enable_raw_mode()?;
    io::stdout().execute(EnterAlternateScreen)?;
    let backend = ratatui::backend::CrosstermBackend::new(io::stdout());
    let mut terminal = Terminal::new(backend)?;
    let result = ui.event_loop(&mut terminal);
    disable_raw_mode()?;
    io::stdout().execute(LeaveAlternateScreen)?;
    result?;

    // The install's own report, so it stays in the scrollback.
    for l in shared.lock().unwrap().install_report.iter() {
        println!("{l}");
    }
    // Hand over to a newer installer, with the answers it just wrote and the
    // same pairing code, so the browser keeps talking to the same install.
    let relaunch = shared.lock().unwrap().relaunch.clone();
    if let Some(exe) = relaunch {
        use std::os::unix::process::CommandExt;
        println!("starting the newer installer...");
        let mut c = Command::new(&exe);
        c.arg("tui");
        for a in std::env::args().skip(2) {
            c.arg(a);
        }
        let e = c.exec();
        eprintln!("could not start {exe}: {e}");
        return Ok(1);
    }
    Ok(0)
}

impl Ui {
    // ------------------------------------------------------------ rows

    /// The rows a screen has, from the model alone (no locking here).
    fn rows_of(w: &Wizard, open_secret: Option<usize>) -> Vec<Row> {
        let mut r = Vec::new();
        if let (Step::Domain, Some(i)) = (w.step, open_secret) {
            if let Some(s) = w.secrets.get(i) {
                if s.path_only {
                    r.push(Row::SecretField(0));
                } else {
                    r.extend((0..s.fields.len()).map(Row::SecretField));
                }
                r.push(Row::Save);
                r.push(Row::Skip);
                r.push(Row::Back);
                return r;
            }
        }
        match w.step {
            Step::Welcome => {}
            Step::Kit => r.extend((0..w.kits.len()).map(Row::Kit)),
            Step::Storage => {
                if w.disks.is_empty() {
                    r.push(Row::Field(0));
                } else {
                    r.extend((0..w.disks.len()).map(Row::Disk));
                }
            }
            Step::Profile => r.extend((0..w.profile.len()).map(Row::Field)),
            Step::Ssh => {
                r.push(Row::Field(0));
                r.push(Row::Field(1));
                r.extend((0..w.keys.len()).map(Row::Key));
            }
            Step::Domain => {
                r.extend((0..w.domain.len()).map(Row::Field));
                r.extend((0..w.secrets.len()).map(Row::Secret));
            }
            Step::Extras => {
                r.extend((0..w.open_values().len()).map(Row::Value));
                r.extend((0..w.modules.len()).map(Row::Module));
            }
            Step::Review | Step::Install | Step::Done => {}
        }
        if !matches!(w.step, Step::Welcome | Step::Install | Step::Done) {
            r.push(Row::Back);
        }
        r.push(Row::Continue);
        r
    }

    /// Refresh the cached rows: the only place the row list is computed.
    fn sync_rows(&mut self) {
        let rows = {
            let w = self.w.lock().unwrap();
            Self::rows_of(&w, self.open_secret)
        };
        self.rows = rows;
        if self.focus >= self.rows.len() {
            self.focus = self.rows.len().saturating_sub(1);
        }
    }

    fn focused(&self) -> Row {
        self.rows.get(self.focus).copied().unwrap_or(Row::Continue)
    }

    fn move_focus(&mut self, d: i32) {
        let n = self.rows.len() as i32;
        if n > 0 {
            self.focus = ((self.focus as i32 + d).rem_euclid(n)) as usize;
        }
    }

    // ------------------------------------------------------------ loop

    fn event_loop(&mut self, terminal: &mut Terminal<ratatui::backend::CrosstermBackend<io::Stdout>>) -> Result<()> {
        loop {
            self.sync_rows();
            terminal.draw(|f| self.draw(f))?;
            if event::poll(Duration::from_millis(120))? {
                if let Event::Key(key) = event::read()? {
                    if key.kind == KeyEventKind::Press {
                        self.on_key(key);
                    }
                }
            }
            {
                let mut w = self.w.lock().unwrap();
                if w.step == Step::Install {
                    w.poll_install();
                }
                // A newer installer has been fetched (from the browser's
                // gear): give it this terminal and this process.
                if w.relaunch.is_some() {
                    self.quit = true;
                }
            }
            if self.quit {
                return Ok(());
            }
        }
    }

    fn on_key(&mut self, key: KeyEvent) {
        self.sync_rows();
        if let Some((row, buf)) = &mut self.editing {
            let row = *row;
            match key.code {
                KeyCode::Esc => {
                    self.editing = None;
                    self.w.lock().unwrap().say("edit cancelled", false);
                }
                KeyCode::Enter => {
                    let text = buf.clone();
                    self.editing = None;
                    self.commit(row, &text);
                }
                KeyCode::Backspace => {
                    buf.pop();
                }
                KeyCode::Tab | KeyCode::BackTab | KeyCode::Up | KeyCode::Down => {
                    self.w.lock().unwrap().say("finish this field first: Enter keeps what you typed, Esc throws it away", true);
                }
                KeyCode::Char(c) => buf.push(c),
                _ => {}
            }
            return;
        }
        if key.code == KeyCode::Char('q') && key.modifiers.contains(KeyModifiers::CONTROL) {
            self.quit = true;
            return;
        }
        // While a browser's request is on the screen, the only answers are
        // "no" and walking away; everything else would move the form under
        // the person who is about to confirm it.
        if self.w.lock().unwrap().install_pin.is_some() {
            if key.code == KeyCode::Esc {
                self.w.lock().unwrap().refuse_install();
                self.sync_rows();
            }
            return;
        }
        if self.w.lock().unwrap().step == Step::Install {
            return;
        }
        if matches!(key.code, KeyCode::Char('l') | KeyCode::Char('L')) && self.editing.is_none() && !self.w.lock().unwrap().locked_out.is_empty() {
            self.w.lock().unwrap().clear_lockouts();
            return;
        }
        match key.code {
            KeyCode::Up | KeyCode::BackTab => self.move_focus(-1),
            KeyCode::Down | KeyCode::Tab => self.move_focus(1),
            KeyCode::Esc => self.back(),
            KeyCode::Char(' ') => self.activate(true),
            KeyCode::Enter => self.activate(false),
            _ => {}
        }
    }

    /// Enter (or Space on a choice) on the focused row.
    fn activate(&mut self, space: bool) {
        let row = self.focused();
        match row {
            Row::Continue => self.forward(),
            Row::Back => self.back(),
            Row::Kit(i) => self.w.lock().unwrap().set_kit(i),
            Row::Disk(i) => self.w.lock().unwrap().cycle_disk(i),
            Row::Key(i) => {
                if space {
                    self.w.lock().unwrap().remove_key(i);
                    self.sync_rows();
                } else {
                    self.w.lock().unwrap().say("Space removes this key", false);
                }
            }
            Row::Module(i) => {
                let name = self.w.lock().unwrap().modules[i].name.clone();
                let r = self.w.lock().unwrap().toggle_module(&name);
                if let Err(e) = r {
                    self.w.lock().unwrap().say(e, false);
                }
            }
            Row::Secret(i) => {
                self.open_secret = Some(i);
                self.focus = 0;
                self.sync_rows();
            }
            Row::Save => {
                if let Some(i) = self.open_secret {
                    let option = self.w.lock().unwrap().secrets[i].option.clone();
                    let r = self.w.lock().unwrap().save_secret(&option);
                    match r {
                        Ok(m) => {
                            self.w.lock().unwrap().say(m, false);
                            self.open_secret = None;
                            self.focus = 0;
                        }
                        Err(e) => self.w.lock().unwrap().say(e, true),
                    }
                }
            }
            Row::Skip => {
                if let Some(i) = self.open_secret {
                    let option = self.w.lock().unwrap().secrets[i].option.clone();
                    let r = self.w.lock().unwrap().skip_secret(&option);
                    match r {
                        Ok(m) => {
                            self.w.lock().unwrap().say(m, true);
                            self.open_secret = None;
                            self.focus = 0;
                        }
                        Err(e) => self.w.lock().unwrap().say(e, true),
                    }
                }
            }
            Row::Field(_) | Row::SecretField(_) | Row::Value(_) => {
                // A field with a list: Space walks it, Enter types a value
                // the list does not have.
                if space {
                    if let Some(next) = self.next_choice(row) {
                        self.commit(row, &next);
                        return;
                    }
                }
                self.start_edit(row)
            }
        }
    }

    /// The next value of a pick-list field, if this row has one.
    fn next_choice(&self, row: Row) -> Option<String> {
        let w = self.w.lock().unwrap();
        match (w.step, self.open_secret, row) {
            (Step::Profile, _, Row::Field(i)) => w.profile.get(i).and_then(|f| f.next_choice()),
            (Step::Domain, Some(i), Row::SecretField(j)) => w.secrets.get(i).and_then(|s| s.fields.get(j)).and_then(|f| f.next_choice()),
            _ => None,
        }
    }

    fn start_edit(&mut self, row: Row) {
        let w = self.w.lock().unwrap();
        let current = match (w.step, self.open_secret, row) {
            (Step::Domain, Some(i), Row::SecretField(j)) => {
                let s = &w.secrets[i];
                if s.path_only {
                    s.path.clone()
                } else {
                    s.fields[j].value.clone()
                }
            }
            (Step::Storage, _, Row::Field(_)) => w.disk_fallback.value.clone(),
            (Step::Profile, _, Row::Field(i)) => w.profile[i].value.clone(),
            (Step::Ssh, _, Row::Field(0)) => w.github_user.value.clone(),
            (Step::Ssh, _, Row::Field(1)) => w.pasted_key.value.clone(),
            (Step::Domain, _, Row::Field(i)) => w.domain[i].value.clone(),
            (Step::Extras, _, Row::Value(i)) => w.open_values().get(i).map(|v| v.1.clone()).unwrap_or_default(),
            _ => return,
        };
        drop(w);
        self.editing = Some((row, current));
    }

    fn commit(&mut self, row: Row, text: &str) {
        let (step, open) = (self.w.lock().unwrap().step, self.open_secret);
        let mut w = self.w.lock().unwrap();
        match (step, open, row) {
            (Step::Domain, Some(i), Row::SecretField(j)) => {
                let (option, var) = {
                    let s = &w.secrets[i];
                    (s.option.clone(), if s.path_only { "__path".to_string() } else { s.fields[j].key.clone() })
                };
                match w.set_secret_field(&option, &var, text) {
                    Ok(()) => w.say("kept", false),
                    Err(e) => w.say(e, true),
                }
            }
            (Step::Storage, _, Row::Field(_)) => {
                w.disk_fallback.value = text.trim().to_string();
                w.say("kept", false);
            }
            (Step::Profile, _, Row::Field(i)) => {
                let key = w.profile[i].key.clone();
                match w.set_profile(&key, text) {
                    Ok(()) => w.say("kept", false),
                    Err(e) => w.say(e, true),
                }
            }
            (Step::Ssh, _, Row::Field(0)) => match w.import_github_keys(text) {
                Ok(m) if m.is_empty() => {}
                Ok(m) => w.say(m, false),
                Err(e) => w.say(e, true),
            },
            (Step::Ssh, _, Row::Field(1)) => match w.add_key(text) {
                Ok(m) if m.is_empty() => {}
                Ok(m) => w.say(m, false),
                Err(e) => w.say(e, true),
            },
            (Step::Domain, _, Row::Field(i)) => {
                let key = w.domain[i].key.clone();
                match w.set_domain(&key, text) {
                    Ok(()) => w.say("kept", false),
                    Err(e) => w.say(e, true),
                }
            }
            (Step::Extras, _, Row::Value(i)) => {
                if let Some((name, _, _)) = w.open_values().get(i).cloned() {
                    w.set_value(&name, text);
                }
            }
            _ => {}
        }
    }

    fn forward(&mut self) {
        let step = self.w.lock().unwrap().step;
        if step == Step::Done {
            self.quit = true;
            return;
        }
        let r = self.w.lock().unwrap().advance();
        match r {
            Ok(()) => {
                self.focus = 0;
            }
            Err(b) => self.w.lock().unwrap().say(b.message, true),
        }
    }

    fn back(&mut self) {
        if self.open_secret.is_some() {
            self.open_secret = None;
            self.focus = 0;
            return;
        }
        self.w.lock().unwrap().back();
        self.focus = 0;
    }

    // ------------------------------------------------------------ draw

    fn draw(&mut self, f: &mut Frame) {
        let outer = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Min(5), Constraint::Length(2)])
            .split(f.area());
        let (title, step, status) = {
            let w = self.w.lock().unwrap();
            let shown = w.step.index().min(crate::wizard::QUESTION_STEPS - 1) + 1;
            (
                Line::from(vec![
                    Span::styled(format!(" {} ", w.step.title()), Style::default().add_modifier(Modifier::BOLD | Modifier::REVERSED)),
                    Span::raw(format!("  step {shown} of {}  ·  homelab installer", crate::wizard::QUESTION_STEPS)),
                ]),
                w.step,
                w.status.clone(),
            )
        };
        f.render_widget(Paragraph::new(title), outer[0]);

        let body = Layout::default().direction(Direction::Vertical).constraints([Constraint::Min(3), Constraint::Length(3)]).split(outer[1]);
        // A browser asking to erase the disks: the number is the only thing
        // on this screen until someone here answers it one way or the other.
        let waiting = self.w.lock().unwrap().install_pin.clone();
        if let Some(pin) = waiting {
            self.draw_install_request(f, body[0], &pin);
            let hint = Paragraph::new(Line::from(Span::styled(
                " Esc refuses it. The number is on this screen only; it is never sent to the browser. ",
                Style::default().add_modifier(Modifier::REVERSED),
            )));
            f.render_widget(hint, body[1]);
            if let Some((msg, err)) = status {
                f.render_widget(Paragraph::new(Line::from(Span::styled(msg, Style::default().fg(if err { Color::Red } else { Color::Green })))), outer[2]);
            }
            return;
        }
        if self.open_secret.is_some() && step == Step::Domain {
            self.draw_secret_form(f, body[0]);
        } else {
            match step {
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
        }
        self.draw_buttons(f, body[1], step);

        let footer = match &status {
            Some((m, err)) => Line::from(Span::styled(format!(" {m}"), if *err { Style::default().fg(Color::Red).add_modifier(Modifier::BOLD) } else { Style::default().fg(Color::Green) })),
            None if self.editing.is_some() => Line::from(" typing: Enter keeps it · Esc throws it away"),
            None => Line::from(" up/down or Tab: move · Space: choose · Enter: edit, or press the button · Esc: back · Ctrl-Q: quit to the shell"),
        };
        f.render_widget(Paragraph::new(footer).wrap(Wrap { trim: true }), outer[2]);
    }

    fn style(&self, row: Row) -> Style {
        if self.focused() == row && self.editing.is_none() {
            Style::default().add_modifier(Modifier::REVERSED | Modifier::BOLD)
        } else {
            Style::default()
        }
    }

    fn marker(&self, row: Row) -> &'static str {
        if self.focused() == row {
            "> "
        } else {
            "  "
        }
    }

    /// A label/value line. While this row is being edited the value is the
    /// buffer and the real cursor sits at its end: editing happens in the
    /// field, the way subiquity does it.
    fn field_line(&self, f: &mut Frame, area: Rect, y: u16, row: Row, label: &str, shown: String) {
        if y >= area.y + area.height {
            return;
        }
        let editing = matches!(&self.editing, Some((r, _)) if *r == row);
        let text = match &self.editing {
            Some((r, buf)) if *r == row => {
                if self.masked(row) {
                    "*".repeat(buf.chars().count())
                } else {
                    buf.clone()
                }
            }
            _ => shown,
        };
        let style = if editing { Style::default().add_modifier(Modifier::UNDERLINED) } else { self.style(row) };
        let line = Line::from(vec![
            Span::styled(format!("{}{:<LABEL$}", self.marker(row), label), style.add_modifier(Modifier::BOLD)),
            Span::styled(text.clone(), style),
        ]);
        f.render_widget(Paragraph::new(line), Rect { x: area.x, y, width: area.width, height: 1 });
        if editing {
            let x = area.x + 2 + LABEL as u16 + text.chars().count() as u16;
            f.set_cursor_position(Position { x: x.min(area.x + area.width.saturating_sub(1)), y });
        }
    }

    fn masked(&self, row: Row) -> bool {
        let w = self.w.lock().unwrap();
        match (w.step, self.open_secret, row) {
            (Step::Profile, _, Row::Field(i)) => w.profile.get(i).map(|f| f.masked).unwrap_or(false),
            (Step::Domain, Some(i), Row::SecretField(j)) => w.secrets.get(i).and_then(|s| s.fields.get(j)).map(|f| f.masked).unwrap_or(false),
            _ => false,
        }
    }

    fn split(area: Rect, intro: u16, help: u16) -> (Rect, Rect, Rect) {
        let v = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(intro), Constraint::Min(2), Constraint::Length(help)])
            .split(area);
        (v[0], v[1], v[2])
    }

    fn intro(&self, f: &mut Frame, area: Rect, text: &str) {
        f.render_widget(Paragraph::new(text.to_string()).wrap(Wrap { trim: true }), area);
    }

    fn help(&self, f: &mut Frame, area: Rect, title: &str, text: &str) {
        f.render_widget(Paragraph::new(text.to_string()).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(format!(" {title} "))), area);
    }

    /// A browser has pressed Install. It cannot erase anything on its own:
    /// this number has to be read here and typed there, which means someone
    /// is standing at the machine at the moment the disks are erased.
    fn draw_install_request(&self, f: &mut Frame, area: Rect, pin: &str) {
        let w = self.w.lock().unwrap();
        let who = w.controller.clone().unwrap_or_else(|| "a browser".into());
        let erased = w.erased();
        drop(w);
        let mut lines = vec![
            Line::from(Span::styled(format!("The browser at {who} wants to install."), Style::default().add_modifier(Modifier::BOLD))),
            Line::from(""),
            Line::from("Type this number in that browser to go ahead:"),
            Line::from(""),
            Line::from(Span::styled(format!("   {}   ", spaced(pin)), Style::default().add_modifier(Modifier::BOLD | Modifier::REVERSED))),
            Line::from(""),
            Line::from(Span::styled("These disks are erased:", Style::default().fg(Color::Red))),
        ];
        for d in &erased {
            lines.push(Line::from(Span::styled(format!("   {d}"), Style::default().fg(Color::Red))));
        }
        lines.push(Line::from(""));
        lines.push(Line::from("If you did not press Install in a browser, press Esc."));
        f.render_widget(Paragraph::new(lines).block(Block::default().borders(Borders::ALL).title(" Confirm at the machine ")), area);
    }

    fn draw_buttons(&self, f: &mut Frame, area: Rect, step: Step) {
        let live = self.w.lock().unwrap().live_usb;
        let mut spans = vec![Span::raw("  ")];
        if self.open_secret.is_some() {
            spans.push(Span::styled("[ Save ]", self.style(Row::Save)));
            spans.push(Span::raw("   "));
            spans.push(Span::styled("[ Skip ]", self.style(Row::Skip)));
            spans.push(Span::raw("   "));
            spans.push(Span::styled("[ Back ]", self.style(Row::Back)));
            f.render_widget(Paragraph::new(Line::from(spans)).block(Block::default().borders(Borders::TOP)), area);
            return;
        }
        if self.rows.contains(&Row::Back) {
            spans.push(Span::styled("[ Back ]", self.style(Row::Back)));
            spans.push(Span::raw("   "));
        }
        let label = match step {
            Step::Review => {
                if live {
                    "[ Install ]"
                } else {
                    "[ Write the configuration ]"
                }
            }
            Step::Install => "[ working... ]",
            Step::Done => "[ Close ]",
            _ => "[ Continue ]",
        };
        spans.push(Span::styled(label, self.style(Row::Continue)));
        let hint = match step {
            Step::Review if live => "   Install erases the disks listed above.",
            Step::Done => "   Then type `reboot` and pull the USB stick out as the screen goes dark.",
            _ => "",
        };
        spans.push(Span::raw(hint));
        f.render_widget(Paragraph::new(Line::from(spans)).block(Block::default().borders(Borders::TOP)), area);
    }

    // -------------------------------------------------------- screens

    fn draw_welcome(&mut self, f: &mut Frame, area: Rect) {
        let (intro, ram, disks, address, internet, live, port, code, seen) = {
            let w = self.w.lock().unwrap();
            let (a, i) = w.network();
            (w.step.intro(w.live_usb), w.ram_mib, w.disks.len(), a, i, w.live_usb, w.web_port, w.pairing.clone(), w.web_seen.clone())
        };
        let gb = |m: u64| format!("{:.1} GB", m as f64 / 1024.0);
        let net = match (address.is_empty(), internet) {
            (true, _) => "waiting for a network address — plug in a network cable; checked again every few seconds".to_string(),
            (false, Some(true)) => format!("network {address} · internet reachable"),
            (false, Some(false)) => format!("network {address} · internet NOT reachable yet"),
            (false, None) => format!("network {address} · checking the internet..."),
        };
        let url = if address.is_empty() { String::new() } else { format!("http://{address}:{port}/?code={code}") };
        self.refresh_qr(&url);
        let mut lines = vec![
            Line::from(intro),
            Line::from(""),
            Line::from(vec![Span::styled("This machine  ", Style::default().add_modifier(Modifier::BOLD)), Span::raw(format!("{} of RAM · {disks} disk(s) available · {net}", if ram == 0 { "unknown".into() } else { gb(ram) }))]),
        ];
        if !url.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from(vec![
                Span::styled("Easier: finish this in a browser  ", Style::default().add_modifier(Modifier::BOLD)),
                Span::styled(format!("http://{address}:{port}"), Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
                Span::raw(format!("   code {code}")),
            ]));
            lines.push(Line::from("Open it on your own PC or phone and fill the form there — you can paste, which this screen cannot. Both screens follow each other."));
            if let Some(s) = &seen {
                lines.push(Line::styled(format!("A browser at {s} is filling this in."), Style::default().fg(Color::Green)));
            }
        }
        if !live {
            lines.push(Line::from(""));
            lines.push(Line::from("Not running from the installer: the last screen writes the configuration and prints the install command instead of running it."));
        }
        let text_h = lines.len() as u16 + 1;
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), Rect { height: text_h.min(area.height), ..area });
        if let Some((_, qr)) = &self.qr {
            let y = area.y + text_h;
            if y + 2 < area.y + area.height {
                let rows: Vec<Line> = qr.iter().map(|l| Line::from(l.clone())).collect();
                f.render_widget(Paragraph::new(rows), Rect { x: area.x + 1, y, width: area.width.saturating_sub(1), height: (area.y + area.height).saturating_sub(y) });
            }
        }
    }

    /// The QR of the browser URL, rendered once per URL (qrencode draws it
    /// with block characters the console font has).
    fn refresh_qr(&mut self, url: &str) {
        if url.is_empty() || self.qr.as_ref().map(|(u, _)| u == url).unwrap_or(false) {
            return;
        }
        if let Ok(o) = Command::new("qrencode").args(["-t", "UTF8i", "-m", "1", "-o", "-", url]).output() {
            if o.status.success() {
                let lines: Vec<String> = String::from_utf8_lossy(&o.stdout).lines().map(|l| l.to_string()).collect();
                if !lines.is_empty() {
                    self.qr = Some((url.to_string(), lines));
                }
            }
        }
    }

    fn draw_kit(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 3, 5);
        let w = self.w.lock().unwrap();
        self.intro(f, i, w.step.intro(w.live_usb));
        let state = w.state_json();
        let mut lines = Vec::new();
        if let Some(kits) = state["kits"].as_array() {
            for (idx, k) in kits.iter().enumerate() {
                let row = Row::Kit(idx);
                let fits = k["fits"].as_bool().unwrap_or(true);
                lines.push(Line::from(vec![
                    Span::styled(format!("{}({}) {:<16}", self.marker(row), if k["chosen"].as_bool().unwrap_or(false) { "*" } else { " " }, k["name"].as_str().unwrap_or("")), self.style(row)),
                    Span::styled(format!(" ~{} GB ", k["need_gb"].as_u64().unwrap_or(0)), Style::default().fg(if fits { Color::Green } else { Color::Red }).add_modifier(Modifier::BOLD)),
                    Span::raw(k["blurb"].as_str().unwrap_or("").to_string()),
                ]));
            }
        }
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), l);
        let name = w.kits[w.kit].name;
        let modules = w.kits[w.kit].modules.clone();
        let (_, verdict, _) = w.memory();
        drop(w);
        self.help(f, h, &format!("{name} (chosen)"), &format!("{verdict}\nmodules: {}", if modules.is_empty() { "none until you pick them".to_string() } else { modules.join(" ") }));
    }

    fn draw_storage(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 4, 6);
        let w = self.w.lock().unwrap();
        self.intro(f, i, w.step.intro(w.live_usb));
        if w.disks.is_empty() {
            let (label, shown, help) = (w.disk_fallback.label.clone(), w.disk_fallback.shown(), w.disk_fallback.help.clone());
            drop(w);
            self.field_line(f, l, l.y, Row::Field(0), &label, shown);
            self.help(f, h, &label, &help);
            return;
        }
        let mut lines = Vec::new();
        for (idx, (d, r)) in w.disks.iter().zip(&w.roles).enumerate() {
            let row = Row::Disk(idx);
            let role_style = Style::default().add_modifier(Modifier::BOLD).fg(match r {
                Role::System => Color::Yellow,
                Role::Data => Color::Green,
                Role::Parity => Color::Cyan,
                Role::Unused => Color::Reset,
            });
            lines.push(Line::from(vec![
                Span::styled(format!("{}[{:<8}] ", self.marker(row), r.label()), self.style(row).patch(role_style)),
                Span::styled(format!("{:<9}{:>9}  {:<5} {}", d.kernel, d.size_human(), d.transport, d.model), self.style(row)),
            ]));
        }
        f.render_widget(Paragraph::new(lines), l);
        let help: String = match self.focused() {
            Row::Disk(i) => format!("{}\nSpace cycles the role.\nSYSTEM: {}\ndata: {}\nparity: {}", w.disks[i].id.display(), Role::System.help(), Role::Data.help(), Role::Parity.help()),
            _ => "Continue needs one SYSTEM disk.".to_string(),
        };
        drop(w);
        self.help(f, h, "about this disk", &help);
    }

    fn draw_profile(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 2, 5);
        let w = self.w.lock().unwrap();
        self.intro(f, i, w.step.intro(w.live_usb));
        let fields: Vec<(String, String)> = w.profile.iter().map(|f| (f.label.clone(), if f.choices.is_empty() { f.shown() } else { f.label_of(&f.value) })).collect();
        let help: Option<(String, String)> = match self.focused() {
            Row::Field(i) => w.profile.get(i).map(|f| (f.label.clone(), format!("{}{}", f.help, if f.choices.is_empty() { "" } else { "\nSpace walks the list; Enter types one the list does not have." }))),
            _ => None,
        };
        let kept = w.admin_hash.is_some() && w.field("password").is_empty();
        drop(w);
        for (idx, (label, shown)) in fields.iter().enumerate() {
            self.field_line(f, l, l.y + idx as u16, Row::Field(idx), label, shown.clone());
        }
        match help {
            Some((label, text)) => {
                let extra = if label == "Password" && kept { " (the password from the last run is kept unless you type a new one)" } else { "" };
                self.help(f, h, &label, &format!("{text}{extra}"))
            }
            None => self.help(f, h, "Continue", "Checks the name and that the two passwords match."),
        }
    }

    fn draw_ssh(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 3, 5);
        let w = self.w.lock().unwrap();
        self.intro(f, i, w.step.intro(w.live_usb));
        let two = [(w.github_user.label.clone(), w.github_user.shown()), (w.pasted_key.label.clone(), w.pasted_key.shown())];
        let keys: Vec<String> = w.keys.iter().map(|k| key_summary(k)).collect();
        let help: (String, String) = match self.focused() {
            Row::Field(0) => (w.github_user.label.clone(), w.github_user.help.clone()),
            Row::Field(1) => (w.pasted_key.label.clone(), w.pasted_key.help.clone()),
            Row::Key(i) => ("this key".to_string(), format!("{}\nSpace removes it.", w.keys.get(i).cloned().unwrap_or_default())),
            _ => ("Continue".to_string(), "The keys go into the admin account's authorized_keys on the new system.".to_string()),
        };
        drop(w);
        for (idx, (label, shown)) in two.iter().enumerate() {
            self.field_line(f, l, l.y + idx as u16, Row::Field(idx), label, shown.clone());
        }
        let mut lines = vec![Line::from(Span::styled(format!("  keys that may log in ({}):", keys.len()), Style::default().add_modifier(Modifier::UNDERLINED)))];
        for (idx, k) in keys.iter().enumerate() {
            let row = Row::Key(idx);
            lines.push(Line::from(Span::styled(format!("{}{k}", self.marker(row)), self.style(row))));
        }
        f.render_widget(Paragraph::new(lines), Rect { y: l.y + 3, height: l.height.saturating_sub(3), ..l });
        self.help(f, h, &help.0, &help.1);
    }

    fn draw_domain(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 4, 5);
        let w = self.w.lock().unwrap();
        self.intro(f, i, w.step.intro(w.live_usb));
        let fields: Vec<(String, String)> = w.domain.iter().map(|f| (f.label.clone(), f.shown())).collect();
        let secrets: Vec<(String, String, bool)> = w.secrets.iter().map(|s| (s.short(), s.state(), s.filled())).collect();
        let help: (String, String) = match self.focused() {
            Row::Field(i) => (w.domain[i].label.clone(), w.domain[i].help.clone()),
            Row::Secret(i) => (w.secrets[i].short(), "Enter opens the form for this credential: one line per value it needs, with the steps to get them.".to_string()),
            _ => ("Continue".to_string(), "Needs the domain, the email, and every credential either filled in or skipped.".to_string()),
        };
        drop(w);
        for (idx, (label, shown)) in fields.iter().enumerate() {
            self.field_line(f, l, l.y + idx as u16, Row::Field(idx), label, shown.clone());
        }
        let base = l.y + fields.len() as u16 + 1;
        let mut lines = vec![Line::from(Span::styled("  credentials — Enter opens each form:", Style::default().add_modifier(Modifier::UNDERLINED)))];
        for (idx, (short, state, filled)) in secrets.iter().enumerate() {
            let row = Row::Secret(idx);
            lines.push(Line::from(vec![
                Span::styled(format!("{}{:<LABEL$}", self.marker(row), short), self.style(row).add_modifier(Modifier::BOLD)),
                Span::styled(state.clone(), self.style(row).fg(if *filled { Color::Green } else { Color::Reset })),
            ]));
        }
        f.render_widget(Paragraph::new(lines), Rect { y: base, height: (l.y + l.height).saturating_sub(base), ..l });
        self.help(f, h, &help.0, &help.1);
    }

    /// One credential's form: the steps to get it, then a line per value.
    fn draw_secret_form(&self, f: &mut Frame, area: Rect) {
        let Some(i) = self.open_secret else { return };
        let w = self.w.lock().unwrap();
        let Some(s) = w.secrets.get(i) else { return };
        let title = s.title.clone();
        // The console has no clipboard and no browser, so the address is
        // printed to be typed on the phone in the reader's hand.
        let steps = match &s.walkthrough {
            Some(url) => format!("{}\n\nThe full walkthrough, with pictures: {url}", s.steps),
            None => s.steps.clone(),
        };
        // The browser seals what it sends to this machine's key. Printing the
        // fingerprint here is what makes that checkable: the page shows the
        // same one, and a mismatch means something is sitting in between.
        let steps = format!("{steps}\n\nTyped in a browser, this value is sealed to this machine before it leaves. Key {}", w.sealer.fingerprint());
        let path_only = s.path_only;
        let rows: Vec<(String, String)> = if path_only {
            vec![("File on this machine".to_string(), if s.path.is_empty() { "—".into() } else { s.path.clone() })]
        } else {
            s.fields
                .iter()
                .zip(&s.optional)
                .map(|(f, opt)| {
                    let shown = if f.choices.is_empty() { f.shown() } else { f.label_of(&f.value) };
                    (format!("{}{}", f.label, if *opt { " (optional)" } else { "" }), shown)
                })
                .collect()
        };
        let help: Option<(String, String)> = match self.focused() {
            Row::SecretField(j) if !path_only => s.fields.get(j).map(|f| (f.label.clone(), format!("{}{}", f.help, if f.choices.is_empty() { "" } else { "\nSpace walks the list." }))),
            Row::Save => Some(("Save".to_string(), "Writes the file (mode 600) and checks the value where the provider has an API.".to_string())),
            Row::Skip => Some(("Skip".to_string(), "Leaves this credential unset. The install still runs; what depends on it does not work until you set it on the machine and rebuild.".to_string())),
            _ => None,
        };
        drop(w);
        let (intro_a, list_a, help_a) = Self::split(area, 8, 5);
        f.render_widget(Paragraph::new(steps).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::BOTTOM).title(format!(" {title} "))), intro_a);
        for (idx, (label, shown)) in rows.iter().enumerate() {
            self.field_line(f, list_a, list_a.y + idx as u16, Row::SecretField(idx), label, shown.clone());
        }
        match help {
            Some((t, text)) => self.help(f, help_a, &t, &text),
            None => self.help(f, help_a, "this credential", "Enter edits a line; Save writes the file; Skip leaves it for later; Back returns."),
        }
    }

    fn draw_extras(&self, f: &mut Frame, area: Rect) {
        let (i, l, h) = Self::split(area, 2, 5);
        let w = self.w.lock().unwrap();
        let (_, verdict, short) = w.memory();
        self.intro(f, i, &format!("{} {verdict}", w.step.intro(w.live_usb)));
        let values = w.open_values();
        let modules: Vec<(String, String, bool, bool, u64)> = w.modules.iter().map(|m| (m.name.clone(), m.description.clone(), m.chosen, m.locked, m.memory)).collect();
        let help: Option<(String, String)> = match self.focused() {
            Row::Value(i) => values.get(i).map(|v| (v.0.clone(), v.2.clone())),
            Row::Module(i) => modules.get(i).map(|m| {
                let req = w.schema.catalog.get(&m.0).map(|c| c.requires.join(" ")).unwrap_or_default();
                (m.0.clone(), format!("{}\nmemory ~{} MiB{}{}", m.1, m.4, if req.is_empty() { String::new() } else { format!(" · needs: {req}") }, if m.3 { " · foundation, always on" } else { "" }))
            }),
            _ => None,
        };
        drop(w);
        for (idx, (name, value, _)) in values.iter().enumerate() {
            self.field_line(f, l, l.y + idx as u16, Row::Value(idx), &format!("! {name}"), if value.is_empty() { "— (needs a value)".into() } else { value.clone() });
        }
        let top = l.y + values.len() as u16;
        let room = (l.y + l.height).saturating_sub(top) as usize;
        let focus_idx = match self.focused() {
            Row::Module(i) => i,
            _ => 0,
        };
        let start = focus_idx.saturating_sub(room.saturating_sub(1));
        let mut lines = Vec::new();
        for (idx, (name, desc, chosen, locked, _)) in modules.iter().enumerate().skip(start) {
            let row = Row::Module(idx);
            let mark = if *locked {
                "#"
            } else if *chosen {
                "x"
            } else {
                " "
            };
            lines.push(Line::from(vec![
                Span::styled(format!("{}[{mark}] {:<22}", self.marker(row), name), self.style(row).add_modifier(if *locked { Modifier::DIM } else { Modifier::BOLD })),
                Span::styled(desc.clone(), self.style(row)),
            ]));
        }
        f.render_widget(Paragraph::new(lines), Rect { y: top, height: (l.y + l.height).saturating_sub(top), ..l });
        match help {
            Some((t, text)) => self.help(f, h, &t, &text),
            None => self.help(f, h, "Continue", if short { "The machine is short of memory for this set: drop a module, or go on and expect swapping." } else { "Everything chosen fits this machine." }),
        }
    }

    fn draw_review(&self, f: &mut Frame, area: Rect) {
        let w = self.w.lock().unwrap();
        let s = w.state_json();
        let r = &s["review"];
        let bold = Style::default().add_modifier(Modifier::BOLD);
        let (_, verdict, short) = w.memory();
        let list = |v: &serde_json::Value| v.as_array().map(|a| a.iter().filter_map(|x| x.as_str().map(String::from)).collect::<Vec<_>>()).unwrap_or_default();
        let mut lines = vec![
            Line::from(vec![Span::styled("Machine   ", bold), Span::raw(format!("{} · time zone {} · admin `{}`{}", r["host"].as_str().unwrap_or(""), r["time_zone"].as_str().unwrap_or(""), r["admin"].as_str().unwrap_or(""), if r["minted_password"].as_bool().unwrap_or(false) { " · password minted into FIRST-LOGIN.md" } else { "" }))]),
            Line::from(vec![Span::styled("Kit       ", bold), Span::raw(format!("{} · {} module(s): {}", r["kit"].as_str().unwrap_or(""), list(&r["modules"]).len(), list(&r["modules"]).join(" ")))]),
            Line::from(vec![Span::styled("Memory    ", bold), Span::styled(verdict, Style::default().fg(if short { Color::Red } else { Color::Green }))]),
            Line::from(vec![Span::styled("SSH keys  ", bold), Span::raw(if list(&r["keys"]).is_empty() { "none (console login only)".to_string() } else { list(&r["keys"]).join("; ") })]),
            Line::from(vec![Span::styled("Domain    ", bold), Span::raw(if r["domain"].as_str().unwrap_or("").is_empty() { "—".to_string() } else { r["domain"].as_str().unwrap_or("").to_string() })]),
        ];
        let skipped = list(&r["skipped_secrets"]);
        if !skipped.is_empty() {
            lines.push(Line::from(vec![Span::styled("Skipped   ", bold), Span::styled(format!("{} — set these on the machine later and rebuild", skipped.join(", ")), Style::default().fg(Color::Yellow))]));
        }
        lines.push(Line::from(""));
        lines.push(Line::from(vec![Span::styled("ERASED    ", Style::default().fg(Color::Red).add_modifier(Modifier::BOLD)), Span::styled(list(&r["erased"]).join("  "), Style::default().fg(Color::Red))]));
        lines.push(Line::from(""));
        lines.push(Line::from(format!("answers → {}    configuration → {}", w.out_answers.display(), r["out_dir"].as_str().unwrap_or(""))));
        lines.push(Line::from(""));
        lines.push(Line::from(w.step.intro(w.live_usb)));
        drop(w);
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
    }

    fn draw_install(&self, f: &mut Frame, area: Rect) {
        let lines = self.w.lock().unwrap().progress_lines();
        let h = area.height.saturating_sub(2) as usize;
        let start = lines.len().saturating_sub(h);
        let text: Vec<Line> = lines[start..].iter().map(|l| Line::from(l.clone())).collect();
        f.render_widget(Paragraph::new(text).block(Block::default().borders(Borders::ALL).title(format!(" installing · {} lines so far · do not turn the machine off ", lines.len()))), area);
    }

    fn draw_done(&self, f: &mut Frame, area: Rect) {
        let w = self.w.lock().unwrap();
        let failed = w.status.as_ref().map(|(_, e)| *e).unwrap_or(false);
        let tail = w.done_tail(area.height.saturating_sub(4) as usize);
        drop(w);
        let mut lines: Vec<Line> = Vec::new();
        if failed {
            lines.push(Line::styled("It stopped before finishing; the lines below say where.", Style::default().fg(Color::Red).add_modifier(Modifier::BOLD)));
            lines.push(Line::from("Press Close: the full output is in the terminal, and `homelab-configure tui` opens this form again with your answers in it."));
            lines.push(Line::from(""));
        }
        lines.extend(tail.into_iter().map(Line::from));
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }).block(Block::default().borders(Borders::ALL).title(" what happened ")), area);
    }
}

/// `123456` as `123 456`: six digits run together are easy to misread off a
/// VGA console, and this is typed on another machine.
fn spaced(pin: &str) -> String {
    let (a, b) = pin.split_at(pin.len() / 2);
    format!("{a} {b}")
}
