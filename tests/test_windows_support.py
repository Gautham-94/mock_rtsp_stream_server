import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mock_cameras import __main__ as app_main
from mock_cameras import go2rtc


class WindowsPlatformTests(unittest.TestCase):
    @patch("mock_cameras.go2rtc.platform.system", return_value="Windows")
    @patch("mock_cameras.go2rtc.platform.machine", return_value="AMD64")
    def test_resolves_windows_amd64_asset(self, _machine: Mock, _system: Mock) -> None:
        self.assertEqual(go2rtc._resolve_asset(), ("go2rtc_win64.zip", True))

    @patch("mock_cameras.go2rtc.platform.system", return_value="Windows")
    @patch("mock_cameras.go2rtc.platform.machine", return_value="ARM64")
    def test_resolves_windows_arm64_asset(self, _machine: Mock, _system: Mock) -> None:
        self.assertEqual(go2rtc._resolve_asset(), ("go2rtc_win_arm64.zip", True))

    @patch("mock_cameras.go2rtc.platform.system", return_value="Windows")
    def test_uses_windows_executable_cache_name(self, _system: Mock) -> None:
        self.assertEqual(go2rtc._binary_path("bin"), Path("bin/go2rtc.exe"))

    @patch("mock_cameras.go2rtc.platform.system", return_value="Windows")
    @patch("mock_cameras.go2rtc.sp.Popen")
    def test_does_not_request_new_process_session_on_windows(
        self, popen: Mock, _system: Mock
    ) -> None:
        process = go2rtc.Go2rtcProcess("go2rtc.exe", "config.yaml")
        process.start()

        self.assertNotIn("start_new_session", popen.call_args.kwargs)

    @patch("mock_cameras.go2rtc.platform.system", return_value="Darwin")
    @patch("mock_cameras.go2rtc.sp.Popen")
    def test_keeps_process_session_on_posix(self, popen: Mock, _system: Mock) -> None:
        process = go2rtc.Go2rtcProcess("go2rtc", "config.yaml")
        process.start()

        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    @patch("mock_cameras.go2rtc.platform.system", return_value="Windows")
    def test_stops_windows_process_and_escalates_after_timeout(self, _system: Mock) -> None:
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [go2rtc.sp.TimeoutExpired("go2rtc", 1), None]
        process = go2rtc.Go2rtcProcess("go2rtc.exe", "config.yaml")
        process._proc = child

        process.stop(timeout=1)

        child.terminate.assert_called_once_with()
        child.kill.assert_called_once_with()
        self.assertEqual(child.wait.call_count, 2)

    @patch("mock_cameras.__main__.sys.platform", "win32")
    @patch("mock_cameras.__main__.signal.signal")
    def test_signal_handler_schedules_stop_on_windows(self, signal_handler: Mock) -> None:
        app = Mock()
        loop = Mock()

        app_main._install_signal_handlers(app, loop)
        callback = signal_handler.call_args_list[0].args[1]
        callback(0, None)

        self.assertEqual(signal_handler.call_count, 2)
        loop.call_soon_threadsafe.assert_called_once_with(app.request_stop)


if __name__ == "__main__":
    unittest.main()