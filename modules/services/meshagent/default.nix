# MeshCentral MeshAgent — the endpoint agent that self-manages this NixOS host
# by connecting to your MeshCentral server.
#
# There is no meshagent package/module in nixpkgs; this is the gap this module
# fills. The prebuilt Linux binary is patchelf'd in ./package.nix. The agent
# expects its `.msh` identity file NEXT TO the executable and needs a WRITABLE
# datapath (it creates meshagent.db + a DAIPC socket), so the service stages the
# store binary + the sops-encrypted .msh into /var/lib/meshagent and runs there.
#
# CONSUMER MUST DECLARE the secret in their own flake (the .msh identity —
# server URL + MeshID + server cert hash — is enrollment-capable and must stay
# out of git) and point homelab.meshagent.mshFile at it.
{ config, lib, pkgs, ... }:
let
  meshagent = pkgs.callPackage ./package.nix { };
  datapath = "/var/lib/meshagent";
in
{
  imports = [ ../../options.nix ];

  systemd.services.meshagent = {
    description = "MeshCentral agent (self-manage this host via MeshCentral)";
    wantedBy = [ "multi-user.target" ];
    after = [ "network-online.target" ];
    wants = [ "network-online.target" ];
    serviceConfig = {
      Type = "simple";
      StateDirectory = "meshagent";
      WorkingDirectory = datapath;
      # Stage binary + .msh into the writable datapath each start. StartupType
      # is appended at start (systemd = 1); the other fields come from the
      # server-generated .msh.
      ExecStartPre = pkgs.writeShellScript "meshagent-stage" ''
        set -eu
        install -m0555 ${meshagent}/bin/meshagent ${datapath}/meshagent
        umask 077
        cat ${config.homelab.meshagent.mshFile} > ${datapath}/meshagent.msh
        echo "StartupType=1" >> ${datapath}/meshagent.msh
      '';
      ExecStart = "${datapath}/meshagent connect";
      Restart = "always";
      RestartSec = 10;
      # Runs as root — MeshCentral's model is full device management (the remote
      # terminal manages the host). Env gives the spawned remote-terminal shell a
      # working PATH + bash: THIS is the "confined shell" fix (the known NixOS
      # failure was a bare env with no bash/commands). Update prevention is by
      # version-match: the agent (1.1.59) == the server's bundled agent, so the
      # server never pushes a self-update.
      Environment = [
        "PATH=/run/current-system/sw/bin:/usr/bin:/bin"
        "SHELL=/run/current-system/sw/bin/bash"
        # X11 remote-desktop (KVM) library discovery. To decide it supports
        # remote desktop, the agent locates libX11/libXtst/libXext (+ Xfixes for
        # the cursor, xkbfile for the keyboard) via `ldconfig -p` then `ls /lib`
        # — BOTH empty on NixOS, so it reports "X11 support: false" and the
        # Desktop tab never appears. monitor-info.js's _check() honors these env
        # overrides; point them at the store .so files. LIB+TST+EXT gate the tab.
        "Location_X11LIB=${pkgs.xorg.libX11}/lib/libX11.so.6"
        "Location_X11TST=${pkgs.xorg.libXtst}/lib/libXtst.so.6"
        "Location_X11EXT=${pkgs.xorg.libXext}/lib/libXext.so.6"
        "Location_X11FIXES=${pkgs.xorg.libXfixes}/lib/libXfixes.so.3"
        "Location_X11KB=${pkgs.xorg.libxkbfile}/lib/libxkbfile.so.1"
      ];
    };
  };
}
