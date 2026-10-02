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
- Every module needs an entry in `modules/catalog.nix` (`nix flake check`
  refuses otherwise): description, enable mechanism, option prefixes,
  `requires`, vhosts, secrets with their class (generate / supply).
- Secrets are paths: a `homelab.*File` option, the header says what the file
  must carry, the catalog says who can mint it. Never `config.sops.secrets.<name>`.
- Prove a change against a real consumer: build the reference machine with
  `--override-input homelab-modules path:<this checkout>` and compare the
  toplevel (or diff `/etc/systemd/system`) with the pinned build. Say in the
  PR which store paths, which units, which lines differed.
- `configurator/tests/vm-test.sh profiles/<name>.json` is the end-to-end
  install proof (generate → nixos-anywhere --vm-test). It takes 10+ minutes.
