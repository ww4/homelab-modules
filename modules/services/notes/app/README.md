# notes

A Google Keep-style card wall over a folder of Markdown files.

No database, no sidecar, no import step. It reads `.md` files with YAML
frontmatter, serves them as cards, and writes your edits straight back to the
same files. Point it at a folder you already have — a SilverBullet space, an
Obsidian vault, a git repo of notes — and the files stay exactly as portable
as they were.

Built because every notes app that gets the *files* right is miserable on a
phone, and every app that's good on a phone wants your notes in its database.

## ⚠️ There is no authentication

This app has no login, no accounts, and no per-note permissions. **Anything
that can reach its port can read, edit and delete every note it serves.**

That is a deliberate design choice, not an oversight — see
[Access](#access) — but it means the deployment is responsible for who gets
in. It listens on `127.0.0.1` by default and **refuses to start** on a public
address unless you say so explicitly.

## What it does

- **Cards, not a file tree.** Title, a preview, a category. Tap to edit.
- **Checkboxes** are real: `- [ ]` toggles in place and writes back.
- **Search** across title and body, ranked, substring not whole-word.
- **Archive, pin, and trash**, with drag-to-reorder for the pinned row.
- **Offline.** A PWA with a service worker, a cached snapshot of the last
  wall, and an outbox — new notes written with no network are queued and go up
  when there is one. The app opens to your notes on a dead link.
- **Phone gestures.** Swipe right archives; swipe left latches the card open
  and waits for you to tap the track, so a delete is never one flick away.
  Long press selects. Pull down syncs.
- **Two people, one folder.** Who archived or pinned a note is recorded per
  person, so one person archiving a shared note doesn't archive it for the
  other. (Trash is shared, because it moves the file. Said plainly rather
  than faked.)

Nothing it knows lives anywhere but the files:

```yaml
---
date: 2024-03-22
category: shopping
author: ana
archived_by: sam, ana
pinned_by: sam
---

- [x] cilantro
- [ ] radishes
```

Those are ordinary frontmatter keys. Any other editor that understands
frontmatter sees them as data; nothing is hidden in a `.app/` directory. The
writer is line-preserving — it replaces one key and leaves every other byte
alone — so editing a note here does not reflow a file you hand-wrote
elsewhere.

## Running it

```
pip install .
NOTES_SPACE_DIR=/path/to/your/markdown notes
```

Then `http://127.0.0.1:3337`.

### Settings

All environment variables, prefixed `NOTES_`. A `.env` file works too.

| variable | default | |
|---|---|---|
| `NOTES_SPACE_DIR` | `/var/lib/silverbullet` | the folder to serve |
| `NOTES_HOST` | `127.0.0.1` | |
| `NOTES_PORT` | `3337` | |
| `NOTES_TRUST_PROXY_AUTH` | `0` | believe an upstream proxy's user header |
| `NOTES_AUTH_USER_HEADER` | `Remote-User` | which header that is |
| `NOTES_AUTH_NAME_HEADER` | `Remote-Name` | optional display name from the proxy |
| `NOTES_ALLOW_PUBLIC_BIND` | `0` | required to bind a public address unauthenticated |

### What it will and won't touch

Only `.md` files, only under the directories it owns (`Keep/`, `Notes/`,
`Inbox/`), plus `Grocery List.md`. Everything else in the folder is invisible
to it and unreachable through its API — a note id naming anything else is
refused, not merely hidden. Deleting moves the file to `Trash/` rather than
unlinking it.

Those names are currently constants in `paths.py`. If you want it over a
differently-shaped folder, that is the file to edit — making them
configurable is the obvious next change.

## Access

There are no per-note permissions to express here: either you can see the
wall or you can't. All-or-nothing is exactly what a reverse proxy already
does, and it does it before a request reaches this code — so this app does
not implement authentication, and won't. Hand-rolled auth in a small
self-hosted project is where the CVEs live.

Pick whichever of these you already run.

**Nothing at all**, on a private network. The default. Keep it on loopback
and reach it over a VPN or tailnet.

```sh
tailscale serve 3337        # https on your tailnet, nothing exposed
```

**A password**, if you just want a speed bump:

```nginx
location / {
    auth_basic "notes";
    auth_basic_user_file /etc/nginx/notes.htpasswd;
    proxy_pass http://127.0.0.1:3337;
}
```

**Real SSO**, with identity carried through. If your proxy authenticates the
user, tell the app to believe it and that name becomes the identity that
archives and pins notes — so the "who is using this device?" prompt
disappears and `archived_by:` comes from your directory instead of a cookie.

```sh
NOTES_TRUST_PROXY_AUTH=1
NOTES_AUTH_USER_HEADER=Remote-User
```

| proxy | header |
|---|---|
| Authelia | `Remote-User` |
| Authentik | `X-authentik-username` |
| oauth2-proxy | `X-Forwarded-User` |
| Cloudflare Access | `Cf-Access-Authenticated-User-Email` |
| `tailscale serve` | `Tailscale-User-Login` |

An email is reduced to its local part, so `ana@example.com` becomes `ana`.

> **The header is only as trustworthy as the port.** If anything can reach
> the app directly, it can send `Remote-User: anyone`. Bind loopback, or put
> the app on a private network where only the proxy can reach it. This is why
> trusting the header is opt-in.

## Users

Everyone who uses the space has an **account**. **Users** in the side menu
lists them, and it is a view over a markdown table in the space —
`Notes/People.md` — which the page says at the bottom, because editing that
file by hand must stay just as good an answer.

```markdown
| Username | Display name | Emails |
| --- | --- | --- |
| ana | Ana Parker | ana@example.com, a.parker@work.example |
| maryj | Mary | maryj@example.com |
| marys | Mary | marys@example.com |
```

- **username** — unique, chosen once, and the only thing a note records
  (`archived_by: ana`). It does not change, because changing it would orphan
  every note already filed under it.
- **display name** — a label, optional. Free text, and **not** unique: two
  people called Mary is an ordinary situation, and the usernames keep them
  apart. A card shows the display name, falling back to the username.
- **emails** — optional. How a sign-in that was authenticated elsewhere is
  matched to an account. Nothing is verified here; the proxy did that. One
  address belongs to one account at a time.

A sign-in is matched two ways, both exact: against the username itself, or
against one of the account's emails. Nothing is inferred from spelling — an
unrecognised sign-in is a question for the person using it. A browser that
has never said who it is gets a sign-in overlay on first load: pick an
account, or create one with a username, an optional display name and an
optional email.

There are no passwords and no sessions here. See [Access](#access) for how
authentication is actually done.

If a proxy sends something that is neither an email nor an account id
(`github:someone`, an opaque OIDC subject), there is nowhere to record it —
give that account a username matching what the proxy sends, or configure the proxy
to send the user's email, which most can.

## Why the accounts have no passwords

An account here answers "which of you did this", not "are you allowed in".
The first question needs a stable name to write into a file; the second needs
credential storage, sessions, resets, lockout and rate limiting — a permanent
obligation, and where small self-hosted projects' CVEs live.

They are also different in scope. There are no per-note permissions in this
data model, so being allowed in is all-or-nothing, and a reverse proxy does
all-or-nothing better than this app could, before a request reaches Python.
So accounts carry identity, your proxy carries authentication, and the emails
column is how the two are joined up.

If you need per-note access control, this is the wrong tool — the data model
has nowhere to put it.

## Requirements

Python 3.11+, FastAPI, uvicorn, pydantic. That's the whole dependency list.
`pytest` for the tests, which are worth running if you change the path
scoping:

```sh
pytest
```

