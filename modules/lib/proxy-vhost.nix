# The house proxy vhost, in one place.
#
# Most services carry this exact block: forceSSL + ACME (DNS-01, so
# acmeRoot = null) fronting a loopback backend with the four standard
# forwarding headers. One definition; a call site passes its port and
# appends whatever extra nginx directives it genuinely needs.
#
# Usage:
#   services.nginx.virtualHosts."thing.example.com" =
#     import ../lib/proxy-vhost.nix { port = 1234; };
# Optional: websockets = false; extraConfig = ''...appended directives...'';
{ port, websockets ? true, extraConfig ? "" }:

{
  forceSSL = true;
  enableACME = true;
  acmeRoot = null;
  locations."/" = {
    proxyPass = "http://127.0.0.1:${toString port}";
    proxyWebsockets = websockets;
    extraConfig = ''
      proxy_set_header Host $host;
      proxy_set_header X-Real-IP $remote_addr;
      proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
      proxy_set_header X-Forwarded-Proto $scheme;
    '' + extraConfig;
  };
}
