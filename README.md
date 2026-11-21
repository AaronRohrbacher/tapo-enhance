# Tapo Enhance

A local-first web interface for viewing and downloading recordings from your
Tapo camera.

Tapo Enhance runs on a machine on your LAN and talks directly to the camera.
It provides live video, SD-card event browsing, native event thumbnails,
progressive playback, explicit downloads, battery status, discovery, and
recovery for sleeping or DHCP-moved cameras.

Developed and hardware-tested with the **Tapo D225**. Compatibility with other
models depends on their firmware and supported protocols. This is an independent
community project, unaffiliated with TP-Link.

## Credit

Tapo Enhance builds on [Juraj Nyíri's `pytapo`](https://github.com/JurajNyiri/pytapo).
Thank you to Juraj and the contributors for making direct Tapo communication
possible. The separately maintained
[`pytapo` fork](https://github.com/AaronRohrbacher/pytapo) adds native recording
thumbnails and recording/live stream support, and is pinned to an exact commit
in `requirements.txt`.

## Installation

### Docker

Requires Docker Engine on Linux, on the camera's LAN. The published image
includes the application and its dependencies.

#### Docker run

Pull the latest image from Docker Hub and start the container in one command:

```bash
docker run -d --pull=always --name tapo-enhance \
  --network host \
  --restart unless-stopped \
  -e APP_PORT=8000 \
  -e TAPO_CACHE_ROOT=/data \
  -e TAPO_KEY_ROOT=/keys \
  -v tapo-data:/data \
  -v tapo-keys:/keys \
  aaronrohrbacher/tapo-enhance:latest
```

Open `http://localhost:8000` on the Docker host or `http://DOCKER-HOST:8000`
from another machine on the LAN.

#### Docker Compose

Alternatively, use Docker Compose with the same published image. Choose one
of these installation methods; you do not need to run both.

Create a directory for the application and save the following as `compose.yaml`.
It pulls the published image directly from
[Docker Hub](https://hub.docker.com/r/aaronrohrbacher/tapo-enhance); no source
checkout or Python installation is needed.

```yaml
services:
  tapo-enhance:
    image: aaronrohrbacher/tapo-enhance:latest
    network_mode: host
    environment:
      APP_PORT: 8000
      TAPO_CACHE_ROOT: /data
      TAPO_KEY_ROOT: /keys
    volumes:
      - tapo-data:/data
      - tapo-keys:/keys
    restart: unless-stopped

volumes:
  tapo-data:
  tapo-keys:
```

From that directory, start the application:

```bash
docker compose up -d
```

Open `http://localhost:8000` on the Docker host
or `http://DOCKER-HOST:8000` from the LAN. Host networking is required so
camera discovery sees and scans the Docker host's physical LAN directly.

The application image includes `nmap`, `iproute2`, `ffmpeg`, `ffprobe`, and
`curl`. If automatic subnet detection needs an override, enter the LAN subnet
in first-run setup or add `TAPO_SUBNET` to the Compose `environment` section:

```yaml
      TAPO_SUBNET: 192.168.1.0/24
```

Encrypted state survives container replacement in the Docker-managed
`tapo-data` volume; its separate installation key is kept in the `tapo-keys`
volume. Both volumes are prepared for the unprivileged application user at
container startup.

Custom networking is an advanced deployment choice. The supplied configuration
uses host networking and does not install network plugins or change the host's
forwarding or firewall settings.

To update to the latest published image:

```bash
docker compose pull
docker compose up -d
```

To pin a release, replace `latest` in the image name with a version such as
`0.1.2`. Updates preserve the named volumes and saved configuration.

### From the repository

#### Requirements

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

#### Install and run

```bash
git clone https://github.com/AaronRohrbacher/tapo-enhance.git
cd tapo-enhance
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
```

Optional non-secret server settings may be placed in `.env`:

```dotenv
APP_HOST=0.0.0.0
APP_PORT=8000
```

Camera credentials are deliberately rejected from environment variables.

Start the server:

```bash
.venv/bin/python app.py
```

Then open `http://SERVER-IP:8000` from a browser on your LAN. `Ctrl-C` closes
active application connections and exits immediately.

### Connect your camera

If no camera credentials exist, the browser opens a first-run configuration
form. The same form is available later under **Settings → configure another
camera**. New details are verified against the camera before replacing the
working configuration.

First-run setup automatically derives the LAN subnet from the server's default
route and scans it for Tapo camera candidates. A single result fills the camera
address automatically; multiple results are shown for explicit selection. The
subnet and address remain editable for segmented networks or unusual routing.

## What it does

### Live view and recordings

Watch live video, browse SD-card events by date, and start playback while the
recording is still arriving. Native camera thumbnails let you find an event
without downloading the whole clip.

Download a single event or select several for a batch download. Saved clips
carry a **LOCAL** indicator and play directly from disk. Watching a recording
does not save a permanent copy. The app remembers your camera and can rediscover
it after a DHCP address change.

### DVR Mode

DVR Mode is available during first-run setup and later under **Settings**. It
automatically downloads the camera's recording history, then checks on a
user-selected interval. The default is 12:10am server time each day for the
previous day's recordings. **Archive** shows sync status and provides **Sync
now**; locally downloaded camera clips carry a persistent **LOCAL** indicator,
including clips downloaded by DVR Mode. Local clips are validated and played
from disk without opening another camera media session. A corrupt cached copy
is retrieved again and atomically replaced. The default
history length is the number of days from the
oldest recording currently available through today. A longer history is valid:
the app keeps checking and retains that many days as recordings become
available. Retention deletes only local copies; it never deletes recordings on
the camera. Initial setup shows the camera's available history; Archive shows
both camera history and the number of local dates containing downloaded video.

### Themes

Themes are YAML files, so comments are supported. The built-ins are Phosphor
(default), Amber Terminal, Midnight Blue, and Daylight. Selection is stored in
the browser, not on the camera.

Copy [`themes/phosphor.yaml`](themes/phosphor.yaml) into `custom-themes/`, give
it a unique `id` and `name`, and change any values under `colors`. Restart is
not required; reload the page and the server reads all YAML files again.
Unknown CSS variable names are ignored, malformed files are reported without
breaking valid themes, and files larger than 64 KiB are rejected.

### Camera settings

Settings includes a hardware-tested D225 dashboard for SD-card health and
capacity, recording/loop state, video format, battery profile, firmware state,
and supported detection/audio/camera states. Reversible controls are provided
for the status LED, motion/person/package detection, recording audio, doorbell
ring handling, and day/night mode. Deliberately omitted high-risk operations
include SD formatting, firmware flashing, sirens, and ambiguous power changes.

### Local storage

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
