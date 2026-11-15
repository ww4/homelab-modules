"""netradio — the glue around Icecast + Liquidsoap for the library stations.

Two commands, one package:

  playlists   scan the music library's genre tags into one .m3u per station
  wake        start a station's encoder when a listener asks for it, stop it
              again once nobody has listened for a while
  dj          sequence each station's tracks and, every few of them, say what
              just played and what is next (Kokoro)
  profile     listen to each track once: is it talk (kept off the stations),
              does it start or end in chatter (no crossfade over it), what
              era does it sound like
  profile-server  the same listening as an HTTP service, for the fast box
  admin       the API behind <radio host>/admin (feeds, stations,
              schedule); files compile/apply requests for the root side
  liq         render the Liquidsoap script from the runtime station list
  compile-prompt / compile-apply
              the AI step that turns a feed's description into its rule
"""
