"""Console diagnostics must never block the recorder's asyncio thread."""
import logging
import os
import queue
import sys
import threading


class QueuedConsoleHandler(logging.Handler):
    """Bound console buffering; dropping diagnostics never drops observations.

    A dedicated daemon writes directly to a duplicate of stderr's descriptor.
    It must not hold a Python buffered-stream lock or a logging handler lock
    while writing: either lock can otherwise hang interpreter shutdown when a
    terminal is paused. Recording and shutdown never join this writer.
    """

    def __init__(self, stream=None, capacity=1024):
        super().__init__()
        stream = sys.stderr if stream is None else stream
        self._queue = queue.Queue(maxsize=capacity)
        self._stopped = threading.Event()
        self.dropped_records = 0
        self._encoding = getattr(stream, 'encoding', None) or 'utf-8'
        try:
            self._fd = os.dup(stream.fileno())
        except (AttributeError, OSError, ValueError):
            # In-memory streams used by embedding applications have no fd.
            self._fd = None
        self._stream = stream if self._fd is None else None
        self._thread = threading.Thread(
            target=self._write_messages, name='crec-console', daemon=True)
        self._thread.start()

    def emit(self, record):
        if self._stopped.is_set():
            return
        try:
            message = self.format(record) + '\n'
            self._queue.put_nowait(message)
        except queue.Full:
            self.dropped_records += 1
        except Exception:
            # logging.handleError writes to stderr, which may be blocked too.
            self.dropped_records += 1

    def _write_messages(self):
        try:
            while not self._stopped.is_set():
                try:
                    message = self._queue.get(timeout=.1)
                except queue.Empty:
                    continue
                try:
                    if self._fd is None:
                        self._stream.write(message)
                        self._stream.flush()
                    else:
                        data = memoryview(message.encode(self._encoding, errors='backslashreplace'))
                        while data:
                            written = os.write(self._fd, data)
                            if written == 0:
                                raise OSError('Console write made no progress')
                            data = data[written:]
                finally:
                    self._queue.task_done()
        except Exception:
            # A closed terminal must not stop capture or trigger recursive logging.
            self._stopped.set()
        finally:
            if self._fd is not None:
                os.close(self._fd)

    def close(self):
        self._stopped.set()
        # No joining/flushing: output can remain paused indefinitely.
        super().close()
