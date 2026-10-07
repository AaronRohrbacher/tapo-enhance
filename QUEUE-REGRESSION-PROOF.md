# Live-to-recordings queue regression: evidence, 2026-10-07

## Observed running service

One read of `GET /api/gateway/status` returned:

```json
{"ok":true,"live_attached":false,"live_running":false,"queued":2,"busy":null}
```

The running container's gateway source contained the same unprotected live
teardown as the checkout before this fix. The status alone does not prove
the running worker's exception; the controlled reproduction below proves
the matching code path can crash and strand requests.

## Reproduction before the fix

Command:

```sh
python -m pytest -q tests/test_gateway.py -k live_stop_racing
```

Sequence: attach Live; start stopping it; submit dates and show-date while
the live session is closing; allow cancellation/close to finish.

Observed result: **1 failed**, queued queries timed out. Worker traceback:

```text
File "srv/gateway.py", line 275, in _run
    await self._stop_live_if_running(for_pause=True)
File "srv/gateway.py", line 321, in _stop_live_if_running
    self._live_task.cancel()
AttributeError: 'NoneType' object has no attribute 'cancel'
```

The HTTP stop handler and gateway worker both awaited and cleared the same
mutable live-task reference. One cleared it while the other still used it.

## Fix and results

Live teardown is serialized with a lock and retains its own task reference.
The worker cancels only its own temporary wake waiter, not a task identified
through the mutable live-task field. Detach explicitly wakes the worker.

The identical reproduction command after the fix: **1 passed**.

Repeated that reproduction **100 times**: **100/100 completed**, both queries
returned, queues drained, workers remained alive.

HTTP regression command:

```sh
python -m pytest -q tests/test_web.py -k live_to_recordings
```

Result: **1 passed**. It exercises real application routes with a simulated
camera and controlled live teardown:

- POST Live start returns 200.
- POST Live stop races GET dates and GET recordings for a date; all return 200.
- Date response contains the expected two clips.
- Remote recording playback returns camera HLS; its playlist returns 200.
- Recording download uses real FFmpeg and returns 200.
- Playing that downloaded recording reports `source: local`; its MP4 returns 200.
- Final queue length is zero, nothing is busy, and the worker is still alive.

Final checkout validation:

```text
python -m pytest -q
184 passed, 13 deselected

python -m pytest -q tests/test_browser.py -m browser
13 passed

git diff --check
passed
```

## Verification boundary

These results prove the reproduced lifecycle race is corrected locally.
The camera is simulated; media processing uses real FFmpeg. Browser tests
are additional general coverage, not physical-camera proof. No production
deployment, restart, Git mutation, or infrastructure change was performed.
The updated code has not been verified against the physical camera.
