# notify — thin wrapper around curl -> an ntfy instance.
#
# A plain function, not a module: import it where a script needs to send
# alerts, passing the ntfy URL (usually config.homelab.ntfy.url). Keeping it
# standalone avoids module-to-module dependencies.
{ pkgs, url }:

pkgs.writeShellApplication {
  name = "notify";
  runtimeInputs = [ pkgs.curl ];
  text = ''
    # Usage: notify <title> <message> [priority] [tags] [click-url]
    #   priority:  min | low | default | high | urgent
    #   tags:      comma-separated ntfy tags/emoji (e.g. warning,floppy_disk)
    #   click-url: makes the whole notification tappable (ntfy "Click:" header)
    title=''${1:?usage: notify <title> <message> [priority] [tags] [click-url]}
    message=''${2:?usage: notify <title> <message> [priority] [tags] [click-url]}
    priority=''${3:-default}
    tags=''${4:-}
    click=''${5:-}

    args=( -fsS --max-time 15
           -H "Title: $title"
           -H "Priority: $priority" )
    if [ -n "$tags" ]; then
      args+=( -H "Tags: $tags" )
    fi
    if [ -n "$click" ]; then
      args+=( -H "Click: $click" )
    fi
    curl "''${args[@]}" -d "$message" \
      "${url}" > /dev/null
  '';
}
