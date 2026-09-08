# SilverBullet — markdown-native notes/tasks web app at notes.<domain>.
#
# Optionally a TWO-WRITER space: the web UI/PWA and a second Unix user (e.g.
# an agent) both read and write the same plain .md files with full mutual
# agency. Set homelab.silverbullet.secondWriter to enable that machinery —
# it is the hard-won part of this module:
#
#   * The service runs as the `silverbullet` user; the second writer joins
#     its group, and recursive default POSIX ACLs keep every file writable
#     by both parties regardless of who created it (plain group perms break
#     here because each side's umask would drop group-write on new files).
#   * SilverBullet sets each synced file's mtime to the client-supplied
#     timestamp (that timestamp is what the sync protocol compares). chtimes
#     needs OWNERSHIP, not write permission, so on second-writer-created
#     files it fails — retry storms of "Failed to set the mtime … operation
#     not permitted" on every phone sync, and conflicted-copy artifacts for
#     exactly the phone-capture pages. CAP_FOWNER lets the service set
#     mtimes on files it can already write but doesn't own.
#   * systemd reapplies the StateDirectory mode on EVERY service start, and
#     the group bits double as the ACL mask — the default 0755 kept
#     resetting the space-root mask to r-x, locking the second writer out of
#     creating at the top level. 0770 keeps the mask rwx across restarts.
#   * SilverBullet writes pages with mode 0640 regardless of UMask; with
#     POSIX ACLs the mask comes from the create mode's GROUP bits, so
#     web-UI pages arrive read-only to the second writer — and only the
#     owner (or root) may setfacl them. Hence the root-run repair timer.
#   * An hourly autosave git commit is the undo log for the collaboration.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.silverbullet;
  spaceDir = "/var/lib/silverbullet";
  writer = cfg.secondWriter;

  # Adds one `preventDefault` on the action buttons' pointerdown so tapping
  # an arrow doesn't blur the editor (which closes the phone keyboard). The
  # minified identifiers change every release, so match by regex, not by
  # literal string. Refuses to patch — and copies the original through
  # untouched — if the handler doesn't match exactly once.
  patchClientJs = pkgs.writeText "patch-client-js.py" ''
    import re
    import sys

    PAT = re.compile(
        r"onClick:([A-Za-z_$][\w$]*)=>\{"
        r"\1\.preventDefault\(\),\1\.stopPropagation\(\),"
        r"[A-Za-z_$][\w$]*\.callback\(\)\}")

    src_path, out_path = sys.argv[1], sys.argv[2]
    src = open(src_path).read()
    matches = list(PAT.finditer(src))
    if len(matches) == 1:
        m = matches[0]
        ev = m.group(1)
        patched = (src[:m.start()]
                   + "onPointerDown:%s=>%s.preventDefault()," % (ev, ev)
                   + m.group(0)
                   + src[m.end():])
        open(out_path, "w").write(patched)
        print("client.js patched: arrow taps no longer steal editor focus")
    else:
        open(out_path, "w").write(src)
        sys.stderr.write(
            "WARNING: SilverBullet's action-button handler matched %d times, "
            "expected 1. Serving the ORIGINAL client.js. The arrows still work; "
            "the phone keyboard will flicker on each tap.\n" % len(matches))
  '';
in
{
  imports = [ ../options.nix ];

  options.homelab.silverbullet = {
    package = lib.mkOption {
      type = lib.types.nullOr lib.types.package;
      default = null;
      description = "SilverBullet package override (e.g. from a newer nixpkgs); null = pkgs.silverbullet.";
    };
    indexPage = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "Home";
      description = "Space page to open on launch (a capture dashboard beats the space map for quick capture).";
    };
    secondWriter = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "agent";
      description = "Unix user granted full mutual write access to the space; null = single-writer.";
    };
  };

  config = {
    services.silverbullet = {
      enable = true;
      listenAddress = "127.0.0.1";
      listenPort = 3336;
      spaceDir = spaceDir;
    } // lib.optionalAttrs (cfg.package != null) {
      package = cfg.package;
    };

    systemd.services.silverbullet.environment = lib.mkIf (cfg.indexPage != null) {
      SB_INDEX_PAGE = cfg.indexPage;
    };

    # New files must be born group-writable or their ACL MASK caps the other
    # party at read-only (mode 644 -> mask r--; web-UI saves to
    # second-writer-created pages fail and vice versa).
    systemd.services.silverbullet.serviceConfig.UMask = "0002";
    systemd.services.silverbullet.serviceConfig.AmbientCapabilities = [ "CAP_FOWNER" ];
    systemd.services.silverbullet.serviceConfig.StateDirectoryMode = "0770";

    users.users = lib.mkIf (writer != null) {
      ${writer}.extraGroups = [ "silverbullet" ];
    };

    # Recursive + default ACLs: everything in the space, present and future,
    # stays rw for the service user (via group) and the second writer. Runs
    # on every activation, so files that somehow lost the ACL get healed.
    systemd.tmpfiles.rules = [
      # Ensure the dir exists at tmpfiles time (first deploy: StateDirectory
      # would otherwise only create it at first start, after the ACL pass had
      # already run against a missing path). Mode 0770: the group bits double
      # as the ACL mask — 0750 silently caps the second writer at r-x.
      "d ${spaceDir} 0770 silverbullet silverbullet - -"
    ] ++ lib.optional (writer != null)
      "A+ ${spaceDir} - - - - u:${writer}:rwX,g:silverbullet:rwX,m::rwX,d:u:${writer}:rwX,d:g:silverbullet:rwX,d:m::rwX";

    # ---- the two-writer permission repair (must run as ROOT) ----
    # Only a file's owner (or root) may change its ACL, and web-UI-created
    # files are owned by `silverbullet` — a setfacl heal from the second
    # writer gets "Operation not permitted". So repair from root, on a short
    # timer: chmod restores the group bits (which restores the mask);
    # setfacl re-asserts it for anything odd.
    systemd.services.silverbullet-perms = lib.mkIf (writer != null) {
      description = "Repair SilverBullet space permissions for the second writer";
      path = [ pkgs.acl pkgs.coreutils ];
      serviceConfig = {
        Type = "oneshot";
        User = "root";
      };
      script = ''
        set -eu
        [ -d ${spaceDir} ] || exit 0
        # group-writable so the ACL mask lands on rwX for both writers
        chmod -R g+rwX ${spaceDir}
        setfacl -R -m m::rwX,u:${writer}:rwX,g:silverbullet:rwX ${spaceDir} || true
      '';
    };
    systemd.timers.silverbullet-perms = lib.mkIf (writer != null) {
      description = "Repair SilverBullet space permissions every 2 min";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnBootSec = "2min";
        OnUnitActiveSec = "2min";
        AccuracySec = "30s";
      };
    };

    # The space git repo is operated by the second writer but the top dir is
    # owned by the silverbullet user (StateDirectory enforces that) — git's
    # safe.directory check calls that "dubious ownership" and refuses.
    # System-wide exception so units without a HOME gitconfig work too.
    programs.git = lib.mkIf (writer != null) {
      enable = true;
      config.safe.directory = spaceDir;
    };

    # Hourly autosave commit — the undo log for two-writer collaboration.
    # .silverbullet.db* is SilverBullet's derived index, not content.
    systemd.services.silverbullet-autosave = lib.mkIf (writer != null) {
      description = "Autosave git commit of the SilverBullet space";
      path = [ pkgs.git ];
      serviceConfig = {
        Type = "oneshot";
        User = writer;
        Group = "silverbullet";
        UMask = "0002";
        WorkingDirectory = spaceDir;
      };
      script = ''
        set -eu
        # Heal ACL masks on files this user OWNS. Files owned by the
        # silverbullet user are repaired by the root-run perms timer instead
        # — setfacl here would fail on them.
        ${pkgs.acl}/bin/setfacl -R -m m::rwX . 2>/dev/null || true
        if [ ! -d .git ]; then
          git init -q
          git config user.name "space-autosave"
          git config user.email "autosave@localhost"
          printf '%s\n' '.silverbullet.db*' > .gitignore
        fi
        git add -A
        git diff --cached --quiet || git commit -q -m "autosave $(date '+%Y-%m-%d %H:%M')"
      '';
    };
    systemd.timers.silverbullet-autosave = lib.mkIf (writer != null) {
      description = "Hourly SilverBullet space autosave";
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = "*:05";
        Persistent = true;
      };
    };

    # ---- keep the phone keyboard open when tapping the move-item arrows ----
    # SilverBullet's action buttons handle onClick but never preventDefault
    # the pointerdown, so tapping one moves focus off the editor and the
    # mobile keyboard slams shut. One `preventDefault` on pointerdown fixes
    # it outright (verified by patching the bundle in a headless browser:
    # zero focus events, item still moves). Serve a patched copy of
    # client.js from nginx, regenerated whenever the package changes.
    #
    # FAILS SAFE: if the needle isn't found exactly once (upstream changed
    # the code), it writes the ORIGINAL bundle unmodified and logs a warning
    # — never a half-patched, broken client. ⚠️ Check this unit's journal
    # after every version bump.
    systemd.services.silverbullet-client-patch = {
      description = "Serve a client.js patched to not steal editor focus";
      after = [ "silverbullet.service" ];
      requires = [ "silverbullet.service" ];
      wantedBy = [ "multi-user.target" ];
      restartTriggers = [ config.services.silverbullet.package ];
      path = [ pkgs.curl pkgs.python3 ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        StateDirectory = "silverbullet-client";
        StateDirectoryMode = "0755";
      };
      script = ''
        set -eu
        src=$(mktemp)
        out=/var/lib/silverbullet-client/client.js

        # wait for silverbullet to answer (it has only just started)
        for _ in $(seq 1 30); do
          curl -fsS -o "$src" http://127.0.0.1:3336/.client/client.js && break
          sleep 1
        done

        python3 ${patchClientJs} "$src" "$out"
        chmod 0644 "$out"
        rm -f "$src"
      '';
    };

    services.nginx.virtualHosts."notes.${config.homelab.domain}" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;
      # exact match wins over the "/" proxy: serve the patched bundle instead
      locations."= /.client/client.js" = {
        alias = "/var/lib/silverbullet-client/client.js";
        extraConfig = ''
          default_type application/javascript;
          add_header Cache-Control "no-cache";
        '';
      };
      locations."/" = {
        proxyPass = "http://127.0.0.1:3336";
        recommendedProxySettings = true;
        proxyWebsockets = true;
        extraConfig = ''
          client_max_body_size 20M;      # attachment uploads
        '';
      };
    };
  };
}
