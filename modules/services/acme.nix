# ACME defaults — Let's Encrypt via DNS-01, shared by every vhost.
#
# DNS-01 (not HTTP-01) because nothing here is reachable from the internet:
# the DNS provider's API proves domain control, so certs issue and renew
# with zero inbound exposure. Every vhost then just sets enableACME +
# acmeRoot = null.
#
# CONSUMER MUST DECLARE the API credential as a sops secret and point
# homelab.acme.credentialsFile at it (e.g. CLOUDFLARE_DNS_API_TOKEN=...;
# systemd reads the environmentFile as root before dropping to the acme
# user).
{ config, lib, ... }:

{
  imports = [ ../options.nix ];

  security.acme = {
    acceptTerms = true;
    defaults = {
      email = config.homelab.acme.email;
      dnsProvider = config.homelab.acme.dnsProvider;
      environmentFile = config.homelab.acme.credentialsFile;
    };
  };
}
