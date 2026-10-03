"""Retain request admission while non-cancellable work releases its resources."""
import asyncio


async def complete_before_cancel(awaitable):
    task = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # In particular, cancelling to_thread does not stop its physical thread.
        # Repeated caller cancellation must not let it outlive its resource slot.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                pass
            except Exception:
                break
        if not task.cancelled():
            task.exception()  # Consume failures while preserving caller cancellation.
        raise
