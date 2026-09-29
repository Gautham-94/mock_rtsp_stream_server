# mock_cameras

Emulates one or more real ONVIF IP cameras, entirely offline, from local video files --
for testing mirage's camera-discovery wizard ("Scan network" step) without any real
camera hardware on the network.

It runs, for N cameras configured in `config.yaml`:

- **go2rtc** (mirage's own vendored RTSP restreaming binary) serving each camera's
  video file as a looping RTSP stream at `rtsp://<host>:<rtsp-port>/<camera-name>`.
  Each stream is always on (fed by a supervised `ffmpeg` publisher per camera, not
  started on demand), so it keeps playing whether or not anyone is watching. Each
  video is first re-encoded once into a camera-like H.264 stream (no B-frames, 1s
  keyframe interval, constant frame rate, capped bitrate, audio stripped) to avoid stutter in VMS
  clients; re-encodes are cached under `.cache/prepared/` and redone only when the
  source file changes (so the first start with a new video takes a minute or two)
- a low-res **substream** per camera at `rtsp://<host>:<rtsp-port>/<camera-name>_sub`
  (320x180, 15fps, ~300 kbps) for VMS live-view grids / video walls, prepared and
  published the same way as the main stream
- a **WS-Discovery** UDP multicast responder (the same protocol real ONVIF cameras use
  to announce themselves), so mirage's wizard finds these cameras automatically, same
  as it would find a real camera on the LAN
- one **ONVIF Device Management + Media SOAP service** per camera (hand-rolled, schema-
  valid XML -- verified against the real `onvif-zeep-async` client mirage itself uses),
  each answering `GetDeviceInformation`, `GetCapabilities`, `GetProfiles`, `GetStreamUri`

This is intentionally a **standalone app**, independent of the `mirage` package: it has
its own `requirements.txt`/venv and does not import mirage's code (see
`mock_cameras/go2rtc.py`'s module docstring for why go2rtc's download/process helpers
are a small vendored copy rather than a dependency on mirage itself).

## Config file format

`config.yaml`:

```yaml
video_dir: /path/to/videos
cameras:
  - name: front_door       # matches front_door.mp4 in video_dir
  - name: backyard         # matches backyard.mp4 in video_dir
    path: /absolute/override/path.mp4   # optional: explicit path instead of video_dir/<name>.<ext>
  - name: garage           # a third camera -- N generalizes to any number, not hardcoded
```

- `video_dir`: directory searched for `<name>.mp4` (also tries `.mkv`/`.mov`/`.avi`) when
  a camera entry has no explicit `path`. Relative paths are resolved against the
  config file's own directory.
- `cameras`: a list of one or more cameras. Each needs a unique `name` (this becomes
  both the RTSP stream path and the value baked into `GetDeviceInformation`/scan
  results). `path`, if given, must be absolute and must exist.

Add or remove entries freely -- the app spins up exactly as many go2rtc streams, ONVIF
SOAP services, and WS-Discovery announcements as there are camera entries.

## Running it

One-time setup:

```
cd mock_cameras
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If you don't have real camera footage handy, generate a couple of quick test clips
with ffmpeg's synthetic test-pattern source (any small mp4 works -- distinct patterns
per camera just make it easy to tell them apart when eyeballing the stream):

```
ffmpeg -f lavfi -i "testsrc=size=320x240:rate=5" -t 5 -pix_fmt yuv420p videos/front_door.mp4
ffmpeg -f lavfi -i "testsrc2=size=320x240:rate=5" -t 5 -pix_fmt yuv420p videos/backyard.mp4
ffmpeg -f lavfi -i "smptebars=size=320x240:rate=5" -t 5 -pix_fmt yuv420p videos/garage.mp4
```

Then run it:

```
python3 -m mock_cameras --config config.yaml
```

On first run this downloads the go2rtc binary (cached under `mock_cameras/bin/`, same
convention as mirage's own `mirage/bin/`) -- no manual setup needed there. Logs print
every camera's RTSP URL and ONVIF port on startup, e.g.:

```
mock_cameras running -- 3 camera(s):
  front_door           rtsp://127.0.0.1:8554/front_door   onvif http://127.0.0.1:8081/onvif/device_service   video=.../front_door.mp4
  backyard             rtsp://127.0.0.1:8554/backyard     onvif http://127.0.0.1:8082/onvif/device_service   video=.../backyard.mp4
  garage               rtsp://127.0.0.1:8554/garage       onvif http://127.0.0.1:8083/onvif/device_service   video=.../garage.mp4
WS-Discovery responder active on udp 239.255.255.250:3702
```

Stop with Ctrl+C -- shutdown is graceful (SIGTERM, escalating to SIGKILL after a
timeout, tears down go2rtc; all HTTP/UDP listeners are closed cleanly).

Useful flags (see `python3 -m mock_cameras --help`): `--rtsp-port` (default 8554),
`--onvif-base-port` (default 8081, camera *i* gets `base+i`), `--go2rtc-api-port`
(default 1985 -- deliberately different from mirage's own go2rtc on 1984, so both can
run side by side on one machine), `-v` for debug logging.

## Pointing mirage's wizard at it

Nothing extra to configure -- run mirage and mock_cameras on the same machine (or same
LAN), open mirage's **Add Camera -> Scan network** wizard step, and the mock cameras
should just show up alongside any real cameras, exactly like a real one would: the
wizard's scan proxies to go2rtc's own WS-Discovery client, which multicasts a Probe on
`239.255.255.250:3702`; this app's WS-Discovery responder answers it, advertising each
camera's own ONVIF Device Service endpoint; the wizard's "resolve" step then talks
ONVIF SOAP directly to that endpoint to fetch the real RTSP stream URI. You can
double-check the same thing this repo's tests use:

```
curl http://127.0.0.1:8000/api/onvif/scan
curl "http://127.0.0.1:8000/api/onvif/resolve?ip=127.0.0.1&port=8081&username=any&password=any"
```

(assuming mirage.api is on port 8000 and `front_door` got ONVIF port 8081 -- match
whatever your own startup log printed).

### Manual fallback (if discovery doesn't fire)

WS-Discovery is UDP multicast, which some setups block (macOS firewall prompts,
corporate VPNs that don't forward multicast, Docker network modes, etc). If the mock
cameras don't show up in the scan step, you don't need discovery at all -- just add the
camera directly with its known RTSP URL:

```
rtsp://127.0.0.1:<rtsp-port>/<camera-name>
```

e.g. `rtsp://127.0.0.1:8554/front_door`, using the exact host/port/name your startup
log printed (or you can still resolve credentials-free via ONVIF directly, without
discovery, by giving the wizard's "add manually" step the camera's ONVIF port and
`127.0.0.1` as the IP -- ONVIF resolution itself doesn't depend on WS-Discovery having
worked, only the automatic "found it on the network" step does).

## Verifying a stream works outside mirage entirely

```
ffprobe -v error -show_entries stream=codec_type,width,height -of default=noprint_wrappers=1 rtsp://127.0.0.1:8554/front_door
```

## Notes / limitations

- Every camera answers with the same fake Manufacturer/Model (`MockCameras`/`MC-1000`)
  and a `SerialNumber` derived from its name -- enough to distinguish cameras in a scan
  list, not meant to imitate a specific real vendor's ONVIF quirks.
- Only the ONVIF operations mirage's own wizard actually calls are implemented
  (`GetDeviceInformation`, `GetCapabilities`, `GetProfiles`, `GetStreamUri`, plus a
  couple of harmless no-ops); anything else gets a SOAP Fault. `GetServices` is
  deliberately unimplemented so the real onvif-zeep-async client's documented
  GetServices-then-GetCapabilities fallback always takes the GetCapabilities path (see
  `mock_cameras/onvif_server.py`'s module docstring for why).
- Each camera profile has exactly one stream/profile (no substreams, no PTZ, no audio
  profile variations) -- sufficient for exercising the add-camera flow, not a general
  ONVIF conformance testbed.
- The RTSP server does not enforce the username/password the wizard collects (like
  many real cameras' RTSP layer, credentials are accepted but not actually checked) --
  fine for this tool's purpose (proving the discovery/resolve/playback pipeline wires
  up correctly), not a security boundary.
