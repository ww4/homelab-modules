# open-webui — the browser front end for the models ollama is serving.
#
# This is the only thing that talks to ollama, over loopback, which is why
# ollama has no vhost of its own.
#
# ⚠️ IT DOES ITS OWN LOGIN AND IS NOT BEHIND AUTHELIA. The first account
# created on a fresh instance becomes the administrator, so the first person
# to open it after an install owns it. On a household network behind the
# source allow-list that is the same exposure as every other vhost here; if
# this machine has guests on its network, add `chat` to
# homelab.authelia.protectedVhosts and it gets a second door.
{ config, lib, ... }:

let
  domain = config.homelab.domain;
  port = 8080;
in
{
  services.open-webui = {
    enable = true;
    host = "127.0.0.1";
    inherit port;
    environment = {
      OLLAMA_BASE_URL = "http://127.0.0.1:11434";
      # No telemetry and no model downloads from the front end: models are a
      # declared list in the ollama module, not something a visitor adds.
      ANONYMIZED_TELEMETRY = "False";
      DO_NOT_TRACK = "True";
      SCARF_NO_ANALYTICS = "True";
    };
  };

  services.nginx.virtualHosts."chat.${domain}" = import ../lib/proxy-vhost.nix { inherit port; };
}
