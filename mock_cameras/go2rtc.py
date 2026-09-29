"""go2rtc binary download + process management for mock_cameras.

Design choice (documented per the task spec): this is a VENDORED COPY of the two
functions mirage/mirage/go2rtc/download.py::ensure_go2rtc_binary and
mirage/mirage/go2rtc/process.py::Go2rtcProcess need, rather than an import of mirage's
own package. Reasoning:

  - `pip install -e ../mirage` would work (mirage/pyproject.toml is a normal
    setuptools package), but it would make this "standalone" app depend on mirage's
    full dependency tree (fastapi, onnxruntime, opencv, ...) just to reuse ~90 lines
    of subprocess/download logic -- overkill and slower to set up.
  - Adding `mirage/` to sys.path (without installing it) is fragile: mirage/mirage's
    own submodules import from `mirage.const`, `mirage.config.schema`, etc., which
    would pull in the same heavy dependency chain anyway, plus it's brittle to
    mirage/'s internal layout changing.
  - A small vendored copy keeps mock_cameras genuinely standalone (its own
    requirements.txt, runnable with no dependency on the mirage package existing on
    disk at all) at the cost of needing to manually re-sync if go2rtc's download URL
    scheme or CLI flags ever change -- an acceptable tradeoff for a test-only tool.

The download logic (URL scheme, asset naming, zip-vs-raw-binary handling) is copied
verbatim from mirage/mirage/go2rtc/download.py, which documents that it was confirmed
directly against the go2rtc GitHub releases API.
"""

from __future__ import annotations

import io
import logging
import os
import platform
import signal
import stat
import subprocess as sp
import urllib.request
import zipfile
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

GO2RTC_VERSION = "v1.9.14"
GO2RTC_RELEASE_BASE = f"https://github.com/AlexxIT/go2rtc/releases/download/{GO2RTC_VERSION}"

# (system, machine) -> (asset filename, is_zip)
_ASSET_MAP: dict[tuple[str, str], tuple[str, bool]] = {
    ("darwin", "arm64"): ("go2rtc_mac_arm64.zip", True),
    ("darwin", "x86_64"): ("go2rtc_mac_amd64.zip", True),
    ("linux", "x86_64"): ("go2rtc_linux_amd64", False),
    ("linux", "aarch64"): ("go2rtc_linux_arm64", False),
    ("linux", "arm64"): ("go2rtc_linux_arm64", False),
    ("windows", "amd64"): ("go2rtc_win64.zip", True),
    ("windows", "arm64"): ("go2rtc_win_arm64.zip", True),
    ("windows", "x86"): ("go2rtc_win32.zip", True),
    ("windows", "i386"): ("go2rtc_win32.zip", True),
    ("windows", "i686"): ("go2rtc_win32.zip", True),
}


class UnsupportedPlatformError(RuntimeError):
    pass


def _resolve_asset() -> tuple[str, bool]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    key = (system, machine)
    if key not in _ASSET_MAP:
        raise UnsupportedPlatformError(
            f"no known go2rtc release asset for platform {system}/{machine}; "
            f"supported: {sorted(_ASSET_MAP)}"
        )
    return _ASSET_MAP[key]


def _binary_path(bin_dir: str) -> Path:
    filename = "go2rtc.exe" if platform.system().lower() == "windows" else "go2rtc"
    return Path(bin_dir) / filename


def ensure_go2rtc_binary(bin_dir: str) -> str:
    """Returns the path to a working go2rtc binary, downloading + extracting it into
    bin_dir if not already cached there. Idempotent: a second call with an
    already-cached binary does no network I/O.
    """
    dest = _binary_path(bin_dir)
    if dest.exists():
        return str(dest)

    Path(bin_dir).mkdir(parents=True, exist_ok=True)
    asset_name, is_zip = _resolve_asset()
    url = f"{GO2RTC_RELEASE_BASE}/{asset_name}"
    logger.info("downloading go2rtc %s from %s", GO2RTC_VERSION, url)

    with urllib.request.urlopen(url, timeout=60) as resp:
        payload = resp.read()

    if is_zip:
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            names = [n for n in zf.namelist() if not n.endswith("/")]
            if len(names) != 1:
                raise RuntimeError(f"expected exactly one file in go2rtc zip, found: {names}")
            with zf.open(names[0]) as member:
                dest.write_bytes(member.read())
    else:
        dest.write_bytes(payload)

    if platform.system().lower() != "windows":
        dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("go2rtc binary ready at %s", dest)
    return str(dest)


def build_go2rtc_config(
    streams: dict[str, str | None],
    rtsp_port: int,
    api_port: int,
) -> dict:
    """Streams are declared with empty (None) sources: go2rtc then accepts an RTSP
    publish to that name, which is how mock_cameras.publisher.CameraPublisher feeds
    each camera as an always-on loop (see publisher.py's module docstring for why this
    replaced go2rtc's own on-demand `ffmpeg:` sources).
    """
    return {
        "streams": streams,
        "rtsp": {"listen": f":{rtsp_port}"},
        "api": {"listen": f":{api_port}"},
        # webrtc/srtp not needed for this tool's purpose (ONVIF discovery + RTSP
        # playback testing only); omit to avoid binding extra ports.
        "log": {"format": "text", "level": "warn"},
    }


def write_go2rtc_config(path: str, streams: dict[str, str | None], rtsp_port: int, api_port: int) -> str:
    payload = build_go2rtc_config(streams, rtsp_port=rtsp_port, api_port=api_port)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
    return path


class Go2rtcProcess:
    """Launches and supervises the go2rtc binary as a subprocess. POSIX shutdown
    (SIGTERM to the process group, wait, SIGKILL on timeout) follows
    mirage/mirage/go2rtc/process.py::Go2rtcProcess.stop; Windows terminates the child
    process directly and kills it if it does not exit before the timeout.
    """

    def __init__(self, binary_path: str, config_path: str) -> None:
        self.binary_path = binary_path
        self.config_path = config_path
        self._proc: sp.Popen | None = None

    def start(self) -> None:
        self._proc = sp.Popen(
            [self.binary_path, "-config", self.config_path],
            stdout=sp.DEVNULL,
            stderr=sp.DEVNULL,
            stdin=sp.DEVNULL,
            **({} if platform.system().lower() == "windows" else {"start_new_session": True}),
        )
        logger.info("go2rtc started (pid=%d)", self._proc.pid)

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        if self._proc is None:
            return
        if platform.system().lower() == "windows":
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=timeout)
                except sp.TimeoutExpired:
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=5)
                    except sp.TimeoutExpired:
                        pass
            logger.info("go2rtc stopped")
            return
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            self._proc.wait(timeout=timeout)
        except sp.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                self._proc.wait(timeout=5)
            except sp.TimeoutExpired:
                pass
        logger.info("go2rtc stopped")
