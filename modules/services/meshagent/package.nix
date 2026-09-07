# MeshAgent — the endpoint agent that connects to a MeshCentral server.
# nixpkgs has no meshagent package, so we patchelf the prebuilt Linux binary
# that ships in the MeshCentral repo. The binary itself is dynamically linked
# only against glibc; the remote-desktop (KVM) feature dlopens X11 libs at
# runtime, so those go in runtimeDependencies (added to the RUNPATH).
{ stdenv, fetchurl, autoPatchelfHook, glibc, xorg }:
stdenv.mkDerivation (finalAttrs: {
  pname = "meshagent";
  version = "1.1.59";
  src = fetchurl {
    url = "https://github.com/Ylianst/MeshCentral/raw/${finalAttrs.version}/agents/meshagent_x86-64";
    hash = "sha256-RgrLs4sL2z0ifeZQELGjI/RI7BloYM5JecC4MUdj61Y=";
  };
  dontUnpack = true;
  nativeBuildInputs = [ autoPatchelfHook ];
  buildInputs = [ glibc stdenv.cc.cc.lib ];
  # KVM screen capture + input injection dlopen these at runtime: libXtst
  # (XTEST inject), libXdamage (screen-change detection), libXfixes (cursor),
  # plus core X11/Xext/Xrandr/Xinerama/Xi. autoPatchelfHook puts them on RUNPATH.
  runtimeDependencies = with xorg; [
    libX11 libXext libXtst libXfixes libXdamage libXrandr libXinerama libXi libXcursor
  ];
  installPhase = ''
    runHook preInstall
    install -Dm755 $src $out/bin/meshagent
    runHook postInstall
  '';
  meta.mainProgram = "meshagent";
})
