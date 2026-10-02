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
                    Press v and paste just the token: the file is written as CLOUDFLARE_DNS_API_TOKEN=… and \
                    the token is checked against your zone right away. ACME uses it for DNS-01 challenges; \
                    `homelab-configure dns` uses it later to create the A records.",
        }),
        "homelab.arrStack.vpnEnvFile" => {
            let provider = values.get("homelab.arrStack.vpnProvider").map(|s| s.trim().to_lowercase()).unwrap_or_default();
            let (title, steps) = match provider.as_str() {
                "mullvad" => ("Mullvad WireGuard credentials", "mullvad.net → account → WireGuard configuration → generate a key (Linux) → download a config for any location. \
                    From that .conf: PrivateKey → WIREGUARD_PRIVATE_KEY, Address → WIREGUARD_ADDRESSES (keep the /32). \
                    Press v and type the lines separated by ` | `, e.g.\n  WIREGUARD_PRIVATE_KEY=… | WIREGUARD_ADDRESSES=10.x.y.z/32 | SERVER_COUNTRIES=Netherlands\n\
                    Mullvad has no port forwarding: expect slower seeding; a tracker that needs an open port wants a provider that forwards one (Proton VPN does)."),
                "protonvpn" | "proton" => ("Proton VPN WireGuard credentials (port forwarding on paid plans)", "account.protonvpn.com → Downloads → WireGuard configuration → Linux · pick a P2P server · enable \"NAT-PMP (port forwarding)\" → Create → download. \
                    From the .conf: PrivateKey → WIREGUARD_PRIVATE_KEY, Address → WIREGUARD_ADDRESSES. Add VPN_PORT_FORWARDING=on for the forwarded port.\n\
                    Press v and type the lines separated by ` | `."),
                "" => ("VPN credentials for the download client", "Set homelab.arrStack.vpnProvider on the Values screen first (mullvad, protonvpn, or any provider gluetun supports); \
                    the steps for that provider appear here."),
                _ => ("VPN credentials for the download client", "This provider is passed to gluetun as VPN_SERVICE_PROVIDER; the variables it needs are in gluetun's wiki page for it. \
                    Most WireGuard providers need WIREGUARD_PRIVATE_KEY, WIREGUARD_ADDRESSES and SERVER_COUNTRIES; one that forwards a port adds FIREWALL_VPN_INPUT_PORTS (set qBittorrent's listen port to the same number). Press v and type the lines separated by ` | `. \
                    (provider: {other})"),
            };
            let steps: &'static str = Box::leak(steps.replace("{other}", &provider).into_boxed_str());
            Some(Guide { title, steps })
        }
        "homelab.backup.remote.environmentFile" => Some(Guide {
            title: "Backblaze B2 credentials for the offsite restic repository",
            steps: "backblaze.com → B2 Cloud Storage → create a bucket (private) → Application Keys → Add a New Application Key, \
                    restricted to that bucket, read and write. Set homelab.backup.remote.repository to b2:<bucket-name>.\n\
                    Press v and type:  B2_ACCOUNT_ID=<keyID> | B2_ACCOUNT_KEY=<applicationKey>\n\
                    Any restic backend works instead (S3, SFTP): then the variables are that backend's.",
        }),
        "homelab.meshagent.mshFile" => Some(Guide {
            title: "MeshCentral agent identity",
            steps: "On your MeshCentral server: My Devices → Add Agent → Linux → download the .msh file for the device group. Point this at that file.",
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
    fn vpn_guide_follows_the_provider() {
        let mut v = BTreeMap::new();
        assert!(for_option("homelab.arrStack.vpnEnvFile", &v).unwrap().steps.contains("vpnProvider"));
        v.insert("homelab.arrStack.vpnProvider".into(), "ProtonVPN".into());
        assert!(for_option("homelab.arrStack.vpnEnvFile", &v).unwrap().steps.contains("VPN_PORT_FORWARDING"));
        v.insert("homelab.arrStack.vpnProvider".into(), "ivpn".into());
        assert!(for_option("homelab.arrStack.vpnEnvFile", &v).unwrap().steps.contains("ivpn"));
        assert!(for_option("homelab.nothing", &v).is_none());
    }
}
