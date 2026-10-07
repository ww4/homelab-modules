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
#     on the console with `docker exec`, or through whichever messaging
#     service you configure, which is an outbound connection it makes;
#   * its credentials are a sops secret like every other key here.
#
# If you want it to reach more than its own directory, that is a deliberate
# edit to homelab.hermes.extraMounts, and the reason you are reading this.
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
    # Everything it is allowed to see.
    volumes = [ "${cfg.stateDir}:/data" ] ++ cfg.extraMounts;
    environment = {
      HERMES_CONFIG_DIR = "/data";
      # Point it at the models on this machine by default. The container
      # reaches the host's ollama on the docker bridge; a paid provider is
      # configured in the secret instead.
      OPENAI_BASE_URL = lib.mkDefault "http://host.docker.internal:11434/v1";
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
