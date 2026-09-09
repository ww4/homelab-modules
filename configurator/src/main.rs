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
mod secrets;
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
}

#[derive(Args)]
struct SchemaArgs {
    /// Restrict to these modules (plus what they require).
    #[arg(long, value_delimiter = ',')]
    modules: Vec<String>,
}

#[derive(Args)]
struct GenerateArgs {
    /// The answers file (see README for the shape).
    #[arg(long, value_name = "FILE")]
    answers: PathBuf,
    /// Output directory — the new flake. Must not exist unless --force.
    #[arg(long, value_name = "DIR")]
    out: PathBuf,
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
            let text = fs::read_to_string(&a.answers)
                .with_context(|| format!("reading {}", a.answers.display()))?;
            let mut answers: Answers = serde_json::from_str(&text)
                .with_context(|| format!("parsing {}", a.answers.display()))?;
            if let Some(l) = a.library {
                answers.library = Some(l);
            }
            if let Some(r) = a.admin_recipient {
                answers.sops.admin_recipient = Some(r);
            }
            let supplied = secrets::parse_supplied(&a.secrets)?;
            let plan = plan::Plan::build(&schema, &answers, &supplied).map_err(anyhow::Error::from)?;

            prepare_out(&a.out, a.force)?;
            let report = emit::write_all(&plan, &a.out)?;

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
