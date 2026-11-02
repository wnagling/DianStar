"""Keep filesystem work off the UI loop; cancellation waits for safe cleanup."""
import asyncio
import threading
from contextlib import asynccontextmanager


async def blocking(function):
    cancel = threading.Event()
    task = asyncio.create_task(asyncio.to_thread(function, cancel))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancel.set()
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Consume cleanup errors; the journal retains recovery state.
        raise


@asynccontextmanager
async def held(factory):
    cancel = threading.Event()
    context = factory(cancel)
    task = asyncio.create_task(asyncio.to_thread(context.__enter__))
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel.set()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled() and task.exception() is None:
            context.__exit__(None, None, None)
        raise
    try:
        yield
    finally:
        context.__exit__(None, None, None)
