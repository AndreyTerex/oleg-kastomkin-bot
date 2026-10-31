"""Bound synchronous database work without blocking Discord or executor shutdown.

A stuck libpq call cannot be killed safely from another Python thread. Dedicated
daemon workers let the process stop after a deadline; they are never reused after
timeout. Unlike asyncio.to_thread, they cannot trap asyncio.run at executor shutdown.
"""
from __future__ import annotations

import asyncio
import threading

from shared_state import SharedStateDeadline

OPERATION_TIMEOUT = 20.0
CLOSE_TIMEOUT = 2.0


async def bounded_call(action, *, timeout=OPERATION_TIMEOUT, invalidate=None, discard=None):
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    abandoned = threading.Event()

    def deliver(value, error):
        if future.done():
            if error is None and discard is not None:
                threading.Thread(target=lambda: discard(value), daemon=True).start()
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(value)

    def work():
        value, error = None, None
        try:
            value = action()
        except BaseException as exc:
            error = exc
        if abandoned.is_set() or loop.is_closed():
            if error is None and discard is not None:
                discard(value)
            return
        try:
            loop.call_soon_threadsafe(deliver, value, error)
        except RuntimeError:
            if error is None and discard is not None:
                discard(value)

    threading.Thread(target=work, name="oleg-db-operation", daemon=True).start()
    try:
        return await asyncio.wait_for(future, timeout)
    except (TimeoutError, asyncio.CancelledError) as error:
        abandoned.set()
        if invalidate is not None:
            invalidate()
        if isinstance(error, asyncio.CancelledError):
            raise
        raise SharedStateDeadline("Database operation exceeded its deadline; bot stopping, commit outcome requires readback") from None
