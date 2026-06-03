"""
Residential proxy support for meeting bots.

Goal: give each bot its own residential egress IP, sticky for the whole meeting,
so Google Meet (and other platforms) see a clean residential address per meeting
instead of one shared/burned datacenter IP that gets flagged + blocked over time.

How it works:
  Chrome cannot put credentials in --proxy-server, and residential providers
  require user:pass auth (a Railway egress IP is not stable enough for IP-allowlist
  auth). So we run a tiny local HTTP-proxy forwarder in a daemon thread:

      Chrome --proxy-server=http://127.0.0.1:<localport>
              -> ResidentialProxyForwarder (adds Proxy-Authorization)
              -> upstream residential proxy gateway (sticky session)
              -> a unique residential IP for this meeting

The sticky session id is generated once per bot process and embedded in the
proxy username via RESIDENTIAL_PROXY_USERNAME_TEMPLATE, so the bot keeps ONE IP
for the meeting's lifetime. Provider-agnostic: the template makes it work with
Bright Data / Oxylabs / Smartproxy(Decodo) / IPRoyal / Webshare / etc.

Env vars (set on the bot worker):
  RESIDENTIAL_PROXY_ENABLED            "true" to turn it on
  RESIDENTIAL_PROXY_HOST               upstream proxy gateway host
  RESIDENTIAL_PROXY_PORT               upstream proxy gateway port
  RESIDENTIAL_PROXY_USERNAME_TEMPLATE  username with a literal "{session}" placeholder
                                       e.g. Bright Data:  "brd-customer-XXXX-zone-resi-session-{session}"
                                            Oxylabs:      "customer-USER-sessid-{session}-sesstime-30"
                                            Smartproxy:   "user-USER-session-{session}-sessionduration-30"
  RESIDENTIAL_PROXY_PASSWORD           upstream proxy password
  RESIDENTIAL_PROXY_INCLUDE_MEDIA      "true" to also force WebRTC media through the
                                       proxy (full IP mask, but expensive). Default
                                       "false": only signaling/HTTPS is proxied
                                       (cheap, and that's what the join-block inspects).
"""

import asyncio
import base64
import logging
import os
import random
import string
import threading

logger = logging.getLogger(__name__)


def residential_proxy_enabled():
    return os.getenv("RESIDENTIAL_PROXY_ENABLED", "false").lower() == "true"


def residential_proxy_include_media():
    return os.getenv("RESIDENTIAL_PROXY_INCLUDE_MEDIA", "false").lower() == "true"


def residential_proxy_bypass_list():
    # Send heavy Google STATIC assets (JS/WASM/fonts/images, served from CDN domains)
    # DIRECT instead of through the metered residential proxy. Bot detection only
    # inspects the meeting-signaling IP (meet.google.com / *.google.com / *.googleapis.com,
    # which stay proxied), NOT which IP fetched a font, so this cuts proxy bandwidth
    # ~5x (≈22MB -> ≈4MB/bot) with no effect on the join gate. Override via env.
    return os.getenv(
        "RESIDENTIAL_PROXY_BYPASS_LIST",
        "localhost;127.0.0.1;*.gstatic.com;*.googleusercontent.com;*.ggpht.com;fonts.googleapis.com;*.gvt1.com",
    )


def _new_session_id(n=12):
    # Process-stable sticky session id -> one residential IP for this bot's meeting.
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


class ResidentialProxyForwarder:
    """Local HTTP-proxy forwarder that adds upstream auth and pins a sticky session.

    Chrome points at 127.0.0.1:<local_port> (no auth needed by Chrome). Each client
    proxy request (CONNECT for HTTPS) is relayed to the authenticated upstream
    residential gateway, after which bytes are tunneled both ways.
    """

    def __init__(self):
        self.upstream_host = os.environ["RESIDENTIAL_PROXY_HOST"]
        self.upstream_port = int(os.environ["RESIDENTIAL_PROXY_PORT"])
        password_template = os.environ["RESIDENTIAL_PROXY_PASSWORD"]
        template = os.environ["RESIDENTIAL_PROXY_USERNAME_TEMPLATE"]

        self.session = _new_session_id()
        # Providers differ on where the sticky-session id goes: Bright Data / Oxylabs /
        # Smartproxy put it in the USERNAME; IPRoyal / Webshare put it in the PASSWORD
        # (e.g. "<pass>_country-in_session-{session}_lifetime-30m"). Substitute in both
        # so the same code is provider-agnostic.
        if "{session}" not in template and "{session}" not in password_template:
            logger.warning("Neither RESIDENTIAL_PROXY_USERNAME_TEMPLATE nor RESIDENTIAL_PROXY_PASSWORD has a {session} placeholder; the bot will not get a sticky per-meeting IP")
        username = template.replace("{session}", self.session)
        password = password_template.replace("{session}", self.session)
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self._auth_header_line = f"Proxy-Authorization: Basic {token}\r\n".encode()

        self.local_port = None
        self._thread = None
        self._loop = None

    def start(self):
        ready = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            server = self._loop.run_until_complete(asyncio.start_server(self._handle, "127.0.0.1", 0))
            self.local_port = server.sockets[0].getsockname()[1]
            ready.set()
            self._loop.run_forever()

        self._thread = threading.Thread(target=run, name="residential-proxy", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=15):
            raise RuntimeError("Residential proxy forwarder failed to start in time")
        logger.info("Residential proxy forwarder listening on 127.0.0.1:%s -> %s:%s (sticky session %s)", self.local_port, self.upstream_host, self.upstream_port, self.session)
        return self.local_port

    async def _handle(self, client_reader, client_writer):
        upstream_writer = None
        try:
            # Read the client's proxy request line + headers (Chrome speaks the HTTP
            # proxy protocol: "CONNECT host:443 HTTP/1.1\r\n...\r\n\r\n" for HTTPS).
            header = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=30)
            upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(self.upstream_host, self.upstream_port), timeout=30)

            # Relay the request to the upstream proxy, injecting Proxy-Authorization.
            first_line, _, rest = header.partition(b"\r\n")
            upstream_writer.write(first_line + b"\r\n" + self._auth_header_line + rest)
            await upstream_writer.drain()

            # Tunnel both directions (works for the CONNECT tunnel and absolute-URI HTTP).
            await asyncio.gather(
                self._pipe(client_reader, upstream_writer),
                self._pipe(upstream_reader, client_writer),
                return_exceptions=True,
            )
        except Exception as e:
            logger.debug("Residential proxy connection error: %s", e)
        finally:
            for w in (client_writer, upstream_writer):
                try:
                    if w is not None:
                        w.close()
                except Exception:
                    pass

    async def _pipe(self, reader, writer):
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass


def maybe_start_forwarder():
    """Start the forwarder if enabled. Returns the local port, or None on disable/failure."""
    if not residential_proxy_enabled():
        return None
    try:
        return ResidentialProxyForwarder().start()
    except Exception as e:
        logger.error("Failed to start residential proxy forwarder; falling back to direct egress: %s", e)
        return None
