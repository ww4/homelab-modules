# ollama — run open-weight language models on this machine.
#
# The daemon listens on loopback only and is not given a vhost. Nothing here
# authenticates: ollama's API will do whatever it is asked by anything that
# can reach it, so the thing that reaches it is open-webui on the same host.
# A vhost would put an unauthenticated model API on the network.
#
# ⚠️ WHERE THE MEMORY GOES. A loaded model lives in the graphics card's
# memory when acceleration is on, and in system memory when it is not. The
# catalog figure for this module is the idle daemon, because the model is the
# reader's choice and can be anything from two gigabytes to forty. The
# installer only offers this kit on a machine with a card it has looked up,
# and says what that card can comfortably run.
#
# ⚠️ ACCELERATION IS NOT GUESSED, AND IT IS A PACKAGE, NOT A SETTING.
# `null` means the processor does the work, which is correct and slow.
# "cuda" and "rocm" pull large, vendor-specific builds, and the CUDA one is
# unfree; the library's base module already allows unfree packages, so that is
# not a new decision here, but it is one worth knowing you have made.
#
# nixpkgs used to take `services.ollama.acceleration` and now refuses it,
# asking for the matching package instead. The flake's own checks caught that
# the first time this module was written, which is what they are for.
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.ollama;
in
{
  services.ollama = {
    enable = true;
    host = "127.0.0.1";
    port = 11434;
    package =
      if cfg.acceleration == "cuda" then pkgs.ollama-cuda
      else if cfg.acceleration == "rocm" then pkgs.ollama-rocm
      else pkgs.ollama;
    # Pulled on first start, so a machine is useful without a second step.
    loadModels = cfg.models;
  };
}
