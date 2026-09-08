# Recyclarr — sync TRaSH-Guides quality profiles + custom formats into
# Sonarr & Radarr on a daily schedule.
#
# Templates pulled from https://github.com/TRaSH-Guides/Guides. The YAML
# config itself (which profiles, which custom formats, your upgrade policy)
# is opinionated per-site and comes from the consumer via
# homelab.recyclarr.configFile.
#
# Secrets — NOT in git. Both *arrs generate their own API keys; grab them
# from each UI (Settings → General → API Key) and drop into
# /var/lib/recyclarr/secrets.yml:
#   sonarr_api_key: <your-key>
#   radarr_api_key: <your-key>
# (the file is created with empty keys on first activation; the service
# no-ops until populated, so no failure-notification spam during the wait.)
{ config, lib, pkgs, ... }:

let
  appData = "/var/lib/recyclarr";
  cfg = config.homelab.recyclarr;

  # Wrapper: copies config from /nix/store into the app-data dir, seeds an
  # empty secrets file on first run, and no-ops cleanly until the user fills
  # in their API keys.
  syncWrapper = pkgs.writeShellScript "recyclarr-sync-wrapper" ''
    set -eu
    install -d -m 0700 -o root -g root ${appData}
    install -m 0644 -o root -g root ${cfg.configFile} ${appData}/recyclarr.yml

    if [ ! -f ${appData}/secrets.yml ]; then
      cat > ${appData}/secrets.yml <<'SECRETS_EOF'
    # Recyclarr secrets — fill these in after generating API keys:
    #   Sonarr UI → Settings → General → API Key
    #   Radarr UI → Settings → General → API Key
    # Once both are populated, the daily timer will run sync.
    sonarr_api_key:
    radarr_api_key:
    SECRETS_EOF
      chmod 0600 ${appData}/secrets.yml
    fi

    # Skip silently if keys aren't filled in yet — avoids notification noise
    # during the period between deploying this module and pasting the keys.
    # The guard is strict on purpose — the key must sit at column 0 as
    # `key: value`, because that is the only form recyclarr's YAML parser
    # accepts. But "empty" is a misleading thing to report when a key IS
    # present and merely mis-indented or missing the space after the colon —
    # the same message sends you hunting for a value that's already there.
    # Distinguish the two cases.
    missing=0
    for key in sonarr radarr; do
      if grep -qE "^''${key}_api_key: \S" ${appData}/secrets.yml; then
        continue
      fi
      missing=1
      if grep -qE "^[[:space:]]*''${key}_api_key:[[:space:]]*\S" ${appData}/secrets.yml; then
        echo "  ''${key}_api_key IS set but malformed — recyclarr will not read it."
        echo "  It must start at column 0 with exactly one space after the colon:"
        echo "    ''${key}_api_key: <value>"
        echo "  (check for leading whitespace, a tab, or no space after the colon)"
      else
        echo "  ''${key}_api_key is empty — fill it from the *arr UI (Settings > General > API Key)."
      fi
    done
    if [ "$missing" -ne 0 ]; then
      echo "  Skipping sync."
      exit 0
    fi

    # NOTE: `--app-data` was REMOVED in recyclarr 8.x and is not accepted in
    # any position ("Error: Unknown option 'app-data'"). Its successor env
    # var RECYCLARR_APP_DATA is *also* rejected, with a message pointing at
    # RECYCLARR_CONFIG_DIR — which is the one that works. (Found the hard
    # way: an empty-key guard that exits first can hide a broken invocation
    # for months, because the binary is never actually reached.)
    export RECYCLARR_CONFIG_DIR=${appData}
    exec ${pkgs.recyclarr}/bin/recyclarr sync
  '';
in
{
  imports = [ ../options.nix ];

  options.homelab.recyclarr = {
    configFile = lib.mkOption {
      type = lib.types.path;
      description = "Your recyclarr.yml (profiles, custom formats, upgrade policy).";
    };
    schedule = lib.mkOption {
      type = lib.types.str;
      default = "*-*-* 05:30:00";
      description = "OnCalendar schedule — pick a quiet-disk window (after any parity sync/scrub).";
    };
  };

  config = {
    environment.systemPackages = [ pkgs.recyclarr ];

    systemd.services.recyclarr-sync = {
      description = "Recyclarr — sync TRaSH-Guides profiles into Sonarr & Radarr";
      serviceConfig = {
        Type = "oneshot";
        ExecStart = "${syncWrapper}";
        User = "root";  # needs to read secrets.yml at 0600
      };
    };

    systemd.timers.recyclarr-sync = {
      description = "Daily Recyclarr sync";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.schedule;
        Persistent = true;
      };
    };
  };
}
