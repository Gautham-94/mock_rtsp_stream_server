"""Minimal ONVIF Device Management + Media SOAP service, one aiohttp app per camera.

This has to satisfy a REAL SOAP client -- onvif-zeep-async's `ONVIFCamera`, backed by
`zeep` -- not just "look like XML". Verified directly against the WSDL/XSD files
bundled in mirage/.venv/lib/python3.12/site-packages/onvif/wsdl/ (devicemgmt.wsdl,
media.wsdl, onvif.xsd), and against zeep's own binding-selection/response-parsing code
(zeep/wsdl/bindings/soap.py), not guessed:

  - The WSDL's `<wsdl:binding>` element binds the `soap` XML prefix to
    `http://schemas.xmlsoap.org/wsdl/soap12/` (`ns.SOAP_12` in zeep). zeep's
    `Soap12Binding.match()` keys off exactly that namespace to decide "this service
    uses SOAP 1.2" -- so despite the WSDL's `transport` attribute literally saying
    `.../soap/http` (the SOAP 1.1 HTTP transport URI, reused verbatim by the ONVIF spec
    for SOAP 1.2-over-HTTP too), the wire envelope must be SOAP *1.2*:
    envelope namespace `http://www.w3.org/2003/05/soap-envelope`, content-type
    `application/soap+xml`. Confirmed against go2rtc's own mock ONVIF server
    (pkg/onvif/envelope.go), which emits exactly this envelope shape.
  - zeep's `SoapOperation.process_reply` requires the HTTP response root element to be
    literally `{http://www.w3.org/2003/05/soap-envelope}Envelope`, requires HTTP status
    200 (any non-200, or a soap-env:Fault under Body, is treated as an error), and then
    hands `list(Body)[0]` -- the single child of Body -- to the schema for the
    OPERATION'S DECLARED OUTPUT ELEMENT to deserialize (zeep/wsdl/messages/soap.py
    `_deserialize_body`). That element's namespace+localname must exactly match what
    the WSDL declares (e.g. `{http://www.onvif.org/ver10/device/wsdl}
    GetDeviceInformationResponse`) -- confirmed reading devicemgmt.wsdl's
    `<wsdl:message name="GetDeviceInformationResponse"><wsdl:part element=
    "tds:GetDeviceInformationResponse"/></wsdl:message>`.
  - `update_xaddrs()` (onvif/client.py) tries `GetServices` FIRST and only falls back
    to `GetCapabilities` if that fails or returns nothing (a SOAP Fault counts as
    "failed", logged at debug and swallowed). This mock deliberately does NOT
    implement GetServices (always answers with a Fault) so every client predictably
    takes the GetCapabilities path, which we do implement fully -- simpler than
    maintaining two parallel XAddr-advertisement mechanisms that have to agree.
  - Only elements the real onvif.py flow (mirage/mirage/api/routers/onvif.py
    resolve_onvif_stream) actually touches are populated with real values; everything
    else in a complexType that isn't `minOccurs="0"`-omittable is filled with the
    simplest schema-valid value (e.g. Profile only requires `Name` + `token` attribute
    -- VideoSourceConfiguration/VideoEncoderConfiguration/etc are all `minOccurs="0"`
    per onvif.xsd's `Profile` complexType, so they're omitted entirely).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from xml.sax.saxutils import escape

from aiohttp import web

logger = logging.getLogger(__name__)

# Namespaces exactly as declared in the bundled WSDLs (onvif/wsdl/devicemgmt.wsdl,
# media.wsdl) -- NOT arbitrary, zeep resolves elements by (namespace, localname).
NS_SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
NS_TDS = "http://www.onvif.org/ver10/device/wsdl"  # devicemgmt
NS_TRT = "http://www.onvif.org/ver10/media/wsdl"  # media
NS_TT = "http://www.onvif.org/ver10/schema"  # shared types

SOAP_CONTENT_TYPE = "application/soap+xml; charset=utf-8"

_ENVELOPE_OPEN = (
    f'<?xml version="1.0" encoding="UTF-8"?>'
    f'<soap-env:Envelope xmlns:soap-env="{NS_SOAP_ENV}" '
    f'xmlns:tds="{NS_TDS}" xmlns:trt="{NS_TRT}" xmlns:tt="{NS_TT}">'
    f"<soap-env:Body>"
)
_ENVELOPE_CLOSE = "</soap-env:Body></soap-env:Envelope>"


def _envelope(body_xml: str) -> bytes:
    return (_ENVELOPE_OPEN + body_xml + _ENVELOPE_CLOSE).encode("utf-8")


def _soap_fault(reason: str) -> bytes:
    # Minimal SOAP 1.2 Fault; zeep's Soap12Binding.process_error only needs
    # soap-env:Body/soap-env:Fault to exist (it reads faultcode/faultstring style
    # subfields defensively), and update_xaddrs() only checks that GetServices
    # *failed* -- it doesn't inspect the fault detail -- so this minimal shape is
    # sufficient to drive the documented GetServices -> GetCapabilities fallback.
    body = (
        "<soap-env:Fault>"
        "<soap-env:Code><soap-env:Value>soap-env:Receiver</soap-env:Value></soap-env:Code>"
        f"<soap-env:Reason><soap-env:Text xml:lang=\"en\">{escape(reason)}</soap-env:Text></soap-env:Reason>"
        "</soap-env:Fault>"
    )
    return _envelope(body)


def _extract_operation(body: bytes) -> str | None:
    """Identify which SOAP operation was requested by the client, from the first
    child element's local name under soap-env:Body. Regex approach mirrors go2rtc's
    own server-side dispatch (pkg/onvif/server.go GetRequestAction) rather than a full
    XML parse, since we only need the tag name to route to a handler -- deliberately
    tolerant of namespace prefixes since different ONVIF clients declare their own.
    """
    import re

    match = re.search(rb"Body[^>]*>\s*<(?:\w+:)?([A-Za-z]+)", body)
    if not match:
        return None
    return match.group(1).decode("ascii")


def _extract_first_tag_text(body: bytes, tag: str) -> str | None:
    import re

    match = re.search(rb"<(?:\w+:)?" + tag.encode("ascii") + rb"\b[^>]*>([^<]+)<", body)
    if not match:
        return None
    return match.group(1).decode("utf-8")


@dataclass(frozen=True)
class CameraOnvifInfo:
    name: str
    onvif_port: int
    rtsp_port: int
    manufacturer: str = "MockCameras"
    model: str = "MC-1000"

    @property
    def serial_number(self) -> str:
        return f"MOCK-{self.name.upper()}"

    @property
    def profile_token(self) -> str:
        # Stable per camera, as required by GetProfiles/GetStreamUri's contract (the
        # resolve flow reads profiles[0].token and passes it straight to GetStreamUri).
        return f"profile_{self.name}"

    @property
    def rtsp_url(self) -> str:
        return f"rtsp://127.0.0.1:{self.rtsp_port}/{self.name}"


def _device_service_response(operation: str, info: CameraOnvifInfo, host: str) -> bytes:
    media_xaddr = f"http://{host}/onvif/media_service"

    if operation == "GetDeviceInformation":
        # tds:GetDeviceInformationResponse: Manufacturer, Model, FirmwareVersion,
        # SerialNumber, HardwareId all required xs:string (devicemgmt.wsdl line ~319).
        body = (
            "<tds:GetDeviceInformationResponse>"
            f"<tds:Manufacturer>{escape(info.manufacturer)}</tds:Manufacturer>"
            f"<tds:Model>{escape(info.model)}</tds:Model>"
            "<tds:FirmwareVersion>1.0.0</tds:FirmwareVersion>"
            f"<tds:SerialNumber>{escape(info.serial_number)}</tds:SerialNumber>"
            "<tds:HardwareId>1.0</tds:HardwareId>"
            "</tds:GetDeviceInformationResponse>"
        )
        return _envelope(body)

    if operation == "GetCapabilities":
        # tds:GetCapabilitiesResponse/tds:Capabilities: all of Analytics/Device/Events/
        # Imaging/Media/PTZ/Extension are minOccurs="0" (onvif.xsd Capabilities
        # complexType) -- only Device+Media are populated since that's all
        # update_xaddrs()/resolve_onvif_stream actually reads (Device.XAddr isn't even
        # read since devicemgmt's own XAddr is hardcoded by the client to
        # http://host:port/onvif/device_service regardless -- see onvif/client.py
        # get_definition -- but included anyway since a real camera always reports it).
        body = (
            "<tds:GetCapabilitiesResponse><tds:Capabilities>"
            f"<tt:Device><tt:XAddr>http://{host}/onvif/device_service</tt:XAddr></tt:Device>"
            "<tt:Media>"
            f"<tt:XAddr>{media_xaddr}</tt:XAddr>"
            "<tt:StreamingCapabilities>"
            "<tt:RTPMulticast>false</tt:RTPMulticast>"
            "<tt:RTP_TCP>false</tt:RTP_TCP>"
            "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>"
            "</tt:StreamingCapabilities>"
            "</tt:Media>"
            "</tds:Capabilities></tds:GetCapabilitiesResponse>"
        )
        return _envelope(body)

    if operation == "GetServices":
        # Deliberately unsupported -- see module docstring: forces update_xaddrs() to
        # take the documented GetCapabilities fallback path instead of maintaining two
        # XAddr sources that must agree.
        return _soap_fault("GetServices not implemented by mock camera")

    if operation == "GetSystemDateAndTime":
        # Not called by resolve_onvif_stream, but ONVIFCamera(adjust_time=False)
        # (the default, and what onvif.py uses) never calls it either -- included only
        # as a defensive no-op in case a future caller enables adjust_time.
        body = (
            "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
            "<tt:DateTimeType>Manual</tt:DateTimeType>"
            "<tt:DaylightSavings>false</tt:DaylightSavings>"
            "<tt:UTCDateTime><tt:Time><tt:Hour>0</tt:Hour><tt:Minute>0</tt:Minute>"
            "<tt:Second>0</tt:Second></tt:Time><tt:Date><tt:Year>2024</tt:Year>"
            "<tt:Month>1</tt:Month><tt:Day>1</tt:Day></tt:Date></tt:UTCDateTime>"
            "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
        )
        return _envelope(body)

    return _soap_fault(f"unsupported devicemgmt operation: {operation}")


def _media_service_response(operation: str, info: CameraOnvifInfo, body: bytes) -> bytes:
    if operation == "GetProfiles":
        # trt:GetProfilesResponse/trt:Profiles (repeated, media.wsdl line ~231): each
        # is tt:Profile-typed. Only `Name` (required child) + `token` (required
        # attribute) are populated -- every other Profile child
        # (VideoSourceConfiguration, AudioSourceConfiguration, VideoEncoderConfiguration,
        # ...) is minOccurs="0" per onvif.xsd's Profile complexType, so omitting them
        # is schema-valid and keeps this mock minimal.
        response_body = (
            "<trt:GetProfilesResponse>"
            f'<trt:Profiles token="{escape(info.profile_token)}" fixed="true">'
            f"<tt:Name>{escape(info.name)}</tt:Name>"
            "</trt:Profiles>"
            "</trt:GetProfilesResponse>"
        )
        return _envelope(response_body)

    if operation == "GetStreamUri":
        # trt:GetStreamUriResponse/trt:MediaUri (media.wsdl line ~1591): Uri,
        # InvalidAfterConnect, InvalidAfterReboot, Timeout are ALL required
        # (no minOccurs="0") per onvif.xsd's MediaUri complexType -- omitting any of
        # them would make zeep's schema validation reject the response.
        response_body = (
            "<trt:GetStreamUriResponse><trt:MediaUri>"
            f"<tt:Uri>{escape(info.rtsp_url)}</tt:Uri>"
            "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>"
            "<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
            "<tt:Timeout>PT0S</tt:Timeout>"
            "</trt:MediaUri></trt:GetStreamUriResponse>"
        )
        return _envelope(response_body)

    return _soap_fault(f"unsupported media operation: {operation}")


def build_camera_app(info: CameraOnvifInfo) -> web.Application:
    """One aiohttp Application per camera, serving both Device (tds) and Media (trt)
    services at their own sub-paths under the SAME port -- matching how a real camera
    exposes /onvif/device_service and /onvif/media_service on one HTTP listener (only
    the path differs, not the port), which is what GetCapabilities's advertised
    Media XAddr (http://host:port/onvif/media_service) implies.
    """

    async def device_service(request: web.Request) -> web.Response:
        raw = await request.read()
        operation = _extract_operation(raw)
        if operation is None:
            return web.Response(status=400, text="malformed SOAP request")
        host = request.host  # e.g. "127.0.0.1:8081", used to build XAddrs
        resp = _device_service_response(operation, info, host)
        return web.Response(body=resp, content_type="application/soap+xml", charset="utf-8")

    async def media_service(request: web.Request) -> web.Response:
        raw = await request.read()
        operation = _extract_operation(raw)
        if operation is None:
            return web.Response(status=400, text="malformed SOAP request")
        resp = _media_service_response(operation, info, raw)
        return web.Response(body=resp, content_type="application/soap+xml", charset="utf-8")

    app = web.Application()
    app.router.add_post("/onvif/device_service", device_service)
    app.router.add_post("/onvif/media_service", media_service)
    return app
