# Sync and application bug hunt — 2026-10-17

The non-DVR manual download path now has a prominent Sync status card in Recordings. It shows the current operation, duration-weighted progress, ready/failed counts, and error details. Single downloads use the same visible status area. Completion remains visible. The disabled DVR card no longer occupies the top of the non-DVR screen. Cache controls are in Settings.

## Root causes and corrections

- Manual bulk jobs polled `/api/jobs/{id}` every 1.5 seconds while neither the fetcher nor converter reported progress to the browser. The job counter waited until the download pass ended before accounting for conversions. Jobs now push snapshots over the existing `/api/events` connection, and count each conversion as it finishes while subsequent downloads continue. The frontend has no job polling loop.
- A reload lost active manual job state. SSE now sends consistent snapshots of jobs, downloads, DVR, gateway, cache and thumbnail work before sending later events. Job revisions reject stale updates. Heartbeats keep idle connections open; a subscriber that falls behind reconnects for snapshots rather than silently losing completion events.
- Staged transport-stream fetches and finished MP4 downloads used the same gateway key despite returning different file formats. Separate keys now preserve each operation's result contract while camera access remains serialized. A regression exercises both operations together and probes the resulting MP4 for H.264 and AAC.
- Conversion progress and output could conflict when overlapping jobs targeted the same clip. The conversion queue shares one encoder per destination and fans progress out to each subscriber. Single-download temporary MP4s have a separate filename from staged conversions. Repeated identical active bulk submissions reuse the job.
- Conversion cancellation occurred outside its process cleanup block. Encoders now have bounded execution and are killed and awaited on cancellation/error. Application shutdown stops manual jobs and conversion tasks. Cancelling a consumer does not cancel a shared encoder. Complete staged downloads survive failed/cancelled conversions.
- Interrupted staged transfers could look resumable, and short recordings could be announced as complete. Fetches write a partial filename, validate duration/video, then publish the staged filename. Final MP4s are validated before publication. Invalid transfers cannot be reported as successful downloads.
- Selection and unrelated state changes rebuilt every thumbnail node. Existing cards are now patched in place. Date navigation ignores older responses, repeated date loads retain thumbnail state, and unsaved DVR settings survive navigation. Unrelated updates also preserve device feature controls.
- A delayed Live-player attachment could run after switching away. It now checks its request generation. Late Live events cannot restart a stopped player. Replaced snapshot object URLs are released.
- Invalid clip dates could escape the intended cache directory; invalid JSON and timestamp shapes could raise unstructured errors. Shared request/date/clip validation rejects them before camera or file access. Privacy accepts actual booleans, cache targets require strings, and camera metadata is escaped before HTML rendering.
- Snapshot, thumbnail and media-probe subprocesses are reaped when their tasks are cancelled.

## Verification

Final results: **224 backend tests passed** (`21` browser tests deliberately deselected in that invocation), then **21 Chromium browser tests passed** in the separate browser invocation. No tests were skipped. Python compilation, all three JavaScript syntax checks, and diff whitespace checks passed.

Run from the repository root:

```sh
python -m pytest -q
python -m pytest -q tests/test_browser.py -m browser
python -m compileall -q srv tests
node --check static/main.js
node --check static/ui.js
node --check static/state.js
git diff --check
```

Browser verification uses an actual Chromium process and a real local Uvicorn server. Successful media tests run actual FFmpeg and ffprobe against generated camera-shaped H.265/AAC transport streams. The browser verifies downloaded video decodes, local playback does not reacquire it from the camera, and manual sync works with DVR disabled.

`tests/test_browser.py` asserts intermediate progress, conversion overlap, immediate completion counts, visible failures, refresh during an active job, finished-job restoration, zero job-status GET requests, preservation of thumbnails/settings controls, and the Live tab-switch race. Each server fixture shuts down and checks its thread has exited. Screenshots are generated at `/tmp/tapo-enhance-manual-sync-running.png` and `/tmp/tapo-enhance-manual-sync-complete.png` and were visually inspected.

The missing Playwright Chromium executable was installed with `python -m playwright install chromium`. The in-app browser backend exposes no sessions in this environment; these results come from working standalone Chromium browser tests, not from an in-app browser claim.

Camera responses are simulated. These checks do not constitute physical-camera or deployed-container verification. No production deployment, service restart, Git mutation, or changes to original recordings were performed.
