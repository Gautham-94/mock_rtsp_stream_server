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
    return Path(bin_dir) / "go2rtc"


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

    dest.chmod(dest.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    logger.info("go2rtc binary ready at %s", dest)
    return str(dest)


# Name of the custom ffmpeg input template (added to the generated go2rtc.yaml's own
# `ffmpeg:` config section) that makes local video files loop forever at real-time
# pace. See stream_source_for()'s docstring for why this has to be a NAMED template
# rather than an inline literal override.
_LOOP_INPUT_TEMPLATE_NAME = "mock_loop"


def build_go2rtc_config(
    streams: dict[str, str],
    rtsp_port: int,
    api_port: int,
) -> dict:
    """Each stream entry loops its source video file forever via ffmpeg, mirroring
    mirage/scripts/run_test_stream.sh's `-re -stream_loop -1 -i <path>` pattern (real-
    time pacing + infinite loop so the RTSP session never runs out of frames). go2rtc's
    own `ffmpeg:` source-string syntax (documented in go2rtc's README) lets us hand it
    an ffmpeg CLI fragment directly instead of writing a wrapper shell script per
    camera.
    """
    return {
        "streams": streams,
        "rtsp": {"listen": f":{rtsp_port}"},
        "api": {"listen": f":{api_port}"},
        # webrtc/srtp not needed for this tool's purpose (ONVIF discovery + RTSP
        # playback testing only); omit to avoid binding extra ports.
        "log": {"format": "text", "level": "warn"},
        # Custom named ffmpeg input template -- see stream_source_for()'s docstring.
        "ffmpeg": {_LOOP_INPUT_TEMPLATE_NAME: "-re -stream_loop -1 -i {input}"},
    }


def stream_source_for(video_path: Path) -> str:
    """go2rtc "ffmpeg:" source syntax: `ffmpeg:<input>#<param>=<value>#<param>=<value>`.

    Confirmed directly against go2rtc source (internal/ffmpeg/ffmpeg.go's `defaults`
    map and `inputTemplate()`/`parseArgs()`), not guessed -- and confirmed by actually
    running go2rtc locally against several candidate source strings (an inline literal
    override with embedded spaces silently produced zero media tracks -- "streams:
    unknown error" -- even though `internal/ffmpeg/README.md` documents
    `#input=-timeout {timeout} -i {input}` as valid syntax; a NAMED custom template
    added to the config's own `ffmpeg:` section and referenced by name, e.g.
    `#input=mock_loop`, works reliably and was verified end-to-end with ffprobe):

      - go2rtc's *built-in* `"file"` input template is just `-re -i {input}` -- it does
        NOT loop (confirmed reading the `defaults["file"]` entry directly). Since mock
        cameras need to serve an RTSP stream indefinitely (real cameras never run out
        of frames), we need `-stream_loop -1` too, matching
        mirage/scripts/run_test_stream.sh's own `-re -stream_loop -1 -i <path>` pattern.
      - Rather than passing that literal template inline via `#input=-re -stream_loop
        -1 -i {input}` (which the README shows as valid but which empirically failed
        here), the template is instead registered ONCE under a name
        (`_LOOP_INPUT_TEMPLATE_NAME`, "mock_loop") in the generated go2rtc.yaml's own
        `ffmpeg:` section (see build_go2rtc_config), and each stream source just
        references it by name: `#input=mock_loop`. This sidesteps whatever go2rtc-side
        parsing quirk rejects the inline spaces-containing form, and also matches the
        README's own primary documented pattern (`ffmpeg: {mycodec: "...", myinput:
        "..."}` config block + `#input=<name>` reference).
      - `#video=copy#audio=copy` keeps both streams uncopied/untranscoded, matching how
        real ONVIF cameras' GetStreamUri output is consumed as-is.

    NOTE: go2rtc splits the source string on `#` and treats each `#`-segment as its own
    `key=value` query param (see `streams.ParseQuery` usage in parseArgs), so this
    string must not contain literal `#`/`&` itself; video file paths are trusted local
    config here, not attacker input.
    """
    return f"ffmpeg:{video_path}#input={_LOOP_INPUT_TEMPLATE_NAME}#video=copy#audio=copy"


def write_go2rtc_config(path: str, streams: dict[str, str], rtsp_port: int, api_port: int) -> str:
    payload = build_go2rtc_config(streams, rtsp_port=rtsp_port, api_port=api_port)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(payload, f, sort_keys=False)
    return path


class Go2rtcProcess:
    """Launches and supervises the go2rtc binary as a subprocess. Shutdown pattern
    (SIGTERM to the process group, wait, SIGKILL on timeout) copied from
    mirage/mirage/go2rtc/process.py::Go2rtcProcess.stop.
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
            start_new_session=True,
        )
        logger.info("go2rtc started (pid=%d)", self._proc.pid)

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def stop(self, timeout: float = 10.0) -> None:
        if self._proc is None:
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
