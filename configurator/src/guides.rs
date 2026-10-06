//! Guides for the secrets only the user can supply: where the value comes
//! from, what the file must look like, and — where an API lets us — a check
//! that the value works before the install depends on it. Shown on the TUI's
//! Secrets screen; `shape()` turns a bare typed value into the file the
//! module reads; `verify()` runs the check.

use std::collections::BTreeMap;
use std::process::Command;

pub struct Guide {
    pub title: &'static str,
    pub steps: &'static str,
    /// A page on the guide site that walks the whole thing through, for
    /// someone who has never seen the other company's web interface. The
    /// steps above are the short version for someone who has.
    pub walkthrough: Option<&'static str>,
}

// The walkthrough addresses below are published pages on the guide site, so
// they are part of this installer's interface. A reader at the console types
// one into a phone; a reader in the browser clicks it. Do not move a page
// without moving the link, and check it answers before shipping a change.

/// One variable inside a secret file: what to call it on screen, what it is,
/// and whether to hide it while it is typed. A secret is a FORM, not one
/// blob — "the file must carry WIREGUARD_PRIVATE_KEY, WIREGUARD_ADDRESSES,
/// SERVER_COUNTRIES" is no use to someone staring at a single prompt.
pub struct SecretField {
    pub var: String,
    pub label: String,
    pub help: String,
    pub masked: bool,
    pub optional: bool,
    /// A pick-list of (value, label); empty means free text.
    pub choices: Vec<(String, String)>,
    /// This answer is a `homelab.*` option, not a line of the credentials
    /// file — the VPN provider decides which lines the file even needs.
    pub is_option: bool,
}

fn f(var: &str, label: &str, help: &str, masked: bool, optional: bool) -> SecretField {
    SecretField { var: var.into(), label: label.into(), help: help.into(), masked, optional, choices: Vec::new(), is_option: false }
}

fn choice(var: &str, label: &str, help: &str, choices: &[(&str, &str)]) -> SecretField {
    SecretField {
        var: var.into(),
        label: label.into(),
        help: help.into(),
        masked: false,
        optional: false,
        choices: choices.iter().map(|(v, l)| (v.to_string(), l.to_string())).collect(),
        is_option: true,
    }
}

/// The variables a secret file needs, in the order a person fills them.
/// Empty means the value is a path to a file the user already has (the
/// MeshCentral .msh), not a set of variables.
pub fn fields(option: &str, values: &BTreeMap<String, String>) -> Vec<SecretField> {
    match option {
        "homelab.acme.credentialsFile" => vec![f(
            "CLOUDFLARE_DNS_API_TOKEN",
            "Cloudflare API token",
            "The token itself, about 40 characters. Cloudflare shows it once, when you create it.",
            true,
            false,
        )],
        "homelab.arrStack.vpnEnvFile" => {
            let provider = values.get("homelab.arrStack.vpnProvider").map(|s| s.trim().to_lowercase()).unwrap_or_default();
            // The provider comes first: it decides which lines the file needs.
            let mut out = vec![choice(
                "homelab.arrStack.vpnProvider",
                "VPN provider",
                "Who you have an account with. The boxes below change to the values that provider needs; gluetun supports many more, and `other` lets you name one.",
                &[("mullvad", "Mullvad"), ("protonvpn", "Proton VPN"), ("ivpn", "IVPN"), ("nordvpn", "NordVPN"), ("other", "another gluetun provider")],
            )];
            out.extend([
                f("WIREGUARD_PRIVATE_KEY", "WireGuard private key", "The PrivateKey line of the .conf your provider gave you, without `PrivateKey = `.", true, false),
                f("WIREGUARD_ADDRESSES", "WireGuard address", "The Address line of the same file, keeping the /32, e.g. 10.64.0.2/32.", false, false),
                f("SERVER_COUNTRIES", "Server country", "Where to come out, e.g. Netherlands. One country name.", false, false),
            ]);
            match provider.as_str() {
                "protonvpn" | "proton" => out.push(f("VPN_PORT_FORWARDING", "Port forwarding", "`on` to ask Proton for a forwarded port (needed for good seeding).", false, true)),
                "mullvad" => {}
                _ => out.push(f("FIREWALL_VPN_INPUT_PORTS", "Forwarded port", "The port your provider forwards, if it does. Leave empty otherwise; set qBittorrent's listen port to the same number.", false, true)),
            }
            out
        }
        "homelab.backup.remote.environmentFile" => vec![
            f("B2_ACCOUNT_ID", "Application key ID", "The keyID Backblaze shows when you add an application key.", false, false),
            f("B2_ACCOUNT_KEY", "Application key", "The applicationKey beside it, shown once.", true, false),
        ],
        _ => Vec::new(),
    }
}

/// The file content for a filled form: `KEY=value` lines, empty ones dropped.
pub fn compose(values: &[(String, String)]) -> String {
    let mut s = String::new();
    for (k, v) in values {
        let v = v.trim();
        if !v.is_empty() {
            s.push_str(&format!("{k}={v}\n"));
        }
    }
    s
}

/// The guide for a secret option, given the current values (the VPN guide
/// depends on `homelab.arrStack.vpnProvider`).
pub fn for_option(option: &str, values: &BTreeMap<String, String>) -> Option<Guide> {
    match option {
        "homelab.acme.credentialsFile" => Some(Guide {
            title: "Cloudflare DNS API token (TLS for every vhost, no inbound port)",
            steps: "Your domain's DNS must be at Cloudflare (free plan is fine): add the site there and point \
                    your registrar's nameservers at the two Cloudflare gives you.\n\
                    Then: dash.cloudflare.com → profile icon → My Profile → API Tokens → Create Token → \
                    use the \"Edit zone DNS\" template → Zone Resources: Include · Specific zone · your domain \
                    → Continue → Create → copy the token (shown once).\n\
                    The box below takes just the token; it is saved as CLOUDFLARE_DNS_API_TOKEN=… and \
                    checked against your zone right away. ACME uses it for DNS-01 challenges; \
                    `homelab-configure dns` uses it later to create the A records.",
            walkthrough: Some("https://ww4.github.io/imperfect-homelab/accounts/cloudflare-dns/"),
        }),
        "homelab.arrStack.vpnEnvFile" => {
            let provider = values.get("homelab.arrStack.vpnProvider").map(|s| s.trim().to_lowercase()).unwrap_or_default();
            let (title, steps) = match provider.as_str() {
                "mullvad" => ("Mullvad WireGuard credentials", "mullvad.net → account → WireGuard configuration → generate a key (Linux) → download a config for any location. \
                    Fill the boxes below from that .conf: PrivateKey and Address (keep the /32).\n\
                    Mullvad has no port forwarding: expect slower seeding; a tracker that needs an open port wants a provider that forwards one (Proton VPN does)."),
                "protonvpn" | "proton" => ("Proton VPN WireGuard credentials (port forwarding on paid plans)", "account.protonvpn.com → Downloads → WireGuard configuration → Linux · pick a P2P server · enable \"NAT-PMP (port forwarding)\" → Create → download. \
                    Fill the boxes below from the .conf: PrivateKey and Address. Set port forwarding to `on` for a forwarded port."),
                "" => ("VPN credentials for the download client", "Pick your provider on the first line below; the steps for that provider then appear here, and the boxes change to the values it needs."),
                _ => ("VPN credentials for the download client", "This provider is passed to gluetun as VPN_SERVICE_PROVIDER; the variables it needs are in gluetun's wiki page for it. \
                    Most WireGuard providers need a private key, an address and a country; one that forwards a port adds the port number (set qBittorrent's listen port to the same). \
                    (provider: {other})"),
            };
            let steps: &'static str = Box::leak(steps.replace("{other}", &provider).into_boxed_str());
            Some(Guide { title, steps, walkthrough: Some("https://ww4.github.io/imperfect-homelab/accounts/vpn/") })
        }
        "homelab.backup.remote.environmentFile" => Some(Guide {
            title: "Backblaze B2 credentials for the offsite restic repository",
            steps: "backblaze.com → B2 Cloud Storage → create a bucket (private) → Application Keys → Add a New Application Key, \
                    restricted to that bucket, read and write. Set homelab.backup.remote.repository to b2:<bucket-name>.\n\
                    Fill the two boxes below with the keyID and the applicationKey.\n\
                    Any restic backend works instead (S3, SFTP): then the variables are that backend's.",
            walkthrough: None,
        }),
        "homelab.meshagent.mshFile" => Some(Guide {
            title: "MeshCentral agent identity",
            steps: "On your MeshCentral server: My Devices → Add Agent → Linux → download the .msh file for the device group. Point this at that file.",
            walkthrough: None,
        }),
        _ => None,
    }
}

/// A bare typed value → the file content the module expects. `KEY=…` lines
/// pass through; ` | ` separates several on one input line.
pub fn shape(option: &str, typed: &str) -> String {
    let t = typed.trim();
    let lines: Vec<String> = t.split(" | ").map(|s| s.trim().to_string()).filter(|s| !s.is_empty()).collect();
    let text = if option == "homelab.acme.credentialsFile" && lines.len() == 1 && !lines[0].contains('=') {
        format!("CLOUDFLARE_DNS_API_TOKEN={}", lines[0])
    } else {
        lines.join("\n")
    };
    text + "\n"
}

/// Check a supplied value where an API makes that possible. Ok(message) or
/// Err(message); None when there is nothing to check for this option.
pub fn verify(option: &str, content: &str, values: &BTreeMap<String, String>) -> Option<Result<String, String>> {
    match option {
        "homelab.acme.credentialsFile" => {
            let token = content.lines().find_map(|l| l.strip_prefix("CLOUDFLARE_DNS_API_TOKEN=")).map(|t| t.trim().trim_matches('"'))?;
            let domain = values.get("homelab.domain").map(|d| d.trim().to_string()).unwrap_or_default();
            if domain.is_empty() {
                return Some(Err("set homelab.domain first so the token can be checked against your zone".into()));
            }
            let out = Command::new("curl")
                .args(["-fsS", "--max-time", "15", "-H", &format!("Authorization: Bearer {token}"), &format!("https://api.cloudflare.com/client/v4/zones?name={domain}")])
                .output();
            Some(match out {
                Ok(o) if o.status.success() => {
                    let body = String::from_utf8_lossy(&o.stdout);
                    let v: serde_json::Value = serde_json::from_str(&body).unwrap_or_default();
                    let n = v["result"].as_array().map(|a| a.len()).unwrap_or(0);
                    if n == 1 {
                        Ok(format!("token verified: it can see the zone {domain}"))
                    } else {
                        Err(format!("the token works but sees no zone named {domain} — is the domain at Cloudflare, and the token scoped to it?"))
                    }
                }
                Ok(_) => Err("Cloudflare rejected the token (401/403): recreate it with the Edit zone DNS template".into()),
                Err(e) => Err(format!("could not reach Cloudflare: {e}")),
            })
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_bare_cloudflare_token_becomes_the_env_line() {
        assert_eq!(shape("homelab.acme.credentialsFile", "abc123"), "CLOUDFLARE_DNS_API_TOKEN=abc123\n");
        assert_eq!(shape("homelab.acme.credentialsFile", "CLOUDFLARE_DNS_API_TOKEN=x"), "CLOUDFLARE_DNS_API_TOKEN=x\n");
        assert_eq!(shape("homelab.arrStack.vpnEnvFile", "A=1 | B=2"), "A=1\nB=2\n");
    }

    #[test]
    fn vpn_fields_and_guide_follow_the_provider() {
        let mut v = BTreeMap::new();
        // With no provider chosen the guide points at the choice itself.
        assert!(for_option("homelab.arrStack.vpnEnvFile", &v).unwrap().steps.contains("provider"));
        assert!(fields("homelab.arrStack.vpnEnvFile", &v)[0].is_option);
        assert!(!fields("homelab.arrStack.vpnEnvFile", &v)[0].choices.is_empty());
        v.insert("homelab.arrStack.vpnProvider".into(), "ProtonVPN".into());
        assert!(fields("homelab.arrStack.vpnEnvFile", &v).iter().any(|f| f.var == "VPN_PORT_FORWARDING"));
        v.insert("homelab.arrStack.vpnProvider".into(), "mullvad".into());
        assert!(fields("homelab.arrStack.vpnEnvFile", &v).iter().all(|f| f.var != "VPN_PORT_FORWARDING"));
        v.insert("homelab.arrStack.vpnProvider".into(), "ivpn".into());
        assert!(for_option("homelab.arrStack.vpnEnvFile", &v).unwrap().steps.contains("ivpn"));
        assert!(for_option("homelab.nothing", &v).is_none());
    }

    #[test]
    fn a_secret_is_a_form_of_named_variables() {
        let v = BTreeMap::new();
        assert_eq!(fields("homelab.acme.credentialsFile", &v)[0].var, "CLOUDFLARE_DNS_API_TOKEN");
        assert!(fields("homelab.acme.credentialsFile", &v)[0].masked);
        // A path-only secret has no variables: the user points at a file.
        assert!(fields("homelab.meshagent.mshFile", &v).is_empty());
        // Empty values are dropped, so a skipped optional line is not written.
        assert_eq!(compose(&[("A".into(), "1".into()), ("B".into(), "  ".into())]), "A=1\n");
    }
}
