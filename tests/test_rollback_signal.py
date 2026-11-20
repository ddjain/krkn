import signal
import threading
import unittest
from unittest.mock import Mock, call, patch

from krkn.rollback.signal import SignalHandler, signal_handler


def expected_registered_signals():
    expected = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        expected.append(signal.SIGHUP)
    return expected


class SignalHandlerTestCase(unittest.TestCase):
    """Resets SignalHandler class state and mocks the process signal boundary.

    signal.signal / signal.raise_signal / execute_rollback_version_files are
    mocked so no real handler is ever installed and no rollback runs.
    """

    def setUp(self):
        saved = (
            SignalHandler._signal_handlers_installed,
            SignalHandler._original_handlers,
            SignalHandler._signal_lock,
            SignalHandler._local,
        )
        self.addCleanup(self._restore_signal_state, saved)
        SignalHandler._signal_handlers_installed = False
        SignalHandler._original_handlers = {}
        SignalHandler._signal_lock = threading.Lock()
        SignalHandler._local = threading.local()
        self.mock_sigsig = self._start_patch("krkn.rollback.signal.signal.signal")
        self.mock_raise = self._start_patch("krkn.rollback.signal.signal.raise_signal")
        self.mock_rb = self._start_patch(
            "krkn.rollback.signal.execute_rollback_version_files"
        )

    def _start_patch(self, target):
        patcher = patch(target)
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    @staticmethod
    def _restore_signal_state(saved):
        (
            SignalHandler._signal_handlers_installed,
            SignalHandler._original_handlers,
            SignalHandler._signal_lock,
            SignalHandler._local,
        ) = saved

    def set_full_context(self, run_uuid="run-uuid-1", scenario_type="pod"):
        telemetry = Mock(name="telemetry_ocp")
        SignalHandler._set_context(run_uuid, scenario_type, telemetry)
        return telemetry

    def assert_lock_free(self):
        acquired = SignalHandler._signal_lock.acquire(blocking=False)
        self.assertTrue(acquired, "signal lock was not released")
        SignalHandler._signal_lock.release()


class TestSetGetContext(SignalHandlerTestCase):
    def test_set_context_then_get_returns_same_triple(self):
        telemetry = Mock()
        SignalHandler._set_context("uuid-1", "pod", telemetry)
        self.assertEqual(
            SignalHandler._get_context(), ("uuid-1", "pod", telemetry)
        )

    def test_get_context_unset_thread_local_returns_none_triple(self):
        self.assertEqual(SignalHandler._get_context(), (None, None, None))

    def test_get_context_is_isolated_across_threads(self):
        def set_in_worker():
            SignalHandler._set_context("worker-uuid", "pod", Mock())

        worker = threading.Thread(target=set_in_worker)
        worker.start()
        worker.join()
        self.assertEqual(SignalHandler._get_context(), (None, None, None))

    def test_set_context_clear_keeps_telemetry(self):
        telemetry = Mock()
        SignalHandler._set_context("uuid-1", "pod", telemetry)
        SignalHandler._set_context(None, None, telemetry)
        self.assertEqual(SignalHandler._get_context(), (None, None, telemetry))


class TestSignalHandler(SignalHandlerTestCase):
    def test_signal_handler_full_context_performs_rollback_and_clears(self):
        SignalHandler._register_signal_handler()
        telemetry = self.set_full_context("uuid-1", "pod")

        SignalHandler._signal_handler(signal.SIGTERM, None)

        self.mock_rb.assert_called_once_with(telemetry, "uuid-1", "pod")
        self.assertEqual(SignalHandler._get_context(), (None, None, telemetry))
        self.assert_lock_free()

    def test_signal_handler_incomplete_context_skips_rollback(self):
        cases = [
            ("run_uuid_none", None, "pod", Mock()),
            ("scenario_type_none", "uuid-1", None, Mock()),
            ("telemetry_none", "uuid-1", "pod", None),
            ("run_uuid_empty", "", "pod", Mock()),
        ]
        for name, run_uuid, scenario_type, telemetry in cases:
            with self.subTest(case=name):
                SignalHandler._set_context(run_uuid, scenario_type, telemetry)
                SignalHandler._signal_handler(signal.SIGTERM, None)
                self.mock_rb.assert_not_called()
                self.mock_rb.reset_mock()

    def test_signal_handler_incomplete_context_dispatches_original(self):
        original = Mock()
        SignalHandler._original_handlers[signal.SIGTERM] = original
        cases = [
            ("run_uuid_none", None, "pod", Mock()),
            ("scenario_type_none", "uuid-1", None, Mock()),
            ("telemetry_none", "uuid-1", "pod", None),
            ("run_uuid_empty", "", "pod", Mock()),
        ]
        for name, run_uuid, scenario_type, telemetry in cases:
            original.reset_mock()
            SignalHandler._set_context(run_uuid, scenario_type, telemetry)
            SignalHandler._signal_handler(signal.SIGTERM, None)
            self.assertEqual(
                original.call_args_list, [call(signal.SIGTERM, None)], msg=name
            )

    def test_signal_handler_lock_already_held_skips_rollback(self):
        telemetry = self.set_full_context()
        SignalHandler._signal_lock.acquire()
        try:
            SignalHandler._signal_handler(signal.SIGTERM, None)
        finally:
            SignalHandler._signal_lock.release()
        self.mock_rb.assert_not_called()
        self.assertEqual(SignalHandler._get_context(), ("run-uuid-1", "pod", telemetry))

    def test_signal_handler_rollback_exception_releases_lock_and_clears_context(self):
        self.set_full_context()
        self.mock_rb.side_effect = Exception("rollback failed")

        try:
            SignalHandler._signal_handler(signal.SIGTERM, None)
        except Exception as exc:
            self.assertEqual(str(exc), "rollback failed")

        self.mock_rb.assert_called_once()
        self.assertEqual(SignalHandler._get_context()[0], None)
        self.assert_lock_free()

    def test_signal_handler_rollback_exception_dispatches_original_without_escaping(self):
        self.set_full_context()
        self.mock_rb.side_effect = Exception("rollback failed")
        original = Mock()
        SignalHandler._original_handlers[signal.SIGTERM] = original

        try:
            SignalHandler._signal_handler(signal.SIGTERM, None)
        except Exception as exc:
            self.fail(f"rollback exception escaped the signal handler: {exc!r}")

        original.assert_called_once_with(signal.SIGTERM, None)

    def test_signal_handler_missing_original_restores_default_and_reraises(self):
        telemetry = self.set_full_context()

        SignalHandler._signal_handler(signal.SIGTERM, None)

        self.mock_rb.assert_called_once_with(telemetry, "run-uuid-1", "pod")
        self.mock_sigsig.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        self.mock_raise.assert_called_once_with(signal.SIGTERM)

    def test_signal_handler_none_original_restores_default_and_reraises(self):
        self.set_full_context()
        SignalHandler._original_handlers[signal.SIGTERM] = None

        SignalHandler._signal_handler(signal.SIGTERM, None)

        self.mock_sigsig.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        self.mock_raise.assert_called_once_with(signal.SIGTERM)

    def test_signal_handler_calls_original_after_rollback_with_ctx_cleared(self):
        telemetry = self.set_full_context()
        seen_context = {}

        def capture_original(signum, frame):
            seen_context["context"] = SignalHandler._get_context()
            seen_context["args"] = (signum, frame)

        original = Mock(side_effect=capture_original)
        SignalHandler._original_handlers[signal.SIGTERM] = original

        SignalHandler._signal_handler(signal.SIGTERM, "frame-obj")

        self.mock_rb.assert_called_once_with(telemetry, "run-uuid-1", "pod")
        self.assertEqual(seen_context["args"], (signal.SIGTERM, "frame-obj"))
        self.assertEqual(
            seen_context["context"], (None, None, telemetry)
        )

    def test_signal_handler_sig_dfl_original_restores_default_and_reraises(self):
        self.set_full_context()
        SignalHandler._original_handlers[signal.SIGTERM] = signal.SIG_DFL

        SignalHandler._signal_handler(signal.SIGTERM, None)

        self.mock_sigsig.assert_called_once_with(signal.SIGTERM, signal.SIG_DFL)
        self.mock_raise.assert_called_once_with(signal.SIGTERM)

    def test_signal_handler_sig_ign_original_returns_silently(self):
        self.set_full_context()
        SignalHandler._original_handlers[signal.SIGTERM] = signal.SIG_IGN

        SignalHandler._signal_handler(signal.SIGTERM, None)

        self.mock_rb.assert_called_once()
        self.mock_sigsig.assert_not_called()
        self.mock_raise.assert_not_called()

    def test_signal_handler_invalid_signum_raises_value_error(self):
        self.set_full_context()

        with self.assertRaisesRegex(ValueError, "99 is not a valid Signals"):
            SignalHandler._signal_handler(99, None)

        self.mock_rb.assert_not_called()

    def test_signal_handler_double_delivery_runs_rollback_twice(self):
        telemetry = self.set_full_context()

        SignalHandler._signal_handler(signal.SIGTERM, None)
        SignalHandler._set_context("run-uuid-1", "pod", telemetry)
        SignalHandler._signal_handler(signal.SIGTERM, None)

        self.assertEqual(
            self.mock_rb.call_args_list,
            [call(telemetry, "run-uuid-1", "pod")] * 2,
        )

    def test_signal_handler_sigint_parity_performs_rollback(self):
        SignalHandler._register_signal_handler()
        telemetry = self.set_full_context("uuid-int", "pod")

        SignalHandler._signal_handler(signal.SIGINT, None)

        self.mock_rb.assert_called_once_with(telemetry, "uuid-int", "pod")
        self.assertEqual(SignalHandler._get_context(), (None, None, telemetry))
        self.assert_lock_free()


class TestRegisterSignalHandler(SignalHandlerTestCase):
    def test_register_fresh_installs_all_posix_signals(self):
        original = Mock(name="previous_handler")
        self.mock_sigsig.return_value = original

        SignalHandler._register_signal_handler()

        expected = expected_registered_signals()
        self.assertEqual(
            self.mock_sigsig.call_args_list,
            [call(sig, SignalHandler._signal_handler) for sig in expected],
        )
        for sig in expected:
            self.assertIs(SignalHandler._original_handlers[sig], original)
        self.assertTrue(SignalHandler._signal_handlers_installed)

    def test_register_second_call_is_noop(self):
        SignalHandler._register_signal_handler()
        count = self.mock_sigsig.call_count

        SignalHandler._register_signal_handler()

        self.assertEqual(self.mock_sigsig.call_count, count)

    def test_register_all_failures_stores_no_handlers(self):
        self.mock_sigsig.side_effect = ValueError("signal only works in main thread")

        SignalHandler._register_signal_handler()

        self.assertEqual(
            self.mock_sigsig.call_count, len(expected_registered_signals())
        )
        self.assertEqual(SignalHandler._original_handlers, {})

    def test_register_all_failures_leaves_installed_flag_unset(self):
        self.mock_sigsig.side_effect = ValueError("signal only works in main thread")

        SignalHandler._register_signal_handler()

        self.assertFalse(SignalHandler._signal_handlers_installed)

    def test_register_partial_failure_stores_only_successful_signals(self):
        stored = Mock(name="stored")
        self.mock_sigsig.side_effect = [stored, OSError("nope"), OSError("nope")]

        SignalHandler._register_signal_handler()

        self.assertEqual(SignalHandler._original_handlers, {signal.SIGINT: stored})

    def test_register_partial_failure_retries_failed_signals_on_later_call(self):
        stored = Mock(name="stored")
        failing = {"active": True}

        def register(sig, handler):
            if sig == signal.SIGINT or not failing["active"]:
                return stored
            raise OSError("nope")

        self.mock_sigsig.side_effect = register
        SignalHandler._register_signal_handler()
        failing["active"] = False

        SignalHandler._register_signal_handler()

        self.assertEqual(
            set(SignalHandler._original_handlers),
            set(expected_registered_signals()),
        )

    def test_register_from_two_threads_registers_exactly_once(self):
        first_entered = threading.Event()
        release_first = threading.Event()

        def register_slowly(sig, handler):
            first_entered.set()
            release_first.wait(timeout=10)
            return Mock(name="previous_handler")

        self.mock_sigsig.side_effect = register_slowly
        first = threading.Thread(target=SignalHandler._register_signal_handler)
        second = threading.Thread(target=SignalHandler._register_signal_handler)

        first.start()
        self.assertTrue(first_entered.wait(timeout=10))
        second.start()
        release_first.set()
        first.join(timeout=30)
        second.join(timeout=30)

        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(
            self.mock_sigsig.call_count, len(expected_registered_signals())
        )
        self.assertTrue(SignalHandler._signal_handlers_installed)

    def test_register_without_sighup_registers_two_signals(self):
        sighup_patcher = patch.object(signal, "SIGHUP")
        sighup_patcher.start()
        self.addCleanup(sighup_patcher.stop)
        delattr(signal, "SIGHUP")

        SignalHandler._register_signal_handler()

        self.assertEqual(
            self.mock_sigsig.call_args_list,
            [call(signal.SIGINT, SignalHandler._signal_handler),
             call(signal.SIGTERM, SignalHandler._signal_handler)],
        )
        self.assertEqual(
            set(SignalHandler._original_handlers),
            {signal.SIGINT, signal.SIGTERM},
        )


class TestSignalContext(SignalHandlerTestCase):
    def test_context_enter_sets_context_and_registers_handlers(self):
        telemetry = Mock()
        observed = {}

        with signal_handler.signal_context("uuid-1", "pod", telemetry):
            observed["context"] = SignalHandler._get_context()
            observed["installed"] = SignalHandler._signal_handlers_installed
            observed["registrations"] = self.mock_sigsig.call_count

        self.assertEqual(observed["context"], ("uuid-1", "pod", telemetry))
        self.assertTrue(observed["installed"])
        self.assertEqual(observed["registrations"], len(expected_registered_signals()))

    def test_context_exit_clears_context_but_keeps_telemetry(self):
        telemetry = Mock()

        with signal_handler.signal_context("uuid-1", "pod", telemetry):
            pass

        self.assertEqual(SignalHandler._get_context(), (None, None, telemetry))

    def test_context_body_exception_propagates_and_context_cleared(self):
        telemetry = Mock()

        with self.assertRaisesRegex(RuntimeError, "scenario exploded"):
            with signal_handler.signal_context("uuid-1", "pod", telemetry):
                raise RuntimeError("scenario exploded")

        self.assertEqual(SignalHandler._get_context(), (None, None, telemetry))

    def test_context_nested_inner_exit_restores_outer_context(self):
        outer_telemetry, inner_telemetry = Mock(), Mock()

        with signal_handler.signal_context("outer-uuid", "pod", outer_telemetry):
            with signal_handler.signal_context("inner-uuid", "pod", inner_telemetry):
                pass
            self.assertEqual(
                SignalHandler._get_context(),
                ("outer-uuid", "pod", outer_telemetry),
            )

    def test_context_registration_failure_body_still_runs(self):
        telemetry = Mock()
        self.mock_sigsig.side_effect = ValueError("not main thread")
        observed = {}

        with signal_handler.signal_context("uuid-1", "pod", telemetry):
            observed["context"] = SignalHandler._get_context()

        self.assertEqual(observed["context"], ("uuid-1", "pod", telemetry))
        self.assertEqual(SignalHandler._get_context(), (None, None, telemetry))
