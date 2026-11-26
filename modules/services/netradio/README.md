# netradio

Radio stations built from a music library you already own.

A scanner sorts the library into genre stations; a DJ sequences each one and
talks between the songs; Liquidsoap and Icecast put them on air; a web page and
an optional stand-in for vTuner let you listen from a phone, a browser, a
network receiver, or the machine's own sound card.

It is a NixOS module. Everything site-specific is an option — the smallest
useful configuration is a domain and a library:

```nix
homelab.netradio = {
  enable = true;
  domain = "radio.example.com";
  libraryRoots = [ "/srv/music" ];
};
```

That gives you the stations, the listener page and the admin page. Everything
else is off until asked for.

---

## What you get

| | |
|---|---|
| **Stations** | Genre programmes built from tags, plus "everything", plus any specialty station you describe |
| **A DJ** | Sequences each station, keeps a track from repeating, and records a spoken link between songs |
| **On demand** | An encoder starts when somebody tunes in and stops five minutes after the last listener leaves |
| **Two pages** | A phone remote and a wide-screen player, sharing one design |
| **Feedback** | Skip (which also means *less of this*), a heart, "never again", and song requests |
| **Outputs** | This browser, a network receiver, and the machine's own sound card |

Optional, each behind its own option: a vTuner stand-in so an older network
receiver's "Internet Radio" input works again, Jellyfin for library discovery
and favourites, Pandora and tuner control on a Yamaha receiver, an
acoustic-analysis pass, and agent-written station rules.

## Requirements

- NixOS, with the module imported
- A music library on disk, readable by the service user
- **Liquidsoap** and **Icecast** — the module configures both; you do not
- Optional: a text-to-speech endpoint with an OpenAI-compatible
  `/v1/audio/speech` for the DJ's voice. Without one the stations play music
  and simply never talk
- Optional: Jellyfin, for library discovery and to mirror the heart
- Optional: **YCast**, for the vTuner stand-in

Nothing is fetched from the internet at runtime: the page's Vue and CSS are
vendored by hash at build time.

## Setting it up

### The minimum

```nix
{
  imports = [ homelab-modules.nixosModules.netradio ];

  homelab.netradio = {
    enable = true;
    domain = "radio.example.com";
    libraryRoots = [ "/srv/music" ];
  };
}
```

The scanner walks the library on a timer, sorts it into stations, and writes
the runtime files under `stateDir` (default `/var/lib/netradio`). The page is
served at the domain; the admin page is at `/admin/`.

### Giving the DJ a voice

```nix
homelab.netradio.tts = {
  url = "http://127.0.0.1:8880";   # anything with /v1/audio/speech
  voice = "af_heart";
};
```

### The machine's own speakers

```nix
homelab.netradio.speaker = {
  enable = true;
  label = "Kitchen speakers";
  device = "plughw:0,0";           # an ALSA device, NOT `default`
  defaultMount = "everything";   # a station mount, as listed on /admin/
  startVolume = 70;
};
```

> `default` is usually redirected to a per-user PipeWire that the service user
> cannot reach. Name the hardware device.

### Speakers on another machine

The speakers above belong to the radio server. For a *different* machine — a
desktop in another room, a games PC — enable the `netradio-speaker` module
there instead. It is the listening end on its own: no library, no Liquidsoap,
no page, just speakers that can be told to play one of the stations.

On that machine:

```nix
homelab.netradio-speaker = {
  enable    = true;
  user      = "alice";                            # whose session owns the sound
  output    = "pulse";                            # a desktop, sharing its sink
  stations  = "https://radio.example.com/radio";  # where the mounts are served
  listen    = "0.0.0.0";
  tokenFile = "/run/secrets/netradio-speaker";
  firewallInterfaces = [ "tailscale0" ];
};
```

and on the radio server, register it like any other device:

```nix
homelab.netradio.devices.studio = {
  name = "Studio PC";
  endpoint = "http://studio.example.com:8013";
  capabilities = [ "play" "stop" "volume" "mute" "stations" ];
};
```

Two things differ from the server's own speakers, and both matter:

- **`output = "pulse"` sets the level on this service's own audio stream, not
  on the sink.** On a machine where a game or a browser plays through that same
  sink, moving the sink would move those too. Pulse needs a session to talk to,
  so it runs as a user service and `user` is required.
- **`tokenFile` is required once `listen` is not loopback**, and the service
  refuses to start without it. A loopback endpoint is safe because the radio
  page's own vhost is the access gate; a port on the network is behind nothing,
  and this service turns speakers on in someone's house.

  If something else is your gate — a firewall scoped to one interface, or an
  overlay network nothing unauthenticated can route into — set
  `allowUnauthenticated = true` instead of a token. It has to be explicit,
  because an unauthenticated port on a LAN behaves exactly like a correctly
  configured one right up until someone else finds it.

### The receiver bridge, here or elsewhere

`receiver.apiUrl` is a URL, so whatever talks to the receiver does not have to
run on the radio server. This is a free choice, unlike speakers: the receiver
is reached over the network either way, and a bridge beside the radio server
has exactly the same access to it as a bridge on a different box.

Loopback is the sensible default, and `afterUnits` orders the play logger after
the bridge's unit when the two share a machine. For a bridge elsewhere, name
the address and leave `afterUnits` empty — there is no local unit to wait for:

```nix
homelab.netradio.receiver = {
  enable = true;
  apiUrl = "http://receiver-box:8791";
  afterUnits = [ ];
};
```

`statePath` is there for the same reason. A bridge that answers `status` rather
than `state` is usable as it stands, without being rewritten to match this
module's spelling.

What gates such a bridge is whatever you put in front of it, which is also true
of the receiver. `services.yamaha-ync` binds loopback by default for that
reason; a bridge reachable on the network should be scoped to one interface,
the way `profile.remotes` endpoints are.

Note what a password on the bridge would *not* buy, before adding one. The
receiver's own control protocol has no authentication — that is the reason a
bridge exists in this shape — so anyone who can reach the bridge can reach the
receiver directly and skip it. The lock would be on the second door.

### A picture for one track

A track's artwork is looked for in this order:

1. **A picture named after the track, beside it** — `03 Warszawa.jpg` next to
   `03 Warszawa.mp3`. `.jpg`, `.jpeg`, `.png` and `.webp` are read.
2. Artwork embedded in the file's tags
3. A cover file in the folder — `cover.jpg`, `folder.jpg`, and the rest
4. Jellyfin's picture of the artist, if Jellyfin is configured

A folder cover is an album's sleeve, which is the right idea for an album and
the wrong idea for a folder that isn't one: a mix series, a set of singles, a
field-recording dump. The sidecar is also the only way to correct one wrong
embedded cover without rewriting the file's tags.

Adding one takes effect immediately — the thumbnail cache is keyed on the
sidecar as well as on the track and the folder cover, so a picture dropped into
a library that already has every track cached still appears at once.

If the pictures you have are numbered by episode rather than named after the
tracks, this maps them:

```sh
cd /path/to/the/album
for img in /path/to/pictures/*.jpg; do
  n=$(basename "$img" .jpg)                       # "42"
  for track in *_"$n"-*.mp3; do                   # the track whose name carries 42
    [ -e "$track" ] && cp -n "$img" "${track%.mp3}.jpg"
  done
done
```

Adjust the glob to your filenames, and run it with `echo` in front of `cp`
first to see what it would do.

### Pictures for stations that have no album art

A station's tile is normally a mosaic of its own album covers. Stations with no
albums to draw on — an ambient bed, or a station that is the whole library —
can be given a picture:

```nix
homelab.netradio.stationArt = {
  rain = ./art/rain.jpg;
  everything = ./art/everything.jpg;
};
```

### Jellyfin

```nix
homelab.netradio.jellyfin = {
  enable = true;
  url = "http://127.0.0.1:8096";
  keyFile = "/run/secrets/jellyfin-api";
  discoverRoots = true;   # let Jellyfin's library config name the music folders
  hearts = true;          # mirror the heart to Jellyfin favourites
};
```

`discoverRoots` is a convenience, not a substitute for `libraryRoots`: when
Jellyfin cannot be reached the scanner falls back to the named roots alone, so
a station whose music lives *only* in a discovered folder will empty out during
an outage. Name the folders you care about.

`hearts` is separate from the credentials on purpose. Connecting Jellyfin for
artwork does not give netradio permission to write favourites into your
library.

The full option surface is in `default.nix` under `options.homelab.netradio`.
Every option carries a description; `nixos-option homelab.netradio` will read
them back to you.

---

## How it fits together

```
   library on disk
         │
         ▼
   netradio-playlists ──► station playlists, pools, tiles.json, counts
   (timer, or on demand)          │
                                  ▼
                            netradio-dj ──► Liquidsoap request queues
                                  │           (one per station)
                      spoken links via TTS
                                  │
                                  ▼
                            Liquidsoap ──► Icecast ──► listeners
                                  ▲
                    netradio-wake │ starts an encoder when nginx sees a
                                  │ request for its mount, stops it when idle
                                  │
   browser / phone ───────────────┘
   network receiver ──► YCast (vTuner stand-in) ──┘
   this machine ──────► netradio-speaker
```

The pieces, each its own unit:

| Unit | Does |
|---|---|
| `netradio-playlists` | Walks the library, builds stations, writes what the pages read |
| `netradio-dj` | Sequences every station, queues tracks and spoken links |
| `netradio-liquidsoap` | The encoders, one output per station, all starting stopped |
| `netradio-icecast` | Serves the streams |
| `netradio-wake` | Starts an encoder on demand; nginx asks it before proxying |
| `netradio-admin` | The admin API: stations, feedback, search, cover art |
| `netradio-speaker` | Speakers as a listening target — this machine's card, or another machine's via the `netradio-speaker` module |
| `netradio-resume` | Puts a network receiver back after an encoder restart |
| `netradio-ambient` | Fetches ambient beds for fixed stations |
| `netradio-profile` | Optional acoustic analysis (loudness, talk detection) |
| `netradio-hearts` | Optional Jellyfin favourites sync |
| `netradio-apply` | The privileged side: rescan and restart, on request from the admin API |

### The Python package

`netradio/` is one package with a subcommand per job (`netradio playlists`,
`netradio dj`, …). `cli.py` is the whole dispatch.

| Module | |
|---|---|
| `conventions.py` | Facts every module must agree on: cover and sidecar filenames, what a mount may be called |
| `config.py` | Runtime paths and the JSON files under `stateDir` |
| `playlists.py` | The scanner: tags → stations, pools, tiles |
| `rules.py`, `feeds.py`, `schedule.py` | How a station's membership and its programme are decided |
| `dj.py` | Sequencing, the spoken links, the request queue |
| `liq.py` | Renders the Liquidsoap script |
| `wake.py` | On-demand encoder start/stop |
| `admin.py` | The admin API and cover art |
| `ratings.py` | What a skip and a heart do to the odds |
| `jellyfin.py` | Library discovery and the heart mirror |
| `speaker.py` | The local sound card |
| `resume.py` | Putting a receiver back |
| `pandora.py`, `profile.py`, `profile_server.py`, `compile.py`, `ambient.py`, `migrate.py` | Optional extras |

### The pages

`web/` is served as plain files — no bundler, deliberately.

- `index.html` + `app.js` — the phone remote
- `desktop.html` + `desktop.js` + `desktop.css` — the wide-screen player
- `radio.js` — the fetch helpers both share
- `admin/` — the admin page

A wide screen is redirected from the remote to the desktop page automatically;
either page can be pinned with `?view=remote` or `?view=desktop`.

---

## Developing

```sh
# the tests, from this directory
python3 -m unittest discover -s ./tests -t ./tests

# the page checks need the web directory named
NETRADIO_WEB=./web NETRADIO_NIX=./default.nix python3 -m unittest discover -s ./tests -t ./tests
```

The `netradio-web` derivation runs the page checks at build time, and it is
nginx's document root — so a page that fails them cannot deploy.

Two conventions worth knowing before you edit:

- **`web/` is not part of the Python package's source.** Editing a page must
  not rehash the package and restart every unit. `tests/` *is* part of it, so
  editing a test does restart them.
- **Flake builds read the git tree.** A new file that has not been `git add`ed
  is invisible to the build, and the failure looks like a missing feature
  rather than a missing file.

### Comments name people and dates

Comments like `(2026-09-27)` or a name and a date record *why* a decision was
made and when — which listener complaint, which outage, which measurement. They
are deliberate. The history of a system like this is most of its design
rationale, and a comment that says only *what* the code does is worth much less
than one that says what happened.

---

## Licence

See the repository root.
