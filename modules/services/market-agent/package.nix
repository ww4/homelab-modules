# market-agent — searches Facebook Marketplace against written briefs and
# ranks what it finds. Not in nixpkgs; the application is vendored in ./app
# and built here. It holds no secrets: configuration is environment variables
# (see app/.env.example and app/src/market_agent/config.py), which
# ./default.nix sets from homelab.marketAgent.*.
{ lib, python3Packages }:

python3Packages.buildPythonApplication {
  pname = "market-agent";
  version = "0.1.0";
  pyproject = true;

  src = ./app;

  build-system = [ python3Packages.setuptools ];

  dependencies = with python3Packages; [
    httpx
    pydantic
    pillow
    anthropic # only used when homelab.marketAgent.judge = "api"
  ];

  nativeCheckInputs = [ python3Packages.pytestCheckHook ];

  # The parsing and checking tests carry the listings that once slipped
  # through (a labelled "Mileage: 193,000", a car excluded on an invented
  # driving distance). Running them at build time means a regression fails
  # the deploy instead of quietly dropping cars from the page.
  pytestFlags = [ "tests/" ];

  pythonImportsCheck = [ "market_agent" "market_agent.cli" "market_agent.vehicles.knowledge" ];

  # The facts table and the example briefs are package data. If setuptools
  # drops them the service still starts and then fails on the first brief, so
  # assert they landed.
  postInstall = ''
    pkg="$out/${python3Packages.python.sitePackages}/market_agent"
    for f in data/vehicles.toml data/vehicle_configs.json examples/winter-commuter.toml examples/general.toml; do
      if [ ! -s "$pkg/$f" ]; then
        echo "market-agent: $f missing from the wheel" >&2
        exit 1
      fi
    done
  '';

  meta = with lib; {
    description = "Facebook Marketplace search against written briefs, with checks and Claude scoring";
    mainProgram = "market-agent";
    platforms = platforms.linux;
  };
}
