//! The browser front end: the same `wizard::Wizard` the console drives,
//! served to any computer on the network so the long values (a Cloudflare
//! token, a WireGuard key) can be pasted instead of typed at a console with
//! no clipboard. Fedora's Anaconda WebUI, Proxmox and TrueNAS all install
//! this way; this is the small version of it.
//!
//! No framework and no dependency: a thread per connection, HTTP/1.1 with
//! Content-Length, three routes. An eight-character code shown on the
//! console gates every API call, wrong codes are counted and then refused
//! per source address, and a peer that is not on a local network is hung up
//! on before anything is read. Erasing a disk needs one more thing: a number
//! shown only on the machine's own screen. The page itself is harmless and
//! needs no code.

use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// ⚠️ WHAT BOUNDS THIS SERVER.
///
/// A thread per connection with no limits is a free denial of service: open
/// sockets, send nothing, and every thread sits in `read_line` forever. The
/// installer is short-lived and on a home network, so this is an annoyance
/// rather than a breach, but an installer that stops answering during an
/// install is a bad hour for somebody.
///
/// Three limits, all deliberately generous for the handful of browsers that
/// will ever talk to this, and all fatal to a client that is not really
/// speaking HTTP.
const MAX_CONNECTIONS: usize = 64;
const IO_TIMEOUT: Duration = Duration::from_secs(15);
const MAX_HEADERS: usize = 100;
const MAX_HEADER_BYTES: usize = 16 * 1024;
const MAX_BODY_BYTES: usize = 64 * 1024;

/// Connections being served right now.
static LIVE: AtomicUsize = AtomicUsize::new(0);

/// Decrements the live count however the thread leaves.
struct Live;
impl Drop for Live {
    fn drop(&mut self) {
        LIVE.fetch_sub(1, Ordering::SeqCst);
    }
}

use crate::wizard::{is_local_peer, BadCode, Origin, Role, Step, Wizard};

const INDEX: &str = include_str!("index.html");
/// The page's cryptography, served separately so `index.html` stays readable.
/// See the header of that file for why a library is needed at all.
const NACL: &str = include_str!("tweetnacl.js");

/// Start the server in the background. Failure to bind is not fatal: the
/// console front end still works, and the Welcome screen simply has no URL
/// to offer.
pub fn serve(w: Arc<Mutex<Wizard>>) -> Result<u16, String> {
    let port = w.lock().unwrap().web_port;
    let listener = TcpListener::bind(("0.0.0.0", port)).map_err(|e| format!("the browser installer could not listen on port {port}: {e}"))?;
    std::thread::spawn(move || {
        for stream in listener.incoming().flatten() {
            // Over the cap, hang up at once rather than spawning. A browser
            // retries; a client holding sockets open gets nothing.
            if LIVE.fetch_add(1, Ordering::SeqCst) >= MAX_CONNECTIONS {
                LIVE.fetch_sub(1, Ordering::SeqCst);
                drop(stream);
                continue;
            }
            let w = w.clone();
            std::thread::spawn(move || {
                let _live = Live;
                let _ = stream.set_read_timeout(Some(IO_TIMEOUT));
                let _ = stream.set_write_timeout(Some(IO_TIMEOUT));
                let _ = handle(stream, w);
            });
        }
    });
    Ok(port)
}

fn handle(mut stream: TcpStream, w: Arc<Mutex<Wizard>>) -> std::io::Result<()> {
    // Before anything is read: this installer is for the network it is
    // standing on. A packet from anywhere else is answered with nothing and
    // the connection closed, whatever a household router may be forwarding.
    if let Ok(addr) = stream.peer_addr() {
        if !is_local_peer(&addr.ip()) {
            return Ok(());
        }
    }
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
    let mut header_bytes = 0usize;
    for n in 0..=MAX_HEADERS {
        if n == MAX_HEADERS {
            return reply(&mut stream, 431, "text/plain", b"too many headers");
        }
        let mut line = String::new();
        let read = reader.read_line(&mut line)?;
        if read == 0 || line.trim().is_empty() {
            break;
        }
        header_bytes += read;
        if header_bytes > MAX_HEADER_BYTES {
            return reply(&mut stream, 431, "text/plain", b"headers too large");
        }
        if let Some(v) = line.to_ascii_lowercase().strip_prefix("content-length:") {
            length = v.trim().parse().unwrap_or(0);
        }
    }
    let mut body = vec![0u8; length.min(MAX_BODY_BYTES)];
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
        ("GET", "/nacl.js") => reply(&mut stream, 200, "application/javascript; charset=utf-8", NACL.as_bytes()),
        ("GET", "/api/state") | ("POST", "/api/action") => {
            if w.lock().unwrap().is_locked_out(&peer) {
                return reply(
                    &mut stream,
                    403,
                    "application/json",
                    br#"{"error":"too many wrong codes from this computer. Clear it at the machine's own screen to try again."}"#,
                );
            }
            let expected = w.lock().unwrap().pairing.clone();
            if code.to_uppercase() != expected {
                // Guessing is the only attack on a code, so make each guess
                // cost time once a few have been wrong, and stop entirely
                // after ten. Only someone at the machine can undo that.
                let verdict = w.lock().unwrap().note_bad_code(&peer);
                if verdict != BadCode::Counted {
                    std::thread::sleep(std::time::Duration::from_secs(2));
                }
                let body: &[u8] = match verdict {
                    BadCode::Locked => br#"{"error":"too many wrong codes from this computer. Clear it at the machine's own screen to try again."}"#,
                    _ => br#"{"error":"the code on the machine's screen does not match"}"#,
                };
                return reply(&mut stream, 403, "application/json", body);
            }
            w.lock().unwrap().note_good_code(&peer);
            {
                let mut g = w.lock().unwrap();
                if g.web_seen.as_deref() != Some(peer.as_str()) {
                    g.web_seen = Some(peer.clone());
                }
                if g.step == Step::Install {
                    g.poll_install();
                }
            }
            let action: Value = if method == "POST" { serde_json::from_slice(&body).unwrap_or(json!({})) } else { json!({}) };
            // One browser drives. A second one may watch, and may take over
            // deliberately; the first then sees that it has lost the form.
            let taking = action["do"].as_str() == Some("take_over");
            {
                let mut g = w.lock().unwrap();
                match (&g.controller, method.as_str(), taking) {
                    // Asking is all a browser can do: taking the form away
                    // from somebody is answered at the machine.
                    (_, _, true) => {
                        g.request_takeover(&peer);
                    }
                    (None, "POST", _) => g.controller = Some(peer.clone()),
                    (Some(c), "POST", _) if c != &peer => {
                        let out = with_control(&g, &peer);
                        drop(g);
                        return reply(&mut stream, 409, "application/json", out.to_string().as_bytes());
                    }
                    _ => {}
                }
            }
            let out = if method == "POST" && !taking {
                apply(&w, &action)
            } else {
                w.lock().unwrap().state_json()
            };
            let g = w.lock().unwrap();
            let mut out = out;
            out["web"]["controller"] = json!(g.controller);
            out["web"]["in_control"] = json!(g.controller.as_deref().map(|c| c == peer).unwrap_or(true));
            drop(g);
            reply(&mut stream, 200, "application/json", out.to_string().as_bytes())
        }
        _ => reply(&mut stream, 404, "text/plain", b"not found"),
    }
}

/// The state as this peer sees it, with who holds the form.
fn with_control(g: &Wizard, peer: &str) -> Value {
    let mut out = g.state_json();
    out["web"]["controller"] = json!(g.controller);
    out["web"]["in_control"] = json!(g.controller.as_deref().map(|c| c == peer).unwrap_or(true));
    out
}

fn reply(stream: &mut TcpStream, status: u16, content_type: &str, body: &[u8]) -> std::io::Result<()> {
    let reason = match status {
        200 => "OK",
        403 => "Forbidden",
        409 => "Conflict",
        404 => "Not Found",
        431 => "Request Header Fields Too Large",
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

/// A value from the page, which must arrive sealed when it is a secret.
///
/// ⚠️ REFUSES A SECRET IN THE CLEAR. This server speaks plain HTTP, so a
/// token sent as a bare string is readable by anything watching the segment.
/// An earlier version accepted one and noted it afterwards; that made
/// stopping the page's cryptography a cheaper attack than breaking it, since
/// the plaintext followed either way. Refusing here means the only way to
/// get a secret into this installer over the network is sealed, and the
/// other way in is the machine's own keyboard.
///
/// An empty value is not a secret: clearing a field is allowed to be plain.
fn value_from(g: &mut Wizard, a: &Value, field: &str, secret: bool) -> Result<String, String> {
    match a.get("sealed") {
        Some(v) => g.sealer.open(
            v["epk"].as_str().unwrap_or(""),
            v["nonce"].as_str().unwrap_or(""),
            v["ct"].as_str().unwrap_or(""),
        ),
        None => {
            let plain = a[field].as_str().unwrap_or("").to_string();
            if secret && !plain.is_empty() {
                g.saw_cleartext_secret = true;
                return Err("refused: a secret must be encrypted before it is sent. Type it on the machine's own screen, or reload the page so its cryptography loads.".into());
            }
            Ok(plain)
        }
    }
}

/// One action from the page, then the new state. Every rule lives in the
/// model, so the browser cannot do anything the console could not.
fn apply(w: &Arc<Mutex<Wizard>>, a: &Value) -> Value {
    let s = |k: &str| a[k].as_str().unwrap_or("").to_string();
    let n = |k: &str| a[k].as_u64().unwrap_or(0) as usize;
    let mut g = w.lock().unwrap();
    // ⚠️ FROZEN WHILE THE MACHINE IS ASKING. Somebody at the console is
    // reading the disks this is about to erase. If the answers could still
    // change underneath them they would be approving one thing and getting
    // another, which is the whole value of asking. Escape at the machine
    // unfreezes it.
    if g.install_request.is_some() {
        g.say("the machine's own screen is asking whether to install: answer there. Nothing can change until it is answered.", true);
        return g.state_json();
    }
    match a["do"].as_str().unwrap_or("") {
        "set_kit" => g.set_kit(n("index")),
        "set_disk" => g.set_disk_role(n("index"), Role::from_id(&s("role"))),
        "set_disk_path" => g.set_disk_path(&s("value")),
        "set_profile" => {
            let key = s("key");
            let secret = key == "password" || key == "password2";
            match value_from(&mut g, a, "value", secret).and_then(|v| g.set_profile(&key, &v)) {
                Ok(()) => g.say("kept", false),
                Err(e) => g.say(e, true),
            }
        }
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
            let (option, var) = (s("option"), s("var"));
            match value_from(&mut g, a, "value", true).and_then(|v| g.set_secret_field(&option, &var, &v)) {
                Ok(()) => {}
                Err(e) => g.say(e, true),
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
        "check_update" => match g.check_update() {
            Ok(m) => g.say(m, false),
            Err(e) => g.say(e, true),
        },
        "apply_update" => match g.apply_update() {
            Ok(exe) => {
                g.say("fetched; restarting the installer with your answers", false);
                g.relaunch = Some(exe);
            }
            Err(e) => g.say(e, true),
        },
        "back" => g.back(),
        "continue" => {
            if let Err(b) = g.advance_from(Origin::Browser) {
                g.say(b.message, true);
            }
        }
        // ⚠️ No action here starts an install. Approval is a keypress on the
        // machine's own screen, so nothing about it crosses the network and
        // there is nothing for a listener to catch or a guesser to try.
        "confirm_install" => g.say("the machine's own screen is asking: press Y there to go ahead", true),
        _ => {}
    }
    g.state_json()
}
