"""Regression tests; run: python -m unittest discover -s computer-recorder/tests -v.

Loads recorder modules in an isolated package so database/LLM integrations are
not required. Uses real Pillow, Pydantic and XKB; hardware boundaries are fakes.
"""
import asyncio
import importlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = types.ModuleType("recorder_under_test")
PACKAGE.__path__ = [str(ROOT / "computer-recorder" / "crec")]
sys.modules[PACKAGE.__name__] = PACKAGE
capture = importlib.import_module("recorder_under_test.linux.screencast")
inputs = importlib.import_module("recorder_under_test.linux.inputs")
libinput = importlib.import_module("recorder_under_test.linux.libinput")
screen_module = importlib.import_module("recorder_under_test.observers.screen")
Screen = screen_module.Screen


def monitor(left=0, top=0, width=1920, height=1080, scale=1):
    return dict(left=left, top=top, width=width, height=height, scale=scale)


class CaptureTests(unittest.TestCase):
    def test_equal_size_displays_and_stream_order(self):
        left, right = monitor(), monitor(left=1920)
        sct = capture.PortalScreenCast([left, right])
        streams = [(7, {"position": (1920, 0), "size": (1920, 1080)}),
                   (8, {"position": (0, 0), "size": (1920, 1080)})]
        self.assertEqual(sct._match_streams(streams), [right, left])
        self.assertEqual(Screen._mon_for(2000, 50, [left, right]), 2)

    def test_mixed_scale_negative_origin(self):
        left, right = monitor(left=-1920), monitor(width=1280, height=720, scale=1.5)
        sct = capture.PortalScreenCast([left, right])
        streams = [(1, {"position": (0, 0), "size": (1920, 1080)}),
                   (2, {"position": (-1920, 0), "size": (1920, 1080)})]
        self.assertEqual(sct._match_streams(streams), [right, left])

    def test_size_fallback_only_when_unique_and_after_full_matches(self):
        left, right = monitor(), monitor(left=1920)
        sct = capture.PortalScreenCast([left, right])
        self.assertIsNone(sct._match_hint(None, (1920, 1080)))
        streams = [(1, {"size": (1920, 1080)}),
                   (2, {"position": (0, 0), "size": (1920, 1080)})]
        self.assertEqual(sct._match_streams(streams), [right, left])
        with self.assertRaises(capture.ScreenCastError):
            sct._match_streams([(1, {"size": (1920, 1080)}), (2, {"size": (1920, 1080)})])

    def test_stable_stream_identity(self):
        sct = capture.PortalScreenCast()
        sct.monitors = [dict(monitor(), stream_index=i) for i in range(2)]
        sct._Gst = types.SimpleNamespace(SECOND=1, MapFlags=types.SimpleNamespace(READ=1))
        sct._latest = {0: capture.Frame(1, 1, b'abc'), 1: capture.Frame(1, 1, b'def')}
        self.assertEqual(sct.grab(sct.monitors[1]).rgb, b'def')
        with self.assertRaises(capture.ScreenCastError):
            sct.grab(dict(sct.monitors[1]))

    def test_token_replacement_private_and_does_not_follow_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'screencast.token'
            outside = Path(directory) / 'outside'
            outside.write_text('untouched')
            for symlink in (False, True):
                target.unlink(missing_ok=True)
                if symlink:
                    target.symlink_to(outside)
                else:
                    target.write_text('old')
                    target.chmod(0o666)
                with patch.object(capture, '_STATE_DIR', directory), patch.object(capture, '_TOKEN_FILE', str(target)):
                    capture._write_restore_token('new secret')
                self.assertEqual(target.read_text(), 'new secret')
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertFalse(target.is_symlink())
                self.assertEqual(outside.read_text(), 'untouched')

    def test_pipeline_failures_release_every_resource(self):
        for failure in ('parse', 'state'):
            with self.subTest(failure=failure):
                sct = capture.PortalScreenCast()
                pipeline = Mock()
                pipeline.set_state.side_effect = lambda state: 'FAIL' if state == 'PLAY' and failure == 'state' else 'OK'
                gst = Mock()
                gst.State = types.SimpleNamespace(PLAYING='PLAY', NULL='NULL')
                gst.StateChangeReturn = types.SimpleNamespace(FAILURE='FAIL')
                gst.parse_launch.side_effect = [pipeline, RuntimeError('parse')] if failure == 'parse' else [pipeline]
                loop = Mock()
                quit_event = threading.Event()
                loop.run.side_effect = quit_event.wait
                loop.quit.side_effect = quit_event.set
                glib = Mock()
                glib.MainLoop.new.return_value = loop
                gio = Mock()
                modules = {'gi': Mock(), 'gi.repository': types.SimpleNamespace(Gio=gio, GLib=glib, Gst=gst)}
                original, other = os.pipe()
                streams = [(i, {'position': (i * 100, 0), 'size': (100, 100)}) for i in range(2)]
                with patch.dict(sys.modules, modules), patch.object(sct, '_handshake', return_value=(streams, original)):
                    with self.assertRaises((RuntimeError, capture.ScreenCastError)):
                        sct.start()
                with self.assertRaises(OSError):
                    os.fstat(original)
                os.close(other)
                self.assertFalse(sct._dup_fds)
                self.assertFalse(sct._pipelines)
                self.assertIsNone(sct._loop_thread)
                pipeline.set_state.assert_any_call('NULL')

    def test_private_glib_context_dispatch_and_join(self):
        import gi
        gi.require_version('GLib', '2.0')
        from gi.repository import GLib
        sct = capture.PortalScreenCast()
        gst = Mock()
        gio = Mock()
        main_thread = threading.get_ident()
        def handshake():
            self.assertNotEqual(sct._on_glib(threading.get_ident), main_thread)
            self.assertTrue(sct._on_glib(lambda: GLib.MainContext.get_thread_default() == sct._context))
            raise capture.ScreenCastError('intentional failure after loop starts')
        with patch.dict(sys.modules, {'gi.repository': types.SimpleNamespace(Gio=gio, GLib=GLib, Gst=gst)}), \
             patch.object(gi, 'require_version'), patch.object(sct, '_handshake', handshake):
            with self.assertRaises(capture.ScreenCastError):
                sct.start()
        self.assertIsNone(sct._loop_thread)

    def test_macos_import_does_not_load_linux_dependencies(self):
        import importlib.util
        from importlib.abc import MetaPathFinder
        class RejectLinux(MetaPathFinder):
            def find_spec(self, fullname, *args):
                if '.linux' in fullname or fullname in ('evdev', 'xkbcommon', 'gi'):
                    raise AssertionError(f'Mac attempted Linux import: {fullname}')
        name = 'recorder_under_test.observers.screen_mac_test'
        spec = importlib.util.spec_from_file_location(name, ROOT / 'computer-recorder/crec/observers/screen.py')
        module = importlib.util.module_from_spec(spec)
        mocks = {name: Mock() for name in ('mss', 'Quartz', 'pynput', 'shapely', 'shapely.geometry', 'shapely.ops')}
        blocker = RejectLinux()
        sys.meta_path.insert(0, blocker)
        try:
            with patch.object(sys, 'platform', 'darwin'), patch.dict(sys.modules, mocks):
                spec.loader.exec_module(module)
            self.assertTrue(module._IS_MAC)
            self.assertFalse(module._IS_LINUX)
        finally:
            sys.meta_path.remove(blocker)

    def test_one_sample_supports_repeated_concurrent_grabs_and_reports_eos(self):
        sct = capture.PortalScreenCast()
        sct.monitors = [dict(monitor(), stream_index=0)]
        sct._Gst = types.SimpleNamespace(SECOND=1000000000, BUFFER_OFFSET_NONE=2**64-1, MessageType=types.SimpleNamespace(ERROR=1, EOS=2))
        bus = Mock()
        bus.pop_filtered.return_value = None
        pipeline = Mock()
        pipeline.get_bus.return_value = bus
        sink = Mock()
        sample = Mock()
        sample.get_buffer.return_value.offset = 1
        sent = False
        def pull(*args):
            nonlocal sent
            if not sent:
                sent = True
                return sample
            time.sleep(0.005)
            return None
        sink.emit.side_effect = pull
        frame = capture.Frame(1, 1, b'abc', time.monotonic())
        sct._sinks, sct._pipelines = [sink], [pipeline]
        with patch.object(sct, '_decode_sample', return_value=frame), patch.object(sct, '_sample_time', return_value=frame.captured_at):
            sct._start_readers()
            try:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    frames = list(pool.map(lambda _: sct.grab(sct.monitors[0]), range(20)))
                self.assertTrue(all(value is frames[0] for value in frames))
                self.assertEqual(frames[0].rgb, frame.rgb)
                bus.pop_filtered.return_value = types.SimpleNamespace(type=2)
                deadline = time.monotonic() + 1
                while not sct._stream_errors and time.monotonic() < deadline:
                    time.sleep(0.005)
                with self.assertRaisesRegex(capture.ScreenCastError, 'ended'):
                    sct.grab(sct.monitors[0])
            finally:
                sct.stop()
            self.assertFalse(sct._readers)

    def test_event_history_never_returns_newer_before_frame(self):
        sct = capture.PortalScreenCast()
        sct.monitors = [dict(monitor(), stream_index=0)]
        old = capture.Frame(1, 1, b'old', 10)
        new = capture.Frame(1, 1, b'new', 12)
        sct._history[0] = deque([old, new])
        pair = sct.event_frames(sct.monitors[0], 11)
        self.assertEqual((pair[0], pair.resolve_after()), (old, new))
        self.assertIsNone(sct.event_frames(sct.monitors[0], 9)[0])

    def test_after_waits_through_settle_and_pins_first_result(self):
        sct = capture.PortalScreenCast()
        sct.monitors = [dict(monitor(), stream_index=0)]
        old = capture.Frame(1, 1, b'old', .990)
        new = capture.Frame(1, 1, b'new', 1.016)
        sct._publish_frame(0, old)
        pair = sct.event_frames(sct.monitors[0], 1.0)
        self.assertIs(pair[0], old)
        self.assertIsNone(pair[1])
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(pair.resolve_after)
            time.sleep(.01)
            self.assertFalse(result.done())
            sct._publish_frame(0, new)
            settled = capture.Frame(1, 1, b'end', 1.1)
            sct._publish_frame(0, settled)
            self.assertIs(result.result(1), settled)
        sct.stop()

    def test_native_gstreamer_clock_mapping_preserves_reader_delay(self):
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        pipeline = Gst.parse_launch('videotestsrc is-live=true num-buffers=1 ! '
                                    'video/x-raw,format=RGB,width=100,height=100 ! '
                                    'appsink name=sink sync=false')
        sct = capture.PortalScreenCast()
        sct._Gst, sct._pipelines = Gst, [pipeline]
        pipeline.set_state(Gst.State.PLAYING)
        try:
            sample = pipeline.get_by_name('sink').emit('try-pull-sample', Gst.SECOND)
            self.assertIsNotNone(sample)
            first_time = sct._sample_time(0, sample)
            time.sleep(.04)
            delayed_time = sct._sample_time(0, sample)
            self.assertAlmostEqual(first_time, delayed_time, delta=.005)
            self.assertGreater(time.monotonic() - delayed_time, .03)
            frame = sct._decode_sample(sample, delayed_time)
            self.assertEqual(len(frame.rgb), 30000)
        finally:
            pipeline.set_state(Gst.State.NULL)

    def test_keepalive_requires_explicit_freshness_delay(self):
        sct = capture.PortalScreenCast()
        sct.monitors = [dict(monitor(), stream_index=0)]
        old = capture.Frame(1, 1, b'old', .990)
        sct._publish_frame(0, old)
        pair = sct.event_frames(sct.monitors[0], 1)
        sct._publish_frame(0, capture.Frame(1, 1, b'old', 1.001, repeated=True))
        self.assertIsNone(pair[1], 'an immediate keepalive cannot stand in for the result')
        fresh = capture.Frame(1, 1, b'old', 1.2, repeated=True)
        sct._publish_frame(0, fresh)
        self.assertIs(pair.resolve_after(), fresh)
        sct.stop()

    def test_one_failed_monitor_preserves_other_contexts_and_sets_health(self):
        sct = capture.PortalScreenCast()
        sct.monitors = [dict(monitor(), stream_index=i) for i in range(2)]
        for idx in range(2):
            sct._publish_frame(idx, capture.Frame(1, 1, b'old', .990))
        sct._stream_errors[0] = capture.ScreenCastError('portal revoked')
        pairs = {idx: sct.event_frames(mon, 1) for idx, mon in enumerate(sct.monitors)}
        with self.assertRaisesRegex(capture.ScreenCastError, 'revoked'):
            pairs[0].resolve_after()
        healthy = capture.Frame(1, 1, b'new', 1.1)
        sct._publish_frame(1, healthy)
        self.assertIs(pairs[1].resolve_after(), healthy)
        with self.assertRaisesRegex(capture.ScreenCastError, 'revoked'):
            sct.check_health()
        sct.stop()

    def test_no_after_frame_times_out_and_marks_capture_failed(self):
        sct = capture.PortalScreenCast()
        sct.monitors = [dict(monitor(), stream_index=0)]
        sct._publish_frame(0, capture.Frame(1, 1, b'old', .990))
        pair = sct.event_frames(sct.monitors[0], 1)
        with self.assertRaisesRegex(capture.ScreenCastError, 'stalled'):
            pair.resolve_after(timeout=.01)
        with self.assertRaises(capture.ScreenCastError):
            sct.check_health()
        sct.stop()

    def test_gstreamer_sample_time_uses_pts_despite_delayed_delivery(self):
        import gi
        gi.require_version('Gst', '1.0')
        from gi.repository import Gst
        Gst.init(None)
        sct = capture.PortalScreenCast()
        sct._Gst = Gst
        segment = Gst.Segment()
        segment.init(Gst.Format.TIME)
        segment.start = 10 * Gst.SECOND
        segment.base = 2 * Gst.SECOND
        buf = Gst.Buffer.new_allocate(None, 4, None)
        buf.pts = 11 * Gst.SECOND  # running time = 3s
        sample = Gst.Sample.new(buf, None, segment, None)
        clock = Mock()
        clock.get_time.return_value = 110 * Gst.SECOND
        pipeline = Mock()
        pipeline.get_clock.return_value = clock
        pipeline.get_base_time.return_value = 100 * Gst.SECOND
        sct._pipelines = [pipeline]
        with patch.object(capture.time, 'monotonic', return_value=200):
            self.assertEqual(sct._sample_time(0, sample), 193)
        buf.pts = Gst.CLOCK_TIME_NONE
        with self.assertRaises(capture.ScreenCastError):
            sct._sample_time(0, sample)



class InputTests(unittest.IsolatedAsyncioTestCase):
    def backend(self, layout='us', modifiers=0):
        bridge = Mock(available=True)
        bridge.pointer_history.return_value = []
        bridge.get_keyboard_state.return_value = dict(type='xkb', id=layout, options=[], modifiers=modifiers, model='pc105')
        bridge.keyboard_history.side_effect = lambda since: [
            (0, dict(bridge.get_keyboard_state.return_value))]
        return inputs.EvdevInput(bridge), bridge

    async def test_missing_and_lost_pointer_never_returns_origin(self):
        with self.assertRaises(inputs.InputUnavailable):
            inputs.EvdevInput(Mock(available=False))
        backend, bridge = self.backend()
        bridge.get_pointer.side_effect = [(100, 200), None, (20, 30)]
        self.assertEqual(await backend.pointer_position(), (100, 200))
        self.assertEqual(await backend.pointer_position(), (None, None))
        self.assertEqual(await backend.pointer_position(), (20, 30))

    async def test_non_us_layout_switch_caps_and_existing_shift(self):
        backend, bridge = self.backend('de', 2)
        backend._sync_keyboard()
        self.assertEqual(backend._render_key(21), "'Z'")  # physical US Y
        bridge.get_keyboard_state.return_value.update(id='us', modifiers=0)
        backend._pressed = {('/dev/fake', 42)}  # left shift held before start
        backend._sync_keyboard()
        self.assertEqual(backend._render_key(21), "'Y'")
        backend._state.update_key(50, backend._xkb.KeyDirection.XKB_KEY_UP)
        self.assertEqual(backend._render_key(21), "'y'")
        bridge.get_keyboard_state.return_value.update(type='ibus')
        with self.assertRaises(inputs.InputUnavailable):
            backend._sync_keyboard()

    async def test_callback_error_reports_failure_but_drains_actions(self):
        backend, _ = self.backend()
        backend._on_click = AsyncMock(side_effect=[FileNotFoundError('image'), None])
        backend._dispatch_task = asyncio.create_task(backend._dispatch_events())
        for at in (1, 2):
            await backend._events.put(('click', 'click_left', None, (10, 20), backend._context(at)))
        await backend.stop()
        self.assertEqual(backend._on_click.await_count, 2)
        with self.assertRaisesRegex(inputs.InputUnavailable, 'image'):
            backend.check_health()

    async def test_cancelled_discovery_joins_and_closes_late_devices(self):
        backend, _ = self.backend()
        acquired, release = threading.Event(), threading.Event()
        dev = Mock()
        def discover():
            acquired.set()
            release.wait()
            backend._devices.append(dev)
        with patch.object(backend, '_open_devices', discover):
            task = asyncio.create_task(backend.start(AsyncMock(), AsyncMock(), AsyncMock()))
            await asyncio.to_thread(acquired.wait)
            task.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        dev.close.assert_called_once()
        self.assertEqual(backend._devices, [])

    async def test_native_libinput_bindings_and_context_cleanup(self):
        pointer = libinput.LibinputPointer(lambda: {})
        bind = pointer._bind
        def bind_without_devices(name, result, *args):
            bind(name, result, *args)
            if name == 'udev_assign_seat':
                pointer.udev_assign_seat = Mock(return_value=0)
        with patch.object(pointer, '_bind', side_effect=bind_without_devices), \
             patch.object(pointer, 'read', side_effect=lambda: pointer._devices.add(1)):
            pointer.start()
        self.assertIsNotNone(pointer._context)
        pointer.stop()
        self.assertIsNone(pointer._context)
        self.assertFalse(pointer._fds)

    async def test_touchpad_and_wheel_deltas_without_legacy_duplicates(self):
        pointer = libinput.LibinputPointer(lambda: {})
        pointer._settings = {}
        pointer._last_settings = __import__('time').monotonic()
        pointer.dispatch = Mock(return_value=0)
        pointer.get_event = Mock(side_effect=[403, 404, 405, 406, None])
        pointer.event_get_type = lambda event: event
        pointer.event_get_device = Mock(return_value=1)
        pointer.event_get_pointer_event = lambda event: event
        pointer.event_pointer_get_time_usec = Mock(return_value=1000000)
        pointer.event_pointer_has_axis = Mock(return_value=True)
        pointer.event_pointer_get_scroll_value_v120 = lambda event, axis: (120, -240)[axis]
        pointer.event_pointer_get_scroll_value = lambda event, axis: (2.5, -5)[axis]
        pointer.event_destroy = Mock()
        self.assertEqual(pointer.read(), [('scroll', -2.0, -1.0, 1.0),
                                         ('scroll', -0.5, -0.25, 1.0), ('scroll', -0.5, -0.25, 1.0)])
        self.assertEqual(pointer.event_destroy.call_count, 4)

    async def test_libinput_retains_both_button_edges(self):
        pointer = libinput.LibinputPointer(lambda: {})
        pointer._settings = {}
        pointer._last_settings = time.monotonic()
        pointer.dispatch = Mock(return_value=0)
        pointer.get_event = Mock(side_effect=[402, 402, None])
        pointer.event_get_type = lambda event: event
        pointer.event_get_device = Mock(return_value=1)
        pointer.event_get_pointer_event = lambda event: event
        pointer.event_pointer_get_time_usec = Mock(side_effect=[1000000, 1040000])
        pointer.event_pointer_get_button = Mock(return_value=272)
        pointer.event_pointer_get_button_state = Mock(side_effect=[1, 0])
        pointer.event_destroy = Mock()
        self.assertEqual(pointer.read(), [
            ('button', 'click_left', True, 1.0),
            ('button', 'click_left', False, 1.04),
        ])

    async def test_click_waits_for_release_and_arms_settled_after_frame(self):
        backend, bridge = self.backend()
        bridge.pointer_history.return_value = [(.99, 10, 20), (1.01, 10, 20)]
        pair = Mock()
        pair.complete = Mock()
        backend._snapshot_source = lambda _: {1: pair}
        with patch.object(backend, '_poll_sources', side_effect=[
                (2., [(1., 'button', 'click_left', True, ('pointer', 1))]),
                (2., [(1.04, 'button', 'click_left', False, ('pointer', 2))])]):
            await backend._acquire_batch()
            self.assertTrue(backend._events.empty())
            await backend._acquire_batch()
        click = backend._events.get_nowait()
        self.assertEqual((click[0], click[1], click[3]),
                         ('click', 'click_left', (10, 20)))
        self.assertEqual(click[4]['at'], 1.)
        self.assertEqual(click[4]['completed_at'], 1.04)
        pair.complete.assert_called_once_with(1.04)

    async def test_long_hold_pins_press_and_queued_coordinates_before_eviction(self):
        backend, bridge = self.backend()
        bridge.pointer_history.return_value = [
            (.99, 10, 20), (1.01, 10, 20),
            (1.99, 30, 40), (2.01, 30, 40),
        ]
        with patch.object(backend, '_poll_sources', side_effect=[
                (3., [
                    (1., 'button', 'click_left', True, ('pointer', 1)),
                    (2., 'scroll', 0., -1., ('pointer', 2)),
                ]),
                (21., [(20., 'button', 'click_left', False, ('pointer', 3))])]):
            await backend._acquire_batch()
            self.assertTrue(backend._events.empty())
            # Simulate the extension having evicted all press-time history
            # before the release arrives. No second query should be needed.
            bridge.pointer_history.return_value = [(19.99, 90, 90), (20.01, 90, 90)]
            await backend._acquire_batch()
        click = backend._events.get_nowait()
        scroll = backend._events.get_nowait()
        self.assertEqual((click[0], click[3]), ('click', (10, 20)))
        self.assertEqual((scroll[0], scroll[3]), ('scroll', (30, 40)))
        self.assertEqual(bridge.pointer_history.call_count, 1)

    async def test_final_drain_marks_button_without_release_incomplete(self):
        backend, bridge = self.backend()
        bridge.pointer_history.return_value = [(.99, 10, 20), (1.01, 10, 20)]
        with patch.object(backend, '_poll_sources', return_value=(2., [
                (1., 'button', 'click_left', True, ('pointer', 1))])):
            await backend._acquire_batch(final=True)
        click = backend._events.get_nowait()
        self.assertEqual(click[3], (10, 20))
        self.assertEqual(click[4]['incomplete'], 'button release was not observed')

    async def test_batched_clicks_use_history_not_the_current_pointer(self):
        backend, bridge = self.backend()
        bridge.pointer_history.return_value = [
            (at / 1000, 10 if at < 1500 else 2000, 20) for at in range(900, 2101, 2)]
        bridge.get_pointer.return_value = (2000, 20)
        backend._pointer.read = Mock(return_value=[('click', 'click_left', None, 1.0),
                                                   ('motion', None, None, 1.5),
                                                   ('click', 'click_right', None, 2.0)])
        backend._on_click = AsyncMock()
        backend._last_heartbeat = time.monotonic()
        await backend._acquire_batch()
        first, second = backend._events.get_nowait(), backend._events.get_nowait()
        self.assertEqual(first[3], (10, 20))
        self.assertEqual(second[3], (2000, 20))
        self.assertEqual(first[4]['at'], 1.0)
        bridge.get_pointer.assert_not_called()

    async def test_stationary_sampling_gap_uses_libinput_motion_evidence(self):
        history = [(at / 1000, 10, 20) for at in range(950, 1051, 2)]
        self.assertEqual(inputs.position_at(history, 1), (10, 20))
        self.assertEqual(inputs.position_at(history, 1.001, [1.001]), (None, None))
        self.assertEqual(inputs.position_at([(.99, 10, 20), (1.02, 10, 20)], 1),
                         (10, 20))
        self.assertEqual(inputs.position_at([(.99, 10, 20), (1.02, 10, 20)], 1,
                                            [(.999, 1), (1.001, 3)], 2),
                         (None, None))
        self.assertEqual(inputs.position_at(history, 2), (None, None))

    async def test_blocked_dispatcher_keeps_immutable_event_frames(self):
        backend, _ = self.backend()
        before = capture.Frame(1, 1, b'old', 1)
        newer = capture.Frame(1, 1, b'new', 3)
        cache = {1: (before, before)}
        backend._snapshot_source = lambda _: dict(cache)
        release, entered = asyncio.Event(), asyncio.Event()
        observed = []
        async def callback(*args, event):
            if not entered.is_set():
                entered.set()
                await release.wait()
            observed.append(event['frames'][1][0])
        backend._on_click = callback
        backend._dispatch_task = asyncio.create_task(backend._dispatch_events())
        await backend._events.put(('click', 'click_left', None, (10, 20), backend._context(2)))
        await entered.wait()
        await backend._events.put(('click', 'click_left', None, (10, 20), backend._context(2.1)))
        cache[1] = (newer, newer)
        release.set()
        await backend.stop()
        self.assertEqual(observed, [before, before])

    async def test_move_then_click_and_click_then_move_inside_twenty_ms(self):
        history = [(at / 1000, 10 if at < 990 else 200 if at < 1010 else 300, 20)
                   for at in range(950, 1051, 2)]
        self.assertEqual(inputs.position_at(history, 1, [.988, 1.008]), (200, 20))
        self.assertEqual(inputs.position_at(history, .98, [.988, 1.008]), (10, 20))
        self.assertEqual(inputs.position_at(history, 1.02, [.988, 1.008]), (300, 20))

    async def test_motion_timestamp_resolves_changed_sample_boundary(self):
        history = [(.998, 99, 20), (1.000, 100, 20), (1.002, 100, 20)]
        self.assertEqual(inputs.position_at(history, .9995, [.999]), (100, 20))
        self.assertEqual(inputs.position_at(history, .9985, [.999]), (99, 20))
        self.assertEqual(inputs.position_at(history, .9995, [.999, 1.000]), (None, None))
        self.assertEqual(inputs.position_at(history, .999, [(.999, 1)], 2), (100, 20))
        self.assertEqual(inputs.position_at(history, .999, [(.999, 2)], 1), (99, 20))

    async def test_evdev_clock_uses_native_monotonic_ioctl(self):
        dev = types.SimpleNamespace(fd=42, path='/dev/input/event-test')
        with patch.object(inputs.platform, 'machine', return_value='x86_64'), \
             patch.object(inputs.fcntl, 'ioctl') as ioctl:
            inputs.EvdevInput._set_monotonic_clock(dev)
        request = (1 << 30) | (4 << 16) | (ord('E') << 8) | 0xA0
        ioctl.assert_called_once_with(42, request, inputs.struct.pack('i', time.CLOCK_MONOTONIC))

    async def test_evdev_clock_failure_is_explicit(self):
        dev = types.SimpleNamespace(fd=42, path='/dev/input/event-test')
        with patch.object(inputs.platform, 'machine', return_value='x86_64'), \
             patch.object(inputs.fcntl, 'ioctl', side_effect=OSError('denied')):
            with self.assertRaisesRegex(inputs.InputUnavailable,
                                       'Cannot select monotonic timestamps'):
                inputs.EvdevInput._set_monotonic_clock(dev)

    async def test_unexpected_device_setup_error_is_normalized_and_closes_fd(self):
        backend, _ = self.backend()
        from evdev import ecodes
        dev = Mock(path='/dev/input/event-test', fd=42)
        dev.capabilities.return_value = {ecodes.EV_KEY: [ecodes.KEY_A]}
        with patch('evdev.list_devices', return_value=[dev.path]), \
             patch('evdev.InputDevice', return_value=dev), \
             patch.object(backend, '_set_monotonic_clock',
                          side_effect=AttributeError('unexpected evdev API')):
            with self.assertRaisesRegex(inputs.InputUnavailable,
                                       'Linux input initialization failed'):
                backend._open_devices()
        dev.close.assert_called_once()
        self.assertEqual(backend._devices, [])

    async def test_layout_history_decodes_queued_keys_at_acquisition_layout(self):
        backend, bridge = self.backend()
        from evdev import ecodes
        backend._ec = ecodes
        backend._sync_keyboard()
        us = dict(type='xkb', id='us', options=[], modifiers=0, model='pc105')
        de = dict(type='xkb', id='de', options=[], modifiers=0, model='pc105')
        bridge.keyboard_history.side_effect = None
        bridge.keyboard_history.return_value = [(0, us), (1.5, de)]
        events = [
            types.SimpleNamespace(type=ecodes.EV_KEY, code=21, value=1, timestamp=lambda: 1.),
            types.SimpleNamespace(type=ecodes.EV_KEY, code=21, value=0, timestamp=lambda: 1.1),
            types.SimpleNamespace(type=ecodes.EV_KEY, code=21, value=1, timestamp=lambda: 2.),
        ]
        dev = Mock(path='/dev/fake')
        dev.read.side_effect = [events, BlockingIOError()]
        backend._devices = [dev]
        backend._pointer.read = Mock(return_value=[])
        bridge.pointer_history.return_value = [
            (at / 1000, 10, 20) for at in range(900, 2100, 2)]
        seen = []
        release = asyncio.Event()
        async def record(token, *, event):
            if not seen:
                await release.wait()
            seen.append((token, event['at']))
        backend._on_key = record
        backend._dispatch_task = asyncio.create_task(backend._dispatch_events())
        await backend._acquire_batch(final=True)
        # Changing the desktop again while dispatch is blocked cannot rewrite
        # the configurations already attached to these events.
        bridge.get_keyboard_state.return_value = us
        release.set()
        await backend.stop()
        self.assertEqual(seen, [("'y'", 1.), ("'z'", 2.)])

    async def test_key_without_acquisition_layout_is_rejected_not_misdecoded(self):
        backend, bridge = self.backend('de')
        from evdev import ecodes
        backend._ec = ecodes
        backend._sync_keyboard()
        backend._on_key = AsyncMock()
        dev = types.SimpleNamespace(path='/dev/fake')
        event = types.SimpleNamespace(code=21, value=1)
        backend._dispatch_task = asyncio.create_task(backend._dispatch_events())
        await backend._events.put(('key', dev, event, (10, 20), backend._context(1)))
        await backend.stop()
        backend._on_key.assert_not_awaited()
        with self.assertRaisesRegex(inputs.InputUnavailable, 'event-time keyboard'):
            backend.check_health()

    async def test_slow_keyboard_lookup_cannot_reverse_later_click(self):
        backend, bridge = self.backend()
        from evdev import ecodes
        backend._ec = ecodes
        dev = Mock(path='/dev/fake')
        key = types.SimpleNamespace(type=ecodes.EV_KEY, code=30, value=1, timestamp=lambda: 1.0)
        dev.read.side_effect = [[key], BlockingIOError(), BlockingIOError()]
        backend._devices = [dev]
        backend._pointer.read = Mock(side_effect=[[('click', 'click_left', None, 2)], []])
        bridge.pointer_history.return_value = [(at / 1000, 10, 20) for at in range(900, 2100, 2)]
        entered, release = threading.Event(), threading.Event()
        original = backend._sync_keyboard
        def slow_keyboard(config=None):
            entered.set()
            release.wait(2)
            original(config)
        observed = []
        async def record(*args, event):
            observed.append((args, event['at']))
        backend._on_key = backend._on_click = record
        backend._sync_keyboard = slow_keyboard
        backend._dispatch_task = asyncio.create_task(backend._dispatch_events())
        await backend._acquire_batch()
        await asyncio.to_thread(entered.wait, 2)
        self.assertEqual(observed, [])
        release.set()
        await backend.stop()
        self.assertEqual([at for _, at in observed], [1, 2])
        self.assertEqual(observed[0][0], ("'a'",))

    async def test_source_failure_still_drains_acquired_and_held_events(self):
        backend, bridge = self.backend()
        from evdev import ecodes
        backend._ec = ecodes
        key = types.SimpleNamespace(type=ecodes.EV_KEY, code=30, value=1, timestamp=lambda: 1.)
        dev = Mock(path='/dev/fake')
        dev.read.side_effect = [[key], OSError('keyboard removed')]
        backend._devices = [dev]
        error = inputs.InputUnavailable('pointer removed')
        error.acquired_events = [('click', 'click_left', None, 2.)]
        backend._pointer.read = Mock(side_effect=error)
        bridge.pointer_history.return_value = [(at / 1000, 10, 20) for at in range(900, 2100, 2)]
        await backend._acquire_batch(final=True)
        self.assertEqual([backend._events.get_nowait()[4]['at'] for _ in range(2)], [1., 2.])
        with self.assertRaisesRegex(inputs.InputUnavailable, 'keyboard removed'):
            backend.check_health()
        # A failed poll must not discard an earlier event held by the watermark.
        backend._raw_heap.append((3., 1, 'click', 'click_left', None, backend._context(3.)))
        with patch.object(backend, '_poll_sources', side_effect=OSError('poll failed')):
            await backend._acquire_batch(final=True)
        self.assertEqual(backend._events.get_nowait()[4]['at'], 3.)
        self.assertEqual(backend._raw_heap, [])

    async def test_acquisition_holds_events_until_all_sources_watermark(self):
        backend, bridge = self.backend()
        bridge.pointer_history.return_value = [(at / 1000, 10, 20) for at in range(900, 3100, 2)]
        with patch.object(backend, '_poll_sources', side_effect=[
                (1.0, [(2.0, 'click', 'click_left', None, None)]),
                (2.1, [(1.5, 'click', 'click_right', None, None)])]):
            await backend._acquire_batch()
            self.assertTrue(backend._events.empty())
            await backend._acquire_batch()
        self.assertEqual([backend._events.get_nowait()[4]['at'] for _ in range(2)], [1.5, 2.0])
        self.assertIsNone(backend._failure)

    async def test_slow_dispatch_100_distinct_4k_frames_keeps_bounded_pixel_memory(self):
        import tracemalloc
        backend, _ = self.backend()
        release = asyncio.Event()
        observed = []
        async def save(*args, event):
            await release.wait()
            frame = event['frames'][1][0]
            # Simulate slow decoding/storage in the real callback queue.
            value = await asyncio.to_thread(lambda: frame.rgb[0])
            await asyncio.sleep(.001)
            observed.append(value)
        backend._on_click = save
        backend._dispatch_task = asyncio.create_task(backend._dispatch_events())
        with tempfile.TemporaryDirectory() as directory:
            sct = capture.PortalScreenCast(spool_directory=directory)
            sct.monitors = [dict(monitor(), stream_index=0)]
            tracemalloc.start()
            try:
                for index in range(100):
                    def publish():
                        raw = capture.Frame(3840, 2160, bytes([index]) * (3840 * 2160 * 3), index)
                        sct._publish_frame(0, sct._store_frame(raw))
                    await asyncio.to_thread(publish)
                    event = {'at': index + .1, 'frames': {1: sct.event_frames(sct.monitors[0], index + .1)}}
                    await backend._events.put(('click', 'click_left', None, (10, 20), event))
                retained, peak = tracemalloc.get_traced_memory()
                self.assertLess(retained, 8 * 1024 * 1024, 'queued contexts must not pin RGB data')
                self.assertLess(peak, 100 * 1024 * 1024)
                release.set()
                await backend.stop()
                self.assertEqual(observed, list(range(100)))
            finally:
                release.set()
                await backend.stop()
                tracemalloc.stop()
                sct.stop()
            self.assertEqual(list(Path(directory).iterdir()), [])

    async def test_frame_finalizer_unlinks_on_cleanup_thread(self):
        import gc
        calls = []
        loop_thread = threading.get_ident()
        with tempfile.TemporaryDirectory() as directory:
            sct = capture.PortalScreenCast(spool_directory=directory)
            original = capture.StoredFrame._unlink
            def observe(path):
                calls.append(threading.get_ident())
                original(path)
            with patch.object(capture.StoredFrame, '_unlink', side_effect=observe):
                frame = await asyncio.to_thread(
                    sct._store_frame, capture.Frame(1, 1, b'abc', 1))
                # Calling the finalizer on the asyncio thread must only enqueue;
                # the observed unlink itself belongs to the cleanup worker.
                frame._cleanup()
                del frame
                gc.collect()
                await asyncio.to_thread(sct._retired.join)
            await asyncio.to_thread(sct.stop)
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0], loop_thread)



class ScreenTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        def observer_init(obj):
            obj.update_queue = asyncio.Queue()
            obj._running = True
            obj._task = None
        with patch.object(screen_module.Observer, '__init__', observer_init):
            self.screen = Screen(screenshots_dir=self.tmp.name)
        self.frame = capture.Frame(100, 100, b'\xff' * 30000)

    async def asyncTearDown(self):
        await self.screen.stop()
        self.tmp.cleanup()

    async def test_legacy_filenames_unchanged_on_both_platforms(self):
        for linux in (True, False):
            for token in ("'\\'", "'%'", "'%2F'", "'é'", "'_'", 'Key.enter'):
                tag = f'key_press({token})_first'
                with patch.object(screen_module, '_IS_LINUX', linux):
                    path = await self.screen._save_frame(self.frame, 50, 50, tag, timestamp=123.0)
                self.assertEqual(Path(path).name, '123.00000_' + tag + '.jpg')

    async def test_linux_slash_filename_exception_is_reversible(self):
        tag = "key_press('/')_first"
        path = await self.screen._save_frame(self.frame, 50, 50, tag, timestamp=123.0)
        self.assertTrue(Path(path).is_file())
        self.assertEqual(Path(path).parent, Path(self.tmp.name))
        name = Path(path).name
        self.assertEqual(name, "123.00000_~crec1~key_press('%2F')_first.jpg")
        self.assertEqual(unquote(name.split('~crec1~', 1)[1]), tag + '.jpg')

    async def test_frames_primed_and_raw_actions_survive_image_failure(self):
        sct = Mock(monitors=[monitor(width=100, height=100)])
        sct.grab.return_value = self.frame
        backend = Mock()
        backend.stop = AsyncMock()
        backend.pointer_position = AsyncMock(return_value=(50, 50))
        async def start(**callbacks):
            self.assertIn(1, self.screen._frames)
            await callbacks['on_click'](50, 50, 'click_left')
            # An image save failure must happen after the raw action is retained.
            with patch.object(self.screen, '_save_frame', AsyncMock(side_effect=FileNotFoundError())):
                with self.assertRaises(FileNotFoundError):
                    await callbacks['on_key']("'/'")
            self.screen._frames.clear()
            await callbacks['on_click'](50, 50, 'click_right')
            await callbacks['on_scroll'](50, 50, 0.1, -0.25)
            await callbacks['on_click'](None, None, 'click_left')
            self.screen._running = False
        backend.start = start
        with patch.object(self.screen, '_make_input', return_value=backend):
            await self.screen._capture_worker(sct)
        actions = [self.screen.update_queue.get_nowait().content for _ in range(5)]
        self.assertEqual(actions, ['click_left(50.0, 50.0)', "key_press('/')",
                         'click_right(50.0, 50.0)', 'scroll(50.0, 50.0, dx=0.10, dy=-0.25)',
                         'click_left(nan, nan)'])
        backend.stop.assert_awaited()

    async def test_linux_scroll_debounce_preserves_actions_and_event_frames(self):
        times = [1., 1.01, 1.49, 1.5, 1.99, 2.]
        for debounce, accepted in ((.5, [0, 3, 5]), (0., list(range(6)))):
            with self.subTest(debounce=debounce):
                self.screen._scroll_debounce_sec = debounce
                self.screen._scroll_last_time = None
                self.screen._running = True
                sct = Mock(monitors=[monitor(width=100, height=100)])
                sct.grab.return_value = self.frame
                backend = Mock(stop=AsyncMock())
                events = [
                    {'at': at, 'wall_time': 100 + at, 'frames': {1: (
                        capture.Frame(100, 100, bytes([i]) * 30000, at),
                        capture.Frame(100, 100, bytes([i + 10]) * 30000, at + .1),
                    )}}
                    for i, at in enumerate(times)
                ]

                async def start(**callbacks):
                    # Deliver a backlog immediately: filtering must use event time,
                    # and a stationary pointer must still get later screenshots.
                    for i, event in enumerate(events):
                        await callbacks['on_scroll'](50, 50, 0, i + 1, event=event)
                    self.screen._running = False

                backend.start = start
                with patch.object(self.screen, '_make_input', return_value=backend), \
                     patch.object(self.screen, '_save_frame', AsyncMock(return_value='unused.jpg')) as save:
                    await self.screen._capture_worker(sct)
                updates = [self.screen.update_queue.get_nowait().content for _ in times]
                self.assertEqual(updates, [
                    f'scroll(50.0, 50.0, dx=0.00, dy={i + 1:.2f})'
                    for i in range(len(times))
                ])
                self.assertTrue(self.screen.update_queue.empty())
                self.assertEqual(save.await_count, 2 * len(accepted))
                for pair_index, event_index in enumerate(accepted):
                    for edge in (0, 1):
                        call = save.await_args_list[2 * pair_index + edge]
                        self.assertIs(call.args[0], events[event_index]['frames'][1][edge])
                        self.assertEqual(call.kwargs['timestamp'], events[event_index]['wall_time'])

    async def test_linux_scroll_debounce_does_not_hide_incomplete_events(self):
        sct = Mock(monitors=[monitor(width=100, height=100)])
        sct.grab.return_value = self.frame
        backend = Mock(stop=AsyncMock())
        event = {'at': 1., 'wall_time': 123., 'frames': {1: (self.frame, self.frame)}}

        async def start(**callbacks):
            await callbacks['on_scroll'](50, 50, 0, 1, event=event)
            await callbacks['on_scroll'](
                None, None, 0, 1, event=dict(event, at=1.01, wall_time=123.01))
            await callbacks['on_scroll'](
                50, 50, 0, 1, event=dict(event, at=1.02, wall_time=123.02, frames={}))
            self.screen._running = False

        backend.start = start
        with patch.object(self.screen, '_make_input', return_value=backend), \
             patch.object(self.screen, '_save_frame', AsyncMock(return_value='unused.jpg')) as save:
            await self.screen._capture_worker(sct)
        updates = [self.screen.update_queue.get_nowait().content for _ in range(5)]
        self.assertEqual(updates, [
            'scroll(50.0, 50.0, dx=0.00, dy=1.00)',
            'scroll(nan, nan, dx=0.00, dy=1.00)',
            'recorder_incomplete(Scroll position is ambiguous; unmarked monitor screenshots were retained)',
            'scroll(50.0, 50.0, dx=0.00, dy=1.00)',
            'recorder_incomplete(Scroll has no preceding screenshot; recording is incomplete)',
        ])
        self.assertEqual(save.await_count, 4)

    async def test_worker_startup_off_loop_and_cancellation_joined(self):
        sct = Mock()
        started, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        def start():
            self.assertNotEqual(threading.get_ident(), loop_thread)
            started.set()
            release.wait()
        sct.start = start
        sct.cancel_start.side_effect = release.set
        bridge_type = Mock(return_value=Mock(available=True))
        def make_capture():
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return sct
        with patch('recorder_under_test.linux.ShellBridge', bridge_type), \
             patch.object(self.screen, '_make_capture', side_effect=make_capture), \
             patch.object(self.screen, '_detect_high_dpi', return_value=False):
            task = asyncio.create_task(self.screen._worker())
            await asyncio.to_thread(started.wait)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        sct.cancel_start.assert_called_once()
        sct.stop.assert_called_once()

    async def test_worker_exception_stops_input_and_capture(self):
        sct = Mock()
        backend = Mock(stop=AsyncMock())
        self.screen._input_backend = backend
        with patch('recorder_under_test.linux.ShellBridge', return_value=Mock(available=True)), \
             patch.object(self.screen, '_make_capture', return_value=sct), \
             patch.object(self.screen, '_detect_high_dpi', return_value=False), \
             patch.object(self.screen, '_capture_worker', AsyncMock(side_effect=RuntimeError('worker'))):
            with self.assertRaises(RuntimeError):
                await self.screen._worker()
        backend.stop.assert_awaited_once()
        sct.stop.assert_called_once()

    async def test_backend_setup_errors_become_recorder_notifications(self):
        for error in (ValueError('missing Gst namespace'), RuntimeError('D-Bus failed')):
            sct = capture.PortalScreenCast()
            with patch('recorder_under_test.linux.ShellBridge', return_value=Mock(available=True)), \
                 patch.object(self.screen, '_make_capture', return_value=sct), \
                 patch.object(self.screen, '_detect_high_dpi', return_value=False), \
                 patch.object(sct, '_start', side_effect=error):
                await self.screen._worker()
            update = self.screen.update_queue.get_nowait()
            self.assertIn('recorder_error(ScreenCast setup failed:', update.content)
            self.assertFalse(sct._readers)

    async def test_delayed_callback_saves_pinned_images_not_current_cache(self):
        before = capture.Frame(100, 100, b'\x00' * 30000, 1)
        after = capture.Frame(100, 100, b'\x80' * 30000, 3)
        sct = Mock(monitors=[monitor(width=100, height=100)])
        sct.grab.return_value = self.frame  # Newer cache must not be used.
        backend = Mock(stop=AsyncMock())
        event = {'at': 2, 'wall_time': 123.0, 'frames': {1: (before, after)}}
        async def start(**callbacks):
            self.screen._frames[1] = self.frame
            await callbacks['on_click'](50, 50, 'click_left', event=event)
            self.screen._running = False
        backend.start = start
        with patch.object(self.screen, '_make_input', return_value=backend), \
             patch.object(self.screen, '_save_frame', AsyncMock(return_value='unused.jpg')) as save:
            await self.screen._capture_worker(sct)
        self.assertIs(save.await_args_list[0].args[0], before)
        self.assertIs(save.await_args_list[1].args[0], after)
        self.assertEqual(save.await_args_list[0].kwargs['timestamp'], 123.0)
        self.assertEqual(sct.grab.call_count, 1)  # Initial priming only.

    async def test_ambiguous_pointer_retains_all_frames_and_reports_incomplete(self):
        before = capture.Frame(100, 100, bytes(30000), 1)
        after = capture.Frame(100, 100, bytes([128]) * 30000, 2)
        sct = Mock(monitors=[monitor(width=100, height=100)])
        sct.grab.return_value = self.frame
        backend = Mock(stop=AsyncMock())
        backend.pointer_position = AsyncMock(return_value=(None, None))
        event = {'at': 1.5, 'wall_time': 123., 'frames': {1: (before, after)}}
        async def start(**callbacks):
            await callbacks['on_click'](None, None, 'click_left', event=event)
            # A later action must still be accepted after the local gap.
            await callbacks['on_key']("'a'", event=event)
            self.screen._running = False
        backend.start = start
        with patch.object(self.screen, '_make_input', return_value=backend):
            await self.screen._capture_worker(sct)
        names = sorted(path.name for path in Path(self.tmp.name).glob('*.jpg'))
        self.assertEqual(names, [
            '123.00000_click_left(nan, nan)_unlocated_mon1_after.jpg',
            '123.00000_click_left(nan, nan)_unlocated_mon1_before.jpg',
            "123.00000_key_press('a')_unlocated_mon1_before.jpg",
        ])
        updates = [self.screen.update_queue.get_nowait().content for _ in range(4)]
        self.assertEqual(updates, [
            'click_left(nan, nan)',
            'recorder_incomplete(Pointer position is ambiguous; unmarked monitor screenshots were retained)',
            "key_press('a')",
            'recorder_incomplete(Key position is ambiguous; unmarked monitor screenshots were retained)',
        ])

    async def test_stream_eos_after_start_sets_failure_and_preserves_actions(self):
        sct = Mock(monitors=[monitor(width=100, height=100)])
        sct.grab.return_value = self.frame
        backend = Mock(stop=AsyncMock())
        async def start(**callbacks):
            await callbacks['on_click'](50, 50, 'click_left')
            sct.check_health.side_effect = capture.ScreenCastError('stream ended after start')
        backend.start = start
        with patch('recorder_under_test.linux.ShellBridge', return_value=Mock(available=True)), \
             patch.object(self.screen, '_make_capture', return_value=sct), \
             patch.object(self.screen, '_detect_high_dpi', return_value=False), \
             patch.object(self.screen, '_make_input', return_value=backend):
            await self.screen._worker()
        self.assertIsInstance(self.screen.failure, capture.ScreenCastError)
        actual = [self.screen.update_queue.get_nowait().content for _ in range(2)]
        self.assertEqual(actual[0], 'click_left(50.0, 50.0)')
        self.assertIn('recorder_error(stream ended after start)', actual[1])
        backend.stop.assert_awaited()
        sct.stop.assert_called_once()

    async def test_click_saves_visibly_changed_after_arriving_after_acquisition(self):
        sct = capture.PortalScreenCast(spool_directory=self.tmp.name)
        mon = dict(monitor(width=100, height=100), stream_index=0)
        sct.monitors = [mon]
        old = capture.Frame(100, 100, bytes(30000), .990)
        unchanged = capture.Frame(100, 100, bytes(30000), 1.016)
        new = capture.Frame(100, 100, bytes([128]) * 30000, 1.100)
        sct._publish_frame(0, sct._store_frame(old))
        backend = Mock(stop=AsyncMock())
        async def start(**callbacks):
            event = {'at': 1, 'wall_time': 123., 'frames': callbacks['snapshot_source'](1)}
            self.assertIsNone(event['frames'][1][1])
            event['frames'][1].complete(1.020)
            async def publish_later():
                await asyncio.sleep(.02)
                sct._publish_frame(0, await asyncio.to_thread(sct._store_frame, unchanged))
                sct._publish_frame(0, await asyncio.to_thread(sct._store_frame, new))
            publishing = asyncio.create_task(publish_later())
            await callbacks['on_click'](50, 50, 'click_left', event=event)
            await publishing
            self.screen._running = False
        backend.start = start
        try:
            with patch.object(self.screen, '_make_input', return_value=backend):
                await self.screen._capture_worker(sct)
            from PIL import Image
            for suffix, expected in [('before', 0), ('after', 128)]:
                with Image.open(Path(self.tmp.name) / f'123.00000_click_left(50.0, 50.0)_{suffix}.jpg') as image:
                    self.assertLess(abs(image.getpixel((0, 0))[0] - expected), 3)
        finally:
            sct.stop()

    async def test_missing_button_release_is_persisted_with_click_images(self):
        before = capture.Frame(100, 100, bytes(30000), 1)
        after = capture.Frame(100, 100, bytes([128]) * 30000, 2)
        sct = Mock(monitors=[monitor(width=100, height=100)])
        sct.grab.return_value = self.frame
        backend = Mock(stop=AsyncMock())
        event = {
            'at': 1.5,
            'wall_time': 123.,
            'completed_at': 2.,
            'incomplete': 'button release was not observed',
            'frames': {1: (before, after)},
        }
        async def start(**callbacks):
            await callbacks['on_click'](10, 20, 'click_left', event=event)
            self.screen._running = False
        backend.start = start
        with patch.object(self.screen, '_make_input', return_value=backend):
            await self.screen._capture_worker(sct)
        updates = [self.screen.update_queue.get_nowait().content for _ in range(2)]
        self.assertEqual(updates, [
            'click_left(10.0, 20.0)',
            'recorder_incomplete(click_left: button release was not observed)',
        ])
        self.assertEqual(sorted(path.name for path in Path(self.tmp.name).glob('*.jpg')), [
            '123.00000_click_left(10.0, 20.0)_after.jpg',
            '123.00000_click_left(10.0, 20.0)_before.jpg',
        ])

    async def test_outdated_bridge_fails_before_opening_portal(self):
        from recorder_under_test.linux.shell_bridge import ShellBridge
        bridge = object.__new__(ShellBridge)
        bridge.available = True
        bridge._Gio = Mock()
        bridge._proxy = Mock()
        bridge._proxy.get_connection.return_value.call_sync.return_value.unpack.return_value = (
            '<node><interface name="org.crec.Input"><method name="GetPointer"/></interface></node>',)
        with patch('recorder_under_test.linux.ShellBridge', return_value=bridge), \
             patch.object(self.screen, '_make_capture') as make_capture:
            await self.screen._worker()
        make_capture.assert_not_called()
        self.assertIn('Log out of GNOME', self.screen.update_queue.get_nowait().content)


if __name__ == '__main__':
    unittest.main()
