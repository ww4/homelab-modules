# arr-api-seed — give a *arr app its API key BEFORE first start, so nothing
# downstream has to wait for the app to mint one and nobody copies it by hand.
#
# Every *arr (Sonarr, Radarr, Lidarr, Prowlarr, Readarr, …) reads
# <ApiKey> from <config dir>/config.xml at startup and keeps whatever it
# finds; it only mints a key when the file has none. So a oneshot that runs
# before the container and writes the key from an env file makes the key a
# value the configuration owns — the same value every consumer (unpackerr,
# recyclarr, decluttarr, a request UI, an agent) reads from the same file.
#
# Idempotent and safe on an existing install: a config.xml that already
# carries the key is left untouched; one carrying a different key is
# rewritten (the configuration wins — that is the point); a missing file is
# created minimal and the app fills in the rest on first start. A missing
# variable means "this app keeps minting its own", not a failure.
#
# Usage (from a module):
#   seed = import ../lib/arr-api-seed.nix { inherit lib pkgs; };
#   systemd.services.arr-api-seed-sonarr = seed {
#     app = "sonarr"; var = "SONARR_API_KEY"; dir = "/var/lib/sonarr";
#     owner = s.owner; group = s.group; envFile = s.apiKeyEnvFile;
#   };
{ lib, pkgs }:

{ app, var, dir, owner, group, envFile }:

lib.mkIf (envFile != null) {
  description = "Seed ${app}'s API key from ${var} before it starts";
  # Ordered before the container from THIS unit's side (Before=), and pulled
  # in by multi-user.target rather than by the container: adding a
  # Requires= to docker-${app}.service would change that unit's file and
  # restart every running *arr on the deploy that introduces the seed.
  wantedBy = [ "multi-user.target" ];
  before = [ "docker-${app}.service" ];
  after = [ "systemd-tmpfiles-setup.service" ];
  serviceConfig = {
    Type = "oneshot";
    RemainAfterExit = true;
  };
  path = with pkgs; [ coreutils gnused gnugrep ];
  script = ''
    set -eu
    # The option was set on purpose: a file that is not there is a failed
    # secret, not "no key". Read the one line, don't source the file (`.`
    # would execute it).
    [ -r ${lib.escapeShellArg envFile} ] || { echo "arr-api-seed: ${envFile} is missing or unreadable" >&2; exit 1; }
    key=$(grep -E '^${var}=' ${lib.escapeShellArg envFile} | head -n1 | cut -d= -f2- | tr -d "'\"" || true)
    if [ -z "$key" ]; then
      echo "arr-api-seed: ${var} not in the env file; ${app} keeps its own key"
      exit 0
    fi
    case "$key" in
      *[!A-Za-z0-9]*) echo "arr-api-seed: ${var} has characters outside [A-Za-z0-9]; refusing to write it into XML" >&2; exit 1 ;;
    esac
    f=${dir}/config.xml
    if [ ! -f "$f" ]; then
      install -d -m 0750 -o ${owner} -g ${group} ${dir}
      printf '<Config>\n  <ApiKey>%s</ApiKey>\n</Config>\n' "$key" > "$f"
      chown ${owner}:${group} "$f"; chmod 0640 "$f"
      echo "arr-api-seed: created $f with the configured key"
    elif grep -q '<ApiKey>' "$f"; then
      current=$(sed -n 's|.*<ApiKey>\([^<]*\)</ApiKey>.*|\1|p' "$f" | head -n1)
      if [ "$current" = "$key" ]; then
        echo "arr-api-seed: ${app} already carries the configured key"
      else
        sed -i "s|<ApiKey>[^<]*</ApiKey>|<ApiKey>$key</ApiKey>|" "$f"
        echo "arr-api-seed: replaced ${app}'s key with the configured one"
      fi
    else
      sed -i "s|<Config>|<Config>\n  <ApiKey>$key</ApiKey>|" "$f"
      echo "arr-api-seed: inserted the configured key into $f"
    fi
  '';
}
