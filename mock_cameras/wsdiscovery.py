"""WS-Discovery multicast responder: answers Probe messages with a ProbeMatch per
configured camera.

The only real client of this is go2rtc's own WS-Discovery prober (verified by reading
go2rtc source, not guessed: pkg/onvif/helpers.go `DiscoveryStreamingDevices`, called
from internal/onvif/onvif.go `apiOnvif` -- which is what mirage's own
`GET /api/onvif/scan` route proxies, per mirage/mirage/api/routers/onvif.py's
docstring). That client is deliberately tolerant:

  - It joins no multicast group itself -- it just sends a UDP Probe to
    239.255.255.250:3702 from an ephemeral unicast socket and reads whatever unicast
    UDP replies arrive at that socket within a 5s deadline (`net.ListenUDP` + WriteTo +
    ReadFromUDP). So our responder does not need to be a multicast-group MEMBER either;
    it just needs to be listening ON port 3702 (to receive the multicast-addressed
    Probe) and reply with a plain unicast UDP datagram back to the sender's address.
  - It does no XML schema validation at all -- it extracts fields via regex:
    `FindTagValue(b, "XAddrs")` (any tag ending in "XAddrs", namespace prefix
    irrelevant) and `FindTagValue(b, "Scopes")` (for name/hardware, optional -- absence
    just means the discovered device has no Name in mirage's dropdown). It also
    requires the response bytes to contain the literal substring "onvif" somewhere
    (used as a cheap filter to ignore non-ONVIF multicast chatter e.g. printers) --
    satisfied here via the `dn:NetworkVideoTransmitter` type string and the
    `onvif://www.onvif.org/...` scope URIs.

Despite that client-side leniency, this responder emits a spec-shaped ProbeMatch
(matching the real ONVIF WS-Discovery message format -- wsdd:XAddrs, wsdd:Types,
wsa:MessageID/RelatesTo, etc, per the ONVIF WS-Discovery discovery spec and the pattern
of onvif/wsdl/ws-discovery.xsd) rather than the bare minimum, so this same responder
would also work against a stricter real ONVIF client/VMS, not just go2rtc's.
"""

from __future__ import annotations

import asyncio
import logging
import re
import socket
import struct
import uuid
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MCAST_GROUP = "239.255.255.250"
MCAST_PORT = 3702

_PROBE_RE = re.compile(rb"<(?:\w+:)?Probe\b", re.IGNORECASE)
_MESSAGE_ID_RE = re.compile(rb"<(?:\w+:)?MessageID\b[^>]*>([^<]+)<", re.IGNORECASE)

NS_SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
NS_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
NS_WSDD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
NS_WSDP = "http://schemas.xmlsoap.org/ws/2006/02/devprof"
NS_DN = "http://www.onvif.org/ver10/network/wsdl"


@dataclass(frozen=True)
class DiscoverableCamera:
    name: str
    onvif_port: int


def _probe_match_response(camera: DiscoverableCamera, relates_to: str | None) -> bytes:
    device_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"mock-camera-{camera.name}")
    xaddr = f"http://127.0.0.1:{camera.onvif_port}/onvif/device_service"
    relates_to_header = f"<wsa:RelatesTo>{relates_to}</wsa:RelatesTo>" if relates_to else ""

    # dn:NetworkVideoTransmitter is the ONVIF-defined discovery Type for an NVT (camera)
    # device, in the ONVIF-specific "dn" namespace -- confirmed against the ONVIF
    # WS-Discovery / Network Interface spec's discovery Types convention (the same
    # `dn:NetworkVideoTransmitter` string real ONVIF cameras answer with, and what
    # mirage's own real-camera scan against a Hikvision device on the LAN returned
    # during manual verification of this tool).
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soap:Envelope xmlns:soap="{NS_SOAP_ENV}" xmlns:wsa="{NS_WSA}" '
        f'xmlns:wsdd="{NS_WSDD}" xmlns:wsdp="{NS_WSDP}" xmlns:dn="{NS_DN}">'
        "<soap:Header>"
        f"<wsa:Action>{NS_WSDD}/ProbeMatches</wsa:Action>"
        f"<wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>"
        f"{relates_to_header}"
        "<wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>"
        "</soap:Header>"
        "<soap:Body>"
        "<wsdd:ProbeMatches>"
        "<wsdd:ProbeMatch>"
        f"<wsa:EndpointReference><wsa:Address>urn:uuid:{device_uuid}</wsa:Address></wsa:EndpointReference>"
        "<wsdd:Types>dn:NetworkVideoTransmitter</wsdd:Types>"
        f"<wsdd:Scopes>onvif://www.onvif.org/type/video_encoder "
        f"onvif://www.onvif.org/name/{camera.name} "
        "onvif://www.onvif.org/hardware/MockCamera "
        "onvif://www.onvif.org/Profile/Streaming</wsdd:Scopes>"
        f"<wsdd:XAddrs>{xaddr}</wsdd:XAddrs>"
        "<wsdd:MetadataVersion>1</wsdd:MetadataVersion>"
        "</wsdd:ProbeMatch>"
        "</wsdd:ProbeMatches>"
        "</soap:Body>"
        "</soap:Envelope>"
    ).encode("utf-8")


class WsDiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, cameras: list[DiscoverableCamera]) -> None:
        self.cameras = cameras
        self.transport: asyncio.DatagramTransport | None = None
        # De-dupe by the Probe's own wsa:MessageID: a host with more than one active
        # network interface/address in the same multicast group (observed in practice
        # on macOS with an active Wi-Fi + loopback + VPN utun interfaces all bound to
        # 0.0.0.0) can have the kernel deliver a single sent multicast datagram to this
        # process more than once. The ONVIF/WS-Discovery spec recommends clients
        # dedupe by MessageID for exactly this reason, but go2rtc's own prober
        # (pkg/onvif/helpers.go DiscoveryStreamingDevices) does not -- it just appends
        # every UDP payload it reads -- so the responder dedupes instead of relying on
        # the client to.
        self._recently_answered: dict[str, float] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not _PROBE_RE.search(data):
            return  # not a Probe (could be a ProbeMatch/Hello/Bye from elsewhere)

        match = _MESSAGE_ID_RE.search(data)
        relates_to = match.group(1).decode("utf-8") if match else None

        if relates_to is not None:
            loop_time = asyncio.get_event_loop().time()
            last_seen = self._recently_answered.get(relates_to)
            # Prune opportunistically so this dict can't grow unbounded across a long
            # run -- entries older than the deadline are just discarded on the next
            # Probe, no separate timer needed for a tool whose whole purpose is manual/
            # occasional test scans.
            self._recently_answered = {
                mid: t for mid, t in self._recently_answered.items() if loop_time - t < 5.0
            }
            if last_seen is not None and loop_time - last_seen < 5.0:
                logger.debug("duplicate Probe MessageID %s from %s, not re-answering", relates_to, addr)
                return
            self._recently_answered[relates_to] = loop_time

        logger.debug("WS-Discovery Probe from %s, replying with %d camera(s)", addr, len(self.cameras))
        for camera in self.cameras:
            response = _probe_match_response(camera, relates_to)
            assert self.transport is not None
            # Unicast the ProbeMatch straight back to the prober's address -- this is
            # standard WS-Discovery behavior (ProbeMatch replies are unicast, only the
            # Probe itself is multicast) and matches exactly what go2rtc's client reads
            # (a plain ReadFromUDP on its own unicast socket, never joining the
            # multicast group as a listener itself).
            self.transport.sendto(response, addr)

    def error_received(self, exc: Exception) -> None:
        logger.warning("WS-Discovery socket error: %s", exc)


def _make_socket() -> socket.socket:
    """Binds to 0.0.0.0:3702 and joins the 239.255.255.250 multicast group -- both
    required for this process to actually RECEIVE multicast-addressed Probe datagrams
    (join membership) sent to that port from anywhere on the LAN (including go2rtc
    running as a different process on the same host, or a real client on the same
    subnet), not just loopback unicast traffic.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        # Lets a real camera's WS-Discovery responder (or another instance of this
        # tool) coexist on the same host/port without bind() failing -- best-effort,
        # not all platforms support it (guarded by hasattr).
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("0.0.0.0", MCAST_PORT))

    mreq = struct.pack("4s4s", socket.inet_aton(MCAST_GROUP), socket.inet_aton("0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setblocking(False)
    return sock


async def run_wsdiscovery_responder(
    cameras: list[DiscoverableCamera],
) -> tuple[asyncio.DatagramTransport, WsDiscoveryProtocol]:
    """Starts the responder and returns (transport, protocol); caller is responsible
    for calling transport.close() on shutdown.
    """
    loop = asyncio.get_running_loop()
    sock = _make_socket()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: WsDiscoveryProtocol(cameras), sock=sock
    )
    logger.info(
        "WS-Discovery responder listening on udp %s:%d for %d camera(s)",
        MCAST_GROUP,
        MCAST_PORT,
        len(cameras),
    )
    return transport, protocol  # type: ignore[return-value]
