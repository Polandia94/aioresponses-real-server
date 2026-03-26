import socket
import asyncio
import warnings
from functools import wraps
from re import Pattern
from unittest.mock import patch

import aiohttp
from aiohttp import web, hdrs
from aiohttp.connector import TCPConnector
from aiohttp.resolver import ThreadedResolver, AsyncResolver
from aiohttp.test_utils import TestServer
from multidict import MultiDict
from yarl import URL


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def normalize_url(url: "URL | str") -> URL:
    """Normalize url to make comparisons."""
    url = URL(url)
    if url.fragment:
        url = url.with_fragment(None)
    return url.with_query(sorted(url.query.items()))


def merge_params(url: "URL | str", params: "dict | None" = None) -> URL:
    url = URL(url)
    if params:
        query_params = MultiDict(url.query)
        query_params.extend(url.with_query(params).query)
        return url.with_query(query_params)
    return url


# ---------------------------------------------------------------------------
# aioresponses
# ---------------------------------------------------------------------------

class aioresponses:
    """
    Mock aiohttp requests by redirecting DNS to a local aiohttp.web test server.

    Works on sessions that were created *before* the mock context is entered,
    because:
      1. Both ThreadedResolver.resolve and AsyncResolver.resolve are patched at
         the **class** level (Python's MRO lookup finds the patch on every
         existing instance).
      2. The connector's DNS cache is cleared on entry so stale entries cannot
         bypass the patch.
    """

    def __init__(self, passthrough=None, **kwargs):
        self._passthrough_urls = passthrough or []
        self._passthrough_hosts: list[str] = []
        for p in self._passthrough_urls:
            try:
                host = URL(p).host
                self._passthrough_hosts.append(host if host else p)
            except Exception:
                self._passthrough_hosts.append(p)

        self._kwargs = kwargs
        self.param = kwargs.pop("param", None)
        self.passthrough_unmatched = kwargs.pop("passthrough_unmatched", False)

        # {host: (target_ip, target_port, repeat)}
        self._host_map: dict[str, tuple[str, int, bool | int]] = {}

        # {path: async handler}
        self.handlers: dict[str, object] = {}

        # recorded requests: {(METHOD, URL): [web.Request, ...]}
        self.requests: dict[tuple[str, URL], list] = {}

        self.server: TestServer | None = None
        self._patchers: list = []

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "aioresponses":
        # Start the real local HTTP server
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self.server = TestServer(app)
        await self.server.start_server()

        # Patch resolve() on BOTH resolver classes at the class level.
        # This affects every existing and future instance automatically.
        self._originals = {}

        for resolver_cls in (ThreadedResolver, AsyncResolver):
            # Capture the original class method
            original_resolve = resolver_cls.resolve
            self._originals[resolver_cls] = original_resolve

            # Use a closure to capture the correct 'self' (aioresponses instance)
            # while receiving 'resolver_self' (the resolver instance).
            async def mock_resolve(resolver_self, host, port=0, family=socket.AF_INET):
                return await self._fake_resolve(resolver_self, host, port, family)

            p = patch.object(resolver_cls, "resolve", mock_resolve)
            p.start()
            self._patchers.append(p)

        # Patch _get_ssl_context so that https:// requests to mocked hosts
        # connect to our plain-HTTP TestServer without TLS.
        original_get_ssl_context = TCPConnector._get_ssl_context
        self._originals[TCPConnector] = original_get_ssl_context

        def mock_get_ssl_context(connector_self, req):
            return self._fake_ssl_context(connector_self, req)

        p_ssl = patch.object(TCPConnector, "_get_ssl_context", mock_get_ssl_context)
        p_ssl.start()
        self._patchers.append(p_ssl)

        # Clear the DNS cache on every open connector so cached entries
        # from before our patch was applied cannot bypass us.
        self._clear_all_connector_caches()

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        for p in self._patchers:
            p.stop()
        self._patchers.clear()
        if self.server:
            await self.server.close()
            self.server = None
        self._host_map.clear()
        self.handlers.clear()

    # Decorator support
    def __call__(self, f):
        @wraps(f)
        async def wrapper(*args, **kwargs):
            async with self as m:
                if self.param:
                    kwargs[self.param] = m
                else:
                    if args and hasattr(args[0], f.__name__):
                        args = (args[0], m) + args[1:]
                    else:
                        args = args + (m,)
                return await f(*args, **kwargs)
        return wrapper

    # ------------------------------------------------------------------
    # DNS patch
    # ------------------------------------------------------------------

    def _fake_ssl_context(self, connector_self, req):
        """Return None (no TLS) for mocked hosts, real SSL context otherwise."""
        host = req.url.raw_host
        if host in self._host_map:
            # Our TestServer is plain HTTP — disable TLS for mocked hosts.
            return None
        # For unmocked hosts, use the original method to get the correct context.
        original = self._originals[TCPConnector]
        return original(connector_self, req)

    async def _fake_resolve(
        self,
        resolver_self: "ThreadedResolver | AsyncResolver",
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict]:
        """Replacement for resolver.resolve() on both resolver classes."""
        target = self._host_map.get(host)
        if target is not None:
            target_ip, target_port, repeat = target
            # consume the registration
            if isinstance(repeat, bool):
                if not repeat:
                    del self._host_map[host]
            else:
                repeat -= 1
                if repeat == 0:
                    del self._host_map[host]
                else:
                    self._host_map[host] = (target_ip, target_port, repeat)

            return [
                {
                    "hostname": host,
                    "host": target_ip,
                    "port": target_port,
                    "family": family,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        # Not mocked — check if it's a passthrough host or we allow unmatched.
        if host in self._passthrough_hosts or self.passthrough_unmatched:
            original = self._originals[type(resolver_self)]
            return await original(resolver_self, host, port, family)

        raise OSError(f"Connection refused: no mock registered for host {host!r}")
        # consume the registration
        if isinstance(repeat, bool):
            if not repeat:
                del self._host_map[host]
        else:
            repeat -= 1
            if repeat == 0:
                del self._host_map[host]
            else:
                self._host_map[host] = (target_ip, target_port, repeat)

        return [
            {
                "hostname": host,
                "host": target_ip,
                "port": target_port,
                "family": family,
                "proto": 0,
                "flags": 0,
            }
        ]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clear_all_connector_caches() -> None:
        """
        Walk every TCPConnector referenced by a live ClientSession and clear
        its DNS cache.  This ensures pre-patch resolutions are not reused.
        """
        import gc
        for obj in gc.get_objects():
            if isinstance(obj, aiohttp.TCPConnector):
                try:
                    obj.clear_dns_cache()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Request dispatch (local web server)
    # ------------------------------------------------------------------

    async def _dispatch(self, request: web.Request) -> web.Response:
        handler = self.handlers.get(request.path)
        if handler is None:
            return web.Response(status=404, text=f"No mock for path {request.path!r}")
        return await handler(request)

    # ------------------------------------------------------------------
    # Mock registration
    # ------------------------------------------------------------------

    def _register(
        self,
        url: "URL | str",
        method: str,
        status: int = 200,
        body: "str | bytes" = b"",
        payload: "dict | None" = None,
        headers: "dict | None" = None,
        repeat: "bool | int" = False,
        **kwargs,
    ) -> None:
        if isinstance(url, str):
            url = URL(url)

        assert self.server is not None, (
            "Server not started — use `async with aioresponses() as m:` first."
        )

        host = url.host
        assert host, f"Cannot extract host from {url!r}"

        # Map this host → our test server
        self._host_map[host] = (self.server.host, self.server.port, repeat)

        import json as _json

        if payload is not None:
            body = _json.dumps(payload).encode()
        elif isinstance(body, str):
            body = body.encode()

        resp_headers = dict(headers or {})
        if payload is not None and "Content-Type" not in resp_headers:
            resp_headers["Content-Type"] = "application/json"

        _body = body
        _status = status
        _headers = resp_headers
        _method = method.upper()

        async def handler(request: web.Request) -> web.Response:
            # Only match on method
            if request.method.upper() != _method:
                return web.Response(status=405, text="Method Not Allowed")
            key = (request.method.upper(), normalize_url(request.url))
            self.requests.setdefault(key, [])
            self.requests[key].append(request)
            return web.Response(status=_status, body=_body, headers=_headers)

        path = url.path or "/"
        self.handlers[path] = handler

    def get(self, url, **kwargs):
        self._register(url, hdrs.METH_GET, **kwargs)

    def post(self, url, **kwargs):
        self._register(url, hdrs.METH_POST, **kwargs)

    def put(self, url, **kwargs):
        self._register(url, hdrs.METH_PUT, **kwargs)

    def patch(self, url, **kwargs):
        self._register(url, hdrs.METH_PATCH, **kwargs)

    def delete(self, url, **kwargs):
        self._register(url, hdrs.METH_DELETE, **kwargs)

    def head(self, url, **kwargs):
        self._register(url, hdrs.METH_HEAD, **kwargs)

    def options(self, url, **kwargs):
        self._register(url, hdrs.METH_OPTIONS, **kwargs)

    # add() alias used by some tests
    def add(self, url, method=hdrs.METH_GET, **kwargs):
        self._register(url, method, **kwargs)

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def assert_called(self):
        if not self.requests:
            raise AssertionError("Expected at least one call, got none.")

    def assert_not_called(self):
        if self.requests:
            raise AssertionError(
                f"Expected no calls, got {sum(len(v) for v in self.requests.values())}."
            )

    def assert_called_once(self):
        count = sum(len(v) for v in self.requests.values())
        if count != 1:
            raise AssertionError(f"Expected exactly 1 call, got {count}.")

    def assert_any_call(
        self,
        url: "URL | str",
        method: str = hdrs.METH_GET,
        params: "dict | None" = None,
    ):
        url = normalize_url(merge_params(url, params))
        key = (method.upper(), url)
        if key not in self.requests:
            raise AssertionError(f"No calls to {method.upper()} {url}")

    def assert_called_with(
        self,
        url: "URL | str",
        method: str = hdrs.METH_GET,
        params: "dict | None" = None,
    ):
        url = normalize_url(merge_params(url, params))
        key = (method.upper(), url)
        if key not in self.requests:
            raise AssertionError(f"No calls to {method.upper()} {url}")

    def assert_called_once_with(
        self,
        url: "URL | str",
        method: str = hdrs.METH_GET,
        params: "dict | None" = None,
    ):
        self.assert_called_once()
        self.assert_called_with(url, method, params)
