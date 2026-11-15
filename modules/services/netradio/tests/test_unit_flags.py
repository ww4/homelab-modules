"""Every flag the module puts on a command line must exist on that command.

This is the check that was missing on 2026-09-27. `--jellyfin-url` and
`--jellyfin-key-file` were added to netradio-playlists' ExecStart and were READ
in playlists.py, but the `add_argument` lines never landed — an edit aborted
before writing and only part of it was re-applied. Nothing caught it:

  * the unit tests never invoke playlists.main() with the real flag set
  * `nix build` runs those tests, so the BUILD WAS GREEN
  * argparse rejects an unknown flag at RUNTIME, so the failure waited for the
    deploy, where switch-to-configuration failed with status 4 and took the
    whole generation down

A flag is only exercised when the unit runs, which is the worst possible place
to find out. This test reads the ExecStart lines out of default.nix and checks
each `--flag` against the argparse declarations of the subcommand it is passed
to, so the mismatch fails in the suite instead.
"""

import os
import re
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
# default.nix is not part of the Python package's source (editing it must not
# rehash the package), so inside that build there is nothing to read and this
# skips. The `netradio-web` derivation has it and sets NETRADIO_NIX — and that
# derivation is nginx's document root, so it is built on every deploy.
NIX = Path(os.environ.get("NETRADIO_NIX") or (HERE / "default.nix"))
# …and likewise the Python sources: in that derivation the tests directory is
# copied to the store on its own, so netradio/ is not its sibling.
PKG = Path(os.environ.get("NETRADIO_PKG") or (HERE / "netradio"))


def setUpModule():
    if not NIX.exists() or not (PKG / "playlists.py").exists():
        raise unittest.SkipTest(f"default.nix or the sources are not here ({NIX}, {PKG}) — "
                                "this runs in the netradio-web build")

# `netradio <cmd>` -> the module whose parser has to know the flags
MODULE_FOR = {
    "playlists": "playlists", "wake": "wake", "dj": "dj", "profile": "profile",
    "profile-server": "profile_server", "admin": "admin", "liq": "liq",
    "migrate": "migrate", "pandora": "pandora", "ambient": "ambient",
    "speaker": "speaker", "resume": "resume", "jellyfin": "jellyfin",
    "compile-prompt": "compile", "compile-apply": "compile",
}


def declared(module: str) -> set[str]:
    """Long options the module's argparse declares."""
    src = (PKG / f"{module}.py").read_text()
    return set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', src))


def commands_in_nix() -> dict[str, set[str]]:
    """{subcommand: flags passed to it} from every ExecStart in the module.

    The ExecStarts are Nix lists of strings, so the flags are found by taking
    everything from `bin/netradio <cmd>` up to the end of that list.
    """
    text = NIX.read_text()
    out: dict[str, set[str]] = {}
    for m in re.finditer(r'bin/netradio ([a-z-]+)"', text):
        cmd = m.group(1)
        if cmd not in MODULE_FOR:
            continue
        # Everything up to the NEXT command or unit boundary. Stopping at a
        # bare "]" was wrong: an ExecStart that ends in `++ lib.optional …` does
        # not have one, so the scan ran on into the next unit and blamed
        # netradio speaker for pandora's flags. A detector that over-reaches is
        # as useless as one that under-reaches.
        tail = text[m.end():]
        bounds = [r for r in (re.search(r'bin/netradio [a-z-]+"', tail),
                              re.search(r'^\s*systemd\.(services|timers|paths)\.', tail, re.M),
                              re.search(r'^\s*Environment\s*=', tail, re.M))
                  if r]
        end = min((r.start() for r in bounds), default=len(tail))
        flags = set(re.findall(r'"(--[a-z0-9-]+)', tail[:end]))
        out.setdefault(cmd, set()).update(flags)
    return out


class UnitFlags(unittest.TestCase):
    def test_the_module_passes_no_flag_the_command_does_not_declare(self):
        found = commands_in_nix()
        self.assertTrue(found, "no netradio commands found in default.nix — the scan is broken")
        problems = []
        for cmd, flags in sorted(found.items()):
            known = declared(MODULE_FOR[cmd])
            for f in sorted(flags - known):
                problems.append(f"netradio {cmd} is passed {f}, which {MODULE_FOR[cmd]}.py does not declare")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_the_scan_finds_the_commands_it_should(self):
        """A positive control: this test is worthless if the regex silently
        matches nothing, which is exactly how the original bug survived."""
        found = commands_in_nix()
        for cmd in ("playlists", "dj", "speaker", "admin", "wake"):
            self.assertIn(cmd, found, f"{cmd} should appear in default.nix")
        self.assertIn("--config", found["playlists"], "the scan should see real flags")
        self.assertGreater(len(found["playlists"]), 5, found.get("playlists"))

    def test_and_it_would_have_caught_the_real_one(self):
        """Hold the parser to the state it was in and confirm the check fires."""
        flags = commands_in_nix().get("playlists", set())
        self.assertIn("--jellyfin-url", flags, "the module does pass this to the scanner")
        pretend_undeclared = declared("playlists") - {"--jellyfin-url", "--jellyfin-key-file"}
        self.assertTrue(flags - pretend_undeclared,
                        "with those two undeclared the check must report something")
