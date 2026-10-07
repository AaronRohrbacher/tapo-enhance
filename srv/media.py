"""Bound media I/O and honour cancellation even when the camera is silent."""

import asyncio


async def media_wait(awaitable, cancel: asyncio.Event, *, timeout: float):
    operation = asyncio.ensure_future(awaitable)
    cancelled = asyncio.create_task(cancel.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, cancelled), timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancelled in done:
            raise asyncio.CancelledError
        if operation not in done:
            raise TimeoutError("camera media I/O timed out")
        return await operation
    finally:
        for task in (operation, cancelled):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, cancelled, return_exceptions=True)
