# market-agent — a Facebook Marketplace watcher driven by written briefs.
#
# A brief is a TOML file saying what you want (price, distance, hard limits
# like "clean title" or "AWD", preferences). On a timer the service searches
# Marketplace for every brief, reads each listing, checks the hard limits,
# looks up recalls and VINs with NHTSA, has Claude score the survivors against
# the brief (photos included), and publishes one static page at
# <subdomain>.<domain>: cards with a photo, score and price, grouped by brief,
# with sorting, a model filter, "new" dogears, shared stars, and "+ New" /
# Edit / Pause for watches (a small loopback endpoint, market-agent-web, behind
# /api/ on the same vhost; a saved watch is searched at once via a .path unit).
# See ./app/README.md for the brief format and how scoring works.
#
# Briefs live in <stateDir>/briefs/*.toml and are NOT part of this module:
# they are the user's data. Start one with
#   sudo -u <user> env MARKET_AGENT_HOME=<stateDir> market-agent brief new <name> --example winter-commuter
#
# ⚠️ THE JUDGE. judge = "cli" (the default) runs the `claude` CLI as
# homelab.marketAgent.user, so that user must already be logged in to Claude
# Code, and its home must be writable (the CLI keeps state there). That is why
# `user` is an option: point it at the account that has the login. "api" uses
# an Anthropic API key from environmentFile instead. "none" skips scoring and
# the page shows checks only.
#
# ⚠️ FACEBOOK. Searches are logged-out requests to Facebook's own endpoints,
# about one every 1.5 s. That is against Facebook's terms; at personal volume
# the realistic consequence is being throttled. A residential IP works; cloud
# IPs get throttled fast (environmentFile can carry MARKET_AGENT_PROXY).
# When every search for a brief fails, notifyCommand is told: that is a broken
# scraper, not a quiet market.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.marketAgent;
  market-agent = pkgs.callPackage ./package.nix { };
  ownUser = cfg.user == "market-agent";
  group = if ownUser then "market-agent" else config.users.users.${cfg.user}.group;
  home = if ownUser then cfg.stateDir else config.users.users.${cfg.user}.home;
  pageUrl = "https://${cfg.subdomain}.${config.homelab.domain}/";
in
{
  imports = [ ../../options.nix ];

  options.homelab.marketAgent = {
    subdomain = lib.mkOption {
      type = lib.types.str;
      default = "market";
      description = "Host label for the page, as <subdomain>.<homelab.domain>.";
    };

    stateDir = lib.mkOption {
      type = lib.types.str;
      default = "/var/lib/market-agent";
      description = ''
        Briefs (briefs/*.toml), the listing database, downloaded photos, and
        the published page (www/index.html, served by nginx).
      '';
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "market-agent";
      example = "alice";
      description = ''
        Account the runs execute as. The default creates a system user, which
        suits judge = "api" or "none". For judge = "cli", name the account that
        is logged in to Claude Code; it must already exist.
      '';
    };

    schedule = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ "*-*-* 07:00:00" "*-*-* 12:00:00" "*-*-* 17:00:00" ];
      description = ''
        systemd OnCalendar times for a run. Repeat runs are cheap: listings
        are re-read only when their price changes and re-scored only when
        something about them changed.
      '';
    };

    judge = lib.mkOption {
      type = lib.types.enum [ "cli" "api" "none" ];
      default = "cli";
      description = "How candidates are scored: the claude CLI, the Anthropic API, or not at all.";
    };

    claudeBin = lib.mkOption {
      type = lib.types.str;
      default = "claude";
      example = "/etc/profiles/per-user/alice/bin/claude";
      description = "The claude CLI, for judge = \"cli\". A bare name is looked up on the unit's PATH.";
    };

    model = lib.mkOption {
      type = lib.types.str;
      default = "claude-opus-5-5";
      description = "Model the judge uses.";
    };

    environmentFile = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/secrets/market-agent.env";
      description = ''
        Optional EnvironmentFile for secrets: ANTHROPIC_API_KEY (judge = "api")
        and/or MARKET_AGENT_PROXY (a residential proxy URL for Facebook).
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 3341;
      description = ''
        Loopback port for the small star endpoint (/api/favorites) that sits
        behind the same vhost as the page. Stars are shared by everyone who
        can open the page, survive every rebuild, and keep a listing tracked
        until it sells.
      '';
    };

    proxy = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "http://127.0.0.1:8888";
      description = ''
        HTTP proxy for every Facebook request (MARKET_AGENT_PROXY), e.g. a
        VPN container's built-in proxy, so a Facebook rate limit or block
        lands on the VPN's address instead of the host's. A proxy URL with a
        password belongs in environmentFile instead.
      '';
    };

    probe = {
      enable = lib.mkEnableOption ''
        the rate-limit probe: a few nightly sessions that read listings at a
        fixed pace until Facebook refuses, then measure how long the block
        lasts (market_agent/probe.py). It runs only through `proxy`, only
        after 3 quiet hours, never alongside a watch run, and stops after
        its schedule of three paces'';
      schedule = lib.mkOption {
        type = lib.types.str;
        default = "*-*-* 01:00:00";
        description = "When to try the next probe session (systemd OnCalendar). A session can take ~4 h.";
      };
    };

    notifyCommand = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "/run/current-system/sw/bin/notify";
      description = ''
        Run as `<command> <title> <body>` when a listing newly reaches "worth
        contacting" (once per listing) and when every search for a brief
        fails. null sends nothing; the page still updates.
      '';
    };
  };

  config = {
    users.users = lib.mkIf ownUser {
      market-agent = { isSystemUser = true; group = "market-agent"; home = cfg.stateDir; description = "market-agent"; };
    };
    users.groups = lib.mkIf ownUser { market-agent = { }; };

    # nginx reads www/ as "other"; everything else stays the owner's.
    systemd.tmpfiles.rules = [
      "d ${cfg.stateDir} 0755 ${cfg.user} ${group} -"
      "d ${cfg.stateDir}/briefs 0750 ${cfg.user} ${group} -"
      "d ${cfg.stateDir}/www 0755 ${cfg.user} ${group} -"
      "d ${cfg.stateDir}/requests 0750 ${cfg.user} ${group} -"
    ];

    # The page's "+ New" / Edit / Resume leave a file in requests/; this starts
    # the normal service for it (which searches only the requested watches and
    # empties the directory). The star/watch endpoint itself never runs a search.
    systemd.paths.market-agent = {
      wantedBy = [ "paths.target" ];
      pathConfig = {
        DirectoryNotEmpty = "${cfg.stateDir}/requests";
        Unit = "market-agent.service";
      };
    };

    environment.systemPackages = [ market-agent ];

    systemd.services.market-agent = {
      description = "market-agent: search, score and publish every brief";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [ pkgs.coreutils ];
      environment = {
        HOME = home;
        MARKET_AGENT_HOME = cfg.stateDir;
        MARKET_AGENT_JUDGE = cfg.judge;
        MARKET_AGENT_MODEL = cfg.model;
        MARKET_AGENT_CLAUDE_BIN = cfg.claudeBin;
      } // lib.optionalAttrs (cfg.proxy != null) { MARKET_AGENT_PROXY = cfg.proxy; };
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = group;
        ExecStart = lib.escapeShellArgs ([
          (lib.getExe market-agent) "watch"
          "--page" "${cfg.stateDir}/www/index.html"
          "--page-url" pageUrl
        ] ++ lib.optionals (cfg.notifyCommand != null) [ "--notify-cmd" cfg.notifyCommand ]);
        EnvironmentFile = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
        # A first run reads every listing (minutes) and scores dozens of them.
        TimeoutStartSec = "3h";
        Nice = 10;

        ProtectSystem = "strict";
        # The claude CLI keeps its login and state under the user's home.
        ReadWritePaths = [ cfg.stateDir ] ++ lib.optional (cfg.judge == "cli" && !ownUser) home;
        ProtectHome = cfg.judge != "cli" || ownUser;
        PrivateTmp = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictNamespaces = true;
        LockPersonality = true;
        # No MemoryDenyWriteExecute: the claude CLI is Node, and V8's JIT needs W+X pages.
      };
    };

    systemd.services.market-agent-probe = lib.mkIf cfg.probe.enable {
      description = "market-agent: measure Facebook's rate limit (one session)";
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      environment = {
        HOME = home;
        MARKET_AGENT_HOME = cfg.stateDir;
        MARKET_AGENT_JUDGE = "none";
      } // lib.optionalAttrs (cfg.proxy != null) { MARKET_AGENT_PROXY = cfg.proxy; };
      serviceConfig = {
        Type = "oneshot";
        User = cfg.user;
        Group = group;
        # Without a proxy it refuses and exits 0: it never probes from the host's IP.
        ExecStart = lib.escapeShellArgs [ (lib.getExe market-agent) "probe-limit" ];
        EnvironmentFile = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
        TimeoutStartSec = "5h";
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictNamespaces = true;
        LockPersonality = true;
      };
    };
    systemd.timers.market-agent-probe = lib.mkIf cfg.probe.enable {
      wantedBy = [ "timers.target" ];
      timerConfig.OnCalendar = cfg.probe.schedule;
    };

    # Stars. Unauthenticated by design, like the page: it refuses to bind
    # anything but loopback, and the vhost's own gating applies to /api/.
    systemd.services.market-agent-web = {
      description = "market-agent: star endpoint for the watch page";
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" ];
      environment = {
        HOME = home;
        MARKET_AGENT_HOME = cfg.stateDir;
        MARKET_AGENT_JUDGE = "none"; # never judges; it only records stars and rebuilds the page
      };
      serviceConfig = {
        ExecStart = lib.escapeShellArgs [
          (lib.getExe market-agent) "serve"
          "--port" (toString cfg.port)
          "--page" "${cfg.stateDir}/www/index.html"
        ];
        User = cfg.user;
        Group = group;
        Restart = "on-failure";
        RestartSec = "5s";
        ProtectSystem = "strict";
        ReadWritePaths = [ cfg.stateDir ];
        ProtectHome = true;
        PrivateTmp = true;
        PrivateDevices = true;
        NoNewPrivileges = true;
        RestrictAddressFamilies = [ "AF_INET" "AF_INET6" "AF_UNIX" ];
        RestrictNamespaces = true;
        LockPersonality = true;
        MemoryDenyWriteExecute = true;
        SystemCallArchitectures = "native";
        CapabilityBoundingSet = [ ];
      };
    };

    systemd.timers.market-agent = {
      wantedBy = [ "timers.target" ];
      timerConfig = {
        OnCalendar = cfg.schedule;
        Persistent = true;
        RandomizedDelaySec = "10min";
      };
    };

    services.nginx.virtualHosts."${cfg.subdomain}.${config.homelab.domain}" = {
      forceSSL = true;
      enableACME = true;
      acmeRoot = null;
      root = "${cfg.stateDir}/www";
      locations."/" = {
        index = "index.html";
        # The page changes after every run; never serve yesterday's.
        extraConfig = ''add_header Cache-Control "no-cache";'';
      };
      locations."/api/" = {
        proxyPass = "http://127.0.0.1:${toString cfg.port}";
        recommendedProxySettings = true;
      };
    };
  };
}
