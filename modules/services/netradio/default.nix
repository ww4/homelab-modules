# Radio stations built from a music library: genre playlists, on-demand
# encoders, a DJ that talks between the songs, and a web remote. Optionally a
# stand-in for vTuner, so an older Yamaha receiver's "Net Radio" input works
# again after vTuner went pay-to-use.
#
# Everything site-specific is an option — see `options.homelab.netradio`
# below. The smallest useful configuration is a domain and a library:
#
#     homelab.netradio = {
#       enable = true;
#       domain = "radio.example.com";
#       libraryRoots = [ "/srv/music" ];
#     };
#
# That gives the stations, the page and the admin UI. The receiver stand-in
# (`vtuner.*`), this machine's own sound card (`speaker.*`), receiver controls
# (`receiver.*`), the DJ's voice (`tts.*`) and agent-written feed rules
# (`compile.*`) are each off until asked for.
#
# --- the vTuner stand-in, when enabled ---------------------------------------
# How the receiver finds radio at all: it looks up radioyamaha.vtuner.com,
# speaks plain HTTP on port 80 to whatever answers, and plays the stream URLs
# it is handed. So:
#
#   receiver ──DNS──▶ Blocky: radioyamaha.vtuner.com = this box (LAN IP)
#            ──:80──▶ nginx vhost radioyamaha.vtuner.com
#                       /            → YCast (vTuner emulation, 127.0.0.1:8010)
#                                      • Radiobrowser: the community index
#                                        (genres / countries / popular)
#                                      • My Stations: the library stations
#                                        below + a few internet radio
#                       /radio/X.mp3 → auth_request → netradio-wake (:8011)
#                                      starts Liquidsoap output X, waits for
#                                      Icecast to list it, then nginx proxies
#                                      the listener to Icecast (127.0.0.1:8020)
#
# Library stations are genre-tag playlists, era-filtered by the profiler's
# audio-quality buckets (netradio-playlists, nightly), fed
# to Icecast by Liquidsoap, sequenced by a DJ (netradio-dj) that queues the
# tracks and every 3-4 of them a Kokoro-voiced break naming what just played
# and what is next. A profiler (netradio-profile, nightly) listens to each
# track once so spoken intros/stage talk cut as their own tracks stay off the
# stations, and songs that start or end in chatter aren't crossfaded over.
# Encoders are ON DEMAND: a station's MP3 encoder
# runs only while someone is listening (+5 min grace), so nine stations idle
# at zero CPU. Nothing here needs a port opened — the receiver only ever
# touches port 80, which is already open and LAN/Tailscale-gated by
# nginx-access.nix. Icecast, YCast and the wake server are loopback-only.
#
# Known limit: the receiver cannot do TLS, and YCast rewrites https:// stream
# URLs to http://. A station that serves HTTPS only will fail to play; the
# Radiobrowser index still has plenty that don't.
#
# Stations, specialty feeds and the schedule are RUNTIME config
# (${stateDir}/config/, seeded once from this file) edited on
# <radio host>/admin. Two kinds of station: CURATED (a broad base
# rule plus a schedule of segments — themed hours drawn from specialty feeds
# or artist spotlights with similar artists mixed in; `auto` slots are the
# DJ's own pick for the day) and SPECIALTY (one feed, listenable on its own).
# A new feed's rule is written by `claude -p` from its description (the
# apply path unit runs it as the claude user); the DJ announces segments as
# they start and the day's schedule at breaks, radio-style.
#
# The page at the configured domain (web/) shows what's playing, the last
# few played, what the DJ queued next, listener counts and a visualiser, and
# serves stations.m3u / .pls for radio apps. Every station also has a
# "-lo" mount at 96 kbps (cellular). Nothing here needs a port opened.
#
# Ops:
#   sudo cat /var/lib/netradio/credentials.env      Icecast passwords (generated)
#   <radio host>/admin                              feeds, schedule, stations (the config)
#   /var/lib/netradio/config/                       feeds.json stations.json schedule.json picks.json
#   journalctl -u netradio-apply                    what the admin's requests did (rescan / restart / compile)
#   systemctl start netradio-playlists              rescan the library now
#   systemctl start netradio-profile                profile new tracks now (first run: hours)
#   /var/lib/netradio/profile/profile-report.txt    what the profiler flagged; fix in profile-overrides.json beside it
#   journalctl -u netradio-wake                     which station started/stopped
#   http://127.0.0.1:8020/status.xsl                what Icecast is serving
{ config, lib, pkgs, ... }:

let
  cfg = config.homelab.netradio;

  ycast = pkgs.callPackage ./ycast { };
  netradio = pkgs.callPackage ./package.nix { };

  # The vTuner names the receiver resolves. Both point at this box.
  vtunerHost = cfg.vtuner.host;
  vtunerBackup = cfg.vtuner.backupHost;
  # The LAN address nginx is on. Where Blocky also answers for the site's own
  # names — reading it back out of config.services.blocky.settings while also
  # adding to it is an infinite recursion, so it is a value here, not derived.
  lanIP = cfg.vtuner.address;

  ycastPort = cfg.ports.ycast;
  wakePort = cfg.ports.wake;
  adminPort = cfg.ports.admin;
  speakerPort = cfg.ports.speaker;
  speakerCard = cfg.speaker.card;
  speakerDefaultMount = cfg.speaker.defaultMount;
  speakerStartVolume = cfg.speaker.startVolume;
  icecastPort = cfg.ports.icecast;

  user = cfg.user;
  stateDir = cfg.stateDir;
  credsEnv = "${stateDir}/credentials.env";
  playlistDir = "${stateDir}/playlists";
  cacheDir = "${stateDir}/cache";
  tagCache = "${cacheDir}/tags.json";
  djDir = "${stateDir}/dj";
  nowDir = "${stateDir}/now";   # what's playing / last played / up next, per station — the page reads it via nginx
  # Its own subdir, like playlists/ and dj/: ${stateDir} itself is root-owned
  # (it holds credentials.env), so the netradio user cannot create the
  # profile.json.tmp the atomic save needs there (2026-09-16 first-run failure).
  ambientDir = "${stateDir}/ambient";   # rain and the like: the fixed stations' beds, never in the music library
  profileDir = "${stateDir}/profile";
  profileJson = "${profileDir}/profile.json";
  profileOverrides = "${profileDir}/profile-overrides.json";   # {path: "talk"|"music"}, hand-edited
  profileReport = "${profileDir}/profile-report.txt";

  yamnet = pkgs.callPackage ./yamnet.nix { };

  # The profiling can be offloaded to a faster box (`netradio profile-server`,
  # one per remote): the local workers try each remote first and measure here
  # when nobody answers, so a remote that is switched off is only slower.
  profileRemotes = cfg.profile.remotes;
  profileWorkers = cfg.profile.workers;

  runDir = "/run/netradio";
  liqSocket = "${runDir}/liquidsoap.sock";

  # The DJ's voice: a Kokoro-compatible TTS endpoint (several are tried in
  # order, so a fast remote can lead and a local container back it up).
  kokoroVoice = cfg.tts.voice;
  kokoroUrls = cfg.tts.urls;

  # Where the music is: every root the scanner walks.
  libraryRoots = cfg.libraryRoots;
  # Jellyfin is only usable when it is enabled AND a key was handed over.
  jellyfinReady = cfg.jellyfin.enable && cfg.jellyfin.keyFile != null;
  # Pictures for stations that have no album art to draw a mosaic from:
  # <mount>.jpg here becomes that station's tile. Runtime, not the store,
  # so one can be dropped in without a rebuild.
  stationArtDir = "${configDir}/art";
  # a station picture keeps its own extension, so a PNG is served as a PNG
  extOf = src: let parts = lib.splitString "." (builtins.baseNameOf src);
               in if builtins.length parts > 1 then lib.last parts else "jpg";

  # The group that can read those roots, for the units that open the files.
  libraryGroups = lib.optional (cfg.libraryGroup != null) cfg.libraryGroup;

  # --- the runtime config and its seeds ---------------------------------------
  # Stations, specialty feeds and the schedule are RUNTIME config under
  # ${stateDir}/config/, edited on <radio host>/admin; the scanner,
  # the DJ, Liquidsoap (script rendered at start), YCast (menu file) and the
  # wake service all read it. These seeds are copied in ONCE, on a box that
  # has no config yet — the owner's edits are never overwritten by a deploy.
  #
  # A feed's rule (feeds.py): artists / genres / instruments (YAMNet means)
  # / era, clauses AND-ed. A station's base is the same shape. Families are
  # the compatibility vocabulary (config.py): the admin page offers a
  # station only feeds and artists whose families overlap its own.
  configDir = "${stateDir}/config";
  poolsDir = "${stateDir}/pools";

  noShellac = { exclude = [ "shellac" ]; };
  # The seeds are OPTION DEFAULTS, copied in once on a box with no config yet
  # and never again — so they shape a fresh install and nothing else. Someone
  # with a different record collection sets homelab.netradio.seed.* (or edits
  # them on the admin page after the first boot).
  seedFeeds = cfg.seed.feeds;
  seedStations = cfg.seed.stations;
  seedSchedule = cfg.seed.schedule;
  internetRadio = cfg.internetRadio ++ cfg.extraInternetRadio;
  # What the receiver browsing YCast can decode. The `room` device is the one
  # with a menu to walk, so its declaration is what filters that menu; the web
  # page is a browser and is offered everything.
  receiverCodecs = if cfg.devices ? room then cfg.devices.room.codecs else [ "mp3" ];
  radioHost = cfg.domain;

  defaultFeeds = {
    brother-duets = {
      shellac = true;   # 1930s-40s sides are the point of this feed
      title = "Brother Duets";
      description = "Close-harmony duets by brothers and brotherly pairs: the Louvins, Delmores, Blue Sky Boys, Stanleys, Monroe Brothers, Lilly Brothers, Whitsteins, Bailes Brothers, Osbornes.";
      family = [ "bluegrass" "country" ]; status = "ready"; count = 0;
      note = "seeded from the library's artist list";
      rule = { artists = [ "Stanley Brothers" "Louvin Brothers" "Blue Sky Boys" "Delmore Brothers" "Lilly Brothers" "Whitstein Brothers"
                           "Bailes Brothers" "Osborne Brothers" "Bill and Charlie Monroe" "Monroe Brothers" "Jim & Jesse"
                           "Bobby Osborne And Jesse McReynolds" "Jesse McReynolds & Charles Whitstein" "Everly Brothers" ];
               genres = [ "brother duets" ]; };
    };
    western-swing = {
      shellac = true;   # 1930s-40s sides are the point of this feed
      title = "Western Swing";
      description = "Dance-hall country with jazz in it: Bob Wills and the Texas Playboys, Milton Brown, Spade Cooley, and the revivalists — Asleep at the Wheel, Hot Club of Cowtown.";
      family = [ "country" ]; status = "ready"; count = 0; note = "seeded; thin until the metadata pass adds Last.fm genres";
      rule = { artists = [ "Bob Wills" "Milton Brown" "Spade Cooley" "Asleep At The Wheel" "Hot Club of Cowtown" "Light Crust Doughboys" "Tex Williams" "Hank Thompson" ];
               genres = [ "western swing" ]; };
    };
    honky-tonk = {
      shellac = true;   # 1930s-40s sides are the point of this feed
      title = "Honky Tonk";
      description = "Barroom country of the 1950s and 60s and its keepers: Hank Williams, Lefty Frizzell, Ernest Tubb, Webb Pierce, Ray Price, George Jones, Faron Young, Johnny Bush, Gary Stewart, Moe Bandy.";
      family = [ "country" ]; status = "ready"; count = 0; note = "seeded from the library's artist list";
      rule = { artists = [ "Hank Williams" "Lefty Frizzell" "Ernest Tubb" "Webb Pierce" "Ray Price" "George Jones" "Faron Young"
                           "Johnny Bush" "Gary Stewart" "Moe Bandy" "Hank Thompson" "Johnny Paycheck" "Vern Gosdin" ];
               genres = [ "honky tonk" ]; };
    };
    old-time-fiddle = {
      title = "Old-Time Fiddle";
      description = "Fiddle-led old-time tunes: Tommy Jarrell, Art Stamper, Benton Flippen, Bruce Molsky, Clyde Davenport — the tune, not the song.";
      family = [ "bluegrass" "folk" ]; status = "ready"; count = 0; note = "seeded: old-time tag AND a fiddle in the mix";
      rule = { genres = [ "old time" "oldtime" "fiddle" ]; artists = [ "Tommy Jarrell" "Art Stamper" "Benton Flippen" "Bruce Molsky" "Clyde Davenport" ];
               instruments = { violin = [ 0.15 null ]; }; };
    };
    banjo-instrumentals = {
      title = "Banjo Instrumentals";
      description = "Banjo out front and nobody singing: bluegrass and clawhammer instrumentals.";
      family = [ "bluegrass" "folk" ]; status = "ready"; count = 0; note = "seeded from the audio measurements";
      rule = { instruments = { banjo = [ 0.3 null ]; singing = [ null 0.05 ]; }; };
    };
    a-cappella = {
      title = "A Cappella & Lined-Out Singing";
      description = "Unaccompanied singing: Old Regular Baptist lined-out hymnody, ballad singers, shape-note and quartet singing without instruments.";
      family = [ "gospel" "folk" ]; status = "ready"; count = 0; note = "seeded from the audio measurements";
      rule = { instruments = { a_capella = [ 0.15 null ]; }; };
    };
    cajun-zydeco = {
      title = "Cajun & Zydeco";
      description = "Louisiana dance music: Cajun two-steps and waltzes, zydeco.";
      family = [ "folk" "country" ]; status = "ready"; count = 0; note = "seeded from the genre tags";
      rule = { genres = [ "cajun" "zydeco" ]; };
    };
  };
  specialtyStation = id: f: { mount = id; name = f.title; kind = "specialty"; feed = id; inherit (f) family; };
  defaultStations = [
    { mount = "all";        name = "Everything";           kind = "curated"; family = [ "any" ];                base = { all = true; }; }
    { mount = "scratchy";   name = "Old Scratchy Records"; kind = "curated"; family = [ "any" ];                base = { all = true; era = { only = [ "shellac" ]; }; }; }
    { mount = "bluegrass";  name = "Bluegrass & Old-Time"; kind = "curated"; family = [ "bluegrass" "folk" ];   base = { genres = [ "bluegrass" "old time" "oldtime" "newgrass" "string band" "brother duets" "appalachian" ]; era = noShellac; }; }
    { mount = "country";    name = "Classic Country";      kind = "curated"; family = [ "country" ];            base = { genres = [ "country" "western swing" "honky tonk" "western" "cowboy" ]; exclude_genres = [ "bluegrass" "old time" "oldtime" "newgrass" "string band" "brother duets" "appalachian" ]; era = noShellac; }; }
    { mount = "folk";       name = "Folk";                 kind = "curated"; family = [ "folk" "bluegrass" ];   base = { genres = [ "folk" "singer songwriter" "celtic" "traditional" "americana" "acoustic" "irish" "cajun" "zydeco" ]; era = noShellac; }; }
    { mount = "rock";       name = "Rock";                 kind = "curated"; family = [ "rock" ];               base = { genres = [ "rock" "alternative" "pop" "punk" "metal" "indie" "new wave" ]; era = noShellac; }; }
    { mount = "blues";      name = "Blues";                kind = "curated"; family = [ "blues-jazz" ];         base = { genres = [ "blues" ]; era = noShellac; }; }
    { mount = "jazz";       name = "Jazz";                 kind = "curated"; family = [ "blues-jazz" ];         base = { genres = [ "jazz" "big band" "swing" "fusion" "bebop" "dixieland" ]; era = noShellac; }; }
    { mount = "soul";       name = "Soul & R&B";           kind = "curated"; family = [ "blues-jazz" ];         base = { genres = [ "soul and r&b" "soul" "r&b" "funk" "motown" ]; era = noShellac; }; }
    { mount = "gospel";     name = "Gospel";               kind = "curated"; family = [ "gospel" ];             base = { genres = [ "gospel" "religious" "christian" "hymns" "sacred" "spiritual" ]; }; }  # leak-scan-ok: a genre word, not a name
    # "opera" is a trap: genre tags are matched on WORD boundaries, so a rock
    # opera tagged "Rock; Rock Opera; Classic Rock" matches it, and Quadrophenia
    # turned up on the classical station (2026-09-30). Excluding rock
    # outright is the fix rather than dropping "opera", which would cost the
    # station every actual opera in the library.
    { mount = "classical";  name = "Classical";            kind = "curated"; family = [ "classical" ];          base = { genres = [ "classical" "baroque" "orchestral" "opera" "chamber" ]; exclude_genres = [ "rock" "metal" "punk" ]; }; }
    { mount = "holiday";    name = "Holiday";              kind = "curated"; family = [ "holiday" ];            base = { genres = [ "holiday" "christmas" "xmas" ]; }; }  # leak-scan-ok: a genre word, not a name
    # every specialty feed is also listenable as its own station
  ] ++ lib.mapAttrsToList specialtyStation cfg.seed.feeds;
  defaultSchedule = [
    { id = "country-sat-swing";  station = "country";   name = "Western Swing Hour";     kind = "feed"; feed = "western-swing";   days = [ "sat" ]; start = "10:00"; minutes = 60; }
    { id = "country-happy-hour"; station = "country";   name = "Honky Tonk Happy Hour";  kind = "feed"; feed = "honky-tonk";      days = "weekdays"; start = "17:00"; minutes = 60; }
    { id = "country-spotlight";  station = "country";   kind = "auto"; like = "artist";  days = "daily"; start = "20:00"; minutes = 60; }
    { id = "bluegrass-duets";    station = "bluegrass"; name = "Brother Duets Hour";     kind = "feed"; feed = "brother-duets";   days = [ "sun" ]; start = "09:00"; minutes = 60; }
    { id = "bluegrass-banjo";    station = "bluegrass"; name = "Banjo Hour";             kind = "feed"; feed = "banjo-instrumentals"; days = [ "wed" ]; start = "19:00"; minutes = 60; }
    { id = "folk-fiddle";        station = "folk";      name = "Old-Time Fiddle Hour";   kind = "feed"; feed = "old-time-fiddle"; days = [ "sat" ]; start = "09:00"; minutes = 60; }
  ];
  seedFile = name: data: pkgs.writeText "netradio-seed-${name}" (builtins.toJSON data);
  seeds = { feeds = seedFile "feeds.json" seedFeeds; stations = seedFile "stations.json" seedStations; schedule = seedFile "schedule.json" seedSchedule; };

  # A few known-good plain-HTTP streams so the input has something to play
  # before any browsing (all verified 2026-09-15). Discovery is Radiobrowser's
  # job; this is not a curated list.
  defaultInternetRadio = [
    { name = "NPR News";            url = "http://npr-ice.streamguys1.com/live.mp3"; }
    { name = "WFPK Louisville";     url = "http://lpm.streamguys1.com/wfpk-web"; }
    { name = "WUKY Lexington";      url = "http://wuky.streamguys1.com/wuky"; }
    { name = "Radio Paradise";      url = "http://stream.radioparadise.com/mp3-128"; }
    { name = "KEXP Seattle";        url = "http://kexp-mp3-128.streamguys1.com/kexp128.mp3"; }
    { name = "WWOZ New Orleans";    url = "http://wwoz-sc.streamguys1.com/wwoz-hi.mp3"; }
    { name = "WQXR Classical";      url = "http://stream.wqxr.org/wqxr"; }
    { name = "SomaFM Folk Forward"; url = "http://ice1.somafm.com/folkfwd-128-mp3"; }
    { name = "SomaFM Boot Liquor";  url = "http://ice1.somafm.com/bootliquor-128-mp3"; }
    { name = "SomaFM Groove Salad"; url = "http://ice1.somafm.com/groovesalad-128-mp3"; }
    { name = "SomaFM Secret Agent"; url = "http://ice1.somafm.com/secretagent-128-mp3"; }
  ];
  # An outside station the receiver cannot decode is not dropped: it gets a
  # RELAY — the same stream re-encoded to MP3 on its own mount, started on
  # demand and stopped when the last listener leaves, like every other mount
  # here. The listener sees one list; the codec is plumbing, not a category
  # (2026-09-28). About 4% of a core while somebody is listening.
  slugOf = name:
    let
      allowed = lib.stringToCharacters "abcdefghijklmnopqrstuvwxyz0123456789";
      mapped = lib.concatMapStrings (c: if lib.elem c allowed then c else "-")
                 (lib.stringToCharacters (lib.toLower name));
    in "ir-" + lib.concatStringsSep "-" (lib.filter (x: x != "") (lib.splitString "-" mapped));

  needsRelay = e: !(lib.elem (e.codec or "mp3") receiverCodecs);
  relays = map (e: { mount = slugOf e.name; inherit (e) name url; })
               (lib.filter needsRelay internetRadio);
  relaysJson = pkgs.writeText "netradio-relays.json" (builtins.toJSON relays);

  # the menu needs to know which entry has a relay, so it can point at it
  internetRadioJson = pkgs.writeText "netradio-internet-radio.json" (builtins.toJSON
    (map (e: if needsRelay e then e // { relay = slugOf e.name; } else e) internetRadio));
  # A stations.yml with only the Internet Radio, for YCast to start on before
  # the scanner has written the real one (JSON is valid YAML).
  ycastSeedYaml = pkgs.writeText "netradio-stations-seed.yml"
    (builtins.toJSON { "Internet Radio" = builtins.listToAttrs (map (p: { name = p.name; value = p.url; }) internetRadio); });

  # Library streams, shared by both vhosts. auth_request wakes the encoder
  # first; then the listener is proxied straight to Icecast, unbuffered, with
  # the ICY metadata headers passing both ways so the display shows the track.
  streamLocations = {
    "/radio/" = {
      proxyPass = "http://127.0.0.1:${toString icecastPort}/";
      extraConfig = ''
        auth_request /_wake;
        proxy_http_version 1.1;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 1h;
        proxy_set_header Icy-MetaData $http_icy_metadata;
      '';
    };
    "= /_wake" = {
      extraConfig = ''
        internal;
        proxy_pass http://127.0.0.1:${toString wakePort}/wake;
        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
        proxy_set_header X-Original-URI $request_uri;
        proxy_read_timeout 20s;
      '';
    };
  };

  # The radio page and the admin page (web/): Vue 3 (global build, no
  # bundler), vendored here by hash — the vhost is
  # Tailscale-only, so nothing loads from a CDN at runtime. Everything
  # station-shaped the pages need is RUNTIME data (the scanner's now/ files
  # and the admin API); nothing here depends on the station list.
  vueJs = pkgs.fetchurl {
    url = "https://cdn.jsdelivr.net/npm/vue@3.5.13/dist/vue.global.prod.js";
    hash = "sha256-xFm6fMjbZcmCWJ+l1kx/9HiHfo5bD9dWgyB87GpOieg=";
  };
  # The names the page puts on its three playback targets. Written at build
  # time rather than hardcoded in app.js, so the page does not say "Kitchen
  # speakers" on somebody else's box.
  # Every device, in picker order, with its base path and what it can do. The
  # page reads this instead of knowing any device's name.
  deviceList = map (id: {
    inherit id;
    inherit (cfg.devices.${id}) name capabilities statePath order;
    base = "device/${id}";
  }) (lib.sort (a: b: cfg.devices.${a}.order < cfg.devices.${b}.order)
               (lib.attrNames cfg.devices));

  siteJson = pkgs.writeText "netradio-site.json" (builtins.toJSON {
    title = cfg.title;
    devices = deviceList;
    # the current page still reads these; the device list replaces them when
    # the page is refactored to loop rather than branch
    localName = cfg.speaker.label;
    localMount = cfg.speaker.defaultMount;
    roomName = cfg.receiver.label;
    tvInput = cfg.receiver.tvInput;
    # A build id for the PAGES. `./web` is content-addressed, so its store path
    # name changes exactly when a page asset changes and not otherwise.
    #
    # An open tab keeps the JavaScript it loaded, and this page is deliberately
    # built never to reload — that is what keeps the stream alive across a view
    # change. So a deploy is invisible to a tab that is already open, and a fix
    # that shipped correctly can look like it did not: "I just merged that, and
    # tested, and it still didn't gate" (2026-11-02 — the page was right,
    # his tab was four minutes older than it). The page compares this against
    # what it loaded with and offers a reload.
    build = builtins.baseNameOf "${./web}";
    hasLocal = cfg.speaker.enable;
    hasRoom = cfg.receiver.enable;
    hasRoku = cfg.roku.enable && cfg.roku.apiUrl != "";
    rokuName = cfg.roku.label;
    bedMount = cfg.bedMount;
  });

  # Building the document root also CHECKS it. The pages are plain files with no
  # build step, so a template expression naming something out of scope fails
  # only when a finger lands on it, in a browser, silently — the speaker volume
  # slider called a module-scope helper and threw on every drag (2026-09-26).
  # tests/test_web.py catches that class. It cannot run inside the Python
  # package's build, because web/ is deliberately not part of that source; here
  # it can, and this derivation is nginx's document root, so nothing deploys
  # without it passing.
  radioWeb = pkgs.runCommand "netradio-web" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    NETRADIO_WEB=${./web} NETRADIO_NIX=${./default.nix} \
      python3 -m unittest discover -s ${./tests} -t ${./tests} -p 'test_web.py' -v
    # …and the check that every flag this file puts on a command line is one the
    # command declares. Nothing else catches that: argparse rejects an unknown
    # flag at RUNTIME, so #351 built green and then failed the deploy with
    # status 4 (2026-09-27).
    NETRADIO_NIX=${./default.nix} NETRADIO_PKG=${./netradio} \
      python3 -m unittest discover -s ${./tests} -t ${./tests} -p 'test_unit_flags.py' -v
    # …and that Liquidsoap accepts the script its encoders run on. That script
    # is rendered at SERVICE START, so until now a typo in the template was
    # caught by liquidsoap itself — with every station off the air at once.
    # --check parses and type-checks and opens no outputs. liquidsoap is
    # already in this module's closure, so this costs the check and nothing else.
    # This one has to IMPORT the package to render, where the two above only
    # read files, so the store path needs a directory actually called netradio.
    mkdir -p pkgroot && ln -s ${./netradio} pkgroot/netradio
    PYTHONPATH=$PWD/pkgroot NETRADIO_LIQUIDSOAP=${pkgs.liquidsoap}/bin/liquidsoap \
      python3 -m unittest discover -s ${./tests} -t ${./tests} -p 'test_liq.py' -v

    mkdir -p $out/admin $out/vendor
    cp ${siteJson} $out/site.json
    cp ${./web}/index.html ${./web}/app.js ${./web}/remote.css ${./web}/radio.js ${./web}/tokens.css ${./web}/shared.js $out/   # radio.js: fetch helpers; shared.js: the options both pages mix in; tokens.css: the palette
    cp ${./web}/desktop.html ${./web}/desktop.js ${./web}/desktop.css $out/   # the wide-screen page (the remote redirects there)
    cp ${./web}/remote-panel.html ${./web}/remote-panel.css $out/   # the remote: one component, both pages mount it
    cp ${./web}/ask-panel.html ${./web}/dialog.css $out/   # the in-page question + the new-build strip, both pages
    cp ${./web}/manifest.webmanifest ${./web}/sw.js ${./web}/icon.svg ${./web}/icon-192.png ${./web}/icon-512.png $out/   # the PWA
    cp ${./web/admin}/index.html ${./web/admin}/admin.js ${./web/admin}/admin.css $out/admin/
    cp ${./web/admin}/panel.html $out/admin/   # the admin markup, fetched at runtime by whichever page mounts it
    cp ${vueJs} $out/vendor/vue.global.prod.js
  '';

  # YCast's menu (config/stations.yml) and the Liquidsoap script
  # (/run/netradio/netradio.liq) are rendered from the runtime station list:
  # the scanner writes the first, `netradio liq` the second at service start.
  liqScript = "${runDir}/netradio.liq";

  # Icecast config, rendered at start with the generated passwords. The
  # nixpkgs module takes the password as a Nix string, which would put it in
  # the store; this keeps it in the root-only env file.
  icecastTemplate = pkgs.writeText "icecast.xml.in" ''
    <?xml version="1.0"?>
    <icecast>
      <location>${config.networking.hostName}</location>
      <admin>root@localhost</admin>
      <hostname>127.0.0.1</hostname>
      <limits>
        <clients>32</clients>
        <sources>64</sources>   <!-- two encodes per station; the list is runtime config -->
        <queue-size>524288</queue-size>
        <client-timeout>30</client-timeout>
        <header-timeout>15</header-timeout>
        <source-timeout>10</source-timeout>
        <burst-size>65535</burst-size>
      </limits>
      <authentication>
        <source-password>@ICECAST_SOURCE_PASSWORD@</source-password>
        <relay-password>@ICECAST_SOURCE_PASSWORD@</relay-password>
        <admin-user>admin</admin-user>
        <admin-password>@ICECAST_ADMIN_PASSWORD@</admin-password>
      </authentication>
      <listen-socket>
        <port>${toString icecastPort}</port>
        <bind-address>127.0.0.1</bind-address>
      </listen-socket>
      <paths>
        <logdir>/var/log/icecast</logdir>
        <webroot>${pkgs.icecast}/share/icecast/web</webroot>
        <adminroot>${pkgs.icecast}/share/icecast/admin</adminroot>
        <alias source="/" dest="/status.xsl"/>
        <mime-types>${pkgs.mailcap}/etc/mime.types</mime-types>
      </paths>
      <logging>
        <accesslog>access.log</accesslog>
        <errorlog>error.log</errorlog>
        <loglevel>2</loglevel>
        <logsize>10000</logsize>
      </logging>
      <security>
        <chroot>0</chroot>
      </security>
    </icecast>
  '';

  hardening = {
    NoNewPrivileges = true;
    PrivateTmp = true;
    ProtectSystem = "strict";
    ProtectHome = true;
    ProtectKernelTunables = true;
    ProtectControlGroups = true;
    RestrictSUIDSGID = true;
  };
in
{
  options.homelab.netradio = {
    enable = lib.mkEnableOption "library radio stations (Icecast + Liquidsoap) with a DJ and a web remote";

    domain = lib.mkOption {
      type = lib.types.str;
      example = "radio.example.com";
      description = ''
        The name the page and the streams are served under. This vhost is
        expected to be access-gated by whatever guards the rest of the site —
        nothing here opens a port.
      '';
    };

    title = lib.mkOption {
      type = lib.types.str;
      default = "Radio";
      description = "What the page calls itself.";
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "netradio";
      description = "The system user every unit runs as.";
    };

    stateDir = lib.mkOption {
      type = lib.types.path;
      default = "/var/lib/netradio";
      description = ''
        Playlists, the runtime config, the DJ's rendered breaks, the profile
        and the generated Icecast passwords. Survives a rebuild; the seeds
        below are copied in only where a file does not already exist.
      '';
    };

    stationArt = lib.mkOption {
      type = lib.types.attrsOf lib.types.path;
      default = { };
      example = lib.literalExpression ''
        {
          rain = ./art/rain.jpg;
          rainymood = ./art/rainymood.jpg;
        }
      '';
      description = ''
        A picture for a station, by mount. Stations whose tile is otherwise a
        mosaic of their own album covers need nothing here; this is for the
        ones that have no covers to draw on — an ambient rain bed, or a station
        that is the whole library rather than a handful of artists.

        The file is linked into the runtime art directory and becomes that
        station's tile everywhere the mosaic would have been. No image ships
        with this module: pictures are a matter of taste and of whose rights
        they are, so they come from your own configuration.
      '';
    };

    bedMount = lib.mkOption {
      type = lib.types.str;
      default = "";
      example = "rainymood";
      description = ''
        A station to offer as a BED underneath whatever else is playing: rain,
        usually. The page gets a small panel with its own on/off and its own
        level, and mixes it locally as a second stream.

        It is mixed at the listener, not at the station, on purpose. Mixing it
        into the broadcast would put rain under everyone who tuned in —
        including a receiver that cannot turn it off — and one level cannot
        suit both a soft track and a loud one. As a second stream each listener
        chooses, and sets it where they want it.

        Empty disables the panel. Name a mount that is a seamless loop rather
        than a variety station: it is a bed, and it should not draw attention.
      '';
    };

    libraryRoots = lib.mkOption {
      type = lib.types.listOf lib.types.path;
      default = [ ];
      example = [ "/srv/music" ];
      description = ''
        Every directory the scanner walks for audio files. May be left empty
        when `jellyfin.discoverRoots` is on and Jellyfin knows where the music
        is; anything named here is scanned as well as what Jellyfin reports.
      '';
    };

    libraryGroup = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "media";
      description = ''
        A group that can read `libraryRoots`, added to the units that touch
        the files. Leave null when the library is world-readable.
      '';
    };

    requiresMounts = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = [ "mnt-music.mount" ];
      description = ''
        Mount units the library lives on, ordered before the scanner and the
        encoders so a boot does not scan an empty mountpoint.
      '';
    };

    tls = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Serve the page over HTTPS with an ACME certificate.";
    };

    acmeRoot = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      description = ''
        The ACME webroot for HTTP-01. Null (the default) means DNS-01, which
        is what a name that only resolves on an overlay network needs.
      '';
    };

    ports = {
      ycast = lib.mkOption { type = lib.types.port; default = 8010; description = "YCast (loopback)."; };
      wake = lib.mkOption { type = lib.types.port; default = 8011; description = "The on-demand encoder waker (loopback)."; };
      admin = lib.mkOption { type = lib.types.port; default = 8012; description = "The admin API (loopback)."; };
      speaker = lib.mkOption { type = lib.types.port; default = 8013; description = "The local-speaker API (loopback)."; };
      icecast = lib.mkOption {
        type = lib.types.port;
        default = 8020;
        description = ''
          Icecast (loopback). Note Icecast SEGVs rather than exiting cleanly
          when its bind fails, so a collision here looks like a crash.
        '';
      };
    };

    tts = {
      voice = lib.mkOption {
        type = lib.types.str;
        default = "af_heart";
        description = "The Kokoro voice the DJ speaks in.";
      };
      urls = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "http://127.0.0.1:8880" ];
        description = ''
          Kokoro-compatible TTS endpoints, tried in order — put a fast remote
          first and a local container last. With none reachable the stations
          still play; they simply stop talking.
        '';
      };
      afterUnits = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "docker-kokoro.service" ];
        description = "Units to order the DJ after, when TTS is hosted on this box.";
      };
    };

    speaker = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Play a station on this machine's own sound card.";
      };
      label = lib.mkOption {
        type = lib.types.str;
        default = "These speakers";
        description = "What the page calls this machine's audio output.";
      };
      card = lib.mkOption { type = lib.types.str; default = "0"; description = "ALSA card index or name, for the mixer."; };
      device = lib.mkOption {
        type = lib.types.str;
        default = "plughw:0,0";
        description = ''
          The ALSA device to open. NOT `default`: on a box running PipeWire
          per-user, `default` is redirected into the desktop session and a
          system service opening it gets "Host is down".
        '';
      };
      defaultMount = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "rain";
        description = "Play this station at boot, so a power cycle needs no phone. Empty for none.";
      };
      startVolume = lib.mkOption { type = lib.types.int; default = 35; description = "Mixer level at startup."; };
      fadeInMs = lib.mkOption {
        type = lib.types.ints.between 0 5000;
        default = 300;
        description = "How long an unmute takes to come back up. Longer is gentler.";
      };
      fadeOutMs = lib.mkOption {
        type = lib.types.ints.between 0 5000;
        default = 120;
        description = ''
          How long a mute — and any ordinary volume change — takes. Shorter than
          the fade in on purpose: going away and adjusting should feel immediate.
        '';
      };
      maxVolume = lib.mkOption {
        type = lib.types.ints.between 0 100;
        default = 100;
        example = 80;
        description = ''
          A ceiling no request can exceed. Set it to whatever the amplifier is
          comfortable with: the level is real output, and nothing upstream —
          the page, a script, a stray curl — should be able to put a speaker at
          full scale by accident.
        '';
      };
      startMuted = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Come up silent: the jack stays quiet until something unmutes it.";
      };

      holdStreamWhileMuted = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Stay connected to Icecast while muted, instead of releasing the
          mount. This was the behaviour before 2026-10-28 and it is rarely
          what you want.

          The encoders are on demand — Liquidsoap only runs a station's output
          while somebody is listening — so a speaker that holds its connection
          open while silent keeps one MP3 encode running forever. On the server
          that measured **9.7% of one core, continuously**, with the sole
          listener being this service feeding a jack ramped to -64 dB.

          Releasing it costs reconnect time on unmute: the wake call plus
          ffmpeg starting and buffering. The unmute fade covers most of that,
          since the player reconnects before the level comes up. Set this to
          true if the remaining delay matters more than the core.
        '';
      };
    };

    receiver = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Show living-room controls on the page, proxied to a receiver's own
          HTTP API (`apiUrl`), and log what it plays for library-growing.
        '';
      };
      label = lib.mkOption { type = lib.types.str; default = "Living room"; description = "What the page calls the receiver."; };
      tvInput = lib.mkOption {
        type = lib.types.str;
        default = "OPTICAL";
        example = "HDMI1";
        description = ''
          The input on which this receiver carries the television's sound.

          Starting a station on the receiver takes it off whatever it was
          playing, and when that was the television it cuts the sound out from
          under whoever is watching — from anywhere, including a phone that is
          not in the house (2026-11-02). On this input the page asks
          first; on any other it does not, because nothing is being taken from
          anyone.

          Set to "" to never ask.
        '';
      };
      apiUrl = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "http://127.0.0.1:8014";
        description = "Base URL of the receiver-control API (see services.yamaha-ync for one implementation).";
      };
      afterUnits = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "yamaha-ync-api.service" ];
        description = "Units serving `apiUrl`, to order the play logger after.";
      };
      codecs = lib.mkOption {
        type = lib.types.listOf (lib.types.enum [ "mp3" "aac-lc" "he-aac" "wma" "flac" "ogg" "opus" ]);
        default = [ "mp3" ];
        example = [ "mp3" "wma" "aac-lc" ];
        description = ''
          What this receiver can decode from an internet stream. Only stations in
          these codecs reach its menu, because one it cannot play is worse than
          one it is not shown.

          ⚠️ Read the spec carefully: "MPEG4 AAC" means `aac-lc`. The 32 kbps
          streams many stations now serve are HE-AACv2, a different profile that
          a 2014 decoder refuses — so listing `aac-lc` does NOT admit them.
        '';
      };
    };

    # --- an optional Jellyfin tie-in ---------------------------------------
    # Two independent conveniences for someone who already runs Jellyfin, and
    # nothing that netradio depends on: the library still lives on disk and is
    # still played from disk.
    #
    #   discoverRoots  Jellyfin says where the music is, so `libraryRoots` need
    #                  not be written out by hand. It also catches folders a
    #                  hand-written list forgets — on the server it named two roots
    #                  netradio was not scanning, one of them 786 Christmas  # leak-scan-ok: a genre word, not a name
    #                  files (2026-09-27).
    #   hearts         the page's heart button mirrors to Jellyfin's favourite
    #                  flag, and favourites set in Jellyfin come back.
    #
    # ⚠️ Jellyfin has no STAR rating for a music track — measured on 10.11.11,
    # not assumed: an Audio item's UserData is
    # PlaybackPositionTicks/PlayCount/IsFavorite/Played/Key/ItemId, the item
    # carries no rating field, and 0 of 400 sampled had a CommunityRating. The
    # heart is the only channel there is.
    #
    # With this off the heart button still works; it just stays local, and the
    # hearts merge in if Jellyfin is connected later.
    jellyfin = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = "Tie netradio to an existing Jellyfin for library folders and the heart.";
      };
      url = lib.mkOption {
        type = lib.types.str;
        default = "http://127.0.0.1:8096";
        description = "Jellyfin's base URL.";
      };
      keyFile = lib.mkOption {
        type = lib.types.nullOr lib.types.path;
        default = null;
        example = "/run/secrets/jellyfin-api";
        description = ''
          A file holding the API key, readable by the netradio user — a bare key
          or a `JELLYFIN_API_KEY=…` line. This module names no secret; hand it a
          path from sops-nix or anything else.
        '';
      };
      user = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = ''
          Whose favourites count, as a Jellyfin user id. Empty means the first
          administrator — a household has more than one account, and the other
          one's favourites are not this listener's.
        '';
      };
      discoverRoots = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Add Jellyfin's own music folders to the scan, alongside any libraryRoots.";
      };
      hearts = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Mirror the heart button to Jellyfin's favourites, in both directions.";
      };
      onCalendar = lib.mkOption {
        type = lib.types.str;
        default = "hourly";
        description = "How often to merge hearts with Jellyfin.";
      };
    };

    # --- playback devices --------------------------------------------------
    # A device is anything that can be told to play a station, and it is
    # described rather than special-cased: a base URL plus what it can do. The
    # verbs every device answers are
    #
    #     GET  <endpoint>/<statePath>     what is playing, volume, muted
    #     POST <endpoint>/play    {"mount": "rain"}
    #     POST <endpoint>/stop
    #     POST <endpoint>/volume  {"level": 40} | {"step": 5}
    #     POST <endpoint>/mute    {"on": true}
    #
    # Anything beyond that is a capability the device declares and the page
    # offers only if present — power, input switching, a menu to walk, a tuner.
    # So a new receiver needs a small service speaking those verbs, an entry
    # here, and nothing changed inside netradio.
    #
    # `speaker.*` and `receiver.*` above are sugar that fill this in, so the
    # two devices that already exist keep working untouched.
    # A remote for something that is NOT a playback endpoint. The `devices`
    # registry below is a contract for things you can send a STATION to; a
    # streaming box is not one of those, and listing it there would offer it as
    # somewhere to play the radio. When the page is refactored to loop over
    # `devices` rather than branch on hasRoom/hasLocal, this becomes a device
    # declaring `keys` and no `play` — until then it follows the shape the page
    # actually reads (2026-10-03).
    roku = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Show a Roku remote on the page: a d-pad, transport, Home and Back,
          and a text box for the search fields that are miserable on a physical
          remote. Needs something serving `apiUrl` — `services.roku` is one.
        '';
      };
      label = lib.mkOption {
        type = lib.types.str;
        default = "Roku";
        description = "What the page calls it before the device reports its own name.";
      };
      apiUrl = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "http://127.0.0.1:8793";
        description = "Base URL of the Roku control API. Loopback: it is proxied under the page's own vhost, so it inherits the same access gate.";
      };
    };

    devices = lib.mkOption {
      default = { };
      description = "Playback endpoints, by id. Each is proxied at /device/<id>/ under the page's own vhost, so it inherits the same access gate and needs no port of its own — keep the endpoints on loopback.";
      type = lib.types.attrsOf (lib.types.submodule ({ name, ... }: {
        options = {
          name = lib.mkOption {
            type = lib.types.str;
            default = name;
            description = "What the page calls it.";
          };
          endpoint = lib.mkOption {
            type = lib.types.str;
            example = "http://127.0.0.1:8014";
            description = "Base URL of the service speaking the verbs above. Loopback.";
          };
          capabilities = lib.mkOption {
            type = lib.types.listOf (lib.types.enum [
              "play" "stop" "volume" "mute"
              "stations"
              "power" "inputs" "menu" "tuner" "presets" "feedback"
              # A second stream under the station, mixed AT THE DEVICE. The
              # page has always had a bed, but it mixes it in the browser,
              # which makes it the browser's sound and no use to speakers in
              # another room. Only a device on a shared sink can do this: an
              # exclusive ALSA device cannot be opened twice, which is why it
              # is declared rather than assumed.
              "bed"
              # The device can enumerate where it sends sound, and be told to
              # use a different one — a machine with both analogue and HDMI.
              "outputs"
            ]);
            default = [ "play" "stop" "volume" "mute" "stations" ];
            description = "What this device can do. play/stop/volume/mute plus `stations` are the contract; the rest are extras the page shows only when declared.";
          };
          codecs = lib.mkOption {
            type = lib.types.listOf (lib.types.enum [ "mp3" "aac-lc" "he-aac" "wma" "flac" "ogg" "opus" ]);
            default = [ "mp3" ];
            example = [ "mp3" "aac-lc" "wma" ];
            description = ''
              What this device can actually decode, which decides which outside
              stations it is offered. A station it cannot play is worse than one
              it is not shown: the listener selects it and gets silence.

              ⚠️ `aac-lc` and `he-aac` are different answers. A 2014 receiver
              whose spec says "MPEG4 AAC" means AAC-LC; the 32 kbps streams many
              stations now serve are HE-AACv2 and it will refuse them (measured
              on two local country stations, 2026-09-27).
            '';
          };
          statePath = lib.mkOption {
            type = lib.types.str;
            default = "state";
            description = "The path the device answers its status on. `state` is the contract; this exists so a device already speaking a different dialect (yamaha-ync answers `status`) is usable without being rewritten.";
          };
          order = lib.mkOption {
            type = lib.types.int;
            default = 50;
            description = "Sort order in the page's target picker; lower comes first.";
          };
          afterUnits = lib.mkOption {
            type = lib.types.listOf lib.types.str;
            default = [ ];
            description = "Units that serve this endpoint, for ordering.";
          };
        };
      }));
    };

    vtuner = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Stand in for vTuner so a Yamaha receiver's "Net Radio" input works
          after vTuner went pay-to-use: a DNS answer for the vTuner hostnames
          pointing here, a plain-HTTP vhost on port 80 (the receiver cannot do
          TLS), and YCast behind it serving the station menu.
        '';
      };
      host = lib.mkOption { type = lib.types.str; default = "radioyamaha.vtuner.com"; description = "The vTuner name the receiver resolves."; };
      backupHost = lib.mkOption { type = lib.types.str; default = "radioyamaha2.vtuner.com"; description = "The receiver's fallback name."; };
      address = lib.mkOption {
        type = lib.types.str;
        default = "";
        example = "192.168.1.10";
        description = "The LAN address nginx answers on, handed out for the names above.";
      };
      blockyMapping = lib.mkOption {
        type = lib.types.bool;
        default = true;
        description = "Add the DNS mapping to services.blocky. Turn off to point some other resolver at `address` yourself.";
      };
    };

    profile = {
      workers = lib.mkOption { type = lib.types.int; default = 4; description = "Local analysis workers."; };
      remotes = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ ];
        example = [ "http://faster-box:8790" ];
        description = ''
          `netradio profile-server` endpoints to offload audio analysis to.
          Each is tried before measuring locally, so one that is switched off
          only costs speed.
        '';
      };
    };

    compile = {
      enable = lib.mkOption {
        type = lib.types.bool;
        default = false;
        description = ''
          Let the admin page write a feed's matching rule from its prose
          description by handing the prompt to an agent CLI. Off by default:
          it runs a command as another user.
        '';
      };
      user = lib.mkOption { type = lib.types.str; default = "claude"; description = "The user the agent CLI runs as."; };
      home = lib.mkOption { type = lib.types.path; default = "/home/claude"; description = "HOME for that user."; };
      workingDirectory = lib.mkOption { type = lib.types.path; default = "/home/claude"; description = "Where the CLI is invoked."; };
      program = lib.mkOption { type = lib.types.str; default = "claude"; description = "The CLI, resolved on that user's PATH."; };
    };

    lastfmEnvFile = lib.mkOption {
      type = lib.types.nullOr lib.types.path;
      default = null;
      example = "/run/secrets/lastfm-env";
      description = ''
        An EnvironmentFile holding a Last.fm API key, for the similar-artist
        lookups behind artist spotlights. Optional — without it the DJ simply
        does not reach for neighbours. Provide it with sops-nix, agenix or
        anything else that lands a file; this module never names a secret.
      '';
    };

    internetRadio = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption { type = lib.types.str; description = "Shown in the menu."; };
          url = lib.mkOption { type = lib.types.str; description = "A plain-HTTP stream (a receiver cannot do TLS)."; };
          codec = lib.mkOption {
            type = lib.types.enum [ "mp3" "aac-lc" "he-aac" "wma" "flac" "ogg" "opus" ];
            default = "mp3";
            description = "What the stream is, so a device that cannot decode it is not offered it.";
          };
        };
      });
      default = defaultInternetRadio;
      description = ''
        A handful of outside stations, so the input plays something before any
        browsing. Setting this REPLACES the starter list; to keep it and add your
        own local stations, use `extraInternetRadio`.
      '';
    };

    extraInternetRadio = lib.mkOption {
      type = lib.types.listOf (lib.types.submodule {
        options = {
          name = lib.mkOption { type = lib.types.str; description = "Shown in the menu."; };
          url = lib.mkOption { type = lib.types.str; description = "A plain-HTTP stream (a receiver cannot do TLS)."; };
          codec = lib.mkOption {
            type = lib.types.enum [ "mp3" "aac-lc" "he-aac" "wma" "flac" "ogg" "opus" ];
            default = "mp3";
            description = "What the stream is, so a device that cannot decode it is not offered it.";
          };
        };
      });
      default = [ ];
      example = lib.literalExpression ''[ { name = "WMMT 88.7"; url = "http://example/radio.mp3"; } ]'';
      description = ''
        Local stations to add to the starter list. Separate so a site's own
        stations are site configuration and the module keeps a generic default —
        the same split as the seeded stations.
      '';
    };

    seed = {
      feeds = lib.mkOption {
        type = lib.types.attrsOf (lib.types.attrsOf lib.types.anything);
        default = defaultFeeds;
        description = ''
          Specialty feeds to write into the runtime config on a box that has
          none yet. A feed is a title, a description and a rule (artists /
          genres / instruments / era, AND-ed). Edited on the admin page
          afterwards — a deploy never overwrites them.
        '';
      };
      stations = lib.mkOption {
        type = lib.types.listOf (lib.types.attrsOf lib.types.anything);
        default = defaultStations;
        description = ''
          Stations for a fresh install: `curated` (a broad base rule plus a
          schedule of segments), `specialty` (one feed) or `fixed` (a
          hand-kept playlist, no DJ). Defaults to one per genre family plus
          one per seeded feed.
        '';
      };
      schedule = lib.mkOption {
        type = lib.types.listOf (lib.types.attrsOf lib.types.anything);
        default = defaultSchedule;
        description = "Themed segments and spotlight slots for a fresh install.";
      };
    };
  };

  config = lib.mkIf cfg.enable {
  # The two devices that exist, expressed as devices. This is the only place
  # that knows `speaker` and `receiver` are special — everything downstream
  # reads homelab.netradio.devices, so a third one is config, not code.
  # A device that cannot do the four verbs is not a device, and finding that
  # out by tapping a dead button in the page is the wrong time.
  assertions = [
    {
      assertion = libraryRoots != [ ] || (jellyfinReady && cfg.jellyfin.discoverRoots);
      message = "homelab.netradio: set libraryRoots, or enable jellyfin.discoverRoots with a keyFile — "
                + "otherwise the scanner has nowhere to look for music.";
    }
    {
      assertion = !cfg.jellyfin.enable || cfg.jellyfin.keyFile != null;
      message = "homelab.netradio.jellyfin.enable needs jellyfin.keyFile: every call to Jellyfin is authenticated.";
    }
  ] ++
    (lib.mapAttrsToList (id: d: {
      assertion = lib.all (c: lib.elem c d.capabilities) [ "play" "stop" "volume" "mute" ];
      message = "homelab.netradio.devices.${id}: capabilities must include play, stop, volume and mute — "
                + "those four are the contract. Got: ${lib.concatStringsSep ", " d.capabilities}.";
    }) cfg.devices)
    ++ (lib.mapAttrsToList (id: d: {
      assertion = d.endpoint != "" && lib.hasPrefix "http" d.endpoint;
      message = "homelab.netradio.devices.${id}.endpoint must be an http(s) base URL, e.g. http://127.0.0.1:8014.";
    }) cfg.devices);

  warnings = lib.filter (w: w != "") (lib.mapAttrsToList (id: d:
    lib.optionalString (!(lib.hasInfix "127.0.0.1" d.endpoint || lib.hasInfix "localhost" d.endpoint))
      ("homelab.netradio.devices.${id}.endpoint is not on loopback (${d.endpoint}). It is proxied under the "
       + "page's vhost, which is the access gate — a device reachable another way is outside it.")
  ) cfg.devices);

  homelab.netradio.devices = lib.mkMerge [
    (lib.mkIf cfg.speaker.enable {
      local = {
        name = cfg.speaker.label;
        endpoint = "http://127.0.0.1:${toString speakerPort}";
        capabilities = [ "play" "stop" "volume" "mute" "stations" ];
        order = 20;
        afterUnits = [ "netradio-speaker.service" ];
      };
    })
    (lib.mkIf (cfg.receiver.enable && cfg.receiver.apiUrl != "") {
      room = {
        name = cfg.receiver.label;
        endpoint = cfg.receiver.apiUrl;
        # Overridden per receiver in site config; mp3 is the one thing every
        # net-radio device has ever decoded.
        codecs = cfg.receiver.codecs;
        # yamaha-ync answers /status, not /state. Declared rather than
        # rewritten: the adapter is one word of config.
        statePath = "status";
        capabilities = [ "play" "stop" "volume" "mute" "stations"
                         "power" "inputs" "menu" "tuner" "presets" "feedback" ];
        order = 10;
        afterUnits = cfg.receiver.afterUnits;
      };
    })
  ];

  users.users.${user} = {
    isSystemUser = true;
    group = user;
    extraGroups = lib.optional (cfg.libraryGroup != null) cfg.libraryGroup;
    home = stateDir;
  };
  users.groups.${user} = { };

  # --- DNS: the receiver's lookups land here -------------------------------
  # LAN-only by construction: a device on some other resolver keeps public DNS
  # and gets the real vTuner, which is fine — nothing off-LAN uses this.
  services.blocky.settings.customDNS.mapping =
    lib.mkIf (cfg.vtuner.enable && cfg.vtuner.blockyMapping) {
      ${vtunerHost} = lanIP;
      ${vtunerBackup} = lanIP;
    };

  # --- nginx: the ONE thing the receiver talks to ---------------------------
  # Plain HTTP on 80, deliberately no forceSSL: the receiver has no TLS.
  services.nginx.virtualHosts = lib.optionalAttrs cfg.vtuner.enable { ${vtunerHost} = {
    serverAliases = [ vtunerBackup ];
    extraConfig = ''
      # A 2014 receiver's HTTP parser: no compressed bodies, no chunking.
      gzip off;
      chunked_transfer_encoding off;
    '';

    # vTuner API → YCast. YCast builds the URLs it hands back from the Host
    # header, so it must see radioyamaha.vtuner.com (recommendedProxySettings
    # passes Host through).
    locations = {
      "/" = {
        proxyPass = "http://127.0.0.1:${toString ycastPort}";
        recommendedProxySettings = true;
      };
    } // streamLocations;
  }; } // {

  # --- the page: the same streams for phones and laptops --------------------
  # Reachable wherever this name resolves, and access-gated like the rest of
  # the site. Exists because the vtuner name above only answers for devices
  # pointed at this box's resolver; this one answers everywhere. Index page:
  # the stations, tap to play.
  ${radioHost} = {
    forceSSL = cfg.tls;
    enableACME = cfg.tls;
    acmeRoot = cfg.acmeRoot;
    root = radioWeb;
    locations = {
      "/" = {
        tryFiles = "$uri $uri/index.html =404";
      };
      # Per-station "last played" (Liquidsoap) and "up next" (the DJ), the
      # scanner's counts, and the radio-app playlists — plain files,
      # rewritten atomically. `^~` so no regex location can take these URIs
      # away; the playlist MIME types are a NESTED location, because a
      # `types` block REPLACES the inherited map for its location (put in
      # "/" it served the page as octet-stream, 2026-09-16 11:37) and a
      # top-level regex location outran this prefix and 404'd the m3u
      # files from the site root (17:25 the same day).
      "^~ /now/" = {
        root = stateDir;
        extraConfig = ''
          add_header Cache-Control "no-store";
          location ~ \.(m3u|pls)$ {
            types { audio/x-mpegurl m3u; audio/x-scpls pls; }
            add_header Cache-Control "no-store";
          }
        '';
      };
      # nginx's mime.types has no entry for .webmanifest, and Chrome won't
      # install a PWA whose manifest arrives as octet-stream. An exact-match
      # location for that one file (a `types` block only reaches this file).
      "= /manifest.webmanifest" = {
        extraConfig = ''
          types { } default_type application/manifest+json;
        '';
      };
      # Cover art, unlike the rest of the admin API, is immutable content, and
      # the desktop wall asks for up to four thumbnails per station at once.
      # `/admin/api/` below adds `no-store`, which LANDS ON TOP of the app's own
      # `public, max-age=86400` — two Cache-Control headers, and no-store wins,
      # so every thumbnail was refetched on every load. A wall of 48 covers took
      # ~25 s to finish painting because of it (2026-09-28).
      #
      # A longer PREFIX beats a shorter one, so this wins over `/admin/api/`
      # without a regex — and a location with its own add_header does not
      # inherit the parent's, which is the point.
      "/admin/api/art" = {
        proxyPass = "http://127.0.0.1:${toString adminPort}/api/art";
        extraConfig = ''
          add_header Cache-Control "public, max-age=86400";
        '';
      };
      # The admin API (netradio admin, loopback). The page itself is static
      # under /admin/. The vhost's Tailscale/LAN gate is the perimeter.
      "/admin/api/" = {
        proxyPass = "http://127.0.0.1:${toString adminPort}/api/";
        extraConfig = ''
          add_header Cache-Control "no-store";
        '';
      };
      # Icecast's public status: which mounts are up, titles, listener counts.
      "= /icecast-status" = {
        proxyPass = "http://127.0.0.1:${toString icecastPort}/status-json.xsl";
        extraConfig = ''
          add_header Cache-Control "no-store";
        '';
      };
    } // streamLocations
      # The receiver's own JSON API, for the page's living-room controls.
      # Same access gate as the page.
      // lib.optionalAttrs (cfg.receiver.enable && cfg.receiver.apiUrl != "") {
        "/receiver/" = {
          proxyPass = "${cfg.receiver.apiUrl}/";
          extraConfig = ''
            add_header Cache-Control "no-store";
            proxy_read_timeout 90s;   # a menu walk can take a while
          '';
        };
      }
      # The Roku's control API. Navigation only — the sound leaves the
      # television by optical to the receiver, so volume belongs to the
      # receiver and this never carries it.
      // lib.optionalAttrs (cfg.roku.enable && cfg.roku.apiUrl != "") {
        "/roku/" = {
          proxyPass = "${lib.removeSuffix "/" cfg.roku.apiUrl}/";
          extraConfig = ''
            add_header Cache-Control "no-store";
            proxy_read_timeout 30s;   # a box waking from suspend answers slowly
          '';
        };
      }
      # This machine's own sound card as a third endpoint (netradio-speaker)
      // lib.optionalAttrs cfg.speaker.enable {
        "/speaker/" = {
          proxyPass = "http://127.0.0.1:${toString speakerPort}/";
          extraConfig = ''
            add_header Cache-Control "no-store";
          '';
        };
      }
      # …and every declared device at its own generic path. The two legacy
      # locations above stay until the page stops naming them, so this change
      # adds a route and removes nothing.
      // lib.mapAttrs' (id: d: lib.nameValuePair "/device/${id}/" {
        proxyPass = "${lib.removeSuffix "/" d.endpoint}/";
        extraConfig = ''
          add_header Cache-Control "no-store";
          proxy_read_timeout 90s;   # a device that walks a menu can be slow
        '';
      }) cfg.devices;
  };
  };

  # --- Icecast passwords: generated on the box, never in the repo ------------
  # Read them with `sudo cat ${credsEnv}`; rotate by deleting the file and
  # restarting netradio-credentials netradio-icecast netradio-liquidsoap.
  # Same temp-file-then-move shape as homelab-mcp-credentials, for the same
  # reason (a half-written file must not satisfy the `! -s` guard next time).
  systemd.services.netradio-credentials = {
    description = "Generate the Icecast source/admin passwords if absent";
    wantedBy = [ "multi-user.target" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    script = ''
      set -euo pipefail
      install -d -m 0755 -o root -g root ${stateDir}
      install -d -m 0755 -o ${user} -g ${user} ${playlistDir}
      install -d -m 0700 -o ${user} -g ${user} ${cacheDir}
      install -d -m 0755 -o ${user} -g ${user} ${djDir} ${djDir}/inbox   # inbox: skip/request files from the admin API
      install -d -m 0755 -o ${user} -g ${user} ${nowDir}
      install -d -m 0755 -o ${user} -g ${user} ${profileDir}
      install -d -m 0755 -o ${user} -g ${user} ${ambientDir} ${ambientDir}/rain
      install -d -m 0755 -o ${user} -g ${user} ${poolsDir} ${poolsDir}/feeds ${poolsDir}/artists
      # The runtime config: netradio writes it (admin, scanner, DJ) and the
      # claude user's compile job writes feeds.json too, so it is group
      # `users` and setgid. Seeds land only where no file exists.
      install -d -m 2775 -o ${user} -g users ${configDir} ${configDir}/requests
      [ -s ${configDir}/feeds.json ]    || install -m 0664 -o ${user} -g users ${seeds.feeds} ${configDir}/feeds.json
      [ -s ${configDir}/stations.json ] || install -m 0664 -o ${user} -g users ${seeds.stations} ${configDir}/stations.json
      [ -s ${configDir}/schedule.json ] || install -m 0664 -o ${user} -g users ${seeds.schedule} ${configDir}/schedule.json
      # One-time changes that should reach an EXISTING config too (a seed
      # change only reaches a fresh install): netradio/migrate.py, recorded
      # in config/migrations.json. Runs as the config's owner.
      ${pkgs.util-linux}/bin/runuser -u ${user} -- ${netradio}/bin/netradio migrate --config ${configDir}
      # Liquidsoap watches each playlist FILE (inotify): one that appears
      # after it started is never picked up, but an empty one that is later
      # rewritten is (verified 2026-09-15). So every station's file exists
      # before the first start; the scanner fills them.
      for m in $(${pkgs.jq}/bin/jq -r '.[].mount' ${configDir}/stations.json); do
        f=${playlistDir}/$m.m3u
        [ -e "$f" ] || { printf '#EXTM3U\n' > "$f"; chown ${user}:${user} "$f"; }
      done
      if [ ! -s ${credsEnv} ]; then
        umask 077
        tmp="$(${pkgs.coreutils}/bin/mktemp ${stateDir}/.credentials.XXXXXX)"
        trap 'rm -f "$tmp"' EXIT
        {
          printf 'ICECAST_SOURCE_PASSWORD=%s\n' "$(${pkgs.openssl}/bin/openssl rand -hex 16)"
          printf 'ICECAST_ADMIN_PASSWORD=%s\n'  "$(${pkgs.openssl}/bin/openssl rand -hex 16)"
        } > "$tmp"
        chown root:root "$tmp"
        chmod 0600 "$tmp"
        mv "$tmp" ${credsEnv}
        trap - EXIT
        echo "generated new icecast credentials"
      fi
    '';
  };

  # --- Icecast ---------------------------------------------------------------
  systemd.services.netradio-icecast = {
    description = "Icecast (loopback) for the library radio stations";
    wantedBy = [ "multi-user.target" ];
    after = [ "network.target" "netradio-credentials.service" ];
    requires = [ "netradio-credentials.service" ];
    serviceConfig = hardening // {
      DynamicUser = true;
      EnvironmentFile = credsEnv;
      RuntimeDirectory = "netradio-icecast";
      LogsDirectory = "icecast";
      # The env file is read by PID 1, so ExecStartPre sees the passwords
      # without the unit's user being able to open the file itself.
      ExecStartPre = pkgs.writeShellScript "render-icecast-xml" ''
        set -eu
        umask 077
        ${pkgs.gnused}/bin/sed \
          -e "s|@ICECAST_SOURCE_PASSWORD@|$ICECAST_SOURCE_PASSWORD|" \
          -e "s|@ICECAST_ADMIN_PASSWORD@|$ICECAST_ADMIN_PASSWORD|" \
          ${icecastTemplate} > /run/netradio-icecast/icecast.xml
      '';
      ExecStart = "${lib.getExe pkgs.icecast} -c /run/netradio-icecast/icecast.xml";
      Restart = "on-failure";
      RestartSec = 5;
    };
  };

  # --- Liquidsoap: the encoders (idle until woken) ---------------------------
  systemd.services.netradio-liquidsoap = {
    description = "Liquidsoap — library radio station outputs";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-icecast.service" "netradio-credentials.service" ] ++ cfg.requiresMounts;
    requires = [ "netradio-icecast.service" "netradio-credentials.service" ];
    # Liquidsoap restarts whenever the station list changes (its script
    # does); pull a playlist rebuild along so new stations are populated.
    # Not `requires`: a scan failure must not take the radio down.
    wants = [ "netradio-playlists.service" ];
    environment.HOME = stateDir;
    serviceConfig = hardening // {
      User = user;
      Group = user;
      SupplementaryGroups = libraryGroups;
      EnvironmentFile = credsEnv;
      RuntimeDirectory = "netradio";
      RuntimeDirectoryMode = "0700";
      ReadWritePaths = [ nowDir ];
      # The script is rendered from the runtime station list every start;
      # a station added on the admin page is one restart away (the apply
      # path unit does it when the mount list changed).
      ExecStartPre = lib.concatStringsSep " " [
        "${netradio}/bin/netradio liq" "--config ${configDir}" "--socket ${liqSocket}"
        "--relays ${relaysJson}"
        "--playlists ${playlistDir}" "--now-dir ${nowDir}" "--port ${toString icecastPort}" "--out ${liqScript}"
      ];
      ExecStart = "${lib.getExe pkgs.liquidsoap} ${liqScript}";
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- the DJ -------------------------------------------------------------------
  # Sequences every station's tracks through Liquidsoap's request queue and,
  # every 3-4 of them, queues a spoken break: what just played, what's next.
  # Kokoro af_heart, same voice + URL order as the switchboard (the compute box first,
  # the server's own container as the fallback). Rendered breaks live under
  # ${djDir}/<mount>/ (last few kept). If it is down, stations shuffle as before.
  systemd.services.netradio-dj = {
    description = "Library radio DJ — sequence tracks, announce every few";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-liquidsoap.service" ] ++ cfg.tts.afterUnits;
    bindsTo = [ "netradio-liquidsoap.service" ];
    serviceConfig = hardening // {
      User = user;
      Group = user;
      SupplementaryGroups = libraryGroups;   # reads the tracks' tags
      ReadWritePaths = [ djDir nowDir configDir ];   # picks.json / similar.json live in config
      # similar artists for spotlights; optional, and absent is fine (the "-")
      EnvironmentFile = lib.optional (cfg.lastfmEnvFile != null) "-${cfg.lastfmEnvFile}";
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio dj"
        "--now-dir ${nowDir}"
        "--config ${configDir}"
        "--pools ${poolsDir}"
        "--playlists ${playlistDir}"
        "--socket ${liqSocket}"
        "--out ${djDir}"
        "--voice ${kokoroVoice}"
        "--breaks-every 3-4"
        "--profile ${profileJson}"
        "--overrides ${profileOverrides}"
      ] ++ map (u: "--kokoro-url ${u}") kokoroUrls);
      Restart = "always";
      RestartSec = 10;
    };
  };

  # --- wake/idle controller ---------------------------------------------------
  systemd.services.netradio-wake = {
    description = "Start library station encoders on demand, stop them when idle";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-liquidsoap.service" ];
    bindsTo = [ "netradio-liquidsoap.service" ];  # its socket is the whole job
    serviceConfig = hardening // {
      User = user;
      Group = user;
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio wake"
        "--stations ${configDir}/stations.json"
        "--relays ${relaysJson}"
        "--playlists ${playlistDir}"
        "--socket ${liqSocket}"
        "--icecast-status http://127.0.0.1:${toString icecastPort}/status-json.xsl"
        "--listen 127.0.0.1 --port ${toString wakePort}"
        "--idle-after 300 --tick 30"
      ];
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- the rain: ambient beds a fixed station loops -----------------------------
  # Freely licensed recordings from the Internet Archive; the fetch is
  # idempotent, so it runs at boot and after a deploy and normally does
  # nothing. Drop your own files in ${ambientDir}/rain and they play too.
  # The state dirs the other units get from netradio-credentials' script are
  # created too late for this one: ReadWritePaths is resolved when the unit
  # starts, and a missing path is 226/NAMESPACE before a line of code runs
  # (2026-09-23, the first deploy of the rain station). tmpfiles runs before
  # any service, so the directory is always there.
  systemd.tmpfiles.rules = [
    "d ${ambientDir} 0755 ${user} ${user} -"
    "d ${ambientDir}/rain 0755 ${user} ${user} -"
    "d ${ambientDir}/rainymood 0755 ${user} ${user} -"
    "d ${ambientDir}/rainymood/src 0755 ${user} ${user} -"
    # where a picture per station goes: <mount>.jpg. Group-writable so an
    # operator can drop one in without becoming the service user.
    "d ${stationArtDir} 0775 ${user} ${user} -"
  ] ++ lib.mapAttrsToList (mount: src:
    # `L+` replaces whatever is there, so changing the option in the config is
    # enough — no stale picture survives a rebuild.
    "L+ ${stationArtDir}/${mount}.${extOf src} - - - - ${src}") cfg.stationArt;

  systemd.services.netradio-ambient = {
    description = "Fetch the ambient beds (rain) and write the fixed station's playlist";
    wantedBy = [ "multi-user.target" ];
    before = [ "netradio-liquidsoap.service" ];
    after = [ "network-online.target" "netradio-credentials.service" ];
    wants = [ "network-online.target" ];
    serviceConfig = hardening // {
      Type = "oneshot";
      RemainAfterExit = true;
      User = user;
      Group = user;
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio ambient"
        "--dir ${ambientDir}"
        "--playlists ${playlistDir}"
      ];
      # ffmpeg/ffprobe: the bed check (a slated sound-effects cut must never
      # reach the station again — 2026-09-23) and the seamless-loop cut
      Environment = [ "PATH=${lib.makeBinPath [ pkgs.ffmpeg ]}" ];
      ReadWritePaths = [ ambientDir playlistDir ];
      TimeoutStartSec = "60min";      # a download plus a 34-minute loop re-encode
    };
  };

  # --- this machine's sound card as a playback endpoint -------------------------
  # One ffmpeg decoding a station's mount into the card (`-f alsa <device>` — an
  # argument, so it cannot be ignored the way AUDIODEV was), the ALSA mixer for
  # volume, and a small JSON API behind <domain>/speaker/. Plays the default
  # mount at boot, so a power cycle needs nothing pressed.
  systemd.services.netradio-speaker = lib.mkIf cfg.speaker.enable {
    description = "Play a library station on this machine's own audio output";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-icecast.service" "netradio-wake.service" "sound.target" ];
    wants = [ "netradio-icecast.service" "netradio-wake.service" ];
    serviceConfig = hardening // {
      User = user;
      Group = user;
      SupplementaryGroups = [ "audio" ];
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio speaker"
        "--icecast http://127.0.0.1:${toString icecastPort}"
        "--wake http://127.0.0.1:${toString wakePort}"
        "--listen 127.0.0.1 --port ${toString speakerPort}"
        "--device ${cfg.speaker.device}"
        "--card ${speakerCard}"
        "--max-volume ${toString cfg.speaker.maxVolume}"
        "--fade-in-ms ${toString cfg.speaker.fadeInMs}"
        "--fade-out-ms ${toString cfg.speaker.fadeOutMs}"
      ] ++ lib.optional (speakerDefaultMount != "") "--default-mount ${speakerDefaultMount}"
        ++ [ "--start-volume ${toString speakerStartVolume}" ]
        ++ lib.optional cfg.speaker.startMuted "--start-muted"
        ++ lib.optional cfg.speaker.holdStreamWhileMuted "--hold-stream-while-muted"
        ++ [ "--ffmpeg ${pkgs.ffmpeg}/bin/ffmpeg" ]);
      Environment = [ "PATH=${lib.makeBinPath [ pkgs.alsa-utils pkgs.ffmpeg ]}" ];
      PrivateDevices = false;         # it needs /dev/snd
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- what the receiver plays on a streaming service ---------------------------
  # A well-tuned commercial station is a model for a library station: the log
  # feeds the admin page's "heard elsewhere" tab and the wishlist it builds.
  systemd.services.netradio-pandora = lib.mkIf (cfg.receiver.enable && cfg.receiver.apiUrl != "") {
    description = "Log what the receiver plays (for growing the library)";
    wantedBy = [ "multi-user.target" ];
    after = cfg.receiver.afterUnits;
    serviceConfig = hardening // {
      User = user;
      Group = user;
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio pandora"
        "--api ${cfg.receiver.apiUrl}"
        "--out ${configDir}/pandora.jsonl"
        "--interval 15"
        "--receiver-state ${nowDir}/receiver.json"
        "--menu ${configDir}/stations.yml"
      ];
      ReadWritePaths = [ configDir nowDir ];
      Restart = "always";
      RestartSec = 30;
    };
  };

  # --- put the receiver back after an encoder restart ---------------------------
  # Liquidsoap restarts on any deploy that changes the package, which drops
  # every listener. The phone reconnects and the local speaker has a watchdog;
  # a receiver does not — an R-N301 goes to Stop and stays there, which is how
  # Classic Country was silent in the living room for four hours on 2026-09-27.
  # `wantedBy` on the Liquidsoap unit means this runs every time it starts.
  # The guardrails live in netradio/resume.py: powered on, already on net
  # radio, not already playing, and only a station it was seen playing.
  systemd.services.netradio-resume = lib.mkIf (cfg.receiver.enable && cfg.receiver.apiUrl != "") {
    description = "Put the receiver back on the station it was playing";
    after = [ "netradio-liquidsoap.service" "netradio-wake.service" ] ++ cfg.receiver.afterUnits;
    wants = [ "netradio-wake.service" ];
    wantedBy = [ "netradio-liquidsoap.service" ];
    serviceConfig = hardening // {
      Type = "oneshot";
      User = user;
      Group = user;
      ExecStart = lib.concatStringsSep " " [
        "${netradio}/bin/netradio resume"
        "--api ${cfg.receiver.apiUrl}"
        "--now-dir ${nowDir}"
        "--wake http://127.0.0.1:${toString wakePort}"
        # a receiver takes a moment to notice the stream went away; asking it
        # while it still thinks it is playing would be a no-op
        "--settle 15"
      ];
      TimeoutStartSec = "5min";
    };
  };

  # --- playlist scanner: nightly + shortly after boot --------------------------
  systemd.services.netradio-playlists = {
    description = "Rebuild the library station playlists from genre tags";
    after = [ "netradio-credentials.service" ] ++ cfg.requiresMounts;
    requires = [ "netradio-credentials.service" ];
    serviceConfig = hardening // {
      Type = "oneshot";
      User = user;
      Group = user;
      SupplementaryGroups = libraryGroups;
      ReadWritePaths = [ playlistDir cacheDir nowDir poolsDir configDir ];
      Nice = 10;
      IOSchedulingClass = "idle";
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio playlists"
        "--config ${configDir}"
        "--out ${playlistDir}"
        "--pools ${poolsDir}"
        "--cache ${tagCache}"
        "--genres ${configDir}/genres.json"   # optional: the beets + Last.fm catalogue's words per file
        "--summary ${nowDir}/stations.json"
        "--ycast ${configDir}/stations.yml"
        "--station-art ${stationArtDir}"
      ]
        # The URL the receiver is handed, which must be the plain-HTTP vhost it
        # can actually parse — only meaningful when that stand-in exists.
        ++ lib.optional cfg.vtuner.enable "--public-base http://${vtunerHost}/radio"
        ++ [
        "--web-base ${if cfg.tls then "https" else "http"}://${radioHost}/radio"
        "--internet-radio ${internetRadioJson}"
      ] ++ lib.optional cfg.vtuner.enable
        "--menu-codecs ${lib.concatStringsSep "," receiverCodecs}"
      ++ [
      ] ++ lib.optionals (jellyfinReady && cfg.jellyfin.discoverRoots) [
        "--jellyfin-url ${cfg.jellyfin.url}"
        "--jellyfin-key-file ${cfg.jellyfin.keyFile}"
      ] ++ [
        "--profile ${profileJson}"
        "--overrides ${profileOverrides}"
      ] ++ map (r: "--root ${r}") libraryRoots);
    };
  };
  systemd.timers.netradio-playlists = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "10min";
      # A deploy that changes this timer re-activates it: rebuild a minute
      # later, so a station added in a PR is not empty until 04:30 (the three
      # era stations shipped empty on 2026-09-16 for exactly that reason).
      OnActiveSec = "1min";
      OnCalendar = "04:30";
      Persistent = true;
      RandomizedDelaySec = "10min";
    };
  };

  # --- the talk profiler: nightly, incremental -------------------------------
  # Listens to every track once (YAMNet + a pitch tracker; see profile.py for
  # the rule and the measurements behind it) and writes profile.json: which
  # tracks are talk (the scanner keeps them off every station) and which
  # start or end in chatter (the DJ doesn't crossfade over those). The first
  # pass over ~17k tracks is a few hours at Nice 19 and idle I/O; after that
  # only new files are analysed. Review what it flagged in profile-report.txt;
  # correct it in profile-overrides.json ({"<path>": "music"} or "talk").
  # Runs before the 04:30 playlist rebuild so exclusions land the same night.
  systemd.services.netradio-profile = {
    description = "Profile library tracks for talk vs music (YAMNet + pitch)";
    after = [ "netradio-credentials.service" ] ++ cfg.requiresMounts;
    requires = [ "netradio-credentials.service" ];
    # The playlists are what act on the profile: rebuild them the moment a
    # pass finishes rather than at the next 04:30 (the first pass runs past
    # it), so era stations fill the same morning.
    onSuccess = [ "netradio-playlists.service" ];
    # A deploy must not touch a running pass: switch-to-configuration
    # RESTARTS a changed unit and, for a oneshot, WAITS for it — the #305
    # deploy sat in activation for the whole 90-minute re-measure and
    # queued every deploy behind it (2026-09-16 16:55-18:20). The timer
    # (OnActiveSec) still fires the new version once the pass is over.
    restartIfChanged = false;
    stopIfChanged = false;
    serviceConfig = hardening // {
      Type = "oneshot";
      User = user;
      Group = user;
      SupplementaryGroups = libraryGroups;
      ReadWritePaths = [ stateDir ];
      Nice = 19;
      IOSchedulingClass = "idle";
      CPUWeight = 20;
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio profile"
        # library.m3u is every audio file the scanner found, unfiltered. Never
        # a station playlist: those exclude what the profiler flagged, and a
        # profiler fed one drops those tracks' facts as "gone" (11:44 today).
        "--playlist ${playlistDir}/library.m3u"
        "--model ${yamnet}"
        "--profile ${profileJson}"
        "--overrides ${profileOverrides}"
        "--report ${profileReport}"
        "--workers ${toString profileWorkers}"
      ] ++ map (u: "--remote ${u}") profileRemotes);
    };
  };
  systemd.timers.netradio-profile = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "20min";
      # A deploy that changes this timer re-activates it: run shortly after,
      # so a profiler change (new facts, a new server) is exercised the same
      # day rather than waiting for 01:00.
      OnActiveSec = "2min";
      OnCalendar = "01:00";
      Persistent = true;
    };
  };

  # --- hearts: merge with Jellyfin's favourites ---------------------------
  systemd.services.netradio-hearts = lib.mkIf (jellyfinReady && cfg.jellyfin.hearts) {
    description = "Merge the heart flags with Jellyfin's favourites";
    after = [ "netradio-credentials.service" "jellyfin.service" ];
    serviceConfig = hardening // {
      Type = "oneshot";
      User = user;
      Group = user;
      ReadWritePaths = [ configDir ];
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio jellyfin"
        "--config ${configDir}"
        "--url ${cfg.jellyfin.url}"
        "--key-file ${cfg.jellyfin.keyFile}"
      ] ++ lib.optional (cfg.jellyfin.user != "") "--user ${cfg.jellyfin.user}");
    };
  };
  systemd.timers.netradio-hearts = lib.mkIf (jellyfinReady && cfg.jellyfin.hearts) {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnCalendar = cfg.jellyfin.onCalendar;
      OnBootSec = "5min";
      Persistent = true;
      RandomizedDelaySec = "2min";
    };
  };

  # --- the admin API ------------------------------------------------------------
  # Edits the runtime config; files requests under config/requests/ for the
  # privileged side below. Loopback; nginx proxies /admin/api/ to it.
  systemd.services.netradio-admin = {
    description = "netradio admin API (feeds, stations, schedule)";
    wantedBy = [ "multi-user.target" ];
    after = [ "netradio-credentials.service" ];
    requires = [ "netradio-credentials.service" ];
    serviceConfig = hardening // {
      User = user;
      Group = user;
      ReadWritePaths = [ configDir "${djDir}/inbox" ];
      # /var/cache/netradio, made and owned by systemd. Resized cover art lives
      # here between requests: without it every thumbnail costs a full mutagen
      # decode of the track plus a Pillow resize, and the desktop wall asks for
      # ~150 of them on a cold load (2026-09-28).
      CacheDirectory = "netradio";
      CacheDirectoryMode = "0750";
      ExecStart = lib.concatStringsSep " " ([
        "${netradio}/bin/netradio admin"
        "--config ${configDir}" "--listen 127.0.0.1" "--port ${toString adminPort}"
        "--dj-dir ${djDir}" "--playlists ${playlistDir}"
        "--thumb-cache /var/cache/netradio/thumbs"
      ] ++ lib.optionals (cfg.receiver.enable && cfg.receiver.apiUrl != "") [
        "--receiver-api ${cfg.receiver.apiUrl}"
        "--now-dir ${nowDir}"
        "--wake http://127.0.0.1:${toString wakePort}"
        "--station-art ${stationArtDir}"
      ] ++ lib.optionals jellyfinReady [
        # the credentials alone; they let the art endpoint fall back to an
        # artist's Jellyfin picture when an album has no sleeve
        "--jellyfin-url ${cfg.jellyfin.url}"
        "--jellyfin-key-file ${cfg.jellyfin.keyFile}"
      ] ++ lib.optional (jellyfinReady && cfg.jellyfin.hearts)
        "--jellyfin-hearts"
      ++ lib.optional (jellyfinReady && cfg.jellyfin.user != "")
        "--jellyfin-user ${cfg.jellyfin.user}");
      Restart = "always";
      RestartSec = 5;
    };
  };

  # --- the privileged side: apply and compile -----------------------------------
  # The admin API can't restart units or run the agent, so it drops a request
  # file and this path unit (root) does the work:
  #   apply-*.json    rescan (netradio-playlists), then restart Liquidsoap +
  #                   the wake service ONLY if the station list changed —
  #                   a restart cuts live listeners for a few seconds.
  #   compile-*.json  build a feed's rule from its description: the prompt
  #                   carries the library's artist inventory and the genre
  #                   words; `claude -p` answers with one JSON object (as the
  #                   claude user, the way the weekly digest runs); the
  #                   result is validated and stored, then an apply follows.
  systemd.paths.netradio-apply = {
    wantedBy = [ "multi-user.target" ];
    pathConfig = {
      PathChanged = "${configDir}/requests";
      DirectoryNotEmpty = "${configDir}/requests";
      Unit = "netradio-apply.service";
    };
  };
  systemd.services.netradio-apply = {
    description = "netradio: act on admin requests (rescan / restart / compile a feed)";
    path = [ pkgs.coreutils pkgs.jq pkgs.util-linux pkgs.systemd pkgs.gnugrep ];
    serviceConfig = {
      Type = "oneshot";
      TimeoutStartSec = "30min";
    };
    script = ''
      set -uo pipefail
      req=${configDir}/requests
      shopt -s nullglob
      need_apply=0
      ${lib.optionalString cfg.compile.enable ''
      for f in "$req"/compile-*.json; do
        feed="$(jq -r .feed "$f")"
        rm -f "$f"
        [ -n "$feed" ] || continue
        echo "compiling feed $feed"
        work="$(mktemp -d /tmp/netradio-compile.XXXXXX)"
        chown ${cfg.compile.user} "$work"
        if runuser -u ${cfg.compile.user} -- env HOME=${cfg.compile.home} CLAUDE_AUTONOMOUS=1 \
             PATH=/etc/profiles/per-user/${cfg.compile.user}/bin:/run/current-system/sw/bin:/usr/bin:/bin \
             bash -c '
               cd ${cfg.compile.workingDirectory} || exit 1
               ${netradio}/bin/netradio compile-prompt --config ${configDir} --feed "$1" --genre-words ${configDir}/genre-words.json > "$2/prompt" || exit 1
               timeout 10m ${cfg.compile.program} -p "$(cat "$2/prompt")" > "$2/result" 2>/dev/null || exit 1
               ${netradio}/bin/netradio compile-apply --config ${configDir} --feed "$1" --result "$2/result"
             ' _ "$feed" "$work"; then
          echo "feed $feed compiled"
        else
          echo "feed $feed: compile failed (see above)"
          # compile-apply marks the feed failed with the real reason when it
          # ran; only when the agent produced nothing at all is there no result
          # file, and then the page should say that instead.
          if [ ! -s "$work/result" ]; then
            printf '${cfg.compile.program} -p produced no answer (timeout or failure) — see journalctl -u netradio-apply\n' > "$work/result"
            ${netradio}/bin/netradio compile-apply --config ${configDir} --feed "$feed" --result "$work/result" >/dev/null 2>&1 || true
          fi
        fi
        rm -rf "$work"
        need_apply=1
      done
      ''}
      for f in "$req"/apply-*.json; do
        echo "apply requested: $(jq -r '.reason // ""' "$f")"
        rm -f "$f"
        need_apply=1
      done
      [ "$need_apply" = 1 ] || exit 0
      systemctl start netradio-playlists.service
      # restart the audio chain only if the mount list changed
      want="$(jq -r '.[].mount' ${configDir}/stations.json | sort)"
      have="$(grep -oP '^station\("\K[^"]+' ${liqScript} 2>/dev/null | sort || true)"
      if [ "$want" != "$have" ]; then
        echo "station list changed: restarting liquidsoap + wake"
        systemctl restart netradio-liquidsoap.service netradio-wake.service
      else
        echo "station list unchanged: no restart"
      fi
    '';
  };

  # --- YCast: the vTuner directory ------------------------------------------
  systemd.services.ycast = lib.mkIf cfg.vtuner.enable {
    description = "YCast — vTuner internet radio directory emulation";
    wantedBy = [ "multi-user.target" ];
    # YCast decides ONCE at startup whether "My Stations" exists (it looks
    # for stations.yml then, never again). The 2026-09-16 13:04 deploy
    # started it before anything had written the file at its new path, and
    # the receiver's menu read "'My Stations' feature not configured." for
    # the rest of the day. The scanner is the only writer, and it runs on a
    # timer — so seed the file with the Internet Radio (JSON is YAML) when it
    # is missing, before YCast looks.
    after = [ "network-online.target" "netradio-credentials.service" ];
    wants = [ "network-online.target" ];
    environment.HOME = "/var/lib/ycast";  # ~/.ycast/cache: resized station icons
    serviceConfig = hardening // {
      DynamicUser = true;
      StateDirectory = "ycast";
      ExecStartPre = "+${pkgs.writeShellScript "ycast-seed-stations" ''
        f=${configDir}/stations.yml
        [ -s "$f" ] || install -o netradio -g users -m 664 ${ycastSeedYaml} "$f"
      ''}";
      ExecStart = "${lib.getExe ycast} -l 127.0.0.1 -p ${toString ycastPort} -c ${configDir}/stations.yml";
      Restart = "always";
      RestartSec = 5;
    };
  };

    environment.systemPackages = [ netradio ];
  };
}
