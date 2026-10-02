# Canned profiles

Three answers files the end-to-end test installs into a VM (`tests/vm-test.sh`),
and three starting points for a real install: copy one, replace every
`REPLACE-…` placeholder (disk ids, your SSH key), set your domain, and run
`homelab-configure generate --answers my.json --out my-homelab --secret …`.

| profile | what it is |
|---|---|
| `media-box.json` | the media server: Jellyfin, audiobooks, the download stack inside a VPN, Lidarr, parity on the pool |
| `docs-forge.json` | the household office: Nextcloud, Paperless, Vaultwarden, Forgejo, notes, recipes, photos, with single sign-on |
| `everything.json` | every library module that needs no manual artifact (all but `meshagent` and `recyclarr`) |

All three carry the foundation set (`backup`, `mergerfs-pools`, monitoring and
notifications); `system` and `boot` are added by the configurator.
