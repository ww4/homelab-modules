# YCast — a self-hosted stand-in for the vTuner internet-radio directory that
# Yamaha (and Denon/Marantz/Onkyo…) network receivers of the 2011–2017 era
# were built against. vTuner went pay-to-use; the receiver's "Net Radio"
# input just does a DNS lookup for radioyamaha.vtuner.com and speaks plain
# HTTP, so pointing that name at this instead brings the input back.
#
# Not in nixpkgs. Upstream is dormant (last release 2020) but the protocol it
# emulates is frozen, and it is 300 lines of Flask; the fork tree is all
# device-confirmation reports and Dockerfiles. Pinned to the release tag.
#
# Upstream ships no console entry point (`python -m ycast`); the bin/ycast
# wrapper below is ours so a systemd ExecStart can name a plain executable.
{ lib, python3Packages, fetchFromGitHub }:

python3Packages.buildPythonApplication rec {
  pname = "ycast";
  version = "1.1.0";
  pyproject = true;  # setup.py-only upstream; setuptools builds the wheel from it

  src = fetchFromGitHub {
    owner = "milaq";
    repo = "YCast";
    tag = version;
    hash = "sha256-KMwKiQgWfNjBQ7F1gG+DRYQ9XfH4tkS+qZgd6SMaoOs=";
  };

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    flask
    requests
    pyyaml
    pillow
  ];

  # A python-shebang script in $out/bin gets wrapped with the dependency
  # PYTHONPATH by buildPythonApplication's fixup, same as a real entry point.
  postInstall = ''
    mkdir -p $out/bin
    cat > $out/bin/ycast <<EOF
    #!${python3Packages.python.interpreter}
    from ycast.__main__ import launch_server
    launch_server()
    EOF
    chmod +x $out/bin/ycast
  '';

  pythonImportsCheck = [ "ycast" "ycast.server" ];

  meta = with lib; {
    description = "Self hosted vTuner internet radio service emulation";
    homepage = "https://github.com/milaq/YCast";
    license = licenses.gpl3Only;
    mainProgram = "ycast";
  };
}
