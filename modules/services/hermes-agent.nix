# Hermes — a self-hosted agent harness, for somebody who wants one without
# building it themselves.
#
# It talks to whatever model you point it at: the ollama running on this
# machine, or a paid API if you have one. That is the reason it is here. Local
# models alone give you a chat window; this gives you something that keeps
# working between conversations.
#
# ⚠️ READ THIS BEFORE ENABLING IT. Hermes exists to run code, read and write
# files, fetch things from the web, and talk to messaging services on your
# behalf. That is not a side effect of the design, it is the design. A model
# deciding what to run is a different trust proposition from a model answering
# questions, and the usual homelab reasoning — "it is only on my LAN" — does
# not cover a program whose job is to act.
#
# So it is contained rather than trusted:
#
#   * it runs in a container, which is the boundary its "local" terminal
#     backend runs inside;
#   * it gets one directory of its own and no view of the rest of the machine;
#   * it gets no vhost, so nothing reaches it from the network. You talk to it
#     on the machine with `docker exec`, or through whichever chat platform
#     you configure its gateway for, which is an outbound connection it makes;
#   * its web dashboard is OFF. The image only starts it when HERMES_DASHBOARD
#     is set, and on a non-loopback bind it fails closed unless it is also
#     given an auth provider (HERMES_DASHBOARD_BASIC_AUTH_USERNAME and
#     _PASSWORD, or an OAuth client id). Putting that behind a vhost is a
#     piece of work with its own decisions, so it is not done here;
#   * its credentials are a sops secret like every other key here.
#
# If you want it to reach more than its own directory, that is a deliberate
# edit to homelab.hermes.extraMounts, and the reason you are reading this.
#
# ⚠️ THREE THINGS BELOW ARE THE IMAGE'S, NOT OURS, and all three were got
# wrong on the first pass. The image declares HERMES_HOME=/opt/data and
# writes every piece of mutable state there, so the state directory mounts
# at /opt/data and nowhere else — mounted at /data it is simply an unused
# directory, and the real state lives in the container's writable layer until
# the next recreate throws it away. Its entrypoint runs the CMD as the s6
# supervision tree's main program, and with no CMD that is `hermes`, the
# interactive CLI, which has no terminal here and exits; `sleep infinity` is
# the invocation upstream supports for a container you exec into. Read these
# off the pinned digest with `skopeo inspect --config` and the repository's
# docker/ directory when bumping the pin, because they can change under it.
#
# ⚠️ PINNED BY DIGEST, like every image here, and this one changes often:
# the project publishes dated tags and moves `latest` under them. Bump it by
# reading a digest with `skopeo inspect` and sending a pull request, not by
# chasing a tag.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.hermes;
in
{
  imports = [ ../options.nix ];

  virtualisation.docker.enable = lib.mkDefault true;

  systemd.tmpfiles.rules = [
    "d ${cfg.stateDir} 0700 root root -"
  ];

  virtualisation.oci-containers.containers.hermes-agent = {
    # v2026.9.24. A dated release rather than `latest`, so a rebuild does not
    # quietly change what is running.
    image = "docker.io/nousresearch/hermes-agent:v2026.9.24@sha256:fca358f12efd65bfaaca05884166f15c0e2788375ca30d77061ac1ebc96452b7";
    autoStart = true;
    # Everything it is allowed to see. /opt/data because that is the image's
    # HERMES_HOME; see the note at the top.
    volumes = [ "${cfg.stateDir}:/opt/data" ] ++ cfg.extraMounts;
    # Keeps the supervision tree up so there is something to exec into. Not
    # a placeholder: the image routes a bare executable straight through, and
    # its own main service is this same call.
    cmd = [ "sleep" "infinity" ];
    # Point it at the models on this machine, and only when that is the whole
    # story: this is a `-e`, and docker lets `-e` beat `--env-file`, so
    # setting it unconditionally would send a provider key to a loopback port
    # with nothing behind it. With credentials supplied, the container's own
    # default address for that provider is the right one, and anyone who wants
    # both can put OPENAI_BASE_URL in the credentials file themselves.
    environment = lib.optionalAttrs (config.services.ollama.enable && cfg.environmentFile == null) {
      OPENAI_BASE_URL = "http://host.docker.internal:11434/v1";
    };
    # Keys for whichever provider is in use. Absent is fine: Hermes asks for
    # one on first run and a local model needs none.
    environmentFiles = lib.optional (cfg.environmentFile != null) cfg.environmentFile;
    extraOptions = [
      "--add-host=host.docker.internal:host-gateway"
      # ⚠️ A model choosing what to run should not also be able to take the
      # machine down by accident. These are a ceiling, not a target.
      "--memory=${cfg.memoryMax}"
      "--pids-limit=512"
    ];
  };
}
