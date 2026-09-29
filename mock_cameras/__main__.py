"""Entry point: `python -m mock_cameras --config config.yaml`.

Spawns, for N configured cameras:
  - one go2rtc process (vendored download/process helpers, see go2rtc.py) serving all
    N RTSP streams, one per camera, each looping its video file
  - one WS-Discovery UDP responder (wsdiscovery.py) answering Probes for all N cameras
  - N separate aiohttp ONVIF SOAP HTTP servers (onvif_server.py), one port each

All in one asyncio event loop, per the task spec ("one asyncio event loop is fine").
Graceful shutdown on SIGINT/SIGTERM tears down go2rtc (SIGTERM->SIGKILL escalation,
see go2rtc.py) and stops all aiohttp runners/the WS-Discovery transport.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from aiohttp import web

from mock_cameras.config import AppConfig, ConfigError, CameraSpec, load_config
from mock_cameras.go2rtc import (
    Go2rtcProcess,
    ensure_go2rtc_binary,
    write_go2rtc_config,
)
from mock_cameras.onvif_server import CameraOnvifInfo, build_camera_app
from mock_cameras.publisher import SUBSTREAM_SUFFIX, VARIANTS, CameraPublisher, prepare_video
from mock_cameras.wsdiscovery import DiscoverableCamera, run_wsdiscovery_responder

logger = logging.getLogger("mock_cameras")

DEFAULT_RTSP_PORT = 8554
DEFAULT_GO2RTC_API_PORT = 1985  # avoid colliding with a real mirage go2rtc on 1984
DEFAULT_ONVIF_BASE_PORT = 8081
_APP_ROOT = Path(__file__).resolve().parent.parent


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m mock_cameras",
        description="Emulate one or more ONVIF IP cameras from local video files, for testing mirage's camera-discovery wizard offline.",
    )
    parser.add_argument("--config", required=True, help="Path to config.yaml (see README.md for format)")
    parser.add_argument("--bin-dir", default=str(_APP_ROOT / "bin"), help="Directory to cache the go2rtc binary in (default: mock_cameras/bin)")
    parser.add_argument("--cache-dir", default=str(_APP_ROOT / ".cache"), help="Directory for generated go2rtc.yaml (default: mock_cameras/.cache)")
    parser.add_argument("--rtsp-port", type=int, default=DEFAULT_RTSP_PORT, help=f"go2rtc RTSP listen port (default: {DEFAULT_RTSP_PORT})")
    parser.add_argument("--go2rtc-api-port", type=int, default=DEFAULT_GO2RTC_API_PORT, help=f"go2rtc's own HTTP API port (default: {DEFAULT_GO2RTC_API_PORT})")
    parser.add_argument("--onvif-base-port", type=int, default=DEFAULT_ONVIF_BASE_PORT, help=f"first ONVIF HTTP port; camera i gets base+i (default: {DEFAULT_ONVIF_BASE_PORT})")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
    return parser.parse_args(argv)


class MockCamerasApp:
    def __init__(self, config: AppConfig, args: argparse.Namespace) -> None:
        self.config = config
        self.args = args
        self.go2rtc_process: Go2rtcProcess | None = None
        self._publishers: list[CameraPublisher] = []
        self._runners: list[web.AppRunner] = []
        self._wsdiscovery_transport = None
        self._stop_event = asyncio.Event()
        # Assign each camera a stable ONVIF port up front so logging/README ports and
        # actually-bound ports can never drift apart.
        self.onvif_ports: dict[str, int] = {
            camera.name: args.onvif_base_port + i for i, camera in enumerate(config.cameras)
        }

    def _camera_info(self, camera: CameraSpec) -> CameraOnvifInfo:
        return CameraOnvifInfo(
            name=camera.name,
            onvif_port=self.onvif_ports[camera.name],
            rtsp_port=self.args.rtsp_port,
        )

    async def start(self) -> None:
        prepared = await self._prepare_videos()
        await self._start_go2rtc(list(prepared))
        self._start_publishers(prepared)
        await self._start_wsdiscovery()
        await self._start_onvif_servers()
        self._log_summary()

    async def _prepare_videos(self) -> dict[str, Path]:
        """Returns {rtsp stream name: prepared video}, one main + one substream per camera."""
        # Sequential on purpose: each libx264 encode already uses every core.
        cache_dir = Path(self.args.cache_dir) / "prepared"
        prepared: dict[str, Path] = {}
        for camera in self.config.cameras:
            for variant in VARIANTS:
                stream = camera.name + variant.suffix
                prepared[stream] = await prepare_video(stream, camera.video_path, cache_dir, variant)
        return prepared

    async def _start_go2rtc(self, stream_names: list[str]) -> None:
        binary_path = ensure_go2rtc_binary(self.args.bin_dir)
        # Empty sources: each stream is fed by our own always-on publisher (see
        # publisher.py) pushing into go2rtc, not by an on-demand go2rtc producer.
        streams: dict[str, str | None] = {name: None for name in stream_names}
        config_path = str(Path(self.args.cache_dir) / "go2rtc.yaml")
        write_go2rtc_config(
            config_path, streams, rtsp_port=self.args.rtsp_port, api_port=self.args.go2rtc_api_port
        )
        self.go2rtc_process = Go2rtcProcess(binary_path, config_path)
        self.go2rtc_process.start()
        # Give go2rtc a brief moment to bind its listeners before we declare success --
        # purely cosmetic for the startup log; RTSP clients connecting before this
        # would just retry/fail fast, nothing depends on this sleep for correctness.
        await asyncio.sleep(0.5)
        if not self.go2rtc_process.is_alive():
            raise RuntimeError("go2rtc exited immediately after start -- check port conflicts")

    def _start_publishers(self, prepared: dict[str, Path]) -> None:
        for stream, video_path in prepared.items():
            publisher = CameraPublisher(stream, video_path, self.args.rtsp_port)
            publisher.start()
            self._publishers.append(publisher)

    async def _start_wsdiscovery(self) -> None:
        discoverable = [
            DiscoverableCamera(name=camera.name, onvif_port=self.onvif_ports[camera.name])
            for camera in self.config.cameras
        ]
        transport, _protocol = await run_wsdiscovery_responder(discoverable)
        self._wsdiscovery_transport = transport

    async def _start_onvif_servers(self) -> None:
        for camera in self.config.cameras:
            info = self._camera_info(camera)
            app = build_camera_app(info)
            runner = web.AppRunner(app, access_log=None)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", info.onvif_port)
            await site.start()
            self._runners.append(runner)
            logger.info(
                "camera %r: ONVIF device_service on http://127.0.0.1:%d/onvif/device_service",
                camera.name,
                info.onvif_port,
            )

    def _log_summary(self) -> None:
        logger.info("=" * 72)
        logger.info("mock_cameras running -- %d camera(s):", len(self.config.cameras))
        for camera in self.config.cameras:
            onvif_port = self.onvif_ports[camera.name]
            logger.info(
                "  %-20s rtsp://127.0.0.1:%d/%s (sub: /%s%s)   onvif http://127.0.0.1:%d/onvif/device_service   video=%s",
                camera.name,
                self.args.rtsp_port,
                camera.name,
                camera.name,
                SUBSTREAM_SUFFIX,
                onvif_port,
                camera.video_path,
            )
        logger.info("WS-Discovery responder active on udp %s:%d", "239.255.255.250", 3702)
        logger.info("go2rtc API (debug): http://127.0.0.1:%d", self.args.go2rtc_api_port)
        logger.info("=" * 72)
        logger.info("Press Ctrl+C to stop.")

    async def stop(self) -> None:
        logger.info("shutting down...")
        if self._wsdiscovery_transport is not None:
            self._wsdiscovery_transport.close()
        for runner in self._runners:
            await runner.cleanup()
        for publisher in self._publishers:
            await publisher.stop()
        if self.go2rtc_process is not None:
            self.go2rtc_process.stop()
        logger.info("shutdown complete")

    def request_stop(self) -> None:
        self._stop_event.set()

    async def wait_for_stop(self) -> None:
        await self._stop_event.wait()


async def _async_main(args: argparse.Namespace) -> int:
    try:
        config = load_config(args.config)
    except ConfigError as e:
        logger.error("config error: %s", e)
        return 1

    app = MockCamerasApp(config, args)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, app.request_stop)

    try:
        await app.start()
    except Exception:
        logger.exception("failed to start mock_cameras")
        await app.stop()
        return 1

    await app.wait_for_stop()
    await app.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
