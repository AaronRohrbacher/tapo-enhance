# Tapo Enhance

The release version lives in [`VERSION`](VERSION). Release images receive that
version at build time; the same value appears in the UI header and at
`GET /api/app`.

**A local-first web interface for viewing and downloading recordings from a
Tapo camera you own.**

Tapo Enhance runs on a machine on your LAN and talks directly to the camera.
It provides live video, SD-card event browsing, native event thumbnails,
progressive playback, explicit downloads, battery status, discovery, and
recovery for sleeping or DHCP-moved cameras.

This project exists because owning hardware should include the freedom to use,
inspect, automate, repair, and improve it.

> [!IMPORTANT]
> Tapo Enhance is an independent community project. It is not affiliated with,
> endorsed by, or supported by TP-Link. Camera protocols and firmware can
> change. The current implementation has been developed and hardware-tested
> against a Tapo D225.

## What it does

- Streams the live camera feed to a browser as a short, rolling HLS window.
- Lists recording events stored on the camera's SD card.
- Retrieves the camera's native JPEG for an event thumbnail. It does **not**
  download an entire recording to manufacture a thumbnail.
- Starts archive playback as soon as the first HLS segments are ready.
- Downloads exactly the selected event only when you explicitly click
  **Download**. Watching and thumbnail generation do not archive recordings.
- Serializes camera media access so live video, thumbnails, playback, and
  downloads do not fight over the camera's single media session.
- Learns and persists the camera's MAC address and last working IP, wakes a
  sleeping camera only after a connection failure, and can rediscover a camera
  that moved after a DHCP lease change.
- Reports current work and failures to the UI over one server-sent event
  connection rather than constant status polling.

## Credit

Tapo Enhance is built on [Juraj Nyíri's `pytapo`](https://github.com/JurajNyiri/pytapo),
whose reverse-engineering and open-source implementation provide the foundation
for direct Tapo communication. Thank you to Juraj and the `pytapo` contributors.

The companion [`pytapo` fork](https://github.com/AaronRohrbacher/pytapo) is
maintained separately and pinned to an exact commit in `requirements.txt`.
The fork preserves
upstream history and licensing and adds native recording thumbnails plus
focused recording/live stream classes in the upstream project's style. This
keeps camera-protocol behavior in the library instead of duplicating it in the
web application.

## Requirements

- Linux on the same LAN as the camera
- Python 3.11 or newer
- `ffmpeg` and `ffprobe`
- `nmap` and the `ip` command for automatic rediscovery
- A Tapo camera account configured in the Tapo mobile app
- An SD card in the camera for archive browsing

On Debian or Ubuntu:

```bash
sudo apt install ffmpeg nmap iproute2 python3-venv
```

## Install

```bash
git clone https://github.com/AaronRohrbacher/tapo-enhance.git
cd tapo-enhance
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
make install
```

Optional non-secret server settings may be placed in `.env`:

```dotenv
APP_HOST=0.0.0.0
APP_PORT=8000
```

Camera credentials are deliberately rejected from environment variables.

Start the server:

```bash
make run
```

Then open `http://SERVER-IP:8000` from a browser on your LAN. `Ctrl-C` closes
active application connections and exits immediately.

If no camera credentials exist, the browser opens a first-run configuration
form. The same form is available later under **Settings → configure another
camera**. New details are verified against the camera before replacing the
working configuration.

First-run setup automatically derives the LAN subnet from the server's default
route and scans it for Tapo camera candidates. A single result fills the camera
address automatically; multiple results are shown for explicit selection. The
subnet and address remain editable for segmented networks or unusual routing.

### DVR Mode

DVR Mode is available during first-run setup and later under **Settings**. It
automatically downloads the camera's recording history, then checks on a
user-selected interval. The default is 12:10am server time each day for the
previous day's recordings; Settings also provides **Sync now**. The default
history length is the number of days from the
oldest recording currently available through today. A longer history is valid:
the app keeps checking and retains that many days as recordings become
available. Retention deletes only local copies; it never deletes recordings on
the camera. Initial setup shows the camera's available history; Settings shows
both camera history and the number of local dates containing downloaded video.

## Docker Compose

Compose is the recommended way to build and run Tapo Enhance:

```bash
mkdir -p cache custom-themes
docker compose up -d --build
```

The service uses host networking so discovery and Wake-on-LAN can reach the
LAN. Open `http://SERVER-IP:8000` and complete first-run setup. Encrypted state
survives container replacement in `./cache`; its separate installation key is
kept in the gitignored `./.keys` directory.

For unattended updates to published `latest` images, enable the optional
Watchtower service:

```bash
docker compose --profile updates up -d
```

Watchtower has access to the Docker socket, which is highly privileged. If you
prefer explicit updates, omit that profile and run:

```bash
docker compose pull
docker compose up -d
```

Publishing a GitHub Release triggers `.github/workflows/container.yml`, which
builds the application with the companion API fork and publishes release and
`latest` tags to GitHub Container Registry.

### Releasing a public image

1. Run `./scripts/deploy.py`. It proposes the next simple `major.minor`
   version (`0.1` for the first run) and updates `VERSION`. Use `--dry-run` to
   exercise the prompt without changing the file.
2. Review and commit the release changes.
3. Create and publish a GitHub Release whose tag is exactly `v` plus the
   `VERSION` value (for example, `v0.1`).
4. The container workflow publishes both
   `ghcr.io/aaronrohrbacher/tapo-enhance:<version>` and `:latest`, embedding the
   same version in the image and UI.
5. On the package page in GitHub, open **Package settings → Change visibility**
   and select **Public**. GHCR visibility is account/package state and cannot be
   guaranteed by repository code alone.

The workflow also supports manual dispatch, which publishes only the immutable
`VERSION` tag. Release publishing is intentionally left to a repository owner.

## Themes

Themes are YAML files, so comments are supported. The built-ins are Phosphor
(default), Amber Terminal, Midnight Blue, and Daylight. Selection is stored in
the browser, not on the camera.

Copy [`themes/phosphor.yaml`](themes/phosphor.yaml) into `custom-themes/`, give
it a unique `id` and `name`, and change any values under `colors`. Restart is
not required; reload the page and the server reads all YAML files again.
Unknown CSS variable names are ignored, malformed files are reported without
breaking valid themes, and files larger than 64 KiB are rejected.

## Device features

Settings includes a hardware-tested D225 dashboard for SD-card health and
capacity, recording/loop state, video format, battery profile, firmware state,
and supported detection/audio/camera states. Reversible controls are provided
for the status LED, motion/person/package detection, recording audio, doorbell
ring handling, and day/night mode. Deliberately omitted high-risk operations
include SD formatting, firmware flashing, sirens, and ambiguous power changes.

## Storage behavior

All generated data lives below `cache/` and is ignored by Git:

| Directory | Purpose | Retention |
| --- | --- | --- |
| `cache/recordings/` | User downloads and DVR copies | DVR retention, or kept until purged |
| `cache/thumbs/` | Native event JPEGs | Cached until purged |
| `cache/playback/` | Temporary HLS for archive viewing | Replaceable cache |
| `cache/stream/` | Rolling live HLS window | Bounded while live; removed on stop |
| `cache/previews/` | Compatibility preview cache | Replaceable cache |

Every bucket can be inspected and purged from the **Local** section. Purging
`recordings` removes user-downloaded copies, so that action is deliberately
separate from ordinary playback cleanup.

## Architecture

The FastAPI application serves the web UI and coordinates work. Camera protocol
operations live in the separately versioned `pytapo` fork. A single `CameraGateway` owns the
camera's media session and applies this priority order:

```text
explicit playback/download → visible thumbnails → live preview
```

Live preview pauses briefly when a higher-priority camera operation needs the
session, then resumes if the user is still on the Live section. FFmpeg converts
camera media to browser-compatible H.264/AAC and enforces the selected event's
exact duration when camera firmware ignores its requested end time.

## Security

Tapo Enhance currently has no web login. Treat it as a trusted-LAN service:

- Do not expose port 8000 directly to the public internet.
- Never place camera credentials in `.env`, Compose variables, or source files.
- If remote access is needed, put the app behind an authenticated reverse proxy
  or reach the LAN through a VPN.
- Run it as an unprivileged user with write access only to this project/cache.

During setup the password exists briefly in server memory while the camera is
verified. It is then stored only as AES-256-GCM authenticated ciphertext in
`cache/credentials.vault` (mode `0600`). The installation key is separate: a
gitignored `.keys/` directory, mounted at `/keys` in Docker. Neither is
included in Git or the image, and the configuration API never returns the
password. Losing the installation key makes the vault intentionally
unrecoverable; reconfigure the camera to create a new pair.

Initial setup currently travels over HTTP on the trusted LAN. Encryption at
rest does not encrypt that network request. Deploy behind HTTPS before allowing
setup or access across an untrusted network.

The web application still has no login. Anyone who can reach it can operate or
reconfigure the camera, so network isolation or an authenticated reverse proxy
is mandatory outside a trusted LAN.

No Tapo cloud service is required by Tapo Enhance's application workflow.

## Development and tests

```bash
make install-dev
make test-all
```

The suite includes unit tests, API integration tests, real FFmpeg/FFprobe media
tests, gateway contention tests, and Playwright browser tests. Camera-independent
tests use a fake Tapo implementation and generated media fixtures.

Useful commands:

```bash
make test          # backend and integration tests
make test-browser  # browser tests
make clean         # transient media caches and test artifacts
```

Contributions should preserve the central invariants: one camera media session
at a time; no full-video thumbnail generation; no archive write without an
explicit download; bounded transient storage; and errors visible to the user.

## License

Tapo Enhance is licensed under GPL-2.0-only; see [`LICENSE`](LICENSE). The
companion `pytapo` fork retains the upstream MIT license and attribution. Its
Tapo Enhance-specific additions are distributed under GPL-2.0-only.
