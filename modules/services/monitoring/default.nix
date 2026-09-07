# Monitoring stack — Prometheus + Grafana + Alertmanager + node_exporter,
# with alerting PROVISIONED DECLARATIVELY so a rebuild restores the whole
# monitoring configuration, not just the daemons. Alerting that lives only in
# a web UI is alerting you will lose.
#
# Each service binds 127.0.0.1; nginx fronts the user-facing two (grafana,
# prometheus) at HTTPS under homelab.domain. node_exporter is local-only.
#
# Alerting design (all file-managed, read-only in the GUI — that's the point):
#   contact points : one webhook -> homelab.monitoring.alertWebhookUrl
#                    (a tiny shim that forwards to ntfy works well)
#   mute timings   : "nights", GENERATED from homelab.quietHours + the
#                    system time zone — one place defines quiet hours
#   policies       : two-tier — severity=critical bypasses the mute and
#                    repeats hourly; everything else routes through the muted
#                    catch-all. An alert that wakes you for something you
#                    cannot fix at 3 a.m. trains you to ignore alerts.
#   alert rules    : generic rules ship here (systemd unit failures, GitOps
#                    deploy failures, CPU/NVMe temperature, textfile-collector
#                    health); site-specific rules merge in via
#                    homelab.monitoring.extraAlertRuleFiles.
#
# Alert DESCRIPTIONS carry the remediation, not just the symptom — which
# journal to read, what a sustained firing implies that a brief one doesn't.
# Future-you reading a page on a phone is the real audience.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.monitoring;
  domain = config.homelab.domain;
  grafanaHost      = "grafana.${domain}";
  prometheusHost   = "prometheus.${domain}";
  grafanaPort      = 3001;   # 3000 is a popular container port; stay clear
  prometheusPort   = 9090;
  alertmanagerPort = 9093;
  nodeExporterPort = 9100;

  # Quiet hours as Grafana/Alertmanager time intervals, generated from
  # homelab.quietHours. A window that wraps midnight becomes two intervals.
  qh = config.homelab.quietHours;
  fmt = h: (if h < 10 then "0" else "") + toString h + ":00";
  nightTimes =
    if qh.start > qh.end then [
      { start_time = fmt qh.start; end_time = "24:00"; }
      { start_time = "00:00"; end_time = fmt qh.end; }
    ] else [
      { start_time = fmt qh.start; end_time = fmt qh.end; }
    ];

  baseRules = builtins.fromJSON (builtins.readFile ./alert-rules.json);
  extraRules = lib.concatMap (f: (builtins.fromJSON (builtins.readFile f)).groups)
    cfg.extraAlertRuleFiles;
in
{
  # All homelab.monitoring.* options are declared in ../../options.nix so
  # sibling modules (e.g. deploy-drift-watch) can set them without importing
  # the whole stack.
  imports = [ ../../options.nix ];

  config = lib.mkIf cfg.enable {
    # A Prometheus reachable from container bridges: interface-scoped firewall
    # rules drop container->host traffic (the classic docker-bridge gap), so a
    # dashboard container polling Prometheus via the bridge gateway needs this
    # hole. Nothing else reaches 9090 — it's in no interface allowlist, so
    # LAN/tailnet clients still go through the nginx vhost.
    networking.firewall.extraCommands = ''
      iptables -I nixos-fw 1 -i br-+ -p tcp --dport 9090 -j nixos-fw-accept
    '';

    services.prometheus = {
      enable = true;
      port = prometheusPort;
      # 0.0.0.0 so the docker-bridge gateway path answers; the firewall keeps
      # every interface closed except the br-+ hole above.
      listenAddress = "0.0.0.0";
      # Conservative default — raise it in your flake if you import history.
      retentionTime = lib.mkDefault "365d";
      globalConfig = {
        scrape_interval = "1m";
        evaluation_interval = "1m";
      };
      scrapeConfigs = [
        { job_name = "prometheus";
          static_configs = [{ targets = [ "127.0.0.1:${toString prometheusPort}" ]; }];
        }
        { job_name = "node";
          static_configs = [{ targets = [ "127.0.0.1:${toString nodeExporterPort}" ]; }];
        }
      ] ++ cfg.extraScrapeConfigs;
      alertmanagers = [{
        static_configs = [{ targets = [ "127.0.0.1:${toString alertmanagerPort}" ]; }];
      }];

      # Alerting is owned by Grafana Alerting (provisioned below), not
      # Prometheus rules — that keeps schedule/severity visible in the UI
      # while remaining file-managed. Alertmanager stays for the extra routes.

      # node_exporter — host metrics (CPU, RAM, disk, network).
      exporters.node = {
        enable = true;
        port = nodeExporterPort;
        listenAddress = "127.0.0.1";
        enabledCollectors = [ "systemd" "processes" ];
        # The default systemd collector excludes mount units; include them so
        # a unit-failure alert catches mount failures too (a USB drive that
        # drops on reboot is exactly the failure you want paged about). Keep
        # device/automount/scope/slice excluded — noisy, not actionable.
        extraFlags = [
          ''--collector.systemd.unit-exclude=.+\.(automount|device|scope|slice)''
        ];
      };

      alertmanager = {
        enable = true;
        port = alertmanagerPort;
        listenAddress = "127.0.0.1";
        configuration = {
          route = {
            group_by = [ "alertname" ];
            group_wait = "30s";
            group_interval = "5m";
            repeat_interval = "12h";
            receiver = "ntfy";
            routes = cfg.extraAlertmanagerRoutes;
          };
          receivers = [
            {
              name = "ntfy";
              webhook_configs = [{
                url = cfg.alertWebhookUrl;
                send_resolved = true;
              }];
            }
            {
              name = "ntfy-noresolve";
              webhook_configs = [{
                url = cfg.alertWebhookUrl;
                send_resolved = false;
              }];
            }
          ];
          # Quiet hours, generated from homelab.quietHours — referenced by any
          # extra route that should hold until morning.
          time_intervals = [{
            name = "nights";
            time_intervals = [{
              times = nightTimes;
              location = config.time.timeZone;
            }];
          }];
        };
      };
    };

    # Grafana — visualization, plus the provisioned alerting.
    #
    # Grafana dropped its hard-coded default secret_key; generate a random one
    # on first deploy and reference it via $__file. Same for the admin
    # password (anonymous access is Viewer-only; admin edits dashboards).
    systemd.services.grafana-secret-key = {
      description = "Generate Grafana secret_key on first boot";
      wantedBy = [ "multi-user.target" ];
      before = [ "grafana.service" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
      };
      script = ''
        install -d -o grafana -g grafana -m 0750 /var/lib/grafana
        KEY=/var/lib/grafana/secret_key
        if [ ! -s "$KEY" ]; then
          ${pkgs.openssl}/bin/openssl rand -base64 32 > "$KEY"
          chown grafana:grafana "$KEY"
          chmod 0640 "$KEY"
        fi
        PW=/var/lib/grafana/admin_password
        if [ ! -s "$PW" ]; then
          # No trailing newline — Grafana's $__file{} uses the bytes verbatim
          # as the admin password; a stray newline makes it un-typeable.
          ${pkgs.openssl}/bin/openssl rand -base64 18 | tr -d '\n' > "$PW"
          chown grafana:grafana "$PW"
          chmod 0640 "$PW"
        fi
      '';
    };

    services.grafana = {
      enable = true;
      declarativePlugins = cfg.extraPlugins;
      settings = {
        server = {
          http_addr = "127.0.0.1";
          http_port = grafanaPort;
          domain = grafanaHost;
          root_url = "https://${grafanaHost}/";
        };
        security = {
          secret_key = "$__file{/var/lib/grafana/secret_key}";
          admin_user = "admin";
          admin_password = "$__file{/var/lib/grafana/admin_password}";
          # Allow iframe embedding so a dashboard tile can render a panel.
          # Grafana sits behind the network source-gate already.
          allow_embedding = true;
        };
        analytics.reporting_enabled = false;
        # Anonymous Viewer so no-login embeds (dashboard iframes) can fetch
        # panels. Anyone who can reach the URL was already inside the
        # perimeter.
        "auth.anonymous" = {
          enabled = true;
          org_role = "Viewer";
          org_name = "Main Org.";
        };
      } // lib.optionalAttrs (cfg.grafanaOidcSecretFile != null) {
        # OIDC SSO (Authelia-style endpoints at auth.<domain>). Adds a
        # "Sign in" button ALONGSIDE anon-viewer and the admin form — none
        # are removed. Group `admins` -> Grafana Admin, else Viewer.
        "auth.generic_oauth" = {
          enabled = true;
          name = "Authelia";
          icon = "signin";
          client_id = "grafana";
          client_secret = "$__file{${cfg.grafanaOidcSecretFile}}";
          scopes = "openid profile email groups";
          auth_url = "https://auth.${domain}/api/oidc/authorization";
          token_url = "https://auth.${domain}/api/oidc/token";
          api_url = "https://auth.${domain}/api/oidc/userinfo";
          login_attribute_path = "preferred_username";
          groups_attribute_path = "groups";
          role_attribute_path = "contains(groups[*], 'admins') && 'Admin' || 'Viewer'";
          allow_sign_up = true;
          use_pkce = true;
        };
      };
      provision = {
        enable = true;
        datasources.settings.datasources = [
          {
            name = "Prometheus";
            # Pinned so codified alert rules + dashboards that reference this
            # uid always resolve, regardless of provisioning order.
            uid = "PBFA97CFB590B2093";
            type = "prometheus";
            access = "proxy";
            url = "http://127.0.0.1:${toString prometheusPort}";
            isDefault = true;
          }
        ] ++ cfg.extraDatasources;

        # NOTE: dashboards are deliberately NOT provisioned from here.
        # Grafana's apiserver denies the anonymous Viewer read access to
        # provisioned dashboards in the root/General folder (embeds 403), and
        # wedges provisioned dashboards so they can't be moved or deleted.
        # Provision into a NAMED folder from the owning module instead, or
        # import imperatively and keep JSON snapshots in git as references.

        alerting = {
          contactPoints.settings = {
            apiVersion = 1;
            contactPoints = [{
              orgId = 1;
              name = "ntfy";
              receivers = [{
                uid = "afnqi2p8rsk5cd";
                type = "webhook";
                settings = {
                  httpMethod = "POST";
                  url = cfg.alertWebhookUrl;
                };
                disableResolveMessage = false;
              }];
            }];
          };
          muteTimings.settings = {
            apiVersion = 1;
            muteTimes = [{
              orgId = 1;
              name = "nights";
              time_intervals = [{
                times = nightTimes;
                location = config.time.timeZone;
              }];
            }];
          };
          policies.settings = builtins.fromJSON (builtins.readFile ./policies.json);
          rules.settings = {
            apiVersion = 1;
            groups = baseRules.groups ++ extraRules;
          };
        };
      };
    };

    # nginx vhosts, network-gated like everything else.
    services.nginx.virtualHosts."${grafanaHost}" =
      import ../../lib/proxy-vhost.nix { port = grafanaPort; };

    services.nginx.virtualHosts."${prometheusHost}" =
      import ../../lib/proxy-vhost.nix { port = prometheusPort; websockets = false; };
  };
}
