//! Values that must not cross the network in the clear.
//!
//! ⚠️ WHY THIS EXISTS, AND WHY IT IS NOT TLS. The browser installer is served
//! over plain HTTP, and the form carries a Cloudflare API token, a WireGuard
//! private key, a Backblaze application key and the admin's password. Anyone
//! who can watch the network segment can read them as they go past. The
//! pairing code does not help: it says who may drive the installer, not who
//! may read the traffic.
//!
//! The obvious answer, a self-signed certificate, costs every reader a
//! browser warning they must click through, which is a bad habit to teach
//! somebody installing their first server. So instead the page seals the
//! secret values to a key this process makes when it starts, and posts
//! ciphertext. A listener sees ciphertext.
//!
//! ⚠️ WHAT THIS DOES NOT DO, STATED PLAINLY. Someone who can rewrite traffic,
//! rather than only read it, can serve a page carrying their own public key
//! and read everything sealed to it.
//!
//! The fingerprint does NOT fix that and must not be described as if it did,
//! here or anywhere else. One sentence, used everywhere:
//!
//!   The fingerprint can detect a mismatch between the browser and the
//!   console when the traffic has not been modified. It does not
//!   authenticate the connection against an active attacker.
//!
//! Somebody who controls the connection replaces the key, the fingerprint and
//! the page together, so the browser's copy proves nothing. The console's
//! copy is the one that did not travel.
//!
//! So: this closes passive listening, which is the realistic threat on a home
//! network. What bounds the active case is not cryptographic at all: an
//! install cannot start without somebody pressing a key on the machine.
//!
//! X25519 with XSalsa20-Poly1305, which is NaCl's `crypto_box`: the browser
//! side is TweetNaCl, which implements exactly this and nothing else.
//! Browsers do not offer their own cryptography over plain HTTP — `crypto
//! .subtle` is undefined outside a secure context — so the page carries an
//! implementation and this is the one it carries.

use crypto_box::aead::{Aead, OsRng};
use crypto_box::{PublicKey, SalsaBox, SecretKey};

/// This process's key pair. Made at startup, never written anywhere, gone
/// when the installer exits.
pub struct Sealer {
    secret: SecretKey,
}

impl Default for Sealer {
    fn default() -> Self {
        Self::new()
    }
}

impl Sealer {
    pub fn new() -> Self {
        Sealer { secret: SecretKey::generate(&mut OsRng) }
    }

    /// The public half, for the page to seal to.
    pub fn public_hex(&self) -> String {
        to_hex(self.secret.public_key().as_bytes())
    }

    /// A short, readable form of the public key, shown on both screens so a
    /// reader can see whether they match. ⚠️ A mismatch means something is
    /// wrong; a match proves nothing, because the browser's copy came over
    /// the same connection an attacker would be rewriting. Groups of four
    /// because it is read off one screen and compared with another.
    pub fn fingerprint(&self) -> String {
        let hex = self.public_hex();
        hex[..12].as_bytes().chunks(4).map(|c| String::from_utf8_lossy(c).to_string()).collect::<Vec<_>>().join(" ")
    }

    /// A sealer from a known secret key. Tests only: it is what lets a fixed
    /// fixture produced by the browser's library be opened by this one.
    #[cfg(test)]
    pub fn from_secret_hex(hex: &str) -> Self {
        let b: [u8; 32] = from_hex(hex).expect("hex").try_into().expect("32 bytes");
        Sealer { secret: SecretKey::from(b) }
    }

    /// Open what the page sealed: its ephemeral public key, the nonce, and
    /// the ciphertext, all hex.
    pub fn open(&self, epk: &str, nonce: &str, ciphertext: &str) -> Result<String, String> {
        let epk = from_hex(epk).ok_or("the sealed value's key is not hex")?;
        let nonce = from_hex(nonce).ok_or("the sealed value's nonce is not hex")?;
        let ct = from_hex(ciphertext).ok_or("the sealed value is not hex")?;
        let epk: [u8; 32] = epk.try_into().map_err(|_| "the sealed value's key is the wrong length")?;
        let nonce: [u8; 24] = nonce.try_into().map_err(|_| "the sealed value's nonce is the wrong length")?;
        let b = SalsaBox::new(&PublicKey::from(epk), &self.secret);
        let plain = b
            .decrypt(&nonce.into(), ct.as_slice())
            .map_err(|_| "the sealed value did not open: it was not sealed to this installer, or it was altered on the way")?;
        String::from_utf8(plain).map_err(|_| "the sealed value is not text".to_string())
    }

    /// Seal to our own public key. Tests only: the page does this half.
    #[cfg(test)]
    pub fn seal_for_test(&self, plain: &str) -> (String, String, String) {
        use crypto_box::aead::AeadCore;
        let eph = SecretKey::generate(&mut OsRng);
        let b = SalsaBox::new(&self.secret.public_key(), &eph);
        let nonce = SalsaBox::generate_nonce(&mut OsRng);
        let ct = b.encrypt(&nonce, plain.as_bytes()).expect("sealed");
        (to_hex(eph.public_key().as_bytes()), to_hex(&nonce), to_hex(&ct))
    }
}

/// Hex rather than base64, because both ends hand-roll it and hex has no
/// variants to disagree about.
pub fn to_hex(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push(char::from_digit((b >> 4) as u32, 16).unwrap());
        s.push(char::from_digit((b & 0xf) as u32, 16).unwrap());
    }
    s
}

pub fn from_hex(s: &str) -> Option<Vec<u8>> {
    let s = s.trim();
    if s.len() % 2 != 0 {
        return None;
    }
    let b = s.as_bytes();
    (0..s.len() / 2)
        .map(|i| {
            let hi = (b[2 * i] as char).to_digit(16)?;
            let lo = (b[2 * i + 1] as char).to_digit(16)?;
            Some(((hi << 4) | lo) as u8)
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hex_round_trips() {
        let bytes = vec![0u8, 1, 15, 16, 127, 128, 255];
        assert_eq!(to_hex(&bytes), "00010f107f80ff");
        assert_eq!(from_hex("00010f107f80ff").unwrap(), bytes);
        assert!(from_hex("0").is_none(), "odd length");
        assert!(from_hex("zz").is_none(), "not hex");
    }

    #[test]
    fn a_sealed_value_opens_and_a_tampered_one_does_not() {
        let s = Sealer::new();
        let (epk, nonce, ct) = s.seal_for_test("cloudflare-token-value");
        assert_eq!(s.open(&epk, &nonce, &ct).unwrap(), "cloudflare-token-value");

        // One flipped byte in the ciphertext must not open.
        let mut bad = from_hex(&ct).unwrap();
        bad[0] ^= 1;
        assert!(s.open(&epk, &nonce, &to_hex(&bad)).is_err());

        // Sealed to somebody else's key: not ours to open.
        let other = Sealer::new();
        assert!(other.open(&epk, &nonce, &ct).is_err());
    }

    /// ⚠️ THE ONE TEST THAT SPANS BOTH IMPLEMENTATIONS. Everything else here
    /// proves this code can open what this code sealed, which would still
    /// pass if the two sides disagreed about hex, nonce length, key order or
    /// ciphertext layout. These bytes were produced by the browser's own
    /// vendored TweetNaCl, through the same calls `index.html` makes.
    #[test]
    fn it_opens_what_the_browsers_library_actually_produced() {
        let s = Sealer::from_secret_hex("a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90");
        // The browser derives this from the public key the page is given.
        assert_eq!(s.public_hex(), "b90b2e574714a8b6e0763e87e98999ac5f7f45342903923c5ff9951455ed266a");
        let opened = s
            .open(
                "52a5524b0c32e8a47f239ab6ca8e42c3b9316a54cae4854ecdb5889311cc9a7e",
                "000102030405060708090a0b0c0d0e0f1011121314151617",
                "8f232e0abad7d7f3de045fd75dbd13c4adc7df783b0df50203ffcb99f88cd70197b964f4ad53",
            )
            .expect("the browser's ciphertext opens here");
        assert_eq!(opened, "cloudflare-token-value");
    }

    #[test]
    fn the_fingerprint_is_short_and_follows_the_key() {
        let a = Sealer::new();
        let b = Sealer::new();
        assert_eq!(a.fingerprint().len(), 14, "three groups of four and two spaces");
        assert!(a.fingerprint().starts_with(&a.public_hex()[..4]));
        assert_ne!(a.fingerprint(), b.fingerprint());
    }
}
