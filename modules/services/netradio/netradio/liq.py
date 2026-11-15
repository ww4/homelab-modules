"""Generate the Liquidsoap script from the runtime station list.

Stations are runtime config now (the admin page adds specialty feeds), and
each needs its own outputs, so the script is rendered at service start
from stations.json rather than baked into the flake. Adding a station =
save + apply (the apply path unit restarts Liquidsoap).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from netradio.config import Config

HEADER = '''settings.log.stdout := true
settings.log.file := false
settings.log.level := 3
# The server label, QUIETER than the rest. At level 3 it logs a pair of lines
# for every connection — "New client unix socket" and "disconnected without
# saying goodbye" — and the DJ opens one connection per station every 5 s. At
# this library's station count that is about five a second, ~900,000 lines a
# day, and it had eaten the journal: with `SystemMaxUse=1G` the box kept only
# about a day and a half of history, so the evening a deploy restarted the
# speakers was already vacuumed away by the time I went looking for it
# (2026-10-24). Worse, the horizon MOVES while you read: the same query
# returned 1,932 lines and then 0 fifteen minutes later, which reads exactly
# like an event starting at that timestamp. It isn't one. It is the edge of
# what is left.
#
# Level 2 keeps the server's warnings and errors and drops the arrival notice.
# A short-lived connection per command is how this client works, so the
# logging is the thing to fix, not the polling.
settings.server.log.level := 2
settings.server.socket := true
settings.server.socket.path := {socket}
settings.server.socket.permissions := 0o600
settings.server.timeout := -1.

password = environment.get("ICECAST_SOURCE_PASSWORD")
playlists = {playlists}
now_dir = {now_dir}

def station(mount, name) =
  # The DJ (netradio-dj) feeds q_<mount> two items ahead — tracks it chose,
  # and every few of them a rendered break. The plain shuffle is the
  # fallback: it plays whenever the queue is empty (the first track after a
  # wake, or the DJ being down), and hands back at the next track boundary.
  pl = playlist(id="pl_" ^ mount, mode="randomize", reload_mode="watch",
                playlists ^ "/" ^ mount ^ ".m3u")
  q = request.queue(id="q_" ^ mount)
  sw = fallback(id="src_" ^ mount, track_sensitive=true, [q, pl])
  # The DJ's skip. A fallback registers no command of its own, and the
  # output's `<mount>.skip` sits above the crossfade, which lets the abort
  # through twice (two tracks gone, five seconds late — 2026-09-19). Skipping
  # the fallback itself ends the selected source's track and nothing else.
  #
  # It has to be the FALLBACK and not the queue. `q_<mount>.flush_and_skip`
  # skips the queue, and the queue is only one of the two things that can be
  # on air: after a restart, or any time the DJ has fallen behind, `pl` holds
  # the microphone and a skip aimed at the queue is silent (the owner pressed it
  # four times against a Bowie track on Ambient, 2026-09-29 15:24).
  server.register(namespace="src_" ^ mount, description="Skip the playing track.", "skip",
                  fun (_) -> begin sw.skip() "Done" end)
  # Drop what is QUEUED without touching what is playing. Liquidsoap's own
  # `flush_and_skip` does both at once, which is the wrong shape here: the DJ
  # wants to throw away a running order it is about to rewrite, write the new
  # one, and only then take the current track off. Two separate commands, so
  # the refill can sit between them.
  server.register(namespace="src_" ^ mount, description="Drop everything queued behind the playing track.", "flush",
                  fun (_) -> begin q.set_queue([]) "Done" end)
  # Breaks carry liq_amplify (speech renders ~8 dB under the music).
  s = amplify(1., override="liq_amplify", sw)
  s = crossfade(s)
  s = mksafe(s)
  # The first track's metadata is emitted BEFORE the Icecast connection is
  # up, so its ICY title can be lost; keep the last metadata and re-insert
  # it once the mount is connected.
  s = insert_metadata(s)
  last = ref([])
  # The last ten things played, as JSON for the radio page. The re-insert
  # fires this handler a second time for the same track: not added twice.
  history = ref([])
  s.on_metadata(synchronous=true, fun (m) -> begin
    last := m
    log(label=mount, level=3, "now playing: " ^ m["artist"] ^ " - " ^ m["title"])
    if m["title"] != "" then
      # `filename` is the request's path: the page's never-again / request
      # buttons need it (a break's filename is its rendered wav — harmless).
      entry = {{artist = m["artist"], title = m["title"], path = m["filename"],
               kind = (if m["dj"] == "true" then "break" else "track" end), at = time()}}
      same = (fun (h) -> h.artist == entry.artist and h.title == entry.title)
      if not (list.length(history()) > 0 and same(list.hd(default=entry, history()))) then
        history := list.prefix(10, [entry, ...history()])
        file.write(data=json.stringify(history()), now_dir ^ "/" ^ mount ^ ".json")
      end
    end
  end)
  # Two encodes of the same program: 192 kbps, and a 96 kbps "-lo" mount
  # for phones on cellular. Each is an on-demand output of its own.
  def out(id, mnt, kbps) =
    output.icecast(%mp3(bitrate=kbps), id=id, start=false,
                   host="127.0.0.1", port={port}, password=password,
                   mount="/" ^ mnt ^ ".mp3", name=name, genre=name,
                   description="Library station", public=false,
                   on_connect={{ if last() != [] then s.insert_metadata(last()) end }},
                   s)
  end
  out(mount, mount, 192)
  out(mount ^ "-lo", mount ^ "-lo", 96)
end

# An outside station re-encoded to MP3, for a device that cannot decode what it
# is served in. Same on-demand shape as a library station: `start=false`, so
# nothing runs until the wake service starts it and it stops again when the last
# listener leaves. Costs about 4% of a core while somebody is actually listening
# (measured: 10 s of HE-AACv2 transcoded in 0.61 s at 38% of one core).
#
# mksafe so a dropout is silence rather than a dead output — the stream belongs
# to somebody else and will go away sometimes.
def relay(mount, name, stream) =
  s = mksafe(input.http(stream))
  output.icecast(%mp3(bitrate=128), id=mount, start=false,
                 host="127.0.0.1", port={port}, password=password,
                 mount="/" ^ mount ^ ".mp3", name=name, genre=name,
                 description="Internet radio", public=false, s)
end

'''


def render(stations: list[dict], *, socket: str, playlists: str, now_dir: str, port: int,
           relays: list[dict] | None = None) -> str:
    body = HEADER.format(socket=json.dumps(socket), playlists=json.dumps(playlists),
                         now_dir=json.dumps(now_dir), port=port)
    for s in stations:
        body += f"station({json.dumps(s['mount'])}, {json.dumps(s['name'])})\n"
    for r in relays or []:
        body += (f"relay({json.dumps(r['mount'])}, {json.dumps(r['name'])}, "
                 f"{json.dumps(r['url'])})\n")
    return body


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--config", required=True, type=Path)
    ap.add_argument("--socket", required=True)
    ap.add_argument("--playlists", required=True)
    ap.add_argument("--now-dir", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--relays", type=Path,
                    help="JSON [{mount,name,url}] of outside stations to re-encode on demand")
    args = ap.parse_args(argv)
    stations = Config(args.config).stations()
    if not stations:
        print("no stations in config — nothing to run", file=sys.stderr)
        return 1
    relays = []
    if args.relays and args.relays.exists():
        try:
            relays = json.loads(args.relays.read_text())
        except ValueError as e:
            print(f"ignoring {args.relays}: {e}", file=sys.stderr)
    args.out.write_text(render(stations, relays=relays, socket=args.socket, playlists=args.playlists,
                               now_dir=args.now_dir, port=args.port))
    print(f"{args.out}: {len(stations)} stations")
    return 0
