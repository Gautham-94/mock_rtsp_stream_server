"""Per-camera ffmpeg publishers: prepare each video once into a camera-like encoding,
then push it into go2rtc as a looping, always-on RTSP stream.

Why this replaces go2rtc's own on-demand `ffmpeg:` stream sources:

  - go2rtc's `ffmpeg:` sources are ON-DEMAND -- go2rtc only spawns the ffmpeg producer
    when the first RTSP consumer connects and kills it when the last one leaves. A real
    camera never stops streaming, and a VMS reconnecting would otherwise see the video
    restart from 0:00. Instead we run our OWN ffmpeg per camera and PUBLISH into go2rtc
    (`-f rtsp rtsp://127.0.0.1:<port>/<name>`, go2rtc accepts pushes to any stream name
    declared in its config with an empty source), so each stream is live from startup
    regardless of who is watching. A supervisor loop restarts ffmpeg if it ever exits.
  - The source clips are typically web downloads (YouTube-style encodes): B-frames,
    5-6s keyframe intervals, High profile, unconstrained bitrate. Passed through as-is
    (`-c copy`), VMS clients stutter/lag: B-frame reordering adds decode latency and
    jitter, and a client joining mid-GOP shows garbage/freezes for up to 6s until the
    next keyframe. prepare_video() re-encodes once into what a real IP camera emits:
    Main profile, no B-frames, constant frame rate, a keyframe every second, capped
    bitrate, no audio. The result is cached, so streaming itself stays `-c copy`
    (near-zero CPU).
  - Like a real camera, each one also gets a low-res SUBSTREAM (`<name>_sub`, 320x180,
    15fps) for VMS live-view grids / video walls, where decoding N full-res streams
    just to show small tiles wastes client CPU and bandwidth.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SUBSTREAM_SUFFIX = "_sub"

_COMMON_ARGS = [
    "-map", "0:v:0",
    "-an",  # video only -- streams carry no audio track
    "-fps_mode", "cfr",
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-profile:v", "main",
    "-pix_fmt", "yuv420p",
    "-force_key_frames", "expr:gte(t,n_forced*1)",  # keyframe every 1s
    "-x264-params", "bframes=0:scenecut=0",
    "-movflags", "+faststart",
]


@dataclass(frozen=True)
class Variant:
    suffix: str  # appended to the camera name to form the RTSP stream name
    version: str  # bump when this variant's encode args change, invalidating its cache
    args: tuple[str, ...]


MAIN = Variant(
    suffix="",
    version="2",
    args=("-crf", "23", "-maxrate", "4M", "-bufsize", "4M"),
)
SUB = Variant(
    suffix=SUBSTREAM_SUFFIX,
    version="1",
    args=(
        # Fit into 320x180 keeping aspect ratio, letterboxing non-16:9 sources.
        "-vf", "fps=15,scale=320:180:force_original_aspect_ratio=decrease,"
        "pad=320:180:(ow-iw)/2:(oh-ih)/2,setsar=1",
        "-crf", "26", "-maxrate", "300k", "-bufsize", "300k",
    ),
)
VARIANTS = (MAIN, SUB)

_RESTART_DELAY_S = 2.0


def _prepared_path(video_path: Path, cache_dir: Path, variant: Variant) -> Path:
    st = video_path.stat()
    key = f"{variant.version}{variant.suffix}|{video_path.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    return cache_dir / f"{video_path.stem}{variant.suffix}-{digest}.mp4"


async def prepare_video(name: str, video_path: Path, cache_dir: Path, variant: Variant) -> Path:
    """Returns a camera-like re-encode of video_path for the given variant, transcoding
    only if no cached copy for this exact source file (path + size + mtime) exists yet."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = _prepared_path(video_path, cache_dir, variant)
    if out.is_file():
        logger.info("stream %r: using cached prepared video %s", name, out.name)
        return out

    tmp = out.with_suffix(".tmp.mp4")
    logger.info("stream %r: preparing video (one-time re-encode, may take a minute)...", name)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-v", "error", "-y", "-i", str(video_path),
        *_COMMON_ARGS, *variant.args, str(tmp),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"stream {name!r}: ffmpeg prepare failed ({proc.returncode}): "
            f"{stderr.decode(errors='replace').strip()[-500:]}"
        )
    os.replace(tmp, out)
    logger.info("stream %r: prepared video ready (%s)", name, out.name)
    return out


class CameraPublisher:
    """Keeps one `ffmpeg -re -stream_loop -1 ... -f rtsp` process pushing a prepared
    video into go2rtc, restarting it whenever it exits until stop() is called."""

    def __init__(self, name: str, video_path: Path, rtsp_port: int) -> None:
        self.name = name
        self.video_path = video_path
        self.target = f"rtsp://127.0.0.1:{rtsp_port}/{name}"
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"publisher-{self.name}")

    async def _run(self) -> None:
        while not self._stopping:
            self._proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-v", "error",
                "-re", "-stream_loop", "-1", "-fflags", "+genpts",
                "-i", str(self.video_path),
                "-c", "copy",
                "-rtsp_transport", "tcp", "-f", "rtsp", self.target,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                **({"start_new_session": True} if os.name != "nt" else {}),
            )
            logger.debug("camera %r: publisher started (pid=%d)", self.name, self._proc.pid)
            _, stderr = await self._proc.communicate()
            if self._stopping:
                break
            logger.warning(
                "camera %r: publisher exited (%s), restarting in %.0fs: %s",
                self.name,
                self._proc.returncode,
                _RESTART_DELAY_S,
                stderr.decode(errors="replace").strip()[-300:],
            )
            await asyncio.sleep(_RESTART_DELAY_S)

    async def stop(self) -> None:
        self._stopping = True
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
