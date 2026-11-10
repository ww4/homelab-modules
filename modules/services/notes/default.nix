# notes — a Google Keep-style card wall over a folder of markdown files.
#
# It reads `.md` files with YAML frontmatter and serves them as cards: tap to
# edit, checkboxes toggle in place, search is ranked and server-side, and it
# works offline as a PWA. Everything it knows — category, author, archived,
# pinned, order — is ordinary frontmatter, so the files stay as portable as
# they were and any other markdown tool still reads them.
#
# It pairs with ./silverbullet.nix over the SAME directory: that one is the
# editor for long document-shaped pages, this one is the capture surface.
# ⚠️ Both default their vhost to `notes`; a deployment running both must
# repoint one (set homelab.silverbullet.subdomain, or homelab.notes.subdomain).
#
# SCOPE IS A SECURITY BOUNDARY, not a view filter. The app is unauthenticated
# and a note id is a client-supplied path, so it serves only `.md` files under
# the directories it owns (Keep/, Notes/, Inbox/, plus Grocery List.md) and
# refuses anything else with a 404 rather than merely hiding it. A traversal
# corpus runs at build time, so a scope regression fails the build instead of
# shipping. Deleting moves a file to Trash/ rather than unlinking it.
#
# ⚠️ AUTHENTICATION. There is none, deliberately: the data model has no
# per-note permissions, so access is all-or-nothing and a reverse proxy does
# all-or-nothing better and earlier. The app listens on loopback and REFUSES
# to bind a public address unprotected. Put whatever gating you already run in
# front of it; if that proxy authenticates users, set trustProxyAuth and the
# name it sends becomes the account notes are recorded under. See
# ./app/README.md.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.notes;
  notes = pkgs.callPackage ./package.nix { };
in
{
  imports = [ ../../options.nix ];

  options.homelab.notes = {
    spaceDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/silverbullet";
      example = "/var/lib/notes";
      description = ''
        The folder of markdown files to serve. Shares the SilverBullet
        default so the two modules sit over one directory out of the box;
        point it anywhere if you are not running that.
      '';
    };

    spaceGroup = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = "silverbullet";
      example = null;
      description = ''
        A group the service joins so it can write the folder, for the case
        where something else owns it. The silverbullet module lays recursive
        default ACLs for its own group, which is why that is the default;
        null means the service relies on ownership alone.

        ⚠️ Files must be born group-writable or the ACL mask caps every other
        writer at read-only, so the unit runs with UMask 0002 to match.
      '';
    };

    defaultAuthor = lib.mkOption {
      type = lib.types.str;
      default = "owner";
      example = "sam";
      description = ''
        Who an unlabelled note belongs to.

        ⚠️ Not cosmetic. Every note written before this app recorded
        authorship has no `author` key, so this value is who all of them
        belong to; changing it on a populated folder re-attributes them.
        Set it once, to whoever wrote what is already there.
      '';
    };

    subdomain = lib.mkOption {
      type = lib.types.str;
      default = "notes";
      example = "cards";
      description = "Host label for the vhost, as <subdomain>.<homelab.domain>.";
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 3337;
      description = "Loopback port the service listens on, behind nginx.";
    };

    trustProxyAuth = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Believe a username header from the proxy in front, and use it to find
        the account that owns the sign-in.

        ⚠️ Only safe when nothing can reach the service except that proxy —
        anything that can talk to the port directly can send the header.
        Loopback plus a local reverse proxy satisfies that; a shared network
        does not.
      '';
    };

    authUserHeader = lib.mkOption {
      type = lib.types.str;
      default = "Remote-User";
      example = "X-authentik-username";
      description = ''
        Which header carries the authenticated user. Authelia sends
        Remote-User, Authentik X-authentik-username, oauth2-proxy
        X-Forwarded-User, Cloudflare Access
        Cf-Access-Authenticated-User-Email, `tailscale serve`
        Tailscale-User-Login. Ignored unless trustProxyAuth is set.
      '';
    };

    authNameHeader = lib.mkOption {
      type = lib.types.str;
      default = "Remote-Name";
      example = "X-authentik-name";
      description = ''
        Optional header carrying a human-readable name, used as the first
        guess at a new account's display name. Ignored unless trustProxyAuth
        is set.
      '';
    };
  };

  config = {
    # Both modules default their vhost label to "notes" because either one can
    # reasonably be a deployment's primary notes front end. Importing both
    # without repointing one produces a conflicting nginx virtualHost whose
    # error names neither option, so say it plainly instead.
    assertions = [
      {
        assertion = !(config.homelab ? silverbullet)
          || config.homelab.silverbullet.subdomain != cfg.subdomain;
        message = ''
          homelab.notes.subdomain and homelab.silverbullet.subdomain are both
          "${cfg.subdomain}". They would claim the same vhost. Set one of them
          — e.g. homelab.silverbullet.subdomain = "silverbullet".
        '';
      }
    ];

    users.users.notes = {
      isSystemUser = true;
      group = "notes";
      extraGroups = lib.optional (cfg.spaceGroup != null) cfg.spaceGroup;
      description = "notes card wall";
    };
    users.groups.notes = { };

    systemd.services.notes = {
      description = "notes — card wall over ${cfg.spaceDir}";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];

      environment = {
        NOTES_SPACE_DIR = cfg.spaceDir;
        NOTES_DEFAULT_AUTHOR = cfg.defaultAuthor;
        NOTES_HOST = "127.0.0.1";
        NOTES_PORT = toString cfg.port;
      } // lib.optionalAttrs cfg.trustProxyAuth {
        NOTES_TRUST_PROXY_AUTH = "1";
        NOTES_AUTH_USER_HEADER = cfg.authUserHeader;
        NOTES_AUTH_NAME_HEADER = cfg.authNameHeader;
      };

      serviceConfig = {
        ExecStart = lib.getExe notes;
        User = "notes";
        Group = "notes";
        SupplementaryGroups = lib.optional (cfg.spaceGroup != null) cfg.spaceGroup;
        Restart = "on-failure";
        RestartSec = "5s";

        # New files must be born group-writable or the ACL mask caps the other
        # writers at read-only.
        UMask = "0002";

        # Hardening. The folder is the ONLY path this service may write; it
        # reads its own store path and nothing else of consequence.
        ReadWritePaths = [ cfg.spaceDir ];
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictNamespaces = true;
        RestrictRealtime = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        SystemCallFilter = [ "@system-service" "~@privileged" "~@resources" ];
        CapabilityBoundingSet = [ ];
      };
    };

    services.nginx.virtualHosts."${cfg.subdomain}.${config.homelab.domain}" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;
      locations."/" = {
        proxyPass = "http://127.0.0.1:${toString cfg.port}";
        recommendedProxySettings = true;
      };
    };
  };
}
