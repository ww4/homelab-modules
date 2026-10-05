//! The browser front end: the same `wizard::Wizard` the console drives,
//! served to any computer on the network so the long values (a Cloudflare
//! token, a WireGuard key) can be pasted instead of typed at a console with
//! no clipboard. Fedora's Anaconda WebUI, Proxmox and TrueNAS all install
//! this way; this is the small version of it.
//!
//! No framework and no dependency: a thread per connection, HTTP/1.1 with
//! Content-Length, three routes. A six-character code shown on the console
//! gates every API call, so a neighbour on the same network cannot drive an
//! install (the page itself is harmless and needs no code).

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex};

use crate::wizard::{Role, Step, Wizard};

const INDEX: &str = include_str!("index.html");

/// Start the server in the background. Failure to bind is not fatal: the
/// console front end still works, and the Welcome screen simply has no URL
/// to offer.
pub fn serve(w: Arc<Mutex<Wizard>>) -> Result<u16, String> {
    let port = w.lock().unwrap().web_port;
    let listener = TcpListener::bind(("0.0.0.0", port)).map_err(|e| format!("the browser installer could not listen on port {port}: {e}"))?;
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            let w = w.clone();
            std::thread::spawn(move || {
                let _ = handle(stream, w);
            });
        }
    });
    Ok(port)
}

fn handle(mut stream: TcpStream, w: Arc<Mutex<Wizard>>) -> std::io::Result<()> {
    let peer = stream.peer_addr().map(|a| a.ip().to_string()).unwrap_or_default();
    let mut reader = BufReader::new(stream.try_clone()?);
    let mut request = String::new();
    if reader.read_line(&mut request)? == 0 {
        return Ok(());
    }
    let mut parts = request.split_whitespace();
    let method = parts.next().unwrap_or("").to_string();
    let target = parts.next().unwrap_or("/").to_string();
    let mut length = 0usize;
    loop {
        let mut line = String::new();
        if reader.read_line(&mut line)? == 0 || line.trim().is_empty() {
            break;
        }
        if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            length = v.trim().parse().unwrap_or(0);
        }
    }
    let mut body = vec![0u8; length.min(64 * 1024)];
    if !body.is_empty() {
        reader.read_exact(&mut body)?;
    }

    let (path, query) = target.split_once('?').unwrap_or((target.as_str(), ""));
    let code = query
        .split('&')
        .filter_map(|kv| kv.split_once('='))
        .find(|(k, _)| *k == "code")
        .map(|(_, v)| v.to_string())
        .unwrap_or_default();

    match (method.as_str(), path) {
        ("GET", "/") | ("GET", "/index.html") => reply(&mut stream, 200, "text/html; charset=utf-8", INDEX.as_bytes()),
        ("GET", "/api/state") | ("POST", "/api/action") => {
            let expected = w.lock().unwrap().pairing.clone();
            if code.to_uppercase() != expected {
                return reply(&mut stream, 403, "application/json", br#"{"error":"the code on the machine's screen does not match"}"#);
            }
            {
                let mut g = w.lock().unwrap();
                if g.web_seen.as_deref() != Some(peer.as_str()) {
                    g.web_seen = Some(peer.clone());
                }
                if g.step == Step::Install {
                    g.poll_install();
                }
            }
            let out = if method == "POST" {
                let action: Value = serde_json::from_slice(&body).unwrap_or(json!({}));
                apply(&w, &action)
            } else {
                w.lock().unwrap().state_json()
            };
            reply(&mut stream, 200, "application/json", out.to_string().as_bytes())
        }
        _ => reply(&mut stream, 404, "text/plain", b"not found"),
    }
}

fn reply(stream: &mut TcpStream, status: u16, content_type: &str, body: &[u8]) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        403 => "Forbidden",
        404 => "Not Found",
        _ => "OK",
    };
    let head = format!(
        "HTTP/1.1 {status} {reason}\r\nContent-Type: {content_type}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n",
        body.len()
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body)?;
    stream.flush()
}

/// One action from the page, then the new state. Every rule lives in the
/// model, so the browser cannot do anything the console could not.
fn apply(w: &Arc<Mutex<Wizard>>, a: &Value) -> Value {
    let s = |k: &str| a[k].as_str().unwrap_or("").to_string();
    let n = |k: &str| a[k].as_u64().unwrap_or(0) as usize;
    let mut g = w.lock().unwrap();
    match a["do"].as_str().unwrap_or("") {
        "set_kit" => g.set_kit(n("index")),
        "set_disk" => g.set_disk_role(n("index"), Role::from_id(&s("role"))),
        "set_disk_path" => g.set_disk_path(&s("value")),
        "set_profile" => match g.set_profile(&s("key"), &s("value")) {
            Ok(()) => g.say("kept", false),
            Err(e) => g.say(e, true),
        },
        "github" => match g.import_github_keys(&s("user")) {
            Ok(m) if !m.is_empty() => g.say(m, false),
            Ok(_) => {}
            Err(e) => g.say(e, true),
        },
        "add_key" => match g.add_key(&s("text")) {
            Ok(m) if !m.is_empty() => g.say(m, false),
            Ok(_) => {}
            Err(e) => g.say(e, true),
        },
        "remove_key" => g.remove_key(n("index")),
        "set_domain" => match g.set_domain(&s("key"), &s("value")) {
            Ok(()) => g.say("kept", false),
            Err(e) => g.say(e, true),
        },
        "set_secret" => {
            if let Err(e) = g.set_secret_field(&s("option"), &s("var"), &s("value")) {
                g.say(e, true);
            }
        }
        "save_secret" => match g.save_secret(&s("option")) {
            Ok(m) => g.say(m, false),
            Err(e) => g.say(e, true),
        },
        "skip_secret" => match g.skip_secret(&s("option")) {
            Ok(m) => g.say(m, true),
            Err(e) => g.say(e, true),
        },
        "set_value" => g.set_value(&s("name"), &s("value")),
        "toggle_module" => {
            if let Err(e) = g.toggle_module(&s("name")) {
                g.say(e, false);
            }
        }
        "back" => g.back(),
        "continue" => {
            if let Err(b) = g.advance() {
                g.say(b.message, true);
            }
        }
        _ => {}
    }
    g.state_json()
}
