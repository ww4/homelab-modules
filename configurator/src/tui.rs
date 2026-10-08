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
    Ai(bool),
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
    /// The inner `None` means qrencode could not be run — said out loud
    /// rather than left as a gap, because a gap is what a reader reports as
    /// "there was space for one but it didn't print".
    qr: Option<(String, Option<Vec<String>>)>,
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
                // The assistant's credentials belong on its own screen.
                r.extend((0..w.secrets.len()).filter(|i| !w.ai_secret(&w.secrets[*i].option)).map(Row::Secret));
            }
            Step::Ai => {
                r.push(Row::Ai(false));
                r.push(Row::Ai(true));
                if w.ai {
                    r.extend((0..w.secrets.len()).filter(|i| w.ai_secret(&w.secrets[*i].option)).map(Row::Secret));
                }
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
        // A second browser asking for the form: same answer, same keys.
        if self.w.lock().unwrap().install_request.is_none() && self.w.lock().unwrap().takeover_request.is_some() {
            match key.code {
                KeyCode::Char('y') | KeyCode::Char('Y') => {
                    self.w.lock().unwrap().approve_takeover();
                    self.sync_rows();
                }
                KeyCode::Esc => {
                    self.w.lock().unwrap().refuse_takeover();
                    self.sync_rows();
                }
                _ => {}
            }
            return;
        }
        if self.w.lock().unwrap().install_request.is_some() {
            match key.code {
                // Y and not Enter: Enter is the key a person leans on, and
                // this one erases disks.
                KeyCode::Char('y') | KeyCode::Char('Y') => {
                    if let Err(e) = self.w.lock().unwrap().approve_install() {
                        self.w.lock().unwrap().say(e, true);
                    }
                    self.sync_rows();
                }
                KeyCode::Esc => {
                    self.w.lock().unwrap().refuse_install();
                    self.sync_rows();
                }
                _ => {}
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
            Row::Ai(want) => {
                self.w.lock().unwrap().set_ai(want);
                self.sync_rows();
            }
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
                    if let Err(e) = w.set_value(&name, text) {
                        w.say(e, true);
                    }
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
        let asking = {
            let g = self.w.lock().unwrap();
            if g.install_request.is_none() { g.takeover_request.clone().map(|p| (p, g.controller.clone())) } else { None }
        };
        if let Some((peer, holder)) = asking {
            let lines = vec![
                Line::from(Span::styled(format!("The browser at {peer} wants to fill this in."), Style::default().add_modifier(Modifier::BOLD))),
                Line::from(""),
                Line::from(format!("{} is using it now.", holder.unwrap_or_else(|| "Another browser".into()))),
                Line::from(""),
                Line::from(Span::styled("   Press Y here to hand the form over.   ", Style::default().add_modifier(Modifier::BOLD | Modifier::REVERSED))),
                Line::from(""),
                Line::from("Knowing the code is not enough to take the form; this answer is. If you did not ask for this, press Esc."),
            ];
            f.render_widget(
                Paragraph::new(lines).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(" Another browser is asking ")),
                body[0],
            );
            let hint = Paragraph::new(Line::from(Span::styled(
                " Y hands the form over.  Esc refuses.  ",
                Style::default().add_modifier(Modifier::REVERSED),
            )));
            f.render_widget(hint, body[1]);
            if let Some((msg, err)) = status {
                f.render_widget(Paragraph::new(Line::from(Span::styled(msg, Style::default().fg(if err { Color::Red } else { Color::Green })))), outer[2]);
            }
            return;
        }
        let waiting = self.w.lock().unwrap().install_request.clone();
        if let Some(who) = waiting {
            self.draw_install_request(f, body[0], &who);
            let hint = Paragraph::new(Line::from(Span::styled(
                " Y erases the disks and installs.  Esc refuses.  Nothing about this answer crosses the network. ",
                Style::default().add_modifier(Modifier::REVERSED),
            )));
            f.render_widget(hint, body[1]);
            if let Some((msg, err)) = status {
                f.render_widget(Paragraph::new(Line::from(Span::styled(msg, Style::default().fg(if err { Color::Red } else { Color::Green })))), outer[2]);
            }
            return;
        }
        if self.open_secret.is_some() && matches!(step, Step::Domain | Step::Ai) {
            self.draw_secret_form(f, body[0]);
        } else {
            match step {
                Step::Welcome => self.draw_welcome(f, body[0]),
                Step::Kit => self.draw_kit(f, body[0]),
                Step::Storage => self.draw_storage(f, body[0]),
                Step::Profile => self.draw_profile(f, body[0]),
                Step::Ssh => self.draw_ssh(f, body[0]),
                Step::Domain => self.draw_domain(f, body[0]),
                Step::Ai => self.draw_ai(f, body[0]),
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

    /// How many lines a wrapped paragraph needs at this width.
    ///
    /// ⚠️ A FIXED HEIGHT CLIPS, SILENTLY. ratatui's `Length` is not a
    /// minimum: text taller than its box is cut off mid-sentence, with no
    /// ellipsis and nothing to scroll. Every screen here was given a height
    /// guessed against a wide terminal, and a console is 80 columns — at 80
    /// the Modules screen showed 2 of its 5 lines, the Domain screen lost its
    /// last line, and the credentials form cut off the sealing fingerprint,
    /// which is the one copy of that value a reader can trust. So measure.
    ///
    /// Newlines are honoured, because the wrapper honours them and because
    /// the guide texts are written in paragraphs.
    fn text_height(text: &str, width: u16, cap: u16) -> u16 {
        let w = width.max(1) as usize;
        let mut total: u16 = 0;
        for para in text.split('\n') {
            let mut lines: u16 = 1;
            let mut used = 0usize;
            for word in para.split_whitespace() {
                let n = word.chars().count();
                if used == 0 {
                    used = n;
                } else if used + 1 + n <= w {
                    used += 1 + n;
                } else {
                    lines = lines.saturating_add(1);
                    used = n;
                }
            }
            total = total.saturating_add(lines);
        }
        total.clamp(1, cap.max(1))
    }

    /// The three bands, with the top one sized to its own text. `rows` is how
    /// many lines the middle band must keep whatever the text wants.
    fn split_text(area: Rect, text: &str, help: u16, rows: u16) -> (Rect, Rect, Rect) {
        let cap = area.height.saturating_sub(help + rows).max(1);
        Self::split(area, Self::text_height(text, area.width, cap), help)
    }

    fn intro(&self, f: &mut Frame, area: Rect, text: &str) {
        f.render_widget(Paragraph::new(text.to_string()).wrap(Wrap { trim: true }), area);
    }

    fn help(&self, f: &mut Frame, area: Rect, title: &str, text: &str) {
        f.render_widget(Paragraph::new(text.to_string()).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::ALL).title(format!(" {title} "))), area);
    }

    /// A browser has pressed Install. It cannot erase anything on its own:
    /// the answer is a keypress here, so the only way to approve an install
    /// is to be standing at the machine when the disks are erased. Nothing
    /// about the answer travels, so there is nothing to intercept or guess.
    fn draw_install_request(&self, f: &mut Frame, area: Rect, who: &str) {
        let w = self.w.lock().unwrap();
        let erased = w.erased();
        drop(w);
        let mut lines = vec![
            Line::from(Span::styled(format!("The browser at {who} wants to install."), Style::default().add_modifier(Modifier::BOLD))),
            Line::from(""),
            Line::from(Span::styled("   Press Y here to erase these disks and install.   ", Style::default().add_modifier(Modifier::BOLD | Modifier::REVERSED))),
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
        let (intro, ram, disks, address, internet, live, port, code, seen, hw) = {
            let w = self.w.lock().unwrap();
            let (a, i) = w.network();
            // What this box is, in one line: the kit screen reads the same
            // facts to decide what it can honestly offer.
            let chassis = w.machine.chassis.unwrap_or("unknown");
            let cpu = match (&w.machine.cpu, w.machine.cores) {
                (Some(c), n) if n > 0 => format!("{c} ({n} cores)"),
                (Some(c), _) => c.clone(),
                (None, _) => "unknown processor".into(),
            };
            let gpu = match w.machine.best_gpu() {
                Some(g) => format!("{} graphics ({})", g.vendor, g.id),
                None if w.machine.gpus.is_empty() => "no graphics card".into(),
                None => "no graphics card (display is a management chip)".into(),
            };
            (w.step.intro(w.live_usb), w.ram_mib, w.disks.len(), a, i, w.live_usb, w.web_port, w.pairing.clone(), w.web_seen.clone(),
             format!("{chassis} · {cpu} · {gpu}"))
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
            Line::from(vec![Span::styled("              ", Style::default()), Span::raw(hw)]),
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
        // ⚠️ MEASURED, NOT COUNTED. This was `lines.len() + 1`, which counts
        // the lines handed to the paragraph and not the rows it draws after
        // wrapping. At 80 columns the intro alone wraps to five, so the text
        // was cut off AND the square was placed on top of the address and the
        // pairing code — the two things on this screen a reader actually
        // needs. Ask the wrapper how tall each line really is.
        let text_h: u16 = lines
            .iter()
            .map(|l| Self::text_height(&l.to_string(), area.width, u16::MAX))
            .sum::<u16>()
            .saturating_add(1);
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), Rect { height: text_h.min(area.height), ..area });

        let y = area.y + text_h;
        let room = (area.y + area.height).saturating_sub(y);
        match &self.qr {
            // ⚠️ WHOLE OR NOT AT ALL. A clipped QR code does not scan, and
            // drawing one costs the reader the lines the address was on.
            Some((_, Some(rows))) if rows.len() as u16 <= room => {
                let drawn: Vec<Line> = rows.iter().map(|l| Line::from(l.clone())).collect();
                let h = drawn.len() as u16;
                f.render_widget(Paragraph::new(drawn), Rect { x: area.x + 1, y, width: area.width.saturating_sub(1), height: h });
            }
            // Too short a screen for the square: the address above is the
            // whole answer, so say nothing and leave the room to the text.
            Some((_, Some(_))) => {}
            Some((_, None)) if room >= 1 => {
                f.render_widget(
                    Paragraph::new("(no qrencode on this machine, so there is no square to scan — type the address above)")
                        .wrap(Wrap { trim: true })
                        .style(Style::default().add_modifier(Modifier::DIM)),
                    Rect { x: area.x + 1, y, width: area.width.saturating_sub(1), height: room.min(2) },
                );
            }
            _ => {}
        }
    }

    /// The QR of the browser URL, rendered once per URL (qrencode draws it
    /// with block characters the console font has).
    fn refresh_qr(&mut self, url: &str) {
        if url.is_empty() || self.qr.as_ref().map(|(u, _)| u == url).unwrap_or(false) {
            return;
        }
        let drawn = match Command::new("qrencode").args(["-t", "UTF8i", "-m", "1", "-o", "-", url]).output() {
            Ok(o) if o.status.success() => {
                let lines: Vec<String> = String::from_utf8_lossy(&o.stdout).lines().map(|l| l.to_string()).collect();
                if lines.is_empty() { None } else { Some(lines) }
            }
            // Not installed, or it failed: recorded, so the screen can say so.
            _ => None,
        };
        self.qr = Some((url.to_string(), drawn));
    }

    fn draw_kit(&self, f: &mut Frame, area: Rect) {
        let w = self.w.lock().unwrap();
        let text = w.step.intro(w.live_usb);
        let (i, l, h) = Self::split_text(area, text, 5, 8);
        self.intro(f, i, text);
        let state = w.state_json();
        let mut lines = Vec::new();
        if let Some(kits) = state["kits"].as_array() {
            for (idx, k) in kits.iter().enumerate() {
                let row = Row::Kit(idx);
                let fits = k["fits"].as_bool().unwrap_or(true);
                // A kit this machine cannot run is shown with the reason
                // instead of its description. Hiding it would leave somebody
                // wondering whether the installer has such a thing at all.
                let why_not = k["why_not"].as_str();
                lines.push(Line::from(vec![
                    Span::styled(format!("{}({}) {:<16}", self.marker(row), if k["chosen"].as_bool().unwrap_or(false) { "*" } else { " " }, k["name"].as_str().unwrap_or("")), self.style(row)),
                    Span::styled(format!(" ~{} GB ", k["need_gb"].as_u64().unwrap_or(0)), Style::default().fg(if fits { Color::Green } else { Color::Red }).add_modifier(Modifier::BOLD)),
                    match why_not {
                        Some(w) => Span::styled(format!("not available here: {w}"), Style::default().fg(Color::Red)),
                        None => Span::raw(k["blurb"].as_str().unwrap_or("").to_string()),
                    },
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
        let w = self.w.lock().unwrap();
        let text = w.step.intro(w.live_usb);
        let (i, l, h) = Self::split_text(area, text, 6, 6);
        self.intro(f, i, text);
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
        let w = self.w.lock().unwrap();
        let text = w.step.intro(w.live_usb);
        let (i, l, h) = Self::split_text(area, text, 5, 5);
        self.intro(f, i, text);
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
        let w = self.w.lock().unwrap();
        let text = w.step.intro(w.live_usb);
        let (i, l, h) = Self::split_text(area, text, 5, 6);
        self.intro(f, i, text);
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
        let w = self.w.lock().unwrap();
        let text = w.step.intro(w.live_usb);
        let (i, l, h) = Self::split_text(area, text, 5, 6);
        self.intro(f, i, text);
        let fields: Vec<(String, String)> = w.domain.iter().map(|f| (f.label.clone(), f.shown())).collect();
        let secrets: Vec<(String, String, bool)> = w
            .secrets
            .iter()
            .filter(|s| !w.ai_secret(&s.option))
            .map(|s| (s.short(), s.state(), s.filled()))
            .collect();
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

    /// The assistant: one question, and the credential it needs if the answer
    /// is yes. Nothing else on the machine depends on either.
    fn draw_ai(&self, f: &mut Frame, area: Rect) {
        let w = self.w.lock().unwrap();
        let text = w.step.intro(w.live_usb);
        let (i, l, h) = Self::split_text(area, text, 5, 7);
        self.intro(f, i, text);
        let want = w.ai;
        let local = w.can_run_models_locally();
        let card = w.card.as_ref().map(|c| (c.name.clone(), c.runs.clone()));
        let mods = w.ai_modules().join(", ");
        let secrets: Vec<(String, String, bool)> = w
            .secrets
            .iter()
            .filter(|s| w.ai_secret(&s.option))
            .map(|s| (s.short(), s.state(), s.filled()))
            .collect();
        let help: (String, String) = match self.focused() {
            Row::Ai(false) => ("No".into(), "Nothing is installed for this and nothing else changes. You can add it later.".into()),
            Row::Ai(true) => ("Yes".into(), format!("Installs {mods}. You can change your mind on the Modules screen.")),
            Row::Secret(_) => ("Credentials".into(), "Enter opens the form. Skip it if this machine runs its own models, or if you would rather set it up afterwards.".into()),
            _ => ("Continue".into(), "Either answer is fine; no is the default.".into()),
        };
        drop(w);

        let mut lines = Vec::new();
        for (idx, (row, label)) in [(Row::Ai(false), "No, thank you"), (Row::Ai(true), "Yes, set one up")].into_iter().enumerate() {
            let on = (idx == 1) == want;
            lines.push(Line::from(Span::styled(
                format!("{}{} {}", self.marker(row), if on { "(•)" } else { "( )" }, label),
                self.style(row).add_modifier(if on { Modifier::BOLD } else { Modifier::empty() }),
            )));
        }
        lines.push(Line::from(""));
        lines.push(Line::from(match &card {
            Some((name, runs)) if local => format!("This machine has {name}, which can run {runs}. An account elsewhere is optional."),
            Some((name, _)) => format!("This machine has {name}, which is not enough to run a model here, so an assistant would need an account with a provider."),
            None => "This machine has no graphics card the list knows, so an assistant would need an account with a provider.".to_string(),
        }));
        if want && !secrets.is_empty() {
            lines.push(Line::from(""));
            lines.push(Line::from(Span::styled("  credentials — Enter opens the form:", Style::default().add_modifier(Modifier::UNDERLINED))));
            for (idx, (short, state, filled)) in secrets.iter().enumerate() {
                let row = Row::Secret(idx);
                lines.push(Line::from(vec![
                    Span::styled(format!("{}{:<LABEL$}", self.marker(row), short), self.style(row).add_modifier(Modifier::BOLD)),
                    Span::styled(state.clone(), self.style(row).fg(if *filled { Color::Green } else { Color::Reset })),
                ]));
            }
        }
        f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), l);
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
        // This screen's copy of the fingerprint is the trustworthy one: it
        // did not travel. A browser showing a different one is talking to
        // something else. A browser showing the same one has only told you
        // what it was given.
        //
        // ⚠️ IT IS A LINE OF ITS OWN, not the tail of the steps paragraph.
        // On an 80-column console the steps alone are taller than the box,
        // and this sentence — the only copy of the fingerprint that did not
        // cross the network — was the part that got cut.
        let seal = format!(
            "browser-typed values are encrypted first; this machine's key is {} and the browser must show the same",
            w.sealer.fingerprint()
        );
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
        // The rows the middle band must keep: the fingerprint line, a blank,
        // and one line per value. The steps take whatever is left.
        let keep = rows.len() as u16 + 2;
        let (intro_a, list_a, help_a) = Self::split_text(area, &steps, 5, keep);
        f.render_widget(Paragraph::new(steps).wrap(Wrap { trim: true }).block(Block::default().borders(Borders::BOTTOM).title(format!(" {title} "))), intro_a);
        f.render_widget(
            Paragraph::new(seal).wrap(Wrap { trim: true }).style(Style::default().add_modifier(Modifier::DIM)),
            Rect { height: 1.min(list_a.height), ..list_a },
        );
        for (idx, (label, shown)) in rows.iter().enumerate() {
            self.field_line(f, list_a, list_a.y + 2 + idx as u16, Row::SecretField(idx), label, shown.clone());
        }
        match help {
            Some((t, text)) => self.help(f, help_a, &t, &text),
            None => self.help(f, help_a, "this credential", "Enter edits a line; Save writes the file; Skip leaves it for later; Back returns."),
        }
    }

    fn draw_extras(&self, f: &mut Frame, area: Rect) {
        let w = self.w.lock().unwrap();
        let (_, verdict, short) = w.memory();
        let text = format!("{} {verdict}", w.step.intro(w.live_usb));
        let (i, l, h) = Self::split_text(area, &text, 5, 8);
        self.intro(f, i, &text);
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn text_height_counts_wrapped_and_explicit_lines() {
        assert_eq!(Ui::text_height("short", 20, 10), 1);
        // 24 characters at width 10 is three lines of words, not 2.4 of them.
        assert_eq!(Ui::text_height("aaaa bbbb cccc dddd eeee", 10, 10), 3);
        // A blank line between paragraphs is a line.
        assert_eq!(Ui::text_height("one\n\ntwo", 40, 10), 3);
        // The cap is a cap, not a suggestion.
        assert_eq!(Ui::text_height("aaaa bbbb cccc dddd eeee", 10, 2), 2);
        // A word longer than the width still takes a line rather than looping.
        assert_eq!(Ui::text_height("aaaaaaaaaaaaaaa", 4, 9), 1);
    }

    /// A wizard over a catalogue small enough to build in a test.
    fn test_ui(address: &str) -> Ui {
        let catalog = serde_json::json!({
            "acme": {"description": "acme", "enable": "import", "options": [], "requires": [], "vhosts": [], "secrets": []}
        })
        .to_string();
        let schema = Box::leak(Box::new(crate::schema::Schema::parse(&catalog, "[]").expect("a schema")));
        let dir = std::env::temp_dir().join(format!("hl-tui-test-{}", std::process::id()));
        let mut wiz = Wizard::new(schema, &dir.join("answers.json"), &dir, vec![], 8099);
        wiz.set_network_for_test(address);
        Ui { w: Arc::new(Mutex::new(wiz)), focus: 0, editing: None, open_secret: None, qr: None, rows: Vec::new(), quit: false }
    }

    /// ⚠️ THE SQUARE WAS DRAWN ON TOP OF THE ADDRESS. The Welcome screen
    /// placed the QR code at `lines.len() + 1`, a count of the lines handed
    /// to the paragraph rather than the rows it draws after wrapping. At 80
    /// columns the intro alone wraps to five, so the square landed over the
    /// browser address and the pairing code — the two things on that screen
    /// a reader cannot do without. Render it and look.
    #[test]
    fn the_welcome_screen_keeps_its_address_and_code() {
        for (w, h) in [(80u16, 25u16), (100, 40), (120, 60)] {
            let mut ui = test_ui("192.168.1.65");
            let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(w, h)).expect("a terminal");
            term.draw(|f| ui.draw(f)).expect("a frame");
            let screen: String = term.backend().buffer().content().iter().map(|c| c.symbol()).collect();
            let code = ui.w.lock().unwrap().pairing.clone();
            assert!(screen.contains("192.168.1.65:8099"), "{w}x{h}: the browser address is not on the screen");
            assert!(screen.contains(&code), "{w}x{h}: the pairing code is not on the screen");
        }
    }

    /// A clipped QR code does not scan, and drawing one costs the reader the
    /// lines the address was on. Whole or not at all.
    #[test]
    fn a_square_that_does_not_fit_is_not_drawn() {
        let mut ui = test_ui("192.168.1.65");
        // A tall square and a short screen: the rows must not appear.
        ui.qr = Some((
            "http://192.168.1.65:8099/?code=X".to_string(),
            Some((0..60).map(|_| "\u{2588}".repeat(29)).collect()),
        ));
        let mut term = ratatui::Terminal::new(ratatui::backend::TestBackend::new(80, 25)).expect("a terminal");
        term.draw(|f| ui.draw(f)).expect("a frame");
        let screen: String = term.backend().buffer().content().iter().map(|c| c.symbol()).collect();
        assert!(!screen.contains("\u{2588}\u{2588}\u{2588}"), "a square too tall for the screen was drawn anyway");
        assert!(screen.contains("192.168.1.65:8099"), "the address was covered by a square that did not fit");
    }

    /// The band holding the rows must never be squeezed away by a long    /// The band holding the rows must never be squeezed away by a long
    /// paragraph above it. That band is where the fields and the sealing
    /// fingerprint are drawn, so a zero-height one hides them rather than
    /// merely crowding them.
    #[test]
    fn split_text_leaves_the_rows_their_minimum() {
        let long = "word ".repeat(400);
        for height in 8..40u16 {
            for width in [40u16, 80, 100, 200] {
                for keep in [2u16, 5, 8] {
                    let area = Rect { x: 0, y: 0, width, height };
                    let (_, rows, _) = Ui::split_text(area, &long, 5, keep);
                    let room = height.saturating_sub(5 + 1);
                    assert!(
                        rows.height >= keep.min(room),
                        "{width}x{height} keep={keep}: rows got {} lines",
                        rows.height
                    );
                }
            }
        }
    }

    /// ⚠️ A real VGA text console is 80 columns, and every intro here was
    /// written against a terminal twice that wide. Three of them were being
    /// cut off mid-sentence in the place the installer is most often read:
    /// standing at the machine. Six lines is what the layout can give an
    /// intro at 80 columns while the rows below it stay usable, so six lines
    /// is the budget, and it is checked rather than remembered.
    #[test]
    fn every_screens_intro_fits_an_80_column_console() {
        for step in crate::wizard::STEPS {
            for live in [true, false] {
                let text = step.intro(live);
                let need = Ui::text_height(text, 78, u16::MAX);
                assert!(
                    need <= 6,
                    "{:?} needs {need} lines at 80 columns; shorten it or move the detail to the guide page",
                    step
                );
            }
        }
    }
}
