# The homelab.* option set — the single interface between this library and a
# consumer's flake. Implementations read these; the consumer's flake sets them.
# Grown as modules are parameterized; never given personal defaults.
{ lib, ... }:

{
  options.homelab = {
    ntfy = {
      url = lib.mkOption {
        type = lib.types.str;
        default = "http://localhost:8090/alerts";
        description = ''
          Full URL (server + topic) that library modules POST notifications
          to, in ntfy.sh format. Point it at your own ntfy instance/topic.
        '';
      };
    };

    quietHours = {
      # Non-critical notifications are suppressed between start and end.
      # Metrics keep publishing either way — only the phone stays silent.
      start = lib.mkOption {
        type = lib.types.ints.between 0 23;
        default = 22;
        description = "Hour (local time) when non-critical notifications stop.";
      };
      end = lib.mkOption {
        type = lib.types.ints.between 0 23;
        default = 7;
        description = "Hour (local time) when non-critical notifications resume.";
      };
    };

    arrMissingSweep = {
      user = lib.mkOption {
        type = lib.types.str;
        default = "root";
        description = ''
          User the weekly *arr missing-sweep runs as. Set it to whichever user
          owns the sops secret holding the *arr API keys.
        '';
      };
    };
  };
}
