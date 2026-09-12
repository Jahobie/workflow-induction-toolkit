"""Recorder-only integration tests against real temporary SQLite databases."""
import asyncio
import logging
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from crec.crec import crec
from crec.models import Observation
from crec.observers.observer import Observer
from crec.schemas import Update


class ShutdownObserver(Observer):
    def __init__(self, name, final_actions=()):
        self.final_actions = final_actions
        self.started = asyncio.Event()
        super().__init__(name)

    async def _worker(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            for action in self.final_actions:
                await self.update_queue.put(Update(content=action, content_type='input_text'))


class RecorderShutdownTests(unittest.IsolatedAsyncioTestCase):
    async def test_shutdown_waits_for_database_commits_and_preserves_final_actions(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = ShutdownObserver('screen', ["key_press('/')", 'click_left(20.0, 30.0)'])
            recorder = crec('test', observer, data_directory=directory, verbosity=logging.CRITICAL,
                            max_concurrent_updates=4)
            release, entered = asyncio.Event(), asyncio.Event()
            original = recorder._default_handler
            async def blocked_handler(*args):
                if args[1].content == "key_press('a')":
                    entered.set()
                    await release.wait()
                await original(*args)
            with patch.object(recorder, '_default_handler', side_effect=blocked_handler):
                await recorder.__aenter__()
                await observer.started.wait()
                await observer.update_queue.put(Update(content="key_press('a')", content_type='input_text'))
                await entered.wait()
                stopping = asyncio.create_task(recorder.__aexit__(None, None, None))
                await asyncio.sleep(0.02)
                self.assertFalse(stopping.done(), 'shutdown must wait for commit')
                release.set()
                await asyncio.wait_for(stopping, 5)
            async with recorder.Session() as session:
                actual = list((await session.scalars(select(Observation.content).order_by(Observation.id))).all())
            self.assertEqual(actual, ["key_press('a')", "key_press('/')", 'click_left(20.0, 30.0)'])
            self.assertEqual(len(actual), 3)
            self.assertTrue(observer.update_queue.empty())
            self.assertFalse(recorder._tasks)
            self.assertIsNone(recorder._loop_task)
            await recorder.engine.dispose()

    async def test_multiple_observers_do_not_leave_orphan_getters(self):
        with tempfile.TemporaryDirectory() as directory:
            observers = [ShutdownObserver('one', ['last-one']), ShutdownObserver('two', ['last-two'])]
            recorder = crec('test', *observers, data_directory=directory, verbosity=logging.CRITICAL)
            expected = ['last-one', 'last-two']
            async with recorder:
                for i in range(20):
                    for obs in observers:
                        content = f'{obs.name}-{i}'
                        expected.append(content)
                        await obs.update_queue.put(Update(content=content, content_type='input_text'))
                    await asyncio.sleep(0)
            async with recorder.Session() as session:
                actual = list((await session.scalars(select(Observation.content).order_by(Observation.id))).all())
            self.assertCountEqual(actual, expected)
            self.assertTrue(all(not obs.update_queue._getters for obs in observers))
            await recorder.engine.dispose()

    async def test_failed_write_is_reported_and_update_remains_recoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            observer = ShutdownObserver('screen', ["key_press('a')"])
            recorder = crec('test', observer, data_directory=directory, verbosity=logging.CRITICAL)
            with patch.object(recorder, '_default_handler', AsyncMock(side_effect=OSError('disk full'))):
                with self.assertRaisesRegex(RuntimeError, 'could not be persisted'):
                    async with recorder:
                        await observer.started.wait()
            self.assertEqual(observer.update_queue.get_nowait().content, "key_press('a')")
            self.assertFalse(recorder._tasks)
            await recorder.engine.dispose()

    async def test_observer_stop_keeps_undelivered_updates(self):
        observer = ShutdownObserver('screen', ['final'])
        await observer.started.wait()
        await observer.stop()
        self.assertEqual(observer.update_queue.get_nowait().content, 'final')
