"""`netradio resume` — put the receiver back on the station it was playing.

Every encoder restarts when Liquidsoap does, which is any deploy that changes
the package, and that drops every listener for a moment. The browser pages
reconnect by themselves (since 2026-09-29 — before that they said "stream error
— try again" and stopped, which is how this comment came to be wrong) and the
local speaker has a watchdog that restarts its player. A receiver does not: an
R-N301 goes to Stop and stays there. On 2026-09-27 the owner
reported no audio on Classic Country in the living room four hours after the
deploy that had silenced it, and nothing was broken — the receiver had simply
stopped and nobody had told it to start again.

So this runs after Liquidsoap and puts it back. It is somebody's living room,
so the guardrails are the point:

  * only if the receiver is already POWERED ON — it never wakes a unit
  * only if it is already on the receiver's net-radio INPUT — never switches
  * only if it is NOT already playing something
  * only to a station it was genuinely observed playing, remembered by the
    receiver poller in <now>/receiver.json
  * never touches volume, and never touches mute

If any of those does not hold it does nothing and logs which one, because a box
that quietly drives a stereo is worse than one that quietly does not.

It also waits for the station's encoder to be awake before telling the receiver
to play: the encoders are on demand, and a receiver that asks half a second too
early gets a 404 from Icecast and stops again — which is the failure this exists
to clear, arrived at by a different road.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("netradio.resume")

STATE = "receiver.json"
# What a receiver calls the input the library streams arrive on.
NET_RADIO = "NET RADIO"


def _get(url: str, timeout: float = 10.0, headers: dict | None = None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r) if "json" in (r.headers.get("Content-Type") or "") else r.read()


def _post(url: str, body: dict, timeout: float = 90.0):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        try:
            return json.load(r)
        except ValueError:
            return {}


def observe(st: dict | None, menu_text: str, previous: dict | None = None) -> dict:
    """The record to write after one poll of the receiver.

    `station` and `mount` are STICKY: a receiver that has stopped reports no
    station at all, so the last library station seen playing has to be
    remembered or there is nothing to resume to. The live fields (input,
    playback) are not sticky — they describe right now.
    """
    from netradio.playlists import parse_ycast_yaml

    prev = previous or {}
    out = {
        "at": dt.datetime.now().replace(microsecond=0).isoformat(),
        "input": (st or {}).get("input") or "",
        "playback": ((st or {}).get("now_playing") or {}).get("playback") or "",
        "reachable": st is not None,
        "last_library": prev.get("last_library"),
    }
    np = (st or {}).get("now_playing") or {}
    name = np.get("station") or ""
    if st and out["playback"] == "Play" and name:
        for category, entry, url in parse_ycast_yaml(menu_text):
            if entry == name and "/radio/" in url:
                out["last_library"] = {
                    "station": name,
                    "mount": url.rsplit("/", 1)[-1].removesuffix(".mp3"),
                    "category": category,
                    "at": out["at"],
                }
                break
    return out


def load(now_dir: Path) -> dict:
    try:
        return json.loads((now_dir / STATE).read_text())
    except Exception:
        return {}


def save(now_dir: Path, record: dict) -> None:
    path = now_dir / STATE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record))
    tmp.replace(path)


def wake(wake_url: str, mount: str, timeout: float) -> bool:
    """Start the station's encoder, the same door nginx's auth_request uses.
    Keeps trying: right after a Liquidsoap restart the socket is not there yet
    and the wake answers 503."""
    if not wake_url:
        return True
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            _get(f"{wake_url.rstrip('/')}/wake", timeout=30,
                 headers={"X-Original-URI": f"/radio/{mount}.mp3"})
            return True
        except Exception as e:
            last = str(e)
            time.sleep(2)
    log.warning("encoder for %s never woke within %.0fs (%s)", mount, timeout, last)
    return False


def why_not(st: dict | None, remembered: dict | None, force: bool = False) -> str:
    """The reason not to act, or "" to go ahead. Separate from doing it so the
    decision is testable without a receiver."""
    if not remembered or not remembered.get("mount"):
        return "no library station has been seen playing, so there is nothing to resume"
    if st is None:
        return "the receiver's API did not answer"
    if not st.get("on"):
        return "the receiver is in standby — resume never powers it on"
    if (st.get("input") or "") != NET_RADIO:
        return f"the receiver is on {st.get('input') or 'an unknown input'}, not {NET_RADIO}"
    playback = ((st.get("now_playing") or {}).get("playback") or "")
    if playback == "Play" and not force:
        return "the receiver is already playing"
    return ""


def resume(api: str, now_dir: Path, wake_url: str = "", force: bool = False,
           wake_timeout: float = 120.0) -> str:
    """Do it, or say why not. Returns a line fit for a log or the page."""
    api = api.rstrip("/")
    record = load(now_dir)
    remembered = (record or {}).get("last_library") or {}
    try:
        st = _get(f"{api}/status", timeout=15)
    except Exception as e:
        log.debug("status failed: %s", e)
        st = None

    reason = why_not(st, remembered, force)
    if reason:
        log.info("not resuming: %s", reason)
        return f"not resuming: {reason}"

    mount, station = remembered["mount"], remembered["station"]
    category = remembered.get("category") or "Curated"
    wake(wake_url, mount, wake_timeout)
    log.info("resuming %s (%s / %s)", station, category, mount)
    try:
        _post(f"{api}/menu/path", {"path": ["My Stations", category, station]})
    except Exception as e:
        log.warning("resume of %s failed: %s", station, e)
        return f"resume of {station} failed: {e}"
    return f"resumed {station}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", required=True, help="the receiver API base URL")
    ap.add_argument("--now-dir", required=True, type=Path, help="where receiver.json lives")
    ap.add_argument("--wake", default="", help="the wake service, to start the encoder first")
    ap.add_argument("--wake-timeout", type=float, default=120.0)
    ap.add_argument("--settle", type=float, default=0.0,
                    help="wait this long before looking — a receiver takes a moment to notice a dropped stream")
    ap.add_argument("--force", action="store_true", help="re-issue even if it is already playing")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)
    if args.settle > 0:
        time.sleep(args.settle)
    print(resume(args.api, args.now_dir, args.wake, args.force, args.wake_timeout))
    return 0
