"""Real PTY/SQLite tests, with synthetic actions instead of desktop input."""
import asyncio
import io
import json
import logging
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from crec import cli
from crec.crec import crec
from crec.models import Observation
from crec.observers.observer import Observer
from crec.schemas import Update
from sqlalchemy import select as sql_select

if os.name == 'posix':
    import pty
    import termios


class FinalObserver(Observer):
    async def _worker(self):
        try:
            await asyncio.Event().wait()
        finally:
            await self.update_queue.put(
                Update(content='final-action', content_type='input_text'))


async def console_probe(directory, interrupt):
    # A synchronous root handler must not receive recorder child diagnostics.
    logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))
    observer = FinalObserver('synthetic-screen')
    recorder = crec('terminal-probe', observer, data_directory=directory)
    cancelled = False
    try:
        async with recorder:
            print('READY', flush=True)
            await asyncio.to_thread(sys.stdin.readline)
            # Saturate the bounded diagnostic queue while the terminal is paused.
            for i in range(3000):
                logging.getLogger('crec.screen').warning('synthetic diagnostic %s', i)
            for i in range(30):
                await observer.update_queue.put(Update(
                    content=f"key_press('{i}')", content_type='input_text'))
                logging.getLogger('crec.linux.inputs').warning('input diagnostic %s', i)
                await asyncio.sleep(.01)
            if interrupt:
                print('INTERRUPT_READY', flush=True)
                await asyncio.Event().wait()
    except asyncio.CancelledError:
        cancelled = True
    async with recorder.Session() as session:
        actual = list((await session.scalars(
            sql_select(Observation.content).order_by(Observation.id))).all())
    await recorder.engine.dispose()
    handler = recorder.logger.handlers[0]
    print(json.dumps(dict(actions=actual, cancelled=cancelled,
                          dropped=handler.dropped_records,
                          queue_size=handler._queue.qsize(),
                          capacity=handler._queue.maxsize)), flush=True)


@unittest.skipUnless(os.name == 'posix', 'requires a POSIX terminal')
class TerminalLoggingTests(unittest.TestCase):
    def read_line(self, process):
        self.assertTrue(select.select([process.stdout], [], [], 10)[0],
                        'child stalled while terminal output was paused')
        line = process.stdout.readline()
        self.assertTrue(line, 'child exited without the expected result')
        return line.decode().strip()

    def run_paused_recorder(self, interrupt):
        with tempfile.TemporaryDirectory(prefix='crec-tty-test-') as directory:
            master, slave = pty.openpty()
            attrs = termios.tcgetattr(slave)
            attrs[0] |= termios.IXON
            attrs[6][termios.VSTOP] = b'\x13'
            attrs[6][termios.VSTART] = b'\x11'
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            process = subprocess.Popen(
                [sys.executable, '-I', '-B', str(Path(__file__).resolve()),
                 '--probe', directory, str(int(interrupt))],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=slave,
                bufsize=0)
            try:
                self.assertEqual(self.read_line(process), 'READY')
                os.write(master, b'\x13')  # Actual Ctrl+S through the tty driver.
                time.sleep(.03)
                self.assertFalse(select.select([], [slave], [], .1)[1],
                                 'PTY must really be output-paused for this regression')
                process.stdin.write(b'go\n')
                if interrupt:
                    self.assertEqual(self.read_line(process), 'INTERRUPT_READY')
                    process.send_signal(signal.SIGINT)
                result = json.loads(self.read_line(process))
                # Never send Ctrl+Q. Both persistence and interpreter shutdown
                # must succeed with the diagnostic writer still blocked.
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual(result['actions'],
                                 [f"key_press('{i}')" for i in range(30)] + ['final-action'])
                self.assertEqual(result['cancelled'], interrupt)
                self.assertGreater(result['dropped'], 0)
                self.assertLessEqual(result['queue_size'], result['capacity'])
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                process.stdin.close()
                process.stdout.close()
                os.close(master)
                os.close(slave)

    def test_ctrl_s_preserves_database_actions_and_process_exit(self):
        self.run_paused_recorder(interrupt=False)

    def test_ctrl_c_drains_actions_while_console_remains_paused(self):
        self.run_paused_recorder(interrupt=True)

    def test_cli_disables_ixon_and_restores_terminal_on_success_and_failure(self):
        master, slave = pty.openpty()
        try:
            original = termios.tcgetattr(slave)
            original[0] |= termios.IXON
            termios.tcsetattr(slave, termios.TCSANOW, original)
            with os.fdopen(os.dup(slave)) as terminal:
                for fail in (False, True):
                    async def fake_main():
                        self.assertFalse(termios.tcgetattr(slave)[0] & termios.IXON)
                        if fail:
                            raise RuntimeError('synthetic backend failure')
                    with patch.object(cli.sys, 'stdin', terminal), \
                         patch.object(cli.sys, 'stderr', io.StringIO()), \
                         patch.object(cli, '_main', fake_main):
                        if fail:
                            with self.assertRaises(SystemExit) as raised:
                                cli.main()
                            self.assertEqual(raised.exception.code, 1)
                        else:
                            cli.main()
                    self.assertEqual(termios.tcgetattr(slave), original)
        finally:
            os.close(master)
            os.close(slave)

    def test_redirected_stdin_does_not_require_a_terminal(self):
        with patch.object(cli.sys, 'stdin', io.StringIO()):
            with cli._terminal_without_flow_control():
                pass


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == '--probe':
        asyncio.run(console_probe(sys.argv[2], bool(int(sys.argv[3]))))
    else:
        unittest.main()
