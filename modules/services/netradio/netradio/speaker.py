"""`netradio speaker` — a machine's own speakers as a playback endpoint.

The library stations already reach the phone and the living-room receiver;
this makes a pair of speakers a third place to send them. It keeps one ffmpeg
child alive on a station's mount and answers a small JSON API so the remote
can drive it:

    GET  /state                  {playing, mount, volume, muted, error}
    POST /play    {"mount": "rain"}
    POST /stop
    POST /volume  {"level": 40} | {"step": 5} | {"step": -5}
    POST /mute    {"on": true}

Two outputs, because two situations are genuinely different (see Output
below). On a server whose card nothing else is using, the volume is the ALSA
mixer, so it is the real output level rather than a software attenuation. On a
desktop machine sharing a PipeWire sink with a game, the volume is set on
THIS SERVICE'S STREAM, so turning the radio down leaves the game alone.

A crashed player is restarted; a station that stops streaming leaves the
service trying, which is what you want for an all-night rain loop.

The default mount plays at boot, so power-cycling the box brings the rain
back with no phone involved.

The API is unauthenticated when it is bound to loopback, which is right when
a vhost in front of it is the access gate. Bound anywhere else it requires a
shared token, and refuses to start without one — an endpoint on a second
machine is reachable by everything on the network.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from netradio.conventions import is_mount

log = logging.getLogger("netradio.speaker")


# ALSA's `default` is redirected to PipeWire by 99-pipewire-default.conf, and
# PipeWire on this box is a per-user service belonging to the desktop session.
# A system service reaching for it gets EHOSTDOWN — "Host is down".
DEFAULT_DEVICE = "plughw:0,0"


def is_loopback(host: str) -> bool:
    """True only for an address that nothing off this machine can reach.

    A bind address of "" or 0.0.0.0 is emphatically NOT loopback, and that is
    the case this exists to catch.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False



# ---------------------------------------------------------------------------
# Where the sound goes.
#
# Two outputs exist because the two situations are genuinely different, not
# because the code wanted a plug-in point:
#
#   AlsaOutput   the server's own card, which nothing else is using. The level
#                is the card's own mixer, so it is the real output level.
#   PulseOutput  a PipeWire/PulseAudio sink on a machine with a desktop
#                session — a second PC on the network, say, where a game is
#                making sound too. The level is OUR STREAM's, never the
#                sink's: moving the sink would move the game with it.
#
# Everything in Mixer below is policy — the ceiling, the ramp, the mute that
# is a level of zero rather than a switch, the watchdog — and is shared. An
# output only says how ffmpeg reaches it, and how one number is read and
# written. Adding a third is those three methods and nothing else.
# ---------------------------------------------------------------------------


class Output:
    """One place to send sound, plus the level control belonging to it."""

    kind = "none"

    # True when a second stream can play alongside the station (the rain bed).
    # A shared sink mixes for free; an exclusive ALSA device does not.
    mixes = False

    def prepare(self) -> None:
        """Called once, before any level is set."""

    def control_name(self) -> str:
        """Non-empty when a level can be set at all. Empty is not an error: the
        music plays and the volume verbs quietly do nothing, which is better
        than refusing to play because a mixer could not be found."""
        return ""

    def output_args(self) -> list[str]:
        """The tail of the ffmpeg command line — where to send the audio."""
        raise NotImplementedError

    def await_ready(self, timeout: float = 2.0) -> bool:
        """Wait until a level can actually be written, after the player starts.

        A card is ready before anything plays, so this is a no-op for ALSA. A
        stream is not: it appears a moment after ffmpeg does, and setting the
        level before then writes to nothing.
        """
        return True

    def read_state(self) -> tuple[int | None, bool]:
        """(level 0-100 or None, muted-by-something-else).

        One call returns both because on ALSA both come out of a single
        `amixer sget`, and the watchdog runs this every two seconds — making
        it two calls would double the forks for no gain.
        """
        raise NotImplementedError

    def write_level(self, pct: int) -> None:
        raise NotImplementedError


class AlsaOutput(Output):
    """The server's own sound card: ffmpeg decodes straight to an ALSA device
    and the level is the card's mixer through amixer. `control` is the mixer
    control ('Master' on most, 'PCM' on some codecs); the first one that
    answers is used, so a different card needs no configuration."""

    kind = "alsa"

    CANDIDATES = ("Master", "PCM", "Speaker", "Headphone", "Digital")

    def __init__(self, card: str = "0", amixer: str = "amixer", control: str = "",
                 device: str = ""):
        self.card = card
        self.amixer = amixer
        self.device = device or DEFAULT_DEVICE
        self._control = control

    def prepare(self) -> None:
        # The codec's own mute switch is never touched after this (see
        # Mixer.mute), so clear it ONCE at startup — an `[off]` left in
        # alsa-state from before would otherwise make the service silent for
        # ever while it happily ramped a volume nobody could hear. This is the
        # one click per start.
        if self.control_name():
            self._run("sset", self.control_name(), "unmute")

    def control_name(self) -> str:
        if not self._control:
            self._control = self._find()
        return self._control

    def output_args(self) -> list[str]:
        return ["-f", "alsa", self.device]

    def _run(self, *args: str) -> str:
        """amixer missing or the card gone: no volume control, but the music
        keeps playing — never take the player down over the mixer."""
        try:
            return subprocess.run([self.amixer, "-c", self.card, *args],
                                  capture_output=True, text=True, timeout=10).stdout
        except Exception as e:
            log.warning("amixer %s failed: %s", " ".join(args), e)
            return ""

    def _find(self) -> str:
        out = self._run("scontrols")
        names = re.findall(r"Simple mixer control '([^']+)'", out)
        for want in self.CANDIDATES:
            if want in names:
                return want
        return names[0] if names else ""

    def read_state(self) -> tuple[int | None, bool]:
        control = self.control_name()
        if not control:
            return None, False
        out = self._run("sget", control)
        pct = re.search(r"\[(\d{1,3})%\]", out)
        return (int(pct.group(1)) if pct else None), ("[off]" in out)

    def write_level(self, pct: int) -> None:
        control = self.control_name()
        if control:
            self._run("sset", control, f"{pct}%")


class PulseOutput(Output):
    """A PipeWire or PulseAudio sink, for a machine with a desktop session.

    The level is this service's OWN stream, not the sink. That is the whole
    reason this output exists separately: on a machine where something else
    is making sound — a game, a browser — the sink level is shared, so
    turning the radio down by moving the sink would turn the game down with
    it. A sink-input's volume belongs to one stream.

    It follows that the level has to be re-applied every time the stream is
    created, because a brand-new sink-input starts at 100%. Mixer.reapply()
    is called after each (re)connect for exactly that.
    """

    kind = "pulse"
    mixes = True

    def __init__(self, sink: str = "", pactl: str = "pactl",
                 stream_name: str = "netradio"):
        self.sink = sink
        self.pactl = pactl
        self.stream_name = stream_name

    def control_name(self) -> str:
        return "stream" if self.pactl else ""

    def output_args(self) -> list[str]:
        # `-name` is the application name, which is how our own sink-input is
        # found again below. The trailing argument is the stream description
        # the mixer applet shows.
        args = ["-f", "pulse", "-name", self.stream_name]
        if self.sink:
            args += ["-device", self.sink]
        return args + [self.stream_name]

    def _run(self, *args: str) -> str:
        try:
            return subprocess.run([self.pactl, *args],
                                  capture_output=True, text=True, timeout=10).stdout
        except Exception as e:
            log.warning("pactl %s failed: %s", " ".join(args), e)
            return ""

    def _ours(self) -> dict | None:
        """Our sink-input, or None when nothing is playing yet.

        JSON rather than parsing `pactl list`'s prose: the text form is laid
        out for people and has changed shape between releases.
        """
        try:
            items = json.loads(self._run("-f", "json", "list", "sink-inputs") or "[]")
        except ValueError:
            log.warning("pactl did not return JSON for sink-inputs")
            return None
        for si in items or []:
            if (si.get("properties") or {}).get("application.name") == self.stream_name:
                return si
        return None

    def await_ready(self, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._ours() is not None:
                return True
            if time.monotonic() >= deadline:
                log.warning("no %s stream appeared within %.1fs — level not applied",
                            self.stream_name, timeout)
                return False
            time.sleep(0.1)

    def read_state(self) -> tuple[int | None, bool]:
        si = self._ours()
        if not si:
            # Not playing. None means "no reading", which Mixer already
            # handles everywhere — it is the same answer a missing mixer gives.
            return None, False
        vol = si.get("volume") or {}
        for chan in vol.values():
            raw = str((chan or {}).get("value_percent", "")).rstrip("%")
            if raw.isdigit():
                return int(raw), bool(si.get("mute"))
        return None, bool(si.get("mute"))

    def write_level(self, pct: int) -> None:
        si = self._ours()
        if si is None:
            # Nothing to set yet. The level is remembered by Mixer.target and
            # applied by reapply() when the stream appears.
            log.debug("no stream to set the level on yet")
            return
        self._run("set-sink-input-volume", str(si.get("index")), f"{pct}%")


class Mixer:
    """The output level, and every rule about how it may change.

    Knows nothing about ALSA or PulseAudio — that is the `Output` it is
    given. `Mixer(card, ...)` still builds an AlsaOutput itself, because that
    is how it has always been called and how most installations use it.
    """

    CANDIDATES = AlsaOutput.CANDIDATES

    # Class-level defaults so an instance built without __init__ still behaves.
    # Tests and diagnostics do that, and every time a new instance attribute
    # appeared they broke on AttributeError instead of on the thing under test.
    cap = 100
    target = 50
    _muted = False
    # Asymmetric on purpose. Coming back is a fade IN and wants to be gentle;
    # going away, and any ordinary volume change, wants to feel immediate.
    # The owner set 300 ms in by ear (2026-09-27).
    fade_in_ms = 300
    fade_out_ms = 120

    def __init__(self, card: str = "0", amixer: str = "amixer", control: str = "", cap: int = 100,
                 fade_in_ms: int | None = None, fade_out_ms: int | None = None,
                 output: Output | None = None):
        self.out = output if output is not None else AlsaOutput(card, amixer, control)
        # A ceiling nothing can exceed: not the UI, not a stray request, not a
        # diagnostic. The owner found the live level at 100% on 2026-09-26 with no
        # way to tell what had put it there, and on speakers that is the kind of
        # accident worth making impossible rather than unlikely.
        self.cap = max(0, min(100, int(cap)))
        if fade_in_ms is not None:
            self.fade_in_ms = max(0, int(fade_in_ms))
        if fade_out_ms is not None:
            self.fade_out_ms = max(0, int(fade_out_ms))
        self.control = self.out.control_name()
        self.lock = threading.RLock()
        self._muted = False
        self.out.prepare()
        # Where an unmute returns to. A muted control reads 0%, so the level
        # has to be remembered rather than read back.
        cur, _ = self.state()
        self.target = cur if cur else 50

    def state(self) -> tuple[int | None, bool]:
        """(volume 0-100, muted). Muted is OUR state, because the mute is a
        level of zero rather than the codec's switch; an output that reports
        itself muted still counts, so someone muting the card by hand is not
        reported as unmuted."""
        if not self.control:
            return None, False
        level, muted_elsewhere = self.out.read_state()
        return level, (self._muted or muted_elsewhere)

    def reapply(self) -> None:
        """Put the chosen level back on the output.

        Needed because a PulseOutput's level lives on the stream, and a stream
        that has just been created starts at full. Harmless on ALSA, where the
        level is the card's and survives the player restarting, so the caller
        does not have to know which output it has.
        """
        with self.lock:
            if not self.control:
                return
            self.out.await_ready()
            self.set(0 if self._muted else self.target, "stream (re)started")

    def set(self, level: int, why: str = "") -> None:
        """Level only. Deliberately NOT `unmute` as well: the receiver does
        not unmute when you change its volume, and a box that boots muted
        must stay muted until someone asks for sound (2026-09-25).

        `why` is logged. It is empty for the steps of a ramp, which logs itself
        once at the top instead of 25 times — but EVERY other write names its
        reason, because twice now the level has been found at 100% with nothing
        in the journal that put it there, and the reason it could not be traced
        is that this function was silent (2026-09-26, 2026-10-01).
        """
        if not self.control:
            return
        want = max(0, min(self.cap, int(level)))
        if why:
            log.info("set %d%% (%s)", want, why)
        self.out.write_level(want)

    def choose(self, level: int) -> None:
        """A user-driven level change: remember it, and slide rather than jump.
        While muted it is only remembered — changing the volume must not bring
        the sound back."""
        level = max(0, min(self.cap, int(level)))
        with self.lock:
            cur, muted = self.state()
            log.info("volume %s -> %d%s", cur, level, " (muted, remembered only)" if muted else "")
            self.target = level
            if muted:
                return
            self._ramp(cur if cur is not None else level, level, self.fade_out_ms, "asked for")

    def step(self, delta: int) -> None:
        with self.lock:
            self.choose(self.target + delta)

    # One write every ~12 ms: fine enough that the steps are inaudible, coarse
    # enough that a 300 ms fade is 25 subprocess calls and not 300.
    STEP_MS = 12

    def _ramp(self, start: int, end: int, ms: int, why: str = "") -> None:
        """Walk the level from start to end over `ms`, so the change is a slope
        rather than a step. Each amixer write is one codec register write;
        spacing them is the whole trick.

        The pacing is against the CLOCK, not a fixed sleep per step: every
        write forks amixer, which costs a few milliseconds of its own, so
        sleeping a flat interval made the fade reliably longer than asked for.
        """
        if start == end or ms <= 0:
            if start != end:
                self.set(end, why)
            return
        log.info("ramp %d%% -> %d%% over %d ms%s", start, end, ms, f" ({why})" if why else "")
        steps = max(2, round(ms / self.STEP_MS))
        began = time.monotonic()
        for i in range(1, steps + 1):
            self.set(round(start + (end - start) * i / steps))
            due = began + (ms / 1000.0) * i / steps
            remaining = due - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)

    # How often to check the real level against the one we set, and how far
    # apart they may be before it counts. A ramp holds the lock, so nothing
    # here can catch a change in flight.
    #
    # This interval is not a sampling rate — it is HOW LONG THE ROOM IS LOUD.
    # The watchdog cannot stop the rise, only end it, so the number is the
    # worst case a sleeping person is exposed to. It caught the third
    # occurrence at 10:47 p.m. on 2026-10-22 and pulled 100% back to 0 in
    # 120 ms, but it had been sounding for up to ten seconds first, which at
    # that hour is the entire harm (2026-10-23). Two seconds costs one
    # `amixer sget` every two seconds — a fork and a few milliseconds — and
    # buys back eight.
    WATCH_S = 2.0
    DRIFT = 3

    def watch(self, stop: threading.Event) -> None:
        """Pull the level back down if something outside this service raises it.

        Twice the live level has been found at 100% with nothing in the journal
        that put it there — 2026-09-26, and again around 03:14 on 2026-10-01,
        which woke the owner out of a dead sleep. Both times the cause went unfound:
        no restart, no deploy, no API call, no card reset, nothing else on the
        box that drives amixer.

        So this does not try to identify the writer. It notices that the mixer
        no longer reads what this service set, says so LOUDLY, and puts it back.
        That is a guard that does not depend on knowing the cause, which — given
        two unexplained occurrences — is the only kind worth having.

        Only DOWNWARD corrections. If someone has turned the card down by hand
        that is their business; it is the rise that wakes people up.
        """
        while not stop.wait(self.WATCH_S):
            try:
                self.reconcile()
            except Exception:
                log.exception("volume watchdog")

    def reconcile(self) -> bool:
        """One check. Separate from the loop so it can be tested without
        waiting on a timer. True when it had to correct something."""
        if not self.control:
            return False
        with self.lock:
            cur, _ = self.state()
            if cur is None:
                return False
            want = 0 if self._muted else self.target
            if cur <= want + self.DRIFT:
                return False
            log.error("mixer read %d%% but this service set %d%% — pulling it back. "
                      "Nothing here asked for that.", cur, want)
            self._ramp(cur, want, self.fade_out_ms, "unexplained rise")
            return True

    def mute(self, on: bool) -> None:
        """Mute by taking the level to zero. The codec's mute switch is never
        written.

        The story, because it rules out the obvious alternatives. The owner asked
        for zero-crossing detection; a fade was tried first, on the theory that
        the click was the envelope step from full level to nothing. The fade is
        audible and pleasant — and the click was completely unchanged
        (2026-09-26). That measurement is the answer: a transient that does not
        care what the volume is, is not in the signal. It is the pin widget's
        mute bit stepping the analog stage's DC operating point, which is also
        why zero-crossing detection could never have helped — there is no
        crossing to catch.

        So: no switch. `Master` bottoms out at -64 dB (dbmin -6400 on this
        codec), which is inaudible, and the DAC node reports `mute=0` — it has
        no mute of its own to use instead. Ramping to the bottom is both silent
        and click-free, and `_muted` carries the state the switch used to.
        """
        if not self.control:
            return
        with self.lock:
            cur, _ = self.state()
            cur = cur if cur is not None else 50
            log.info("%s (level %s, returning to %d)", "mute" if on else "unmute", cur, self.target)
            if on:
                if not self._muted:
                    self.target = cur          # remember where to come back to
                self._ramp(cur, 0, self.fade_out_ms, "mute")
                self._muted = True
            else:
                self._muted = False
                self._ramp(0, self.target, self.fade_in_ms, "unmute")


class Player:
    """One ffmpeg on one mount, kept alive. Switching mounts kills and
    restarts it — a stream is not seekable, so there is nothing to keep.

    ffmpeg, not ffplay, for one reason: the output device has to be an
    *argument*. ffplay is an SDL program and takes its device from the
    AUDIODEV environment variable — but the ffplay in nixpkgs links
    sdl2-compat, which reimplements the SDL2 API on top of SDL3, and SDL3
    dropped AUDIODEV (the string does not appear in the library at all). So
    the variable was accepted, ignored, and ffplay opened ALSA's `default`
    anyway: straight into PipeWire, "Host is down", forever. An ignored
    environment variable fails silently; `-f alsa plughw:0,0` cannot."""

    def __init__(self, base: str, ffmpeg: str, device: str = "", wake: str = "",
                 output: Output | None = None, on_start=None):
        self.base = base.rstrip("/")
        self.ffmpeg = ffmpeg
        self.device = device or DEFAULT_DEVICE
        self.out = output if output is not None else AlsaOutput(device=self.device)
        # Called after every (re)connect. The level has to be re-applied for a
        # PulseOutput, whose level belongs to the stream rather than the card.
        self.on_start = on_start
        self.wake_url = wake.rstrip("/")
        self.mount = ""
        self.proc: subprocess.Popen | None = None
        self.error = ""
        self.lock = threading.RLock()
        # Suspended = a mount is still SELECTED but we are deliberately not
        # connected to it. Distinct from mount="" (nothing chosen) and from a
        # dead proc (a crash, which watch() repairs). See suspend().
        self.suspended = False

    def url(self, mount: str) -> str:
        return f"{self.base}/{mount}.mp3"

    def wake(self, mount: str, wait: float = 25.0) -> bool:
        """Start the station's encoder before connecting to it.

        The encoders are on-demand: nginx fires `auth_request /_wake` when a
        listener asks for /radio/<mount>.mp3, and only then does Liquidsoap
        start that output. Connecting straight to Icecast skips all of that,
        so the mount does not exist and Icecast answers 404 — which is what
        this service did on every attempt until 2026-09-25. Calling the wake
        service directly is the same door, without nginx's https redirect.

        KEEPS TRYING. Straight after a deploy this service and Liquidsoap start
        in the same second, and for the first few seconds Liquidsoap's control
        socket does not exist yet, so the wake answers 503. One attempt then
        gave up, ffmpeg was pointed at a mount Icecast did not have, and the
        speakers sat in a 404 restart loop until something happened to work —
        about fifteen seconds of silence on every deploy, and a page of alarming
        log (2026-09-29). netradio-resume already waits like this, for the same
        reason and after the same discovery.
        """
        if not self.wake_url:
            return True
        req = urllib.request.Request(self.wake_url + "/wake",
                                     headers={"X-Original-URI": f"/radio/{mount}.mp3"})
        deadline = time.monotonic() + max(0.0, wait)
        last = ""
        while True:
            try:
                urllib.request.urlopen(req, timeout=30).read()
                return True
            except Exception as e:
                last = str(e)
                if time.monotonic() >= deadline:
                    break
                time.sleep(2)
        log.warning("wake for %s never answered within %.0fs (%s) — trying the mount anyway",
                    mount, wait, last)
        return False

    def command(self, mount: str) -> list[str]:
        """The argv, separately so a test can read the device back out of it."""
        return [self.ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "warning",
                # Icecast drops a listener now and then; reconnect rather than
                # wait for watch() to notice five seconds later.
                "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                "-i", self.url(mount),
                *self.out.output_args()]

    def play(self, mount: str, wake_wait: float = 25.0) -> None:
        with self.lock:
            # Selecting a station while suspended records the choice without
            # connecting — otherwise --start-muted would dial the encoder up
            # at boot purely to feed a jack that is at -64 dB.
            if self.suspended:
                log.info("selected %s (suspended — not connecting)", mount)
                self.mount, self.error = mount, ""
                return
            self.stop()
            self.wake(mount, wake_wait)
            cmd = self.command(mount)
            # inherit the unit's environment (PATH comes from Environment= in
            # the service) — hardcoding PATH here broke the player anywhere
            # that path does not exist
            log.info("playing %s: %s", mount, shlex.join(cmd))
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                         env={**os.environ})
            self.mount, self.error = mount, ""
            if self.on_start:
                # A new stream starts at full on a shared sink, so this is not
                # cosmetic: without it the first second of a PulseOutput
                # reconnect is at 100% regardless of the chosen level.
                try:
                    self.on_start()
                except Exception:
                    log.exception("on_start after playing %s", mount)

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc, self.mount = None, ""

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def suspend(self) -> None:
        """Drop the Icecast connection but keep the station selected.

        WHY: the encoders are on-demand (see wake()) — Liquidsoap only runs a
        station's output while something is listening. This service was that
        listener 24/7, so a box sitting muted still cost a continuous MP3
        encode: measured 9.7% of one core on the server on 2026-10-28, with the
        only listener being this process feeding a jack ramped to -64 dB.

        Keeps self.mount, so /state still reports what is selected and resume()
        knows where to go back to.
        """
        with self.lock:
            if self.suspended:
                return
            selected = self.mount
            self.suspended = True
            self.stop()                 # clears self.mount — that is its job
            self.mount = selected       # ...but a suspend keeps the SELECTION
            log.info("suspended (muted) — released %s", selected or "(no mount)")

    def resume(self, wake_wait: float = 25.0) -> None:
        """Reconnect to the selected station after a suspend."""
        with self.lock:
            if not self.suspended:
                return
            self.suspended = False
            if self.mount:
                self.play(self.mount, wake_wait=wake_wait)

    def watch(self, stop: threading.Event, every: float = 5.0) -> None:
        """Restart a player that died while a mount is selected — a station
        that goes quiet for a moment (a deploy restarting Liquidsoap) must
        not end the night's rain."""
        while not stop.wait(every):
            with self.lock:
                # `suspended` is the discriminator between "died" and "we hung
                # up on purpose". Without it this thread would reconnect within
                # five seconds of every mute and the encoder would never stop.
                if self.mount and not self.suspended and not self.alive():
                    err = ""
                    if self.proc and self.proc.stderr:
                        try:
                            err = (self.proc.stderr.read() or b"").decode(errors="replace").strip()[-200:]
                        except Exception:
                            pass
                    self.error = err
                    log.warning("player for %s died (%s), restarting", self.mount, err or "no output")
                    mount = self.mount
                    self.proc = None
                    time.sleep(2)
                    self.play(mount)


def make_handler(player: Player, mixer: Mixer, disconnect_when_muted: bool = True,
                 token: str = ""):
    """`token`, when set, is required as `Authorization: Bearer <token>`.

    Empty means no check, which is right for a loopback endpoint behind the
    page's own vhost — that vhost is the access gate. It is NOT right for an
    endpoint on a second machine, where anything on the network can reach the
    port, so main() refuses to start in that combination rather than leaving
    the speakers open. See the --token-file help.
    """

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _authorised(self) -> bool:
            if not token:
                return True
            got = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not got.startswith(prefix):
                return False
            # Constant time: a timing oracle on a shared token is cheap to
            # avoid and awkward to notice.
            return hmac.compare_digest(got[len(prefix):].strip(), token)

        def _deny(self) -> None:
            self._reply(401, {"error": "unauthorised"})

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _reply(self, code: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n))
            except ValueError:
                return {}

        def _state(self) -> dict:
            vol, muted = mixer.state()
            # `volume` is the CHOSEN level, which is where an unmute returns
            # to. Reporting the live register instead made the slider snap to
            # 0 the moment you muted, and a fade in progress made it jitter.
            return {"playing": player.alive(), "mount": player.mount,
                    "volume": mixer.target if mixer.control else vol,
                    "actual": vol, "muted": muted,
                    "control": mixer.control, "error": player.error,
                    # What this endpoint is driving, so a client does not have
                    # to be told out of band. `mixes` is whether a second
                    # stream can play alongside the station.
                    "output": mixer.out.kind, "mixes": mixer.out.mixes}

        def do_GET(self):
            if not self._authorised():
                return self._deny()
            if urlparse(self.path).path in ("/state", "/"):
                return self._reply(200, self._state())
            self._reply(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorised():
                return self._deny()
            path = urlparse(self.path).path
            body = self._json()
            try:
                if path == "/play":
                    mount = str(body.get("mount") or "")
                    if not is_mount(mount):
                        return self._reply(400, {"error": "mount must be a station mount"})
                    player.play(mount)
                elif path == "/stop":
                    player.stop()
                elif path == "/volume":
                    if "level" in body:
                        mixer.choose(int(body["level"]))
                    elif "step" in body:
                        mixer.step(int(body["step"]))
                    else:
                        return self._reply(400, {"error": "level or step required"})
                elif path == "/mute":
                    on = bool(body.get("on"))
                    if on:
                        # Ramp to silence FIRST, then hang up: killing ffmpeg
                        # under a live level truncates the stream into the DAC.
                        mixer.mute(True)
                        if disconnect_when_muted:
                            player.suspend()
                    else:
                        # Reconnect FIRST, then fade up — the fade-in covers
                        # the wake + connect instead of the listener hearing
                        # silence and then a sudden start.
                        if disconnect_when_muted:
                            player.resume()
                        mixer.mute(False)
                else:
                    return self._reply(404, {"error": "not found"})
            except Exception as e:
                log.exception("%s failed", path)
                return self._reply(500, {"error": str(e)})
            time.sleep(0.3)
            self._reply(200, self._state())

    return Handler


def build_parser() -> argparse.ArgumentParser:
    """The flags, separately from main(), so a test can read a decision out of
    them without starting a server."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--icecast", default="http://127.0.0.1:8020", help="where the mounts are served")
    ap.add_argument("--listen", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8013)
    ap.add_argument("--card", default="0", help="ALSA card index or name for the mixer")
    ap.add_argument("--control", default="", help="mixer control (default: the first of Master/PCM/Speaker…)")
    ap.add_argument("--device", default=DEFAULT_DEVICE,
                    help="ALSA device to open; NOT `default`, which PipeWire claims")
    ap.add_argument("--output", choices=("alsa", "pulse"), default="alsa",
                    help="alsa: this machine's own card, level set on the card (the default). "
                         "pulse: a PipeWire/PulseAudio sink on a machine with a desktop "
                         "session, level set on OUR STREAM so a game sharing the sink is "
                         "left alone.")
    ap.add_argument("--sink", default="",
                    help="with --output pulse: which sink to play to (default: the session's)")
    ap.add_argument("--pactl", default=shutil.which("pactl") or "pactl",
                    help="with --output pulse: the pactl binary")
    ap.add_argument("--stream-name", default="netradio",
                    help="with --output pulse: the application name this service's stream "
                         "carries, which is how its own level is found again")
    ap.add_argument("--token-file", default="",
                    help="a file holding a shared token; every request must then carry "
                         "`Authorization: Bearer <token>`. Not needed when --listen is "
                         "loopback and the page's vhost is the gate — and REQUIRED when it "
                         "is not, because the port is then reachable from the network.")
    ap.add_argument("--allow-unauthenticated", action="store_true",
                    help="bind off loopback with NO token, because something else is the "
                         "gate — a firewall scoping the port to one interface, say, or an "
                         "overlay network. Has to be asked for explicitly: the default is "
                         "to refuse, since the failure is silent and the consequence is "
                         "someone else's speakers.")
    ap.add_argument("--wake", default="", help="the wake service, e.g. http://127.0.0.1:8011 (starts the encoder)")
    ap.add_argument("--default-mount", default="", help="play this at startup (the rain, usually)")
    ap.add_argument("--start-volume", type=int, help="set the mixer here at startup")
    ap.add_argument("--max-volume", type=int, default=100,
                    help="a ceiling no request can exceed — protects the speakers from a stray 100%%")
    ap.add_argument("--fade-in-ms", type=int, default=300, help="how long an unmute takes to come back")
    ap.add_argument("--fade-out-ms", type=int, default=120, help="how long a mute, or a volume change, takes")
    ap.add_argument("--start-muted", action="store_true",
                    help="come up silent — the jack is quiet until unmuted")
    ap.add_argument("--hold-stream-while-muted", action="store_true",
                    help="stay connected to Icecast while muted (the pre-2026-10-28 behaviour). "
                         "By default a mute releases the mount so the on-demand encoder can stop; "
                         "holding it costs a continuous MP3 encode — 9.7%% of a core, measured — "
                         "to feed a jack at -64 dB. Set this if unmute latency matters more.")
    ap.add_argument("--ffmpeg", default=shutil.which("ffmpeg") or "ffmpeg")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    token = ""
    if args.token_file:
        try:
            token = Path(args.token_file).read_text().strip()
        except OSError as e:
            log.error("cannot read --token-file %s: %s", args.token_file, e)
            return 2
        if not token:
            log.error("--token-file %s is empty — refusing to start with a token "
                      "that authorises nothing", args.token_file)
            return 2

    # Fail CLOSED. An endpoint off loopback is reachable by anything on the
    # network, and this service turns speakers on in someone's house; the
    # wrong default here is the one that is quiet about it.
    if not is_loopback(args.listen) and not token:
        if not args.allow_unauthenticated:
            log.error("--listen %s is not loopback and no --token-file was given. Anything "
                      "on the network could drive these speakers. Pass --token-file, bind "
                      "to 127.0.0.1 and reach it through the page's vhost, or say "
                      "--allow-unauthenticated if something else is the gate.", args.listen)
            return 2
        # Asked for explicitly, so it is allowed — but it is said out loud once
        # per start, because the whole risk of this configuration is that it
        # looks and behaves exactly like a working one.
        log.warning("listening on %s with NO authentication, because "
                    "--allow-unauthenticated was given. Whatever reaches this port can "
                    "start these speakers playing.", args.listen)

    if args.output == "pulse":
        out: Output = PulseOutput(sink=args.sink, pactl=args.pactl,
                                  stream_name=args.stream_name)
    else:
        out = AlsaOutput(args.card, control=args.control, device=args.device)

    mixer = Mixer(cap=args.max_volume, fade_in_ms=args.fade_in_ms,
                  fade_out_ms=args.fade_out_ms, output=out)
    log.info("output %s, level control: %s, ceiling %d%%, fade %d/%d ms in/out",
             out.kind, mixer.control or "none found",
             mixer.cap, mixer.fade_in_ms, mixer.fade_out_ms)
    if args.start_volume is not None:
        # `target` is what the watchdog compares against and what an unmute
        # returns to, so it has to mean the level we actually set. It was left
        # holding whatever the card happened to read at startup.
        mixer.target = max(0, min(mixer.cap, int(args.start_volume)))
        mixer.set(mixer.target, "startup")
    if args.start_muted:
        mixer.mute(True)          # after the level, so the level is ready when it is unmuted
        log.info("starting muted")
    player = Player(args.icecast, args.ffmpeg, args.device, args.wake,
                    output=out, on_start=mixer.reapply)
    disconnect_when_muted = not args.hold_stream_while_muted
    if args.start_muted and disconnect_when_muted:
        # Suspend BEFORE the default mount is chosen below, so a box that boots
        # muted records the station without ever waking its encoder.
        player.suspend()
    stop = threading.Event()
    threading.Thread(target=player.watch, args=(stop,), daemon=True).start()
    threading.Thread(target=mixer.watch, args=(stop,), daemon=True).start()

    if args.default_mount:
        # At boot this races Liquidsoap, which needs a good ten seconds
        # before its control socket exists. Nobody is waiting on a button
        # here, so wait properly rather than thrash on 404s.
        player.play(args.default_mount, wake_wait=120.0)

    srv = ThreadingHTTPServer((args.listen, args.port),
                              make_handler(player, mixer, disconnect_when_muted, token))
    # Serve on a THREAD and wait here. The obvious shape — serve_forever() in
    # the main thread with a SIGTERM handler that calls srv.shutdown() —
    # deadlocks: Python runs signal handlers on the main thread, so shutdown()
    # blocks waiting for a serve loop that cannot run until the handler
    # returns. systemd then waited the full 90 s TimeoutStop and SIGKILLed,
    # which is 90 s of silence on every single deploy (2026-09-26).
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    log.info("speaker on %s:%d (%s), playing %s", args.listen, args.port,
             "token required" if token else "no token — loopback only",
             args.default_mount or "nothing")
    try:
        # A POLLING wait, not a bare stop.wait(). An untimed wait depends on the
        # signal interrupting a C-level lock acquire, which held on the box and
        # did NOT in the build sandbox — the process outlived SIGTERM by more
        # than 15 s there. Waking twice a second makes the handler's effect
        # observable no matter how the platform treats the interrupt.
        while not stop.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    stop.set()
    player.stop()
    srv.shutdown()          # safe from here: the loop is on another thread
    srv.server_close()
    return 0
