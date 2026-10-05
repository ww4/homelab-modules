//! homelab-configure — answers in, a private consumer flake out.
//!
//! Three subcommands, all headless and machine-readable with --json:
//!   schema    the question set (modules, their options, their secrets)
//!   generate  write the flake, mint/encrypt secrets, validate by evaluation
//!   validate  evaluate (or build) a generated flake's toplevel
//!
//! Interactive front-ends produce an answers file and call `generate`; an
//! agent does the same. Nothing lives only in a UI.

mod answers;
mod emit;
mod keys;
mod plan;
mod schema;
mod disks;
mod dns;
mod guides;
mod install;
mod secrets;
mod tui;
mod web;
mod wizard;
mod validate;

use anyhow::{anyhow, bail, Context, Result};
use clap::{Args, Parser, Subcommand, ValueEnum};
use serde::Serialize;
use std::fs;
use std::path::{Path, PathBuf};

use crate::answers::Answers;
use crate::schema::Schema;

#[derive(Parser)]
#[command(name = "homelab-configure", version, about)]
struct Cli {
    /// Emit results as JSON on stdout (errors as JSON on stderr).
    #[arg(long, global = true)]
    json: bool,
    /// Catalog JSON to use instead of the embedded one (`nix eval --json <library>#catalog`).
    #[arg(long, global = true, value_name = "FILE")]
    catalog: Option<PathBuf>,
    /// Option docs JSON to use instead of the embedded one (`nix eval --raw <library>/configurator#optionsJson.<system>`).
    #[arg(long, global = true, value_name = "FILE")]
    options: Option<PathBuf>,
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Print the question set: every module with its options and secrets.
    Schema(SchemaArgs),
    /// Generate a consumer flake from an answers file.
    Generate(GenerateArgs),
    /// Evaluate (or build) a generated flake.
    Validate(ValidateArgs),
    /// Interactive front end: fill in the answers in the terminal, then generate.
    Tui(TuiArgs),
    /// Serve the browser installer only (no terminal needed): the same wizard at http://<this machine>:8099.
    Web(TuiArgs),
    /// Create the A records the chosen modules need at Cloudflare (run on the installed box, or pass --ip).
    Dns(DnsArgs),
    /// Install a generated flake onto THIS machine (from a live USB): disko, nixos-install, host key, and the flake carried onto the new system.
    Install(InstallArgs),
}

#[derive(Args)]
struct DnsArgs {
    /// The generated flake directory (answers.json).
    dir: PathBuf,
    /// Address to point the names at; default: the tailnet address, else the default route's.
    #[arg(long, value_name = "ADDR")]
    ip: Option<String>,
    /// File carrying CLOUDFLARE_DNS_API_TOKEN=…; default /run/secrets/acme-credentials or .secrets/acme-credentials.
    #[arg(long, value_name = "FILE")]
    token_file: Option<PathBuf>,
    /// Show what would change; write nothing.
    #[arg(long)]
    dry_run: bool,
}

#[derive(Args)]
struct InstallArgs {
    /// The generated flake directory (holds answers.json and extra-files/).
    dir: PathBuf,
    /// Host name (nixosConfigurations.<host>); defaults to the one in answers.json.
    #[arg(long)]
    host: Option<String>,
    /// Skip the typed confirmation. The named disks are erased.
    #[arg(long)]
    yes: bool,
    /// Print what would run; touch nothing.
    #[arg(long)]
    dry_run: bool,
    /// Where on the new system the flake directory is copied (it is in RAM on a live USB).
    #[arg(long, value_name = "PATH", default_value = "/root/homelab")]
    keep_at: String,
}

#[derive(Args)]
struct TuiArgs {
    /// Start from an answers file or a canned profile (configurator/profiles/*.json).
    #[arg(long, value_name = "FILE")]
    profile: Option<PathBuf>,
    /// Where to write the answers (default ./answers.json).
    #[arg(long, value_name = "FILE", default_value = "answers.json")]
    answers: PathBuf,
    /// Output directory for `generate` (default ./my-homelab).
    #[arg(long, value_name = "DIR", default_value = "my-homelab")]
    out: PathBuf,
    /// Do not serve the browser installer.
    #[arg(long)]
    no_web: bool,
    /// Port for the browser installer (default 8099).
    #[arg(long, default_value = "8099")]
    port: u16,
}

#[derive(Args)]
struct SchemaArgs {
    /// Restrict to these modules (plus what they require).
    #[arg(long, value_delimiter = ',')]
    modules: Vec<String>,
}

#[derive(Args)]
struct GenerateArgs {
    /// The answers file (see README for the shape). Optional when --out already
    /// holds an answers.json from a previous run: that is the starting point.
    #[arg(long, value_name = "FILE")]
    answers: Option<PathBuf>,
    /// Output directory — the new flake, or an existing one to reconfigure.
    #[arg(long, value_name = "DIR")]
    out: PathBuf,
    /// Reconfigure: modules to add to the previous answers. Repeatable or comma-separated.
    #[arg(long, value_name = "MODULE", value_delimiter = ',')]
    add: Vec<String>,
    /// Reconfigure: modules to remove (refused when another chosen module requires it, or for a foundation module).
    #[arg(long, value_name = "MODULE", value_delimiter = ',')]
    remove: Vec<String>,
    /// Reconfigure: set a homelab.* value, `homelab.x.y=<json>` (`null` unsets). Repeatable.
    #[arg(long = "set", value_name = "OPTION=JSON")]
    set: Vec<String>,
    /// A supplied secret: `<homelab.option>=@/path/to/file` or `<homelab.option>=env:VAR`. Repeatable.
    #[arg(long = "secret", value_name = "OPTION=SOURCE")]
    secrets: Vec<String>,
    /// Flake reference for the library input (overrides answers.library).
    #[arg(long, value_name = "FLAKEREF")]
    library: Option<String>,
    /// Existing age recipient for the admin (overrides answers.sops.adminRecipient); none = generate a key.
    #[arg(long, value_name = "age1...")]
    admin_recipient: Option<String>,
    /// What to run on the result.
    #[arg(long, value_enum, default_value_t = ValidateMode::Eval)]
    validate: ValidateMode,
    /// Overwrite an existing output directory.
    #[arg(long)]
    force: bool,
}

#[derive(Args)]
struct ValidateArgs {
    /// The generated flake directory.
    dir: PathBuf,
    /// Host name (nixosConfigurations.<host>); defaults to the only host in the flake.
    #[arg(long)]
    host: Option<String>,
    /// Build the toplevel instead of only evaluating it.
    #[arg(long)]
    build: bool,
    /// Override the library input with a local checkout (path:/…).
    #[arg(long, value_name = "FLAKEREF")]
    library: Option<String>,
}

#[derive(Clone, Copy, ValueEnum, Serialize, PartialEq, Eq, Debug)]
#[serde(rename_all = "lowercase")]
enum ValidateMode {
    /// Evaluate the toplevel derivation (catches every option/type error; no downloads beyond inputs).
    Eval,
    /// Build the toplevel (the full proof; slow).
    Build,
    /// Skip.
    None,
}

/// Exit codes: 0 ok · 1 unexpected · 2 answers rejected · 3 validation failed.
fn main() {
    let cli = Cli::parse();
    let json = cli.json;
    match run(cli) {
        Ok(code) => std::process::exit(code),
        Err(e) => {
            if json {
                let err = serde_json::json!({ "ok": false, "error": format!("{e:#}") });
                eprintln!("{}", serde_json::to_string_pretty(&err).unwrap());
            } else {
                eprintln!("error: {e:#}");
            }
            let code = e.downcast_ref::<Rejected>().map(|_| 2).unwrap_or(1);
            std::process::exit(code);
        }
    }
}

/// The answers were invalid — reported with the full list, exit 2.
#[derive(Debug)]
pub struct Rejected(pub Vec<String>);
impl std::fmt::Display for Rejected {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        writeln!(f, "answers rejected:")?;
        for p in &self.0 {
            writeln!(f, "  - {p}")?;
        }
        Ok(())
    }
}
impl std::error::Error for Rejected {}

fn run(cli: Cli) -> Result<i32> {
    let schema = load_schema(cli.catalog.as_deref(), cli.options.as_deref())?;
    match cli.cmd {
        Cmd::Schema(a) => {
            let set = schema.question_set(&a.modules)?;
            if cli.json {
                println!("{}", serde_json::to_string_pretty(&set)?);
            } else {
                print!("{}", set.render_text());
            }
            Ok(0)
        }
        Cmd::Generate(a) => {
            // An existing install: <out>/answers.json is what it was generated
            // from, and this run is a reconfigure over it.
            let previous: Option<Answers> = {
                let p = a.out.join("answers.json");
                if p.exists() {
                    let text = fs::read_to_string(&p).with_context(|| format!("reading {}", p.display()))?;
                    Some(serde_json::from_str(&text).with_context(|| format!("parsing {}", p.display()))?)
                } else {
                    None
                }
            };
            let mut answers: Answers = match (&a.answers, &previous) {
                (Some(file), _) => {
                    let text = fs::read_to_string(file).with_context(|| format!("reading {}", file.display()))?;
                    serde_json::from_str(&text).with_context(|| format!("parsing {}", file.display()))?
                }
                (None, Some(prev)) => prev.clone(),
                (None, None) => bail!(
                    "--answers is required for a new output directory ({} has no answers.json)",
                    a.out.display()
                ),
            };
            if a.answers.is_some() {
                if let Some(prev) = &previous {
                    // A new answers file over an old install keeps the recorded
                    // password hash unless the file itself carries one.
                    if answers.host.admin_password_hash.is_none() {
                        answers.host.admin_password_hash = prev.host.admin_password_hash.clone();
                    }
                }
            }
            let set: Vec<(String, serde_json::Value)> = a
                .set
                .iter()
                .map(|kv| {
                    let (k, v) = kv
                        .split_once('=')
                        .ok_or_else(|| anyhow!("--set {kv}: expected OPTION=JSON"))?;
                    let v: serde_json::Value = serde_json::from_str(v)
                        .or_else(|_| serde_json::from_str(&format!("\"{v}\"")))
                        .with_context(|| format!("--set {k}: value is not JSON"))?;
                    Ok((k.to_string(), v))
                })
                .collect::<Result<_>>()?;
            if previous.is_none() && (!a.add.is_empty() || !a.remove.is_empty() || !set.is_empty()) {
                bail!("--add/--remove/--set reconfigure an existing output; {} has no answers.json", a.out.display());
            }
            answers.apply(&a.add, &a.remove, &set);
            if let Some(l) = a.library {
                answers.library = Some(l);
            }
            if let Some(r) = a.admin_recipient {
                answers.sops.admin_recipient = Some(r);
            }
            let supplied = secrets::parse_supplied(&a.secrets)?;
            let existing_secrets = existing_secret_names(&a.out)?;
            let plan = plan::Plan::build(&schema, &answers, &supplied, previous.as_ref(), &existing_secrets)
                .map_err(anyhow::Error::from)?;

            let reconfigure = previous.is_some();
            prepare_out(&a.out, a.force || reconfigure)?;
            let mut report = emit::write_all(&plan, &answers, &a.out, reconfigure)?;
            // A --remove that `requires` pulled straight back in did nothing;
            // say so rather than let it read as done.
            for m in &a.remove {
                if plan.modules.contains(m) {
                    let dependents: Vec<&String> = plan
                        .modules
                        .iter()
                        .filter(|o| schema.catalog.get(*o).map(|meta| meta.requires.contains(m)).unwrap_or(false))
                        .collect();
                    report.warnings.push(format!(
                        "--remove {m}: still imported — required by {}",
                        dependents.iter().map(|d| d.as_str()).collect::<Vec<_>>().join(", ")
                    ));
                }
            }

            // No input override here: flake.nix already names the library the
            // answers chose (a path: reference included), so nix can write the
            // consumer's flake.lock as part of validating it.
            let validation = match a.validate {
                ValidateMode::None => None,
                mode => Some(validate::run(
                    &a.out,
                    &plan.host.name,
                    mode == ValidateMode::Build,
                    None,
                )?),
            };
            let ok = validation.as_ref().map(|v| v.ok).unwrap_or(true);
            let out = GenerateReport { ok, validation, report };
            if cli.json {
                println!("{}", serde_json::to_string_pretty(&out)?);
            } else {
                print!("{}", out.render_text());
            }
            Ok(if ok { 0 } else { 3 })
        }
        Cmd::Tui(a) => {
            // The screens run generate/install themselves as child processes
            // of this same binary; they need the global flags.
            let mut prefix = Vec::new();
            if let Some(c) = &cli.catalog { prefix.push("--catalog".to_string()); prefix.push(c.display().to_string()); }
            if let Some(o) = &cli.options { prefix.push("--options".to_string()); prefix.push(o.display().to_string()); }
            // The browser front end shares the model across threads, which
            // wants a schema that outlives them; this process ends with it.
            let schema: &'static Schema = Box::leak(Box::new(schema));
            tui::run(schema, a.profile.as_deref(), &a.answers, &a.out, prefix, !a.no_web, a.port)
        }
        Cmd::Web(a) => {
            let mut prefix = Vec::new();
            if let Some(c) = &cli.catalog { prefix.push("--catalog".to_string()); prefix.push(c.display().to_string()); }
            if let Some(o) = &cli.options { prefix.push("--options".to_string()); prefix.push(o.display().to_string()); }
            let schema: &'static Schema = Box::leak(Box::new(schema));
            let mut wiz = wizard::Wizard::new(schema, &a.answers, &a.out, prefix, a.port);
            if let Some(p) = &a.profile {
                let text = std::fs::read_to_string(p)?;
                wiz.load(&serde_json::from_str(&text)?);
            }
            wiz.watch_network();
            let port = wiz.web_port;
            let code = wiz.pairing.clone();
            let shared = std::sync::Arc::new(std::sync::Mutex::new(wiz));
            web::serve(shared.clone()).map_err(|e| anyhow::anyhow!(e))?;
            let (address, _) = shared.lock().unwrap().network();
            println!("browser installer: http://{}:{port}/?code={code}", if address.is_empty() { "<this machine>".into() } else { address });
            println!("Ctrl-C stops it. The same answers file and flake directory as `tui`.");
            // The install runs in its own threads; this one keeps the process
            // alive and moves the model on when a child finishes.
            loop {
                std::thread::sleep(std::time::Duration::from_millis(500));
                shared.lock().unwrap().poll_install();
            }
        }
        Cmd::Dns(a) => {
            let r = dns::run(&schema, &a.dir, a.ip.as_deref(), a.token_file.as_deref(), a.dry_run)?;
            if cli.json {
                println!("{}", serde_json::to_string_pretty(&r)?);
            } else {
                print!("{}", r.render_text());
            }
            Ok(0)
        }
        Cmd::Install(a) => {
            let r = install::run(&schema, &a.dir, a.host.as_deref(), a.yes, a.dry_run, &a.keep_at)?;
            if cli.json {
                println!("{}", serde_json::to_string_pretty(&r)?);
            } else {
                print!("{}", r.render_text());
            }
            Ok(0)
        }
        Cmd::Validate(a) => {
            let host = match a.host {
                Some(h) => h,
                None => validate::only_host(&a.dir)?,
            };
            let v = validate::run(&a.dir, &host, a.build, a.library.as_deref())?;
            if cli.json {
                println!("{}", serde_json::to_string_pretty(&v)?);
            } else {
                print!("{}", v.render_text());
            }
            Ok(if v.ok { 0 } else { 3 })
        }
    }
}

fn load_schema(catalog: Option<&Path>, options: Option<&Path>) -> Result<Schema> {
    let catalog_text = match catalog {
        Some(p) => fs::read_to_string(p).with_context(|| format!("reading {}", p.display()))?,
        None => schema::EMBEDDED_CATALOG.to_string(),
    };
    let options_text = match options {
        Some(p) => fs::read_to_string(p).with_context(|| format!("reading {}", p.display()))?,
        None => schema::EMBEDDED_OPTIONS.to_string(),
    };
    if catalog_text.trim() == "{}" || options_text.trim() == "[]" {
        bail!(
            "this binary was built without an embedded schema; pass --catalog and --options \
             (build it with `nix build <library>/configurator` to embed them)"
        );
    }
    Schema::parse(&catalog_text, &options_text)
}

/// sops secret names whose encrypted file already exists under <out>/secrets/.
fn existing_secret_names(out: &Path) -> Result<std::collections::BTreeSet<String>> {
    let mut names = std::collections::BTreeSet::new();
    let dir = out.join("secrets");
    if dir.is_dir() {
        for entry in fs::read_dir(&dir)? {
            let p = entry?.path();
            if let (Some(stem), Some("yaml")) = (p.file_stem(), p.extension().and_then(|e| e.to_str())) {
                names.insert(stem.to_string_lossy().to_string());
            }
        }
    }
    Ok(names)
}

fn prepare_out(out: &Path, force: bool) -> Result<()> {
    if out.exists() {
        let non_empty = fs::read_dir(out)?.next().is_some();
        if non_empty && !force {
            return Err(anyhow!(
                "{} exists and is not empty (use --force to overwrite)",
                out.display()
            ));
        }
        if force && non_empty {
            // Overwrite only what we write; never rm -rf a directory we did not create.
        }
    }
    fs::create_dir_all(out).with_context(|| format!("creating {}", out.display()))?;
    Ok(())
}

#[derive(Serialize)]
struct GenerateReport {
    ok: bool,
    #[serde(flatten)]
    report: emit::Report,
    validation: Option<validate::Validation>,
}

impl GenerateReport {
    fn render_text(&self) -> String {
        let mut s = self.report.render_text();
        if let Some(v) = &self.validation {
            s.push('\n');
            s.push_str(&v.render_text());
        }
        s
    }
}
