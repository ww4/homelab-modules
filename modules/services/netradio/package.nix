# The netradio glue: the playlist scanner and the wake/idle controller.
# Own file rather than a `let` in default.nix for the reason newsdesk's
# package.nix records — a let-bound derivation is invisible to sibling
# modules and to the interactive PATH.
#
# Dependencies: mutagen (tags), numpy + onnxruntime (the profiler's YAMNet
# classifier and pitch tracker), ffmpeg on PATH (the profiler decodes with
# it). The wake server and the DJ are stdlib. The tests run in checkPhase
# against fakes, so the encoder start/stop state machine, the DJ's queue
# logic and the profiler's decision rule cannot regress silently into a
# deploy; the model itself is exercised only at runtime.
{ python3, lib, makeWrapper, ffmpeg-headless }:

let
  pyDeps = ps: [ ps.mutagen ps.numpy ps.onnxruntime ps.pillow ];   # pillow: cover thumbnails for the station tiles
  pyEnv = python3.withPackages pyDeps;
in

python3.pkgs.buildPythonApplication {
  pname = "netradio";
  version = "1.0.0";
  format = "other";

  # Only the Python and its tests — NOT `./.`, which pulled default.nix and
  # web/ into the source and so rehashed the package (and restarted every unit
  # on deploy) for a change that could not affect a line of it.
  src = lib.fileset.toSource {
    root = ./.;
    fileset = lib.fileset.unions [ ./netradio ./tests ];
  };

  nativeBuildInputs = [ makeWrapper ];
  propagatedBuildInputs = pyDeps python3.pkgs;

  doCheck = true;
  checkPhase = ''
    runHook preCheck
    ${python3.interpreter} -m unittest discover -s tests -v
    runHook postCheck
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p $out/${python3.sitePackages}
    cp -r netradio $out/${python3.sitePackages}/
    makeWrapper ${pyEnv}/bin/python3 $out/bin/netradio \
      --add-flags "-m netradio.cli" \
      --prefix PYTHONPATH : "$out/${python3.sitePackages}" \
      --prefix PATH : "${lib.makeBinPath [ ffmpeg-headless ]}"
    runHook postInstall
  '';

  meta = with lib; {
    description = "Library radio stations: genre playlists, on-demand encoders, the DJ, the talk profiler";
    mainProgram = "netradio";
    platforms = platforms.linux;
  };
}
