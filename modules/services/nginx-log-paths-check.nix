# GUARD: nginx may only be told to write logs where nginx can actually write.
#
# WHY THIS FILE EXISTS
# --------------------
# A module once added
#   access_log /var/lib/someservice/access.log custom_format;
# to a location block. nginx opens every log path at CONFIG-PARSE time, so when
# that path was not writable nginx did not degrade — it refused to start, and
# every vhost on the box went down with it: the git forge, the media server,
# the password manager, the dashboard. It stayed down until a human reached a
# keyboard.
#
# It took THREE attempts to make that one line work, which is the real argument
# for a mechanical check. The path has to clear three independent hurdles:
#   1. the directory must exist at parse time (a tmpfiles rule naming a
#      nonexistent group silently created nothing);
#   2. the nginx user must own or be able to write it;
#   3. it must be writable INSIDE the unit's mount namespace — nginx runs
#      ProtectSystem=strict with an empty ReadWritePaths, so almost all of
#      /var is read-only to it no matter what the file permissions say.
#
# WHY NOT JUST RUN `nginx -t`. Three reasons, all of them measured on the real
# incident rather than assumed:
#   * `services.nginx.validateConfigFile = true` is a misleading name. Despite
#     it, `writeNginxConfig` runs only `gixy`, a static security linter. It
#     never invokes `nginx -t` at all, which is why the broken config built
#     clean.
#   * It cannot be moved into the build: the Nix sandbox has a read-only root,
#     so absolute production paths like /var/log/nginx cannot even be created
#     to test against (verified — the probe build fails).
#   * And `nginx -t` is not a reliable discriminator for this class ANYWAY.
#     Run against the very config that took the box down, pointed at a
#     directory that provably does not exist, it still reports
#     "the configuration file ... syntax is ok" — it aborts on the pid file
#     first and never reaches opening the per-location log. It only failed in
#     production because the pre-start ran as the nginx user with a writable
#     /run/nginx, got past the pid file, and only then hit the log. A check
#     whose verdict depends on which user ran it and what already exists on
#     disk is not a check you can gate a deploy on.
# Hurdle 3 is invisible to it regardless: `nginx -t` knows nothing of systemd
# mount namespaces.
#
# So this checks the invariant directly and at EVAL time: every log path in the
# rendered nginx configuration must live under a directory nginx is guaranteed
# to be able to write — its own LogsDirectory/CacheDirectory, or a path
# explicitly granted via ReadWritePaths. A module that adds a log somewhere
# else now fails `nixos-rebuild build` with an explanation, instead of taking
# the web server down at deploy time.
#
# It is deliberately a coarse prefix test. It cannot prove a path is writable;
# it proves the path was DECLARED writable, which is the step that was missing.
{ config, lib, ... }:

let
  cfg = config.services.nginx;
  nginxService = config.systemd.services.nginx.serviceConfig or { };

  # Paths systemd itself guarantees for this unit, plus anything the config
  # has explicitly granted write access to.
  declaredWritable =
    [ "/var/log/nginx" "/var/cache/nginx" "/var/spool/nginx" "/run" "/tmp" ]
    ++ (lib.toList (nginxService.ReadWritePaths or [ ]))
    ++ (lib.map (d: "/var/lib/${d}") (lib.toList (nginxService.StateDirectory or [ ])));

  # Every place raw nginx configuration can be written from.
  vhosts = lib.attrValues (cfg.virtualHosts or { });
  configStrings =
    [ (cfg.commonHttpConfig or "") (cfg.appendHttpConfig or "")
      (cfg.httpConfig or "") (cfg.config or "") (cfg.streamConfig or "") ]
    ++ (lib.map (v: v.extraConfig or "") vhosts)
    ++ (lib.concatMap (v: lib.map (l: l.extraConfig or "")
          (lib.attrValues (v.locations or { }))) vhosts);

  # Pull out the argument of every access_log / error_log directive.
  logPathsIn = text:
    let
      parts = builtins.split "(access_log|error_log)[[:space:]]+([^;[:space:]]+)" text;
    in
    lib.concatMap (p: if builtins.isList p then [ (builtins.elemAt p 1) ] else [ ])
      parts;

  found = lib.unique (lib.concatMap logPathsIn (lib.filter (s: s != null) configStrings));

  # `off`, syslog: and memory: targets touch no filesystem. Relative paths are
  # resolved against nginx's prefix, which is in the store and never writable,
  # so those are flagged too.
  needsFilesystem = p:
    p != "off" && !(lib.hasPrefix "syslog:" p) && !(lib.hasPrefix "memory:" p);

  offending = lib.filter
    (p: needsFilesystem p
      && !(lib.any (root: lib.hasPrefix (root + "/") p) declaredWritable))
    found;
in
{
  config.assertions = [{
    assertion = offending == [ ];
    message = ''
      nginx is configured to write a log outside every directory it is allowed
      to write to:

        ${lib.concatStringsSep "\n  " offending}

      nginx opens log paths at CONFIG-PARSE time, so this does not degrade one
      vhost — nginx refuses to start and EVERY site on this host goes down.
      That is exactly the incident this check exists to prevent; see
      nginx-log-paths-check.nix for the postmortem.

      Currently writable to the nginx unit:
        ${lib.concatStringsSep "\n  " declaredWritable}

      Fix it one of these ways, best first:
        1. Do not make nginx write there at all. If you are recording something
           application-specific, give the application its own endpoint and
           `proxy_pass` to it — nginx can always reach 127.0.0.1, and a bug in
           that path then costs one 502 instead of the whole box.
        2. Put the log under /var/log/nginx, which is the unit's LogsDirectory.
        3. If it genuinely must live elsewhere, grant it explicitly:
             systemd.services.nginx.serviceConfig.ReadWritePaths = [ "<dir>" ];
           and make sure something actually creates that directory, owned by
           ${cfg.user or "nginx"}, before nginx starts.
    '';
  }];
}
