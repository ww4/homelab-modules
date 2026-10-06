# For agents working in this repository

This is the public NixOS module library behind Imperfect Homelab
(https://ww4.github.io/imperfect-homelab). Two different jobs land here:

**Installing or reconfiguring a homelab for a user** — do not work in this
repo; drive the configurator headlessly. The procedure is the skill at
https://ww4.github.io/imperfect-homelab/skills/homelab-install/SKILL.md and
the contract is `configurator/README.md`. `nix run '.?dir=configurator' -- schema --json`
is the question set.

**Changing the library** — the rules that the checks enforce:

- A module reads site facts only through `homelab.*` options (`modules/options.nix`)
  and never names a host, a path from a particular machine, a person or a
  secret. `tools/leak-scan.sh` must pass before every push.
- Every module needs an entry in `modules/catalog.nix`: description, enable
  mechanism, option prefixes, `requires`, vhosts, secrets with their class
  (generate / supply), and an integer `memory`.
- **`nix flake check ./configurator` is the command that enforces all of
  this**, not `nix flake check` at the root. The checks live in the sub-flake
  because the library itself deliberately has no inputs, and a check needs
  nixpkgs. It asserts: catalog integrity, the configurator builds and its
  tests pass, the option documentation evaluates, every module evaluates and
  builds with values a real machine would have (`checks/every-module.nix`),
  and the leak scan passes. A workflow that runs the same command on the
  public mirror is written and waiting at `ci/github-checks.yml`; it is not
  installed, because the mirror's token has no `workflow` scope and GitHub
  rejects the whole push when a file under `.github/workflows/` changes. That
  rejection stops the mirror, which is where the installer fetches its own
  updates, so do not move the file there until the token is replaced.
- ⚠️ **Give the check fixture real values, not defaults.** Several modules
  interpolate an option into a shell program, and `writeShellApplication`
  runs shellcheck, so a module can be correct with the default and broken the
  moment the option is set. Three such bugs were in the tree at once while
  the old root-level `nix flake check` passed: a repo URL that made an
  always-false test, a drift watcher that only built with two or more pairs
  configured, and nine modules that failed to evaluate on a machine with no
  time zone.
- Secrets are paths: a `homelab.*File` option, the header says what the file
  must carry, the catalog says who can mint it. Never `config.sops.secrets.<name>`.
- Prove a change against a real consumer: build the reference machine with
  `--override-input homelab-modules path:<this checkout>` and compare the
  toplevel (or diff `/etc/systemd/system`) with the pinned build. Say in the
  PR which store paths, which units, which lines differed.
- `configurator/tests/vm-test.sh profiles/<name>.json` is the end-to-end
  install proof (generate → nixos-anywhere --vm-test). It takes 10+ minutes.
