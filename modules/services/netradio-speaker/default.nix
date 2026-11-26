# netradio speaker — one machine's own speakers as a playback endpoint.
#
# The full `netradio` module runs a radio station: it scans a music library,
# builds playlists, runs Liquidsoap and Icecast, and serves a page. This one
# runs none of that. It is the *listening* end on its own — a machine with
# speakers that can be told to play one of those stations — so it can be
# enabled on a box that is not the radio server at all.
#
# That is the whole reason it exists. The station server's own speakers were
# always available as a device, because the speaker ran inside the big module;
# a SECOND machine on the network had no way to be one. Now it does:
#
#   homelab.netradio-speaker = {
#     enable  = true;
#     user    = "alice";                              # whose session owns the sound
#     output  = "pulse";                              # a desktop machine
#     stations = "https://radio.example.com/radio";   # where the mounts live
#     listen  = "0.0.0.0";
#     tokenFile = "/run/secrets/netradio-speaker";
#   };
#
# and on the radio server, register it as a device:
#
#   homelab.netradio.devices.studio = {
#     name = "Studio PC";
#     endpoint = "http://studio.example.com:8013";
#     capabilities = [ "play" "stop" "volume" "mute" "stations" ];
#   };
#
# ALSA or Pulse, and why it matters:
#
#   output = "alsa"   the machine's own card, nothing else using it. The level
#                     is the card's mixer, so it is the real output level.
#   output = "pulse"  a machine with a desktop session, where a game or a
#                     browser may be making sound through the same sink. The
#                     level is set on THIS SERVICE'S STREAM, so turning the
#                     radio down leaves everything else where it was.
#
# Pulse needs a session to talk to, so that case runs as a user service and
# `user` is required — a system service has no PipeWire to connect to.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.netradio-speaker;
  netradio = cfg.package;

  isPulse = cfg.output == "pulse";

  # Pulse must run inside the user's session; ALSA need not, and running it as
  # a system service is the long-standing behaviour on a server.
  asUserService = isPulse || cfg.user != null;

  args = lib.concatStringsSep " " ([
    "${netradio}/bin/netradio speaker"
    "--icecast ${lib.escapeShellArg cfg.stations}"
    "--listen ${cfg.listen}"
    "--port ${toString cfg.port}"
    "--output ${cfg.output}"
    "--max-volume ${toString cfg.maxVolume}"
    "--fade-in-ms ${toString cfg.fadeInMs}"
    "--fade-out-ms ${toString cfg.fadeOutMs}"
    "--ffmpeg ${pkgs.ffmpeg}/bin/ffmpeg"
  ]
  ++ lib.optional (cfg.wake != "") "--wake ${lib.escapeShellArg cfg.wake}"
  ++ lib.optional (cfg.defaultMount != "") "--default-mount ${cfg.defaultMount}"
  ++ lib.optional (cfg.startVolume != null) "--start-volume ${toString cfg.startVolume}"
  ++ lib.optional cfg.startMuted "--start-muted"
  ++ lib.optional cfg.holdStreamWhileMuted "--hold-stream-while-muted"
  ++ lib.optional (cfg.tokenFile != null) "--token-file ${cfg.tokenFile}"
  ++ lib.optional cfg.allowUnauthenticated "--allow-unauthenticated"
  ++ lib.optional (cfg.bedMount != "") "--bed-mount ${cfg.bedMount}"
  ++ lib.optional (cfg.bedMount != "") "--bed-level ${toString cfg.bedLevel}"
  ++ lib.optionals isPulse ([
       "--pactl ${pkgs.pulseaudio}/bin/pactl"
       "--stream-name ${lib.escapeShellArg cfg.streamName}"
     ] ++ lib.optional (cfg.sink != "") "--sink ${lib.escapeShellArg cfg.sink}")
  ++ lib.optionals (!isPulse) [
       "--card ${cfg.card}"
       "--device ${cfg.device}"
     ]
  ++ lib.optional (!isPulse && cfg.control != "") "--control ${cfg.control}");

  common = {
    description = "netradio speaker (play a station on this machine's speakers)";
    serviceConfig = {
      Type = "exec";
      ExecStart = args;
      Restart = "on-failure";
      RestartSec = "5s";
      # The player is an ffmpeg child, and amixer/pactl are forks too.
      Environment = [
        "PATH=${lib.makeBinPath ([ pkgs.ffmpeg ]
                                 ++ lib.optional isPulse pkgs.pulseaudio
                                 ++ lib.optional (!isPulse) pkgs.alsa-utils)}"
      ];
    };
  };

in
{
  options.homelab.netradio-speaker = {
    enable = lib.mkEnableOption "netradio speaker: this machine's speakers as a playback endpoint";

    package = lib.mkOption {
      type = lib.types.package;
      default = pkgs.callPackage ../netradio/package.nix { };
      defaultText = lib.literalExpression "the netradio package from this module library";
      description = "The netradio package providing `netradio speaker`.";
    };

    stations = lib.mkOption {
      type = lib.types.str;
      # Empty rather than no default on purpose: leaving it undefined makes
      # NixOS throw "option was accessed but has no value defined", which does
      # not say what to put there. The assertion below does.
      default = "";
      example = "https://radio.example.com/radio";
      description = ''
        Base URL the station mounts are served under: a mount named `rain`
        is fetched from `<stations>/rain.mp3`.

        On the radio server itself this is Icecast on loopback
        (`http://127.0.0.1:8020`). From another machine it is the radio page's
        own vhost, which is usually what you want even though it is a longer
        path: the vhost is where the on-demand encoders get woken, so fetching
        through it starts the station without this machine needing any access
        to the station's control socket.
      '';
    };

    wake = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "http://127.0.0.1:8011";
      description = ''
        The wake service, which starts a station's encoder before connecting.
        Only needed when `stations` points straight at Icecast and so bypasses
        the vhost that would otherwise do the waking. Leave empty when
        `stations` is an https page URL.
      '';
    };

    output = lib.mkOption {
      type = lib.types.enum [ "alsa" "pulse" ];
      default = "alsa";
      description = ''
        `alsa` for a machine whose card nothing else is using: the level is the
        card's own mixer, which is the real output level.

        `pulse` for a machine with a desktop session, where something else may
        be playing through the same sink: the level is set on this service's
        own stream, so the radio can be turned down without touching a game's
        volume. Requires `user`, since a system service has no session
        PipeWire to connect to.
      '';
    };

    user = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "alice";
      description = ''
        Run as a user service belonging to this user, rather than as a system
        service. Required for `output = "pulse"`, because the sound belongs to
        that user's session.

        The unit is a user unit with `ConditionUser`, so it starts in that
        user's session and nobody else's.
      '';
    };

    listen = lib.mkOption {
      type = lib.types.str;
      default = "127.0.0.1";
      description = ''
        Bind address. Loopback is right when the radio server's own vhost
        proxies this endpoint, since the vhost is then the access gate.
        Anything else is reachable from the network and REQUIRES `tokenFile`;
        the service refuses to start otherwise.
      '';
    };

    port = lib.mkOption {
      type = lib.types.port;
      default = 8013;
      description = "Port for the JSON API.";
    };

    tokenFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/netradio-speaker";
      description = ''
        A file holding a shared token. Every request must then carry
        `Authorization: Bearer <token>`, and the radio server sends it when
        proxying to this device.

        Required whenever `listen` is not loopback. Without it, anything on
        the network could start the speakers playing.
      '';
    };

    allowUnauthenticated = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Bind off loopback with no token, because something else is the gate.

        A firewall scoping the port to one interface is a legitimate gate, and
        so is an overlay network like Tailscale where nothing unauthenticated
        can route to the host at all. Set this together with
        `firewallInterfaces` and no `tokenFile`.

        It has to be asked for explicitly, because the mistake it guards
        against is silent: an unauthenticated port on a LAN behaves exactly
        like a correctly configured one until someone else uses it.
      '';
    };

    openFirewall = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Open `port` globally. Usually the wrong tool: prefer
        `firewallInterfaces` so only the interface the radio server reaches
        this machine on is opened.
      '';
    };

    firewallInterfaces = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "tailscale0" ];
      description = "Open `port` on these interfaces only.";
    };

    bedMount = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "rain";
      description = ''
        A station to play UNDERNEATH the chosen one — rain, usually — mixed at
        this device rather than in a browser, so it is audible from the sofa
        rather than only to whoever has the page open.

        Needs an output that can mix a second stream, which `pulse` can and
        `alsa` cannot: an exclusive ALSA device will not open twice. Setting
        this with `output = "alsa"` logs a warning and leaves the bed
        unavailable rather than failing.

        Declare `bed` in the device's `capabilities` on the radio server for
        the page to offer it.
      '';
    };

    bedLevel = lib.mkOption {
      type = lib.types.ints.between 0 100;
      default = 25;
      description = ''
        The bed's own level, independent of the station's — the station's
        volume slider does not move it. Under the music is the point.
      '';
    };

    defaultMount = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "rain";
      description = ''
        Play this at startup, so power-cycling the machine brings the sound
        back with no phone involved.
      '';
    };

    maxVolume = lib.mkOption {
      type = lib.types.ints.between 0 100;
      default = 100;
      description = ''
        A ceiling no request can exceed — not the page, not a stray call, not a
        diagnostic. Worth setting on speakers someone sleeps near.
      '';
    };

    startVolume = lib.mkOption {
      type = lib.types.nullOr (lib.types.ints.between 0 100);
      default = null;
      description = "Set the level here at startup instead of adopting whatever it reads.";
    };

    startMuted = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Come up silent: the speakers stay quiet until something unmutes them.";
    };

    holdStreamWhileMuted = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = ''
        Stay connected to the station while muted. By default a mute releases
        the mount so an on-demand encoder can stop; holding it costs a
        continuous encode on the server to feed speakers that are silent. Set
        this only if unmute latency matters more than that.
      '';
    };

    fadeInMs = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 300;
      description = "How long an unmute takes to come back.";
    };

    fadeOutMs = lib.mkOption {
      type = lib.types.ints.unsigned;
      default = 120;
      description = "How long a mute, or an ordinary volume change, takes.";
    };

    # --- ALSA only ---------------------------------------------------------
    card = lib.mkOption {
      type = lib.types.str;
      default = "0";
      description = "ALSA card index or name for the mixer. `output = \"alsa\"` only.";
    };

    control = lib.mkOption {
      type = lib.types.str;
      default = "";
      description = ''
        Mixer control. Empty picks the first of Master/PCM/Speaker/Headphone/
        Digital that answers, so most cards need nothing here.
        `output = "alsa"` only.
      '';
    };

    device = lib.mkOption {
      type = lib.types.str;
      default = "plughw:0,0";
      description = ''
        ALSA device to open. NOT `default`, which on a machine running
        PipeWire is redirected to a per-user service a system unit cannot
        reach — the symptom is "Host is down" for ever.
        `output = "alsa"` only.
      '';
    };

    # --- Pulse only --------------------------------------------------------
    sink = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "alsa_output.pci-0000_0a_00.4.analog-stereo";
      description = ''
        Which sink to play to. Empty uses the session's default, which follows
        whatever the desktop is set to. `output = "pulse"` only.
      '';
    };

    streamName = lib.mkOption {
      type = lib.types.str;
      default = "netradio";
      description = ''
        The application name this service's audio stream carries. It is how the
        service finds its own stream again in order to set that stream's
        volume, so it only needs changing if two of these run for one user.
        `output = "pulse"` only.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion = !isPulse || cfg.user != null;
        message = "homelab.netradio-speaker: output = \"pulse\" needs `user`. The sound "
                  + "belongs to a desktop session, and a system service has no session "
                  + "PipeWire to connect to — it would fail with EHOSTDOWN for ever.";
      }
      {
        # The same check the service makes at startup, moved to build time so
        # it is a message rather than a unit that will not come up.
        assertion = cfg.tokenFile != null || cfg.allowUnauthenticated
                    || builtins.elem cfg.listen [ "127.0.0.1" "::1" "localhost" ];
        message = "homelab.netradio-speaker: listen = \"${cfg.listen}\" is reachable from "
                  + "the network, so tokenFile is required — otherwise anything on the "
                  + "network can start these speakers playing. Set tokenFile, bind to "
                  + "127.0.0.1 and let the radio server's vhost proxy it, or set "
                  + "allowUnauthenticated if something else is the gate (a firewall "
                  + "scoped to one interface, an overlay network).";
      }
      {
        assertion = cfg.stations != "";
        message = "homelab.netradio-speaker: set `stations` to where the mounts are served, "
                  + "e.g. https://radio.example.com/radio (or http://127.0.0.1:8020 on the "
                  + "radio server itself).";
      }
    ];

    systemd.user.services.netradio-speaker = lib.mkIf asUserService (lib.recursiveUpdate common {
      wantedBy = [ "default.target" ];
      after = [ "pipewire.service" ];
      unitConfig = lib.mkIf (cfg.user != null) { ConditionUser = cfg.user; };
    });

    systemd.services.netradio-speaker = lib.mkIf (!asUserService) (lib.recursiveUpdate common {
      wantedBy = [ "multi-user.target" ];
      after = [ "network.target" "sound.target" ];
      serviceConfig = {
        DynamicUser = true;
        SupplementaryGroups = [ "audio" ];
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
      };
    });

    networking.firewall = lib.mkMerge [
      (lib.mkIf cfg.openFirewall { allowedTCPPorts = [ cfg.port ]; })
      (lib.mkIf (cfg.firewallInterfaces != [ ]) {
        interfaces = lib.genAttrs cfg.firewallInterfaces (_: { allowedTCPPorts = [ cfg.port ]; });
      })
    ];
  };
}
