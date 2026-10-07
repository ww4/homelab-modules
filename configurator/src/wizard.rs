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
    Ai,
    Extras,
    Review,
    Install,
    Done,
}

pub const STEPS: [Step; 11] = [
    Step::Welcome,
    Step::Kit,
    Step::Storage,
    Step::Profile,
    Step::Ssh,
    Step::Domain,
    Step::Ai,
    Step::Extras,
    Step::Review,
    Step::Install,
    Step::Done,
];

/// The steps a person counts: Install and Done are not questions.
pub const QUESTION_STEPS: usize = 9;

impl Step {
    pub fn title(self) -> &'static str {
        match self {
            Step::Welcome => "Welcome",
            Step::Kit => "What should this machine be?",
            Step::Storage => "Storage",
            Step::Profile => "Profile",
            Step::Ssh => "SSH access",
            Step::Domain => "Domain and certificates",
            Step::Ai => "An assistant",
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
            Step::Ai => "ai",
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
            // ⚠️ SIX LINES ON AN 80-COLUMN CONSOLE IS THE BUDGET, and a test
            // holds every screen to it. The first draft of this one ran to
            // nine and the console cut the last two off.
            Step::Ai => "This machine can run an assistant: a program that keeps working between conversations, reads and writes the files you give it, and runs what you ask it to. It is Hermes, somebody else's open-source work, and you talk to it here on the machine. Nothing else depends on the answer, so no costs you nothing. Yes needs either a graphics card for models that run here or an account with a model provider, and the line below says which this machine has.",
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
    /// Video memory this kit needs before it is worth offering, in whole
    /// gigabytes. Zero means it does not care about graphics, which is every
    /// kit but one.
    ///
    /// ⚠️ A kit the machine cannot run is still SHOWN, greyed, with the
    /// reason. Hiding it would leave somebody wondering whether the installer
    /// has such a thing at all, and the answer "your card is too small" is
    /// more use than silence.
    pub needs_vram_gb: u64,
}

/// Why a kit is not on offer here, or `None` when it is.
pub fn kit_blocked(kit: &Kit, machine: &crate::machine::Machine, card: Option<&crate::machine::Card>) -> Option<String> {
    if kit.needs_vram_gb == 0 {
        return None;
    }
    match (machine.best_gpu(), card) {
        (None, _) if machine.gpus.is_empty() => Some("this machine has no graphics card".into()),
        (None, _) => Some("this machine's only display adapter is a management chip, not a graphics card".into()),
        (Some(g), None) => Some(format!(
            "the published card list does not know {} — it may be too new, or the list could not be fetched",
            g.id
        )),
        (Some(_), Some(c)) if c.vram_gb < kit.needs_vram_gb => Some(format!(
            "{} has {} GB of memory; this needs at least {} GB",
            c.name, c.vram_gb, kit.needs_vram_gb
        )),
        _ => None,
    }
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
            needs_vram_gb: 0,
        },
        Kit { name: "Media box", blurb: "The Starter plus the whole media pipeline: *arr apps behind a VPN, audiobooks, music, a disk pool with parity.", modules: profile(include_str!("../profiles/media-box.json")), needs_vram_gb: 0 },
        Kit { name: "Docs and forge", blurb: "Documents, photos, notes, passwords, a git forge, single sign-on: the office half.", modules: profile(include_str!("../profiles/docs-forge.json")), needs_vram_gb: 0 },
        // ⚠️ Only offered on a machine with a card the published list knows
        // and rates. Everything else about it is an ordinary kit.
        Kit {
            name: "AI box",
            blurb: "Run open-weight language models on this machine, with a browser front end. Needs a graphics card; what it can comfortably run depends on the card's memory.",
            modules: ["acme", "nginx-access", "ollama", "open-webui", "backup", "monitoring", "ntfy", "alertmanager-ntfy"].iter().map(|s| s.to_string()).collect(),
            needs_vram_gb: 6,
        },
        Kit { name: "Everything", blurb: "Everything that runs on its own: 40 of the library's 42 modules. Needs a domain, a VPN account, a Backblaze account and a few disks. The two left out need a server you already run elsewhere (MeshCentral) or a config file you write yourself (recyclarr); add them on the Modules screen.", modules: profile(include_str!("../profiles/everything.json")), needs_vram_gb: 0 },
        Kit { name: "Minimal", blurb: "Just the foundation: a machine that boots and is yours, serving nothing yet.", modules: vec![], needs_vram_gb: 0 },
        Kit { name: "Custom", blurb: "Whatever you tick on the Modules screen. Picking this changes nothing; it is what the kit says when your list matches none of the above.", modules: vec![CUSTOM.to_string()], needs_vram_gb: 0 },
    ]
}

/// Letters and digits that cannot be confused with each other when read off
/// a screen and typed on another machine: no O/0, no I/1/l, no S/5, no B/8,
/// no Z/2. 8 characters of this is about 37 bits.
const CODE_ALPHABET: &[u8] = b"ACDEFGHJKMNPQRTUVWXY34679";
pub const CODE_LEN: usize = 8;

/// A fresh pairing code. Rejecting a wrong one is cheap, so the length is
/// what keeps a guesser out; the alphabet is what keeps a reader in.
pub fn new_pairing_code() -> String {
    use rand::Rng;
    let mut rng = rand::thread_rng();
    (0..CODE_LEN).map(|_| CODE_ALPHABET[rng.gen_range(0..CODE_ALPHABET.len())] as char).collect()
}

/// How the wizard was driven for one action. The browser is configuration
/// authority; erasing disks additionally needs someone at the machine.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Origin {
    Console,
    Browser,
}

/// What to do about a wrong pairing code from this address.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum BadCode {
    /// Counted; carry on.
    Counted,
    /// Enough wrong answers that each further one is answered slowly.
    SlowDown,
    /// Out of tries. Only someone at the machine can clear it.
    Locked,
}

/// After this many wrong codes every further attempt is answered slowly.
pub const CODE_SLOW_AFTER: u32 = 3;
/// After this many the address is refused until the console clears it.
pub const CODE_LOCK_AFTER: u32 = 10;

/// Whether an address is on a network this installer is willing to talk to.
///
/// The installer is a thing you run on a machine in front of you, on your own
/// network, for an hour. Nothing outside that network has any business
/// driving it, so the server refuses the packet rather than relying on a
/// household router to have been configured correctly. Tailscale's range is
/// included because the project treats a tailnet as the way in from
/// elsewhere, and the installed system's reconfigure mode is reached that way.
pub fn is_local_peer(ip: &std::net::IpAddr) -> bool {
    match ip {
        std::net::IpAddr::V4(v4) => {
            let o = v4.octets();
            v4.is_loopback()
                || v4.is_private()
                || v4.is_link_local()
                || (o[0] == 100 && (64..128).contains(&o[1])) // 100.64/10, the tailnet
        }
        std::net::IpAddr::V6(v6) => {
            if let Some(v4) = v6.to_ipv4_mapped() {
                return is_local_peer(&std::net::IpAddr::V4(v4));
            }
            let o = v6.octets();
            v6.is_loopback()
                || (o[0] & 0xfe) == 0xfc // fc00::/7, unique local
                || (o[0] == 0xfe && (o[1] & 0xc0) == 0x80) // fe80::/10, link local
        }
    }
}

/// What the installer's binary cache says it can serve. Written by
/// `tools/publish-cache.sh` after the closure is up and public, so anything
/// named here is fetchable.
pub const CACHE_LATEST: &str = "https://homelab-installer.nyc3.digitaloceanspaces.com/cache/latest-installer.json";

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
    /// A page that walks this through in full, for someone who has never
    /// seen the other company's web interface.
    pub walkthrough: Option<String>,
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
            "homelab.hermes.environmentFile" => "Model provider key".into(),
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

/// What a check for a newer installer found. `newest` is the newest revision
/// the binary cache can actually serve, which is the only thing worth
/// offering; `pending` names a newer commit that exists but is not published
/// yet, so the answer can explain the wait instead of failing on it.
pub struct Update {
    pub newest: String,
    pub newer: bool,
    pub pending: Option<String>,
    /// The store path the cache will serve and when it was published, so the
    /// reader can see what is about to replace the program they are using.
    pub store_path: Option<String>,
    pub published: Option<String>,
}

/// The name of the key the project signs installer builds with. This is a
/// LABEL, not a key and not the verification: Nix does the verifying, against
/// the `trusted-public-keys` baked into the image, and refuses a substitute
/// that no trusted key signed. `trusted_keys()` reads what is actually in
/// force so the screen shows the machine's own setting rather than this
/// string's good intentions.
pub const CACHE_KEY_NAME: &str = "homelab-installer-1";

/// The public keys this machine's Nix will accept a substitute from, as it is
/// configured right now. Empty if it cannot be read, which is itself worth
/// showing: it means nothing can be said about what will be trusted.
pub fn trusted_keys() -> Vec<String> {
    let out = match Command::new("nix").args(["config", "show", "trusted-public-keys"]).output() {
        Ok(o) if o.status.success() => o,
        _ => return Vec::new(),
    };
    String::from_utf8_lossy(&out.stdout)
        .split_whitespace()
        .map(|s| s.to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

/// Two revisions, one of which may be abbreviated.
pub fn same_rev(a: &str, b: &str) -> bool {
    let n = a.len().min(b.len()).min(40);
    n >= 7 && a[..n].eq_ignore_ascii_case(&b[..n])
}

/// The build must be the one the publisher named. The cache marker carries
/// the store path it published; the pinned revision must evaluate to exactly
/// that. If the two ever disagree, the chain between the publisher and this
/// machine has a link in it that nobody intended, and the right move is to
/// stop rather than to run it.
pub fn promised_matches(built: &str, promised: Option<&str>, rev: &str) -> Result<(), String> {
    match promised {
        None => Ok(()),
        Some(p) if p == built => Ok(()),
        Some(p) => Err(format!(
            "refusing this update: the cache says {rev} is {p}, and that revision builds {built}. Something between the publisher and this machine does not agree."
        )),
    }
}

/// Why a `nix build --max-jobs 0` failed, in a sentence a reader can act on.
/// Nix prints `Output paths:` and the path even when the build FAILED, so
/// taking the last line of stderr told the reader only the path it did not
/// get — which is what the first report of this looked like.
pub fn fetch_failure(stderr: &str) -> String {
    let lower = stderr.to_ascii_lowercase();
    if lower.contains("no suitable substitute") || lower.contains("local builds are disabled") || lower.contains("cannot build") {
        return "the newer installer is not in the binary cache yet: it is built and published a few minutes after the change lands. Check again shortly.".to_string();
    }
    let reason = stderr.lines().map(str::trim).find(|l| l.starts_with("error:") || l.starts_with("Reason:")).unwrap_or("");
    let tail = stderr.lines().map(str::trim).filter(|l| !l.is_empty()).last().unwrap_or("nix said nothing");
    format!("could not fetch the newer installer: {}", if reason.is_empty() { tail } else { reason })
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
    /// What kind of machine this is: chassis, processor, graphics. Read once
    /// at startup, because none of it changes under us.
    pub machine: crate::machine::Machine,
    /// Whether this machine gets an assistant. Off unless asked for: it is a
    /// choice somebody makes, not a default anybody inherits, and nothing
    /// else on the machine depends on the answer.
    pub ai: bool,
    /// What the published card list says about the graphics in this machine.
    /// Looked up once, after the network is known to work, and only when
    /// there is a card to look up.
    pub card: Option<crate::machine::Card>,
    card_list_tried: bool,
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
    /// What a check for a newer installer found.
    pub update: Option<Update>,
    /// A browser has asked to install, and the machine's own screen is
    /// waiting for somebody to approve it. Holds the address that asked.
    ///
    /// ⚠️ NOTHING CROSSES THE NETWORK FOR THIS. An earlier version showed a
    /// six-digit number on the console and had the reader type it into the
    /// browser, which sent it straight back over the same plain HTTP the
    /// number was meant to protect, and which could be asked for again after
    /// three wrong guesses. The approval is a keypress here now, so there is
    /// no secret to intercept and nothing to guess.
    pub install_request: Option<String>,
    /// A second browser asking for the form, waiting for someone at the
    /// machine to allow it.
    ///
    /// ⚠️ WHY THIS NEEDS ASKING. The pairing code travels in the address of
    /// the page, over the same unencrypted connection as everything else, so
    /// anybody who can watch the network can learn it. Without this, that
    /// code alone would let them take the form away from whoever is using it
    /// and keep it for the rest of the session. With it, the code gets them
    /// a look and nothing else unless somebody at the machine agrees.
    pub takeover_request: Option<String>,
    /// Wrong pairing codes, per source address, and the addresses that have
    /// run out of tries. Shared with the console so a person at the machine
    /// can see an attempt and clear it.
    pub code_failures: std::collections::BTreeMap<String, u32>,
    pub locked_out: std::collections::BTreeSet<String>,
    /// This run's key pair, for values the page must not send in the clear.
    pub sealer: crate::sealed::Sealer,
    /// A masked value arrived unsealed. Not refused, because the console
    /// front end and an older page both send plain text, but said out loud
    /// on the Review screen: it means that value crossed the network
    /// readable by anyone watching.
    pub saw_cleartext_secret: bool,
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
            machine: crate::machine::Machine::read(),
            ai: false,
            card: None,
            card_list_tried: false,
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
            pairing: new_pairing_code(),
            web_port,
            web_seen: None,
            controller: None,
            auto_modules: Default::default(),
            update: None,
            install_request: None,
            takeover_request: None,
            code_failures: Default::default(),
            locked_out: Default::default(),
            sealer: crate::sealed::Sealer::new(),
            saw_cleartext_secret: false,
            relaunch: None,
        };

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
        // The GitHub mirror has a commit seconds after a merge, but the
        // closure reaches the binary cache only once the publisher has built
        // it — a few minutes later. Offering the mirror's revision therefore
        // offers something `nix build --max-jobs 0` cannot fetch, which is
        // exactly what the first report of this looked like. So ask the cache
        // what it can actually serve.
        let out = Command::new("curl")
            .args(["-fsS", "--max-time", "15", CACHE_LATEST])
            .output()
            .map_err(|e| format!("could not reach the installer cache: {e}"))?;
        if !out.status.success() {
            return Err("could not reach the installer cache to check for an update".into());
        }
        let v: serde_json::Value = serde_json::from_slice(&out.stdout).map_err(|_| "the installer cache returned something unexpected".to_string())?;
        let published = v["rev"].as_str().unwrap_or("").to_string();
        let store_path = v["store_path"].as_str().map(String::from);
        let published_at = v["published"].as_str().map(String::from);
        if published.is_empty() {
            return Err("the installer cache names no revision".into());
        }
        // Best effort, and only so the answer can explain a wait: the newest
        // commit, which may be ahead of what is published.
        let head = Self::mirror_head();
        let pending = head.filter(|h| !same_rev(h, &published)).map(|h| h[..7.min(h.len())].to_string());

        let mine = Self::version();
        if mine == "dirty" {
            self.update = Some(Update { newest: published, newer: false, pending, store_path, published: published_at });
            return Ok("this installer was built from a work tree, so there is nothing to compare it with".into());
        }
        let newer = !same_rev(&published, &mine);
        let short = published[..7.min(published.len())].to_string();
        self.update = Some(Update { newest: published, newer, pending: pending.clone(), store_path, published: published_at });
        Ok(if newer {
            format!("a newer installer is available ({short})")
        } else if let Some(p) = pending {
            format!("this is the newest published installer; a newer change ({p}) is not in the cache yet, so check again in a few minutes")
        } else {
            "this is the newest installer".into()
        })
    }

    /// The newest commit on the public mirror, or nothing if it cannot be
    /// read. Never an error: this only annotates the answer.
    fn mirror_head() -> Option<String> {
        let out = Command::new("curl")
            .args(["-fsS", "--max-time", "10", "https://api.github.com/repos/ww4/homelab-modules/commits/main"])
            .output()
            .ok()?;
        if !out.status.success() {
            return None;
        }
        let v: serde_json::Value = serde_json::from_slice(&out.stdout).ok()?;
        let sha = v["sha"].as_str()?.to_string();
        if sha.is_empty() {
            None
        } else {
            Some(sha)
        }
    }

    /// Fetch the newest installer and hand this process over to it, keeping
    /// the answers, the saved secrets and the pairing code. Never during an
    /// install: the install is a child of this process.
    pub fn apply_update(&mut self) -> Result<String, String> {
        // ⚠️ NOT WHILE THE MACHINE IS BEING ASKED SOMETHING. Replacing this
        // process in the window between a browser pressing Install and
        // somebody pressing Y at the console is a genuine race, not just an
        // odd click order: the install runs as a child of this process, so an
        // exec here would leave an erase running with nothing supervising it
        // and a second installer serving its own browser interface. The
        // takeover request is not destructive, but there is no reason to let
        // two administrative transitions overlap either.
        if self.progress.is_some() || self.step == Step::Install {
            return Err("not while the install is running".into());
        }
        if self.install_request.is_some() {
            return Err("not while the machine's own screen is asking whether to install: answer there first".into());
        }
        if self.takeover_request.is_some() {
            return Err("not while the machine's own screen is asking who should hold the form: answer there first".into());
        }
        let answers = self.answers();
        std::fs::write(&self.out_answers, serde_json::to_string_pretty(&answers).map_err(|e| e.to_string())? + "\n").map_err(|e| e.to_string())?;
        self.save_session();
        // ⚠️ BUILD THE REVISION WE NAMED, NOT WHATEVER `main` IS NOW.
        //
        // This used to build `github:ww4/homelab-modules?dir=configurator`,
        // which resolves to the branch head at the moment the button is
        // pressed. The check that ran a minute earlier reported a particular
        // revision, so the screen could say one thing and the machine install
        // another. Pinning the reference closes that, and makes the cache
        // marker's `store_path` useful: the pinned reference must evaluate to
        // exactly the path the publisher said it would.
        let u = self.update.as_ref().ok_or("check for an update first")?;
        let rev = u.newest.clone();
        let promised = u.store_path.clone();
        if rev.len() < 7 {
            return Err("the cache marker names no usable revision".into());
        }
        let flake = format!("github:ww4/homelab-modules/{rev}?dir=configurator#default");
        let out = Command::new("nix")
            .args(["build", "--no-write-lock-file", "--max-jobs", "0", "--no-link", "--print-out-paths", &flake])
            .output()
            .map_err(|e| format!("could not run nix: {e}"))?;
        if !out.status.success() {
            return Err(fetch_failure(&String::from_utf8_lossy(&out.stderr)));
        }
        let path = String::from_utf8_lossy(&out.stdout).trim().to_string();
        promised_matches(&path, promised.as_deref(), &rev)?;
        let exe = format!("{path}/bin/homelab-configure");
        if !Path::new(&exe).exists() {
            return Err(format!("{exe}: not there after the fetch"));
        }
        // And ask the program itself, before handing the session to it.
        match Command::new(&exe).arg("revision").output() {
            Ok(o) if o.status.success() => {
                let said = String::from_utf8_lossy(&o.stdout).trim().to_string();
                if !same_rev(&said, &rev) {
                    return Err(format!("refusing this update: it was fetched as {rev} and reports itself as {said}"));
                }
            }
            _ => return Err("refusing this update: the fetched installer could not say which revision it is".into()),
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
        let Some(k) = self.kits.get(i) else { return };
        // ⚠️ The screen greys a kit the machine cannot run, and this refuses
        // it as well. A browser that skipped the page's own JavaScript must
        // not be able to pick a kit the machine has no hardware for.
        if let Some(why) = kit_blocked(k, &self.machine, self.card.as_ref()) {
            let name = k.name;
            self.say(format!("{name} is not available on this machine: {why}"), true);
            return;
        }
        self.kit = i;
        self.apply_kit();
        let (_, verdict, _) = self.memory();
        let name = self.kits[i].name;
        self.say(format!("{name}: {verdict}"), false);
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
                walkthrough: guide.as_ref().and_then(|g| g.walkthrough).map(String::from),
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
            // Propagated, not dropped: a rejected option here would otherwise
            // leave the form showing an answer the model never took.
            self.set_value(var, &v)?;
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

    /// ⚠️ ONLY WHAT THE SCREEN IS ACTUALLY OFFERING. This used to insert any
    /// name it was handed, so a browser that skipped the page's own
    /// JavaScript could set options the Modules screen never shows and the
    /// Review screen does not summarise, including who nginx answers and
    /// which vhosts sit behind Authelia. The claim elsewhere in this file is
    /// that every rule lives in the model and the browser can do nothing the
    /// console could not; this is where that was not yet true.
    pub fn set_value(&mut self, name: &str, text: &str) -> Result<(), String> {
        if !self.open_values().iter().any(|(n, _, _)| n == name) {
            return Err(format!("{name} is not one of the values this screen is asking for"));
        }
        let text = text.trim().to_string();
        if text.is_empty() {
            self.values.remove(name);
        } else {
            self.values.insert(name.to_string(), text);
        }
        self.refresh_secrets();
        Ok(())
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
        self.advance_from(Origin::Console)
    }

    /// Continue, knowing which front end pressed it. Everything is the same
    /// for both except the last screen: pressing Install in a browser asks
    /// the console for a number first, so a browser alone can never erase a
    /// disk. Someone at the console is already standing at the machine.
    pub fn advance_from(&mut self, origin: Origin) -> Result<(), Blocked> {
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
                // The network is up by here, which is what this needs.
                self.ensure_card_known();
            }
            Step::Kit => {}
            // (the card lookup happens on the way out of Welcome, below)
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
            Step::Ai => {}
            Step::Extras => {
                if let Some((name, _, _)) = self.open_values().iter().find(|(_, v, _)| v.trim().is_empty()).cloned() {
                    return Err(hard(&format!("{name} has no default and needs a value")));
                }
            }
            Step::Review => {
                if origin == Origin::Browser && self.live_usb {
                    self.install_request = Some(self.controller.clone().unwrap_or_else(|| "a browser".into()));
                    self.say("go to the machine: its own screen is asking whether to erase the disks and install", false);
                    return Ok(());
                }
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

    /// A browser asking for the form. The first one to ask gets it, because
    /// there is nothing to take; after that it is somebody at the machine
    /// who decides.
    pub fn request_takeover(&mut self, peer: &str) -> bool {
        match &self.controller {
            None => {
                self.controller = Some(peer.to_string());
                self.say(format!("the browser at {peer} is filling this in"), false);
                true
            }
            Some(c) if c == peer => true,
            Some(c) => {
                let c = c.clone();
                self.takeover_request = Some(peer.to_string());
                self.say(format!("the browser at {peer} wants the form from {c}: the machine's own screen is asking"), false);
                false
            }
        }
    }

    /// Somebody at the machine allowing that.
    pub fn approve_takeover(&mut self) {
        if let Some(peer) = self.takeover_request.take() {
            let old = self.controller.replace(peer.clone());
            match old {
                Some(o) => self.say(format!("the browser at {peer} took over from {o}"), false),
                None => self.say(format!("the browser at {peer} is filling this in"), false),
            }
        }
    }

    pub fn refuse_takeover(&mut self) {
        if let Some(peer) = self.takeover_request.take() {
            self.say(format!("the machine refused the form to {peer}"), true);
        }
    }

    /// Somebody at the machine approving a browser's request. There is no
    /// value to check, because nothing was sent: being able to press this key
    /// is the proof, and it cannot be done from the network.
    pub fn approve_install(&mut self) -> Result<(), String> {
        self.install_request.take().ok_or("nothing is waiting to be approved")?;
        self.start_install()
    }

    /// Somebody at the machine refusing a request they did not make.
    pub fn refuse_install(&mut self) {
        if let Some(who) = self.install_request.take() {
            self.say(format!("the request from {who} was refused at the machine"), true);
        }
    }

    /// A wrong pairing code from this address.
    pub fn note_bad_code(&mut self, peer: &str) -> BadCode {
        let n = self.code_failures.entry(peer.to_string()).or_insert(0);
        *n += 1;
        let n = *n;
        if n >= CODE_LOCK_AFTER {
            self.locked_out.insert(peer.to_string());
            self.say(format!("{peer} has typed {n} wrong codes and is now refused; clear it here to let it try again"), true);
            BadCode::Locked
        } else if n >= CODE_SLOW_AFTER {
            BadCode::SlowDown
        } else {
            BadCode::Counted
        }
    }

    /// A right code: that address starts again from nothing.
    pub fn note_good_code(&mut self, peer: &str) {
        self.code_failures.remove(peer);
    }

    pub fn is_locked_out(&self, peer: &str) -> bool {
        self.locked_out.contains(peer)
    }

    /// Someone at the machine letting a locked-out address try again.
    pub fn clear_lockouts(&mut self) {
        if self.locked_out.is_empty() {
            return;
        }
        self.locked_out.clear();
        self.code_failures.clear();
        self.say("every refused address may try the code again", false);
    }

    /// Ask the published list what the card in this machine is.
    ///
    /// ⚠️ Only when there is one. A server with no graphics card never
    /// fetches this, which is the point of keeping the list out of the
    /// binary. Called after the Welcome screen, because that is where the
    /// network is established, and tried once.
    pub fn ensure_card_known(&mut self) {
        if self.card_list_tried || self.machine.best_gpu().is_none() {
            return;
        }
        self.card_list_tried = true;
        let Some(id) = self.machine.best_gpu().map(|g| g.id.clone()) else { return };
        if let Some(list) = crate::machine::fetch_card_list() {
            self.card = crate::machine::card_from_list(&list, &id);
        }
    }

    /// Modules the assistant answer turns on. Hermes is the assistant itself
    /// and runs anywhere; ollama and its front end are added only when this
    /// machine has a card that can actually serve a model, because without
    /// one they would sit there doing nothing useful.
    pub fn ai_modules(&self) -> Vec<&'static str> {
        let mut m = vec!["hermes-agent"];
        if self.can_run_models_locally() {
            m.push("ollama");
            m.push("open-webui");
        }
        m
    }

    /// Whether models could run on this machine rather than somebody else's.
    pub fn can_run_models_locally(&self) -> bool {
        self.card.as_ref().map(|c| c.vram_gb >= 6).unwrap_or(false)
    }

    /// Say yes or no to an assistant.
    ///
    /// ⚠️ The modules this turns on are recorded as the wizard's doing, not
    /// the reader's, so the Kit screen still reads "Starter" rather than
    /// falling through to "Custom" — the same reason marking a data disk does
    /// not rename your kit.
    pub fn set_ai(&mut self, want: bool) {
        self.ai = want;
        let names = self.ai_modules();
        for name in &names {
            let was = self.modules.iter().find(|m| m.name == *name).map(|m| m.chosen);
            if let Some(m) = self.modules.iter_mut().find(|m| m.name == *name) {
                m.chosen = want;
            }
            if want && was == Some(false) {
                self.auto_modules.insert((*name).to_string());
            }
            if !want {
                self.auto_modules.remove(*name);
            }
        }
        // Anything the assistant was alone in wanting goes with it.
        if !want {
            for name in ["ollama", "open-webui"] {
                if let Some(m) = self.modules.iter_mut().find(|m| m.name == name) {
                    if self.kits[self.kit].modules.iter().all(|k| k != name) {
                        m.chosen = false;
                    }
                }
            }
        }
        self.kit = self.chosen_kit();
        self.refresh_secrets();
        self.say(
            if want {
                let local = if self.can_run_models_locally() { " Models will run on this machine's card." } else { "" };
                format!("an assistant it is.{local}")
            } else {
                "no assistant; nothing else changes".to_string()
            },
            false,
        );
    }

    /// The credential rows that belong on the assistant's screen rather than
    /// the one about domains.
    pub fn ai_secret(&self, option: &str) -> bool {
        option.starts_with("homelab.hermes")
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
            } else if r.skipped {
                // Skipping is an offer this screen makes; the generator has
                // to be told, or it refuses and the install dies on the last
                // screen with the reader having done nothing wrong.
                gen.push("--skip-secret".into());
                gen.push(r.option.clone());
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
                "chassis": self.machine.chassis,
                "cpu": self.machine.cpu,
                "cores": self.machine.cores,
                // Every display adapter, with the management chips marked, so
                // a reader can see why a card was or was not counted.
                "gpus": self.machine.gpus.iter().map(|g| json!({
                    "id": g.id, "vendor": g.vendor, "primary": g.primary, "usable": g.usable,
                })).collect::<Vec<_>>(),
                "gpu": self.machine.best_gpu().map(|g| json!({
                    "id": g.id,
                    "vendor": g.vendor,
                    // Filled in from the published list once there is a
                    // network. Absent means "not looked up yet, or the card
                    // is not one the list knows".
                    "card": self.card.as_ref().map(|c| json!({
                        "name": c.name, "vram_gb": c.vram_gb, "tier": c.tier,
                        "runs": c.runs, "note": c.note,
                    })),
                })),
            },
            "memory": { "need_mib": need, "verdict": verdict, "short": short },
            "kit": self.chosen_kit(),
            "kits": self.kits.iter().enumerate().map(|(i, k)| {
                let closed = self.schema.close_over_requires(&k.modules).map(|(m, _)| m).unwrap_or_else(|_| k.modules.clone());
                let n = crate::plan::memory_need(self.schema, &closed);
                let custom = k.modules.iter().any(|m| m == CUSTOM);
                let need = if custom { crate::plan::memory_need(self.schema, &self.closed()) } else { n };
                let blocked = kit_blocked(k, &self.machine, self.card.as_ref());
                json!({
                    "name": k.name, "blurb": k.blurb,
                    "need_gb": (need + 511) / 1024,
                    "fits": self.ram_mib == 0 || need <= self.ram_mib,
                    "offered": blocked.is_none(),
                    "why_not": blocked,
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
                "option": r.option, "short": r.short(), "title": r.title, "steps": r.steps, "walkthrough": r.walkthrough,
                // Which screen asks for it: the assistant's own, or the one
                // about domains. The rule lives here, not in the page.
                "ai": self.ai_secret(&r.option),
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
            // The page seals secret values to this key. The fingerprint is
            // on the console so a reader can check they are talking to this
            // machine; it is never used as a secret.
            "sealing": { "public_key": self.sealer.public_hex(), "fingerprint": self.sealer.fingerprint() },
            "cleartext_secret_seen": self.saw_cleartext_secret,
            "ai": {
                "wanted": self.ai,
                "local_models": self.can_run_models_locally(),
                "card": self.card.as_ref().map(|c| json!({ "name": c.name, "runs": c.runs })),
                "modules": self.ai_modules(),
            },
            "awaiting_console": self.install_request.is_some(),
            "awaiting_takeover": self.takeover_request.is_some(),
            "update": self.update.as_ref().map(|u| json!({
                "newest": u.newest, "newer": u.newer, "pending": u.pending,
                "store_path": u.store_path, "published": u.published,
                "key_name": CACHE_KEY_NAME, "trusted_keys": trusted_keys(),
                "restarting": self.relaunch.is_some(),
            })),
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

    /// The second audit's finding: the updater reported one revision and
    /// built whatever the branch pointed at. The marker's store path is the
    /// cheap way to prove the two agree.
    #[test]
    fn an_update_that_is_not_what_was_promised_is_refused() {
        let built = "/nix/store/aaaa-homelab-configure-0.1.0";
        assert!(promised_matches(built, Some(built), "abc1234").is_ok());
        assert!(promised_matches(built, None, "abc1234").is_ok(), "an older marker without a path cannot be checked");
        let e = promised_matches(built, Some("/nix/store/bbbb-homelab-configure-0.1.0"), "abc1234").unwrap_err();
        assert!(e.contains("refusing this update"), "got: {e}");
        assert!(e.contains("abc1234") && e.contains("bbbb") && e.contains("aaaa"), "it says all three: {e}");
    }

    #[test]
    fn a_short_rev_matches_the_long_one_it_abbreviates() {
        assert!(same_rev("a5d81484168f4241321b34360954628be0e2fb36", "a5d8148"));
        assert!(same_rev("a5d8148", "a5d81484168f4241321b34360954628be0e2fb36"));
        assert!(!same_rev("a5d8148", "8575481"));
        // Too short to mean anything: six characters is not a revision.
        assert!(!same_rev("a5d814", "a5d81484168f4241321b34360954628be0e2fb36"));
        assert!(!same_rev("", "a5d8148"));
    }

    /// The first report of this read "could not fetch the newer installer:
    /// /nix/store/dg1699…", because nix prints `Output paths:` and the path
    /// even when the build FAILED, and the message took the last line.
    #[test]
    fn a_missing_substitute_is_named_as_one() {
        let stderr = "\
error: Cannot build '/nix/store/k48mnl-homelab-configure-0.1.0.drv'.
       Reason: required local builds are disabled (max-jobs = 0) and no suitable substitute was found.
       Output paths:
         /nix/store/dg1699hh5m0q7j22ly3mdi3hm51l45s6-homelab-configure-0.1.0";
        let msg = fetch_failure(stderr);
        assert!(msg.contains("not in the binary cache yet"), "got: {msg}");
        assert!(!msg.contains("/nix/store/dg1699"), "the path is not the reason: {msg}");
    }

    #[test]
    fn another_failure_keeps_its_error_line() {
        let msg = fetch_failure("warning: something\nerror: unable to download: HTTP error 403\nsome trailing noise");
        assert!(msg.contains("HTTP error 403"), "got: {msg}");
    }

    #[test]
    fn only_the_networks_this_thing_lives_on_may_speak_to_it() {
        let yes = ["127.0.0.1", "10.0.0.5", "172.16.3.4", "172.31.255.254", "192.168.1.50", "169.254.7.1", "100.100.1.2", "::1", "fd00::1", "fe80::1", "::ffff:192.168.1.50"];
        let no = ["8.8.8.8", "1.1.1.1", "172.32.0.1", "172.15.0.1", "203.0.113.9", "100.128.0.1", "100.63.255.255", "2606:4700::1", "::ffff:8.8.8.8"];
        for a in yes {
            assert!(is_local_peer(&a.parse().unwrap()), "{a} should be allowed");
        }
        for a in no {
            assert!(!is_local_peer(&a.parse().unwrap()), "{a} should be refused");
        }
    }

    #[test]
    fn the_pairing_code_cannot_be_misread() {
        let c = new_pairing_code();
        assert_eq!(c.len(), CODE_LEN);
        for ch in c.chars() {
            assert!(CODE_ALPHABET.contains(&(ch as u8)), "{ch} is not in the alphabet");
            assert!(!"O0I1LS5B8Z2".contains(ch), "{ch} is easy to misread");
        }
        // Two in a row being equal would mean it is not random at all.
        assert_ne!(new_pairing_code(), new_pairing_code());
    }

    #[test]
    fn wrong_codes_slow_down_and_then_stop() {
        let mut w = test_wizard();
        for i in 1..CODE_SLOW_AFTER {
            assert_eq!(w.note_bad_code("192.168.1.9"), BadCode::Counted, "attempt {i}");
        }
        assert_eq!(w.note_bad_code("192.168.1.9"), BadCode::SlowDown);
        for _ in CODE_SLOW_AFTER + 1..CODE_LOCK_AFTER {
            assert_eq!(w.note_bad_code("192.168.1.9"), BadCode::SlowDown);
        }
        assert_eq!(w.note_bad_code("192.168.1.9"), BadCode::Locked);
        assert!(w.is_locked_out("192.168.1.9"));
        // One address's guessing does not shut anyone else out.
        assert!(!w.is_locked_out("192.168.1.10"));
        assert_eq!(w.note_bad_code("192.168.1.10"), BadCode::Counted);
        // Only someone at the machine can undo it.
        w.clear_lockouts();
        assert!(!w.is_locked_out("192.168.1.9"));
    }

    #[test]
    fn a_right_code_forgets_the_wrong_ones() {
        let mut w = test_wizard();
        w.note_bad_code("192.168.1.9");
        w.note_bad_code("192.168.1.9");
        w.note_good_code("192.168.1.9");
        assert_eq!(w.note_bad_code("192.168.1.9"), BadCode::Counted);
    }

    /// The browser is configuration authority, not destructive authority:
    /// pressing Install there asks the machine's own screen instead of
    /// erasing anything.
    #[test]
    fn a_browser_alone_cannot_erase_a_disk() {
        let mut w = test_wizard();
        w.live_usb = true;
        w.controller = Some("192.168.1.9".into());
        w.step = Step::Review;
        assert!(w.advance_from(Origin::Browser).is_ok());
        assert_eq!(w.step, Step::Review, "nothing started");
        assert_eq!(w.install_request.as_deref(), Some("192.168.1.9"), "the machine is asking about that browser");

        // ⚠️ There is nothing for the network to carry, guess or replay. The
        // earlier design put a six-digit number on the console and had it
        // typed back over the same plain HTTP, and would mint another after
        // three wrong guesses.
        let json = w.state_json();
        assert_eq!(json["awaiting_console"], serde_json::json!(true));
        assert!(json.get("awaiting_pin").is_none(), "no number is involved any more");
        assert!(
            !json.to_string().contains("192.168.1.9") || json["web"]["controller"] == serde_json::json!("192.168.1.9"),
            "the only place the address appears is where it already did"
        );
    }

    /// Third audit, finding 3: the pairing code travels over the same
    /// unencrypted connection as everything else, so it can be read off the
    /// network. It must not also be enough to seize the form.
    /// Fifth audit, finding 1: replacing this process in the window between a
    /// browser pressing Install and somebody pressing Y would leave the erase
    /// running with nothing supervising it.
    #[test]
    fn no_self_update_while_the_machine_is_being_asked_something() {
        let mut w = test_wizard();
        w.live_usb = true;
        w.step = Step::Review;
        assert!(w.advance_from(Origin::Browser).is_ok());
        let e = w.apply_update().unwrap_err();
        assert!(e.contains("whether to install"), "got: {e}");

        w.refuse_install();
        assert!(w.request_takeover("192.168.1.9"), "the first to ask simply gets it");
        assert!(!w.request_takeover("192.168.1.50"), "a second one has to be allowed at the machine");
        let e = w.apply_update().unwrap_err();
        assert!(e.contains("who should hold the form"), "got: {e}");
    }

    /// Fifth audit, finding 2: the browser must not gain anything by skipping
    /// its own JavaScript.
    #[test]
    fn set_value_refuses_an_option_the_screen_is_not_offering() {
        let mut w = test_wizard();
        let e = w.set_value("homelab.nginxAccess.containerBridges", "[]").unwrap_err();
        assert!(e.contains("not one of the values"), "got: {e}");
        assert!(!w.values.contains_key("homelab.nginxAccess.containerBridges"), "and nothing was written");

        // Whatever the screen really is offering still works.
        if let Some((name, _, _)) = w.open_values().first().cloned() {
            assert!(w.set_value(&name, "something").is_ok(), "{name} is on the screen");
        }
    }

    #[test]
    fn a_second_browser_cannot_take_the_form_without_the_machine() {
        let mut w = test_wizard();
        assert!(w.request_takeover("192.168.1.9"), "the first to ask gets it: there is nothing to take");
        assert_eq!(w.controller.as_deref(), Some("192.168.1.9"));
        assert!(w.request_takeover("192.168.1.9"), "asking again when you already hold it is free");

        assert!(!w.request_takeover("192.168.1.50"), "somebody else has it: ask the machine");
        assert_eq!(w.controller.as_deref(), Some("192.168.1.9"), "and nothing moved");
        assert_eq!(w.takeover_request.as_deref(), Some("192.168.1.50"));

        w.refuse_takeover();
        assert_eq!(w.controller.as_deref(), Some("192.168.1.9"), "refused: still theirs");
        assert!(w.takeover_request.is_none());

        assert!(!w.request_takeover("192.168.1.50"));
        w.approve_takeover();
        assert_eq!(w.controller.as_deref(), Some("192.168.1.50"), "approved at the machine");
    }

    #[test]
    fn only_someone_at_the_machine_can_approve_or_refuse() {
        let mut w = test_wizard();
        w.live_usb = true;
        w.step = Step::Review;
        assert!(w.approve_install().is_err(), "nothing is waiting yet");

        assert!(w.advance_from(Origin::Browser).is_ok());
        assert!(w.install_request.is_some());
        w.refuse_install();
        assert!(w.install_request.is_none());
        assert!(w.approve_install().is_err(), "a refused request cannot be approved after the fact");
    }

    /// An install started at the console needs no second approval: that
    /// person is already standing at the machine.
    #[test]
    fn the_console_needs_no_approval_of_its_own() {
        let mut w = test_wizard();
        w.live_usb = true;
        w.step = Step::Review;
        let _ = w.advance_from(Origin::Console);
        assert!(w.install_request.is_none(), "the console does not ask itself");
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
    /// The card gate is a rule of the model, not a style on a button.
    #[test]
    fn a_kit_that_needs_a_card_is_refused_without_one() {
        let mut w = test_wizard();
        let ai = w.kits.iter().position(|k| k.needs_vram_gb > 0).expect("a kit that wants a card");
        let before = w.kit;

        // No card at all.
        w.machine = crate::machine::Machine::default();
        assert!(kit_blocked(&w.kits[ai], &w.machine, None).unwrap().contains("no graphics card"));
        w.set_kit(ai);
        assert_eq!(w.kit, before, "the kit did not change");

        // A card, but the list has never heard of it.
        w.machine.gpus = vec![crate::machine::Gpu {
            id: "10de:ffff".into(), vendor: "NVIDIA".into(), primary: true, usable: true,
        }];
        assert!(kit_blocked(&w.kits[ai], &w.machine, None).unwrap().contains("10de:ffff"));

        // A card the list knows, and too small.
        let small = crate::machine::Card {
            name: "Some Card".into(), vendor: "NVIDIA".into(), vram_gb: 4,
            note: None, tier: "none".into(), runs: "nothing".into(),
        };
        assert!(kit_blocked(&w.kits[ai], &w.machine, Some(&small)).unwrap().contains("4 GB"));

        // A card the list knows, and big enough.
        let big = crate::machine::Card { vram_gb: 24, tier: "large".into(), ..small };
        assert!(kit_blocked(&w.kits[ai], &w.machine, Some(&big)).is_none());
        w.card = Some(big);
        w.set_kit(ai);
        assert_eq!(w.kit, ai, "with a real card it is an ordinary kit");
    }

    /// An assistant is a choice, and choosing it must not look like choosing
    /// a different kit: the same reason marking a data disk does not rename
    /// one.
    #[test]
    fn saying_yes_to_an_assistant_does_not_rename_the_kit() {
        let mut w = test_wizard();
        let starter = w.kits.iter().position(|k| k.name == "Starter").unwrap();
        w.set_kit(starter);
        assert_eq!(w.chosen_kit(), starter);

        w.set_ai(true);
        assert!(w.ai);
        assert_eq!(w.chosen_kit(), starter, "still Starter");

        w.set_ai(false);
        assert!(!w.ai);
        assert_eq!(w.chosen_kit(), starter, "and still Starter after changing your mind");
    }

    /// Without a card that can serve a model, an assistant is still offered:
    /// it just talks to somebody else's. With one, the local pieces come too.
    #[test]
    fn local_models_are_added_only_when_the_card_can_serve_them() {
        let mut w = test_wizard();
        assert_eq!(w.ai_modules(), vec!["hermes-agent"], "no card: the harness alone");

        w.card = Some(crate::machine::Card {
            name: "Some Card".into(), vendor: "NVIDIA".into(), vram_gb: 24,
            note: None, tier: "large".into(), runs: "big models".into(),
        });
        assert!(w.can_run_models_locally());
        assert_eq!(w.ai_modules(), vec!["hermes-agent", "ollama", "open-webui"]);

        w.card = Some(crate::machine::Card {
            name: "Tiny".into(), vendor: "Intel".into(), vram_gb: 2,
            note: None, tier: "none".into(), runs: "nothing".into(),
        });
        assert!(!w.can_run_models_locally(), "2 GB is not a card to serve from");
        assert_eq!(w.ai_modules(), vec!["hermes-agent"]);
    }

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
        // By name, not by position: kits get added in the middle.
        let minimal = ks.iter().find(|k| k.name == "Minimal").expect("a Minimal kit");
        assert!(minimal.modules.is_empty());
        // Only the one kit asks for a graphics card, and it asks for a real one.
        let gpu_kits: Vec<_> = ks.iter().filter(|k| k.needs_vram_gb > 0).collect();
        assert_eq!(gpu_kits.len(), 1, "one kit wants a card: {:?}", gpu_kits.iter().map(|k| k.name).collect::<Vec<_>>());
        assert!(gpu_kits[0].needs_vram_gb >= 6);
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
