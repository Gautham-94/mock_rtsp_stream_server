"""Loads mock_cameras/config.yaml into typed CameraSpec objects.

Config shape (see README.md for the full description)::

    video_dir: /path/to/videos
    cameras:
      - name: front_door       # matches front_door.mp4 in video_dir
      - name: backyard
        path: /absolute/override/path.mp4   # optional explicit override

Deliberately tiny/hand-rolled (no pydantic) to keep this standalone app's
requirements.txt minimal -- see requirements.txt for the reasoning on why we
don't just reuse mirage's own config models.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

# Extensions searched (in order) under video_dir/<name>.<ext> when a camera entry
# does not give an explicit `path`. mp4 is the only one the spec requires; the others
# are free generality since glob-by-stem costs nothing extra.
_SEARCH_EXTS = ("mp4", "mkv", "mov", "avi")


class ConfigError(ValueError):
    """Raised for any structurally-invalid config.yaml."""


@dataclass(frozen=True)
class CameraSpec:
    name: str
    video_path: Path


@dataclass(frozen=True)
class AppConfig:
    video_dir: Path
    cameras: tuple[CameraSpec, ...]


def _resolve_video_path(name: str, video_dir: Path, explicit_path: str | None) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_absolute():
            raise ConfigError(
                f"camera {name!r}: 'path' override must be an absolute path, got {explicit_path!r}"
            )
        if not path.is_file():
            raise ConfigError(f"camera {name!r}: explicit path does not exist: {path}")
        return path

    for ext in _SEARCH_EXTS:
        candidate = video_dir / f"{name}.{ext}"
        if candidate.is_file():
            return candidate

    raise ConfigError(
        f"camera {name!r}: no video file found (looked for "
        f"{', '.join(f'{name}.{ext}' for ext in _SEARCH_EXTS)} in {video_dir}); "
        "either add the file or set an explicit 'path' for this camera"
    )


def load_config(config_path: str | Path) -> AppConfig:
    config_path = Path(config_path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ConfigError(f"{config_path}: top-level YAML must be a mapping")

    raw_video_dir = raw.get("video_dir")
    if not raw_video_dir:
        raise ConfigError(f"{config_path}: 'video_dir' is required")
    # Relative video_dir is resolved against the config file's own directory, not the
    # process cwd, so `mock_cameras --config foo/config.yaml` behaves the same
    # regardless of where it's invoked from.
    video_dir = Path(raw_video_dir).expanduser()
    if not video_dir.is_absolute():
        video_dir = (config_path.parent / video_dir).resolve()

    raw_cameras = raw.get("cameras")
    if not raw_cameras or not isinstance(raw_cameras, list):
        raise ConfigError(f"{config_path}: 'cameras' must be a non-empty list")

    cameras: list[CameraSpec] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(raw_cameras):
        if not isinstance(entry, dict) or not entry.get("name"):
            raise ConfigError(f"{config_path}: cameras[{i}] is missing required 'name'")
        name = str(entry["name"])
        if name in seen_names:
            raise ConfigError(f"{config_path}: duplicate camera name {name!r}")
        seen_names.add(name)
        video_path = _resolve_video_path(name, video_dir, entry.get("path"))
        cameras.append(CameraSpec(name=name, video_path=video_path))

    return AppConfig(video_dir=video_dir, cameras=tuple(cameras))
