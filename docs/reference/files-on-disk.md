# What da-cli writes to disk

## Your downloaded art

One folder per deviation, under the destination you configured:

```text
<destination>/
├── <artist_username>/
│   ├── <deviation_title>/
│   │   ├── description.json          # title, author, stats, description, tags
│   │   └── image.{jpg,png,gif,webp}  # highest-resolution URL the API returned
│   └── <another_title>/
│       ├── description.json
│       └── image.png
└── <another_artist>/
    └── …
```

Sync is idempotent: a folder containing both `description.json` and an
`image.*` counts as already fetched and is skipped on later runs. To
force one to be re-downloaded, delete the file and run the sync again.

### Title collisions

Two deviations by the same artist whose titles sanitise to the same
folder name would collide. The second and later ones get a suffix:
`<title>--<shortid>/`, where `shortid` is the first 8 characters of the
deviation id. An existing folder without the suffix keeps its path, so
the suffix only ever appears on the collision.

### Video deviations

`image.{ext}` is the still preview, not the video. The video URL is in
`description.json` under `metadata.videos[]` — it is time-limited, so
fetch it soon after the sync.

### Atomic writes

Images are written to `image.{ext}.part` and renamed into place. A
crash mid-download therefore leaves no half-written `image.{ext}` that
the "already fetched" check would mistake for a complete file.

## State and configuration

| Path | Mode | What it holds |
| --- | --- | --- |
| `~/.config/da-cli/config.json` | 0600 | Non-secret settings: `client_id`, `destination`, `redirect_uri`, pacing. |
| `~/.local/state/da-cli/state.json` | 0600 | Access and refresh tokens, sync checkpoints, and per-artist gallery progress so an interrupted backfill resumes. Rewritten atomically on every refresh. |
| `~/.local/state/da-cli/index.db` | 0600 | SQLite index of what has been downloaded. |
| `~/.local/state/da-cli/loopback-cert.pem` | 0600 | Self-signed certificate for the `da auth` callback listener. |
| `~/.local/state/da-cli/loopback-key.pem` | 0600 | Its private key. |
| `~/.local/state/da-cli/.sync.lock` | 0600 | Advisory lock preventing two syncs at once. |
| `~/.local/state/da-cli/.token.lock` | 0600 | Advisory lock serialising refresh-token rotation across processes. |

On macOS the `client_secret` is stored in the Keychain instead of in
`config.json`. Elsewhere it falls back to `config.json` at mode 0600,
with a warning. See the [security model](../explanation/security.md).

Both directories follow the XDG specification, so `XDG_CONFIG_HOME` and
`XDG_STATE_HOME` relocate them — see
[environment variables](environment-variables.md).

## Installed files

| Path | Written by |
| --- | --- |
| `~/.local/share/da-cli/` | `install.sh` — the `da` shim and the `dacli` package |
| `~/.local/bin/da` | `install.sh` — symlink onto your `PATH` |
| `~/Applications/da-sync.app` | `install_schedule.sh` (macOS) |
| `~/Library/LaunchAgents/com.fz2000.da-cli.plist` | `install_schedule.sh` (macOS) |
| `~/Library/Logs/da-cli.log` | the scheduled job (macOS) |

## Removing everything

```bash
./install_schedule.sh uninstall        # macOS: remove the scheduled job
rm -rf ~/.local/share/da-cli ~/.local/bin/da
rm -rf ~/.config/da-cli ~/.local/state/da-cli
security delete-generic-password -s da-cli    # macOS: forget the secret
```

Your downloaded art is untouched by all of the above — delete the
destination folder yourself if you want it gone. Revoking da-cli's
access to your DeviantArt account is separate, and done at
<https://www.deviantart.com/settings/applications>.
