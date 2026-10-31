import asyncio
import subprocess
import sys
import threading

import pytest

from async_db import bounded_call
from shared_state import SharedStateError


def test_stuck_call_does_not_block_loop_or_process_shutdown():
    # An executor-backed network call would trap asyncio.run's shutdown here.
    code = """
import asyncio, threading
from async_db import bounded_call
from shared_state import SharedStateError
async def main():
    try:
        await bounded_call(lambda: threading.Event().wait(60), timeout=0.03)
    except SharedStateError:
        print('bounded shutdown', flush=True)
asyncio.run(main())
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=3)
    assert result.returncode == 0 and "bounded shutdown" in result.stdout


def test_late_connection_is_discarded_after_constructor_timeout():
    release, disposed = threading.Event(), threading.Event()
    async def run():
        try:
            with pytest.raises(SharedStateError, match="deadline"):
                await bounded_call(lambda: release.wait(1), timeout=0.02,
                                   discard=lambda _: disposed.set())
        finally:
            release.set()
        assert await asyncio.to_thread(disposed.wait, 1)
    asyncio.run(run())
