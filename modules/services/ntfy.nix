# Notification infrastructure — self-hosted ntfy for backup / system alerts,
# at ntfy.<domain> (and directly on homelab.ntfy.baseUrl for the phone app).
#
# Subscribe the ntfy phone app to baseUrl + homelab.ntfy.topic, with the
# credentials from /var/lib/ntfy-sh/subscriber-password.txt (see below).
# Scripts send alerts with the `notify` helper:
#   notify "<title>" "<message>" [priority] [tags] [click-url]
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.ntfy;
  notify = import ../lib/notify.nix { inherit pkgs; url = cfg.url; };
in
{
  imports = [ ../options.nix ];

  services.ntfy-sh = {
    enable = true;
    settings = {
      base-url = cfg.baseUrl;
      listen-http = ":8090";
      # ⚠️ SECURITY. ntfy's default for this is `read-write`, which means
      # ANONYMOUS clients may both subscribe and publish. Verified live here
      # once: an unauthenticated GET of /<topic>/json returned 200 with full
      # message bodies, and an unauthenticated POST was accepted. If your
      # reverse proxy admits the whole LAN, any device on the network can
      # read every alert (diagnoses, unit failures, device names) and inject
      # fake ones.
      #
      # `write-only` = anonymous may PUBLISH but not SUBSCRIBE. Chosen over
      # `deny-all` deliberately: every local publisher posts anonymously to
      # loopback and keeps working untouched, while reading requires an
      # account. (This also matters if any notification carries an
      # actionable nonce IN the message — a topic reader could otherwise
      # press the action button on the alert about itself.)
      auth-default-access = "write-only";
    };
  };

  # The subscriber account, provisioned idempotently. Anonymous read is
  # denied, so the phone (and any dashboard widget) need credentials.
  #
  # The password is GENERATED here and written to a root-only file rather
  # than being asked for or echoed anywhere: it never passes through a chat,
  # a commit or the nix store. Read it once with:
  #     sudo cat /var/lib/ntfy-sh/subscriber-password.txt
  systemd.services.ntfy-provision = {
    description = "Provision the ntfy subscriber account (idempotent)";
    wantedBy = [ "multi-user.target" ];
    after = [ "ntfy-sh.service" ];
    path = [ pkgs.ntfy-sh pkgs.coreutils pkgs.gnugrep ];
    serviceConfig = { Type = "oneshot"; RemainAfterExit = true; };
    script = ''
      pwfile=/var/lib/ntfy-sh/subscriber-password.txt
      envfile=/var/lib/ntfy-sh/homepage-ntfy.env

      # ⚠️ NO --config FLAG EXISTS on `ntfy user`/`ntfy access`. Passing one
      # makes this unit die immediately after writing the password file — so
      # the password exists, the ACCOUNT never does, and the phone gets "not
      # authorized" while notifications are already locked down. These are
      # server-side commands: they read the default /etc/ntfy/server.yml and
      # operate on its auth-file directly.

      # Reuse an existing password rather than minting a new one, so a re-run
      # never invalidates a password already typed into the phone.
      if [ -s "$pwfile" ]; then
        pw=$(cat "$pwfile")
      else
        pw=$(head -c 32 /dev/urandom | base64 | tr -dc "A-Za-z0-9" | head -c 24)
        umask 077
        printf "%s\n" "$pw" > "$pwfile"
      fi

      # ntfy creates user.db on first start; wait rather than race it.
      for _ in $(seq 1 30); do
        ntfy user list >/dev/null 2>&1 && break
        sleep 2
      done

      # Add, or if the account already exists, force its password to match
      # the file. That makes the file authoritative and the unit
      # self-healing, instead of depending on parsing `user list` output.
      if ! NTFY_PASSWORD="$pw" ntfy user add ${config.homelab.adminUser} 2>/dev/null; then
        NTFY_PASSWORD="$pw" ntfy user change-pass ${config.homelab.adminUser}
      fi
      ntfy access ${config.homelab.adminUser} "${cfg.topic}" rw

      printf "HOMEPAGE_VAR_NTFY_USER=${config.homelab.adminUser}\nHOMEPAGE_VAR_NTFY_PASS=%s\n" "$pw" > "$envfile"
      chmod 600 "$pwfile"
      chmod 640 "$envfile"
      echo "ntfy-provision: subscriber '${config.homelab.adminUser}' ready. Password: sudo cat $pwfile"
    '';
  };

  # Guarantee the env file exists before any dashboard container starts, so
  # a first boot cannot fail on a missing EnvironmentFile.
  systemd.tmpfiles.rules = [
    "f /var/lib/ntfy-sh/homepage-ntfy.env 0640 root root -"
  ];

  # Reachable only over the Tailscale interface and Docker bridges. The
  # extraCommands rule covers user-defined networks (auto-named br-<id>
  # bridges) — where a dashboard container typically lives.
  networking.firewall.interfaces."tailscale0".allowedTCPPorts = [ 8090 ];
  networking.firewall.interfaces."docker0".allowedTCPPorts   = [ 8090 ];
  networking.firewall.extraCommands = ''
    iptables -I nixos-fw 1 -i br-+ -p tcp --dport 8090 -j nixos-fw-accept
  '';

  # nginx vhost so anything reaching for ntfy can use the familiar https
  # pattern with a real cert, matching every other vhost.
  services.nginx.virtualHosts."ntfy.${config.homelab.domain}" = {
    forceSSL = true;
    enableACME = true;
    acmeRoot = null;
    locations."/" = {
      proxyPass = "http://127.0.0.1:8090";
      recommendedProxySettings = true;
      # The ntfy Android app opens a WEBSOCKET for instant delivery. Without
      # this the upgrade handshake never completes and the app reports
      # "WebSocket not supported ... Expected HTTP 101", then falls back to
      # polling — slower notifications and more battery.
      #
      # recommendedProxySettings does NOT cover this: it sets only Host and
      # the X-Forwarded-* headers. WebSocket additionally needs HTTP/1.1
      # plus the Upgrade/Connection headers.
      proxyWebsockets = true;
      extraConfig = ''
        proxy_buffering off;
        proxy_read_timeout 1h;     # ntfy event-stream connections are long-lived
      '';
    };
  };

  # The shell helper, for ad-hoc/manual alerts. (Unit-failure alerting
  # belongs to the monitoring module's Grafana rules, not per-unit hooks.)
  environment.systemPackages = [ notify ];
}
