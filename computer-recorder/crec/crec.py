from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Callable

from .models import Observation, init_db
from .observers import Observer
from .schemas import Update
from .diagnostics import QueuedConsoleHandler

class crec:
    def __init__(
        self,
        user_name: str,
        *observers: Observer,
        data_directory: str = "~/Downloads/records",
        db_name: str = "actions.db",
        max_concurrent_updates: int = 4,
        verbosity: int = logging.INFO,
    ):
        # basic paths
        data_directory = os.path.expanduser(data_directory)
        os.makedirs(data_directory, exist_ok=True)

        # runtime
        self.user_name = user_name
        self.observers: list[Observer] = list(observers)

        # logging
        self.logger = logging.getLogger("crec")
        self.logger.setLevel(verbosity)
        if not self.logger.handlers:
            h = QueuedConsoleHandler()
            h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
            self.logger.addHandler(h)
            # Do not also send records to a synchronous root console handler.
            self.logger.propagate = False

        self.engine = None
        self.Session = None
        self._db_name        = db_name
        self._data_directory = data_directory

        self._update_sem = asyncio.Semaphore(max_concurrent_updates)
        self._tasks: set[asyncio.Task] = set()
        self._loop_task: asyncio.Task | None = None
        self._write_errors = []
        self._observer_writes = {}
        self.update_handlers: list[Callable[[Observer, Update], None]] = []

    def start_update_loop(self):
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._update_loop())

    async def stop_update_loop(self):
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

    async def connect_db(self):
        if self.engine is None:
            self.engine, self.Session = await init_db(
                self._db_name, self._data_directory
            )

    async def __aenter__(self):
        await self.connect_db()
        self.start_update_loop()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        # Keep the DB consumer alive while observers finish their input backlog.
        errors = []
        try:
            for obs in self.observers:
                try:
                    await obs.stop()
                except Exception as error:
                    errors.append(error)
            if self._loop_task is not None:
                drained = asyncio.ensure_future(asyncio.gather(
                    *(obs.update_queue.join() for obs in self.observers)))
                try:
                    done, _ = await asyncio.wait(
                        (drained, self._loop_task), return_when=asyncio.FIRST_COMPLETED)
                    if self._loop_task in done:
                        await self._loop_task  # Surface consumer failure, never hang on join.
                    await drained
                finally:
                    if not drained.done():
                        drained.cancel()
                    await asyncio.gather(drained, return_exceptions=True)
        finally:
            await self.stop_update_loop()
            if self._tasks:
                await asyncio.gather(*tuple(self._tasks))
        if self._write_errors:
            # Retain failed updates for recovery instead of silently claiming
            # persistence. Successful writes have already committed.
            failures, self._write_errors = self._write_errors, []
            for observer, update, error in failures:
                observer.update_queue.put_nowait(update)
            raise RuntimeError(f"{len(failures)} recorder update(s) could not be persisted") from failures[0][2]
        if errors:
            raise RuntimeError("Observer shutdown failed") from errors[0]

    def _dispatch_update(self, observer, update):
        previous = self._observer_writes.get(observer)
        task = asyncio.create_task(self._run_with_gate(observer, update, previous))
        self._observer_writes[observer] = task
        self._tasks.add(task)

    async def _update_loop(self):
        while True:
            if not self.observers:
                await asyncio.sleep(0.05)
                continue
            gets = {asyncio.create_task(obs.update_queue.get()): obs
                    for obs in self.observers}
            try:
                await asyncio.wait(gets, return_when=asyncio.FIRST_COMPLETED)
            finally:
                # No orphan queue.get tasks: a pending getter may have acquired
                # an update during cancellation, and that update still belongs
                # to this consumer and must be dispatched exactly once.
                for future in gets:
                    if not future.done():
                        future.cancel()
                await asyncio.gather(*gets, return_exceptions=True)
                for future, observer in gets.items():
                    if not future.cancelled():
                        self._dispatch_update(observer, future.result())

    async def _run_with_gate(self, observer: Observer, update: Update, previous=None):
        try:
            # Commit each observer's updates in delivery order, even when a
            # previous write is slow. Independent observers may still overlap.
            if previous is not None:
                await previous
            async with self._update_sem:
                await self._default_handler(observer, update)
        except Exception as error:
            self._write_errors.append((observer, update, error))
            self.logger.exception("Failed to persist recorder update")
        finally:
            observer.update_queue.task_done()
            self._tasks.discard(asyncio.current_task())
            if self._observer_writes.get(observer) is asyncio.current_task():
                self._observer_writes.pop(observer, None)

    async def _handle_audit(self, obs: Observation) -> bool:
        return False

    async def _default_handler(self, observer: Observer, update: Update) -> None:
        self.logger.info(f"Processing update from {observer.name}")
        self.logger.info(f"Content ({update.content_type}): {update.content[:10]}")

        async with self._session() as session:
            observation = Observation(
                observer_name=observer.name,
                content=update.content,
                content_type=update.content_type,
            )

            if await self._handle_audit(observation):
                return

            session.add(observation)
            await session.flush()

    @asynccontextmanager
    async def _session(self):
        async with self.Session() as s:
            async with s.begin():
                yield s

    def add_observer(self, observer: Observer):
        self.observers.append(observer)

    def remove_observer(self, observer: Observer):
        if observer in self.observers:
            self.observers.remove(observer)

    def register_update_handler(self, fn: Callable[[Observer, Update], None]):
        self.update_handlers.append(fn)
