// Bake the schema into the binary. The nix package sets HOMELAB_CATALOG_JSON
// and HOMELAB_OPTIONS_JSON to files generated from the library at the same
// revision; a bare `cargo build` embeds empty placeholders and the binary then
// needs --catalog/--options at run time.
use std::{env, fs, path::PathBuf};

fn main() {
    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR"));
    // The nixpkgs rev the generated flake pins: the one this configurator's own
    // flake.lock holds, which is kept at the rev the library was validated
    // against. Bumping it is a deliberate lock update, not a side effect of
    // whoever runs the installer last.
    println!("cargo:rerun-if-changed=flake.lock");
    let rev = fs::read_to_string("flake.lock")
        .ok()
        .and_then(|t| serde_json::from_str::<serde_json::Value>(&t).ok())
        .and_then(|v| v["nodes"]["nixpkgs"]["locked"]["rev"].as_str().map(String::from))
        .unwrap_or_else(|| "nixos-26.05".to_string());
    fs::write(out.join("nixpkgs-rev.txt"), rev).expect("write nixpkgs rev");

    for (var, file, empty) in [
        ("HOMELAB_CATALOG_JSON", "catalog.json", "{}"),
        ("HOMELAB_OPTIONS_JSON", "options.json", "[]"),
    ] {
        println!("cargo:rerun-if-env-changed={var}");
        let content = match env::var(var) {
            Ok(path) => {
                println!("cargo:rerun-if-changed={path}");
                fs::read_to_string(&path).unwrap_or_else(|e| panic!("{var}={path}: {e}"))
            }
            Err(_) => empty.to_string(),
        };
        fs::write(out.join(file), content).expect("write embedded schema");
    }
}
