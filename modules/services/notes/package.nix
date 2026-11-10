# notes — the Keep-style card wall served by ./default.nix.
#
# Not in nixpkgs; this is the gap the module fills, so the application is
# vendored in ./app and built here. It holds no secrets: everything is
# configured by environment (see app/src/notes/config.py), and the module
# turns homelab.notes.* options into those variables.
#
# Why it exists at all: a markdown editor is the wrong front end for the way
# most people actually capture notes. The files are the part that works —
# flat markdown on disk, readable by anything — so this serves the same files
# as cards instead of as a file tree, with an offline PWA and phone gestures.
{ lib, python3Packages, librsvg }:

python3Packages.buildPythonApplication {
  pname = "notes";
  version = "0.1.0";
  pyproject = true;

  src = ./app;

  build-system = [ python3Packages.hatchling ];

  dependencies = with python3Packages; [
    fastapi
    uvicorn
    pydantic
    pydantic-settings
  ];

  # The manifest needs real PNG icons — Android's installer does not take SVG
  # for the home-screen icon — so render them from the single SVG at build
  # time rather than committing binaries that can drift from it.
  nativeBuildInputs = [ librsvg ];

  preBuild = ''
    rsvg-convert -w 192 -h 192 -o src/notes/static/icon-192.png src/notes/static/icon.svg
    rsvg-convert -w 512 -h 512 -o src/notes/static/icon-512.png src/notes/static/icon.svg
  '';

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # The traversal corpus is the security boundary of this service: it is
  # unauthenticated, and a note id is a client-supplied path. Running the
  # corpus at build time means a scope regression fails the deploy instead of
  # shipping. tests/test_store.py additionally pins the "don't reformat the owner's
  # files" promise — unknown frontmatter keys survive a write untouched.
  pytestFlags = [ "tests/" ];

  pythonImportsCheck = [ "notes" "notes.app" "notes.paths" "notes.store" ];

  # The front end is a single file shipped as package data. If hatchling's
  # include rules ever drop it the service still starts and then 500s on every
  # page load, so assert it landed rather than finding out at runtime.
  postInstall = ''
    static="$out/${python3Packages.python.sitePackages}/notes/static"
    for f in index.html sw.js service_worker.js manifest.webmanifest icon.svg icon-192.png icon-512.png; do
      if [ ! -s "$static/$f" ]; then
        echo "notes: $f missing from the wheel ($static/$f)" >&2
        exit 1
      fi
    done
    # A service worker that 404s leaves the app online-only and silent about
    # it, so the packaging is asserted rather than assumed.
  '';

  meta = with lib; {
    description = "Keep-style card wall over a folder of markdown notes";
    mainProgram = "notes";
    platforms = platforms.linux;
  };
}
