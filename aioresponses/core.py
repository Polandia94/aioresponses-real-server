import re
import socket
import asyncio
import warnings
from functools import wraps
from re import Pattern
from unittest.mock import patch
import json as _json
import inspect


import aiohttp
from aiohttp import ClientResponse, web, hdrs
from aiohttp.connector import TCPConnector
from aiohttp.resolver import ThreadedResolver, AsyncResolver
from aiohttp.test_utils import TestServer
from aiohttp.client_exceptions import ClientConnectionError
from aiohttp.web_request import Request
from multidict import MultiDict
from yarl import URL
from typing import Callable, Dict, Optional, Type, Union


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


class CallbackResult:

    def __init__(self, method: str = hdrs.METH_GET,
                 status: int = 200,
                 body: Union[str, bytes] = '',
                 content_type: str = 'application/json',
                 payload: dict | None = None,
                 headers: dict | None = None,
                 response_class: Optional[Type[ClientResponse]] = None,
                 reason: Optional[str] = None):
        self.method = method
        self.status = status
        self.body = body
        self.content_type = content_type
        self.payload = payload
        self.headers = headers
        self.response_class = response_class
        self.reason = reason

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
        self._patterns_map: dict[Pattern[str], tuple[str, int, bool | int]] = {}

        # {path: async handler}
        self.handlers: dict[tuple[str, str], object] = {}
        self.patterns_handler: dict[tuple[Pattern[str], str], object] = {}

        # recorded requests: {(METHOD, URL): [web.Request, ...]}
        self.requests: dict[tuple[str, URL], list[Request]] = {}

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
        self._patterns_map.clear()
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

    def _fake_ssl_context(self, connector_self, req: Request):
        """Return None (no TLS) for mocked hosts, real SSL context otherwise."""
        host = req.url.raw_host
        if host in self._host_map or self._match_pattern(str(req.url)) is not None:
            # Our TestServer is plain HTTP — disable TLS for mocked hosts.
            return None
        # For unmocked hosts, use the original method to get the correct context.
        original = self._originals[TCPConnector]
        return original(connector_self, req)

    def _match_pattern(self, host: str) -> tuple[str, int, bool | int] | None:
        for pattern, target in self._patterns_map.items():
            if pattern.match(host):
                return target
        return None

    async def _fake_resolve(
        self,
        resolver_self: "ThreadedResolver | AsyncResolver",
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict]:
        """Replacement for resolver.resolve() on both resolver classes."""
        print(f"Resolving {host}:{port} (family {family})")
        target = self._host_map.get(host)
        print("Host map lookup for", host, "found", target)
        if target is not None:
            target_ip, target_port, repeat = target
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

        target = self._match_pattern(host)
        if target is not None:
            target_ip, target_port, repeat = target

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

        return [
                {
                    "hostname": host,
                    "host": self.server.host,
                    "port": self.server.port,
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
        key = (request.method.upper(), normalize_url(request.url))
        self.requests.setdefault(key, [])
        request.kwargs = {"headers": request.headers, "query": dict(request.query)}
        # Read body eagerly before the handler runs, because aiohttp sets
        # PayloadAccessError on the stream once the response cycle completes.
        request._captured_body = await request.read() if request.can_read_body else b""
        self.requests[key].append(request)
        if isinstance(self.handlers.get((request.path, request.method)), list):
            if len(self.handlers[(request.path, request.method)]) == 0:
                print(f"Consuming handler for {request.method} {request.path!r}, no handlers left")
                handler = None
            else:
                print("len handlers for", request.path, request.method, len(self.handlers[(request.path, request.method)]))
                handler = self.handlers[(request.path, request.method)][0]
                print(f"Consuming handler for {request.method} {request.path!r}, {len(self.handlers[(request.path, request.method)])} left")
                # we remove the first element of the list, so the next request will match the next handler in the list
                self.handlers[(request.path, request.method)] = self.handlers[(request.path, request.method)][1:]
                print("len handlers for", request.path, request.method, len(self.handlers[(request.path, request.method)]))

        else:
            print("Looking up handler for", request.path, request.method)
            print("Registered handlers:", list(self.handlers.keys()))
            handler = self.handlers.get((request.path, request.method))
        if handler is None:
            # Check if there's a pattern handler for this request
            for (pattern, method), pattern_handler in self.patterns_handler.items():
                if pattern.match(str(request.url)) and method == request.method:
                    if isinstance(pattern_handler, list):
                        handler = pattern_handler[0]
                        remaining = pattern_handler[1:]
                        if remaining:
                            self.patterns_handler[pattern, request.method] = remaining
                        else:
                            del self.patterns_handler[pattern, request.method]
                    else:
                        handler = pattern_handler
                    break

        if handler is None:

            # this should raise ClientConnectionError on the other side
            if request.transport:
                request.transport.close()
            return web.Response(status=502, text="No handler registered for this request.")
        return await handler(request)

    # ------------------------------------------------------------------
    # Mock registration
    # ------------------------------------------------------------------

    def add(
        self,
        url: "URL | str | Pattern[str]",
        method: str = hdrs.METH_GET,
        status: int = 200,
        body: "str | bytes" = b"",
        payload: "dict | None" = None,
        headers: "dict | None" = None,
        repeat: "bool | int" = False,
        content_type: "str | None" = None,
        callback: "Callable[[web.Request], CallbackResult] | None" = None,
        reason: Optional[str] = None,
        **kwargs,
    ) -> None:
        if isinstance(url, str):
            url = URL(url)

        if isinstance(url, Pattern):
            self._patterns_map[url] = (self.server.host, self.server.port, repeat)

        assert self.server is not None, (
            "Server not started — use `async with aioresponses() as m:` first."
        )
        if isinstance(url, URL):
            host = url.host
            assert host, f"Cannot extract host from {url!r}"

            # Map this host → our test server
            self._host_map[host] = (self.server.host, self.server.port, repeat)


        if payload is not None:
            body = _json.dumps(payload).encode()
        elif isinstance(body, str):
            body = body.encode()

        resp_headers = dict(headers or {})
        if payload is not None and "Content-Type" not in resp_headers:
            content_type = "application/json"

        
            

        self._body = body
        self._status = status
        self._headers = resp_headers
        self._method = method.upper()
        self._reason = reason

        async def handler(request: web.Request) -> web.Response:
            if callable(callback):
                if inspect.iscoroutinefunction(callback):
                    result = await callback(url, **kwargs)
                else:
                    result = callback(url, **kwargs)
                _status = result.status
                _body = result.body
                _headers = result.headers or {}
                if result.payload is not None:
                    _body = _json.dumps(result.payload).encode()
                _content_type = result.content_type
                _reason = result.reason
            else:
                _status = status
                _body = body
                _headers = headers
                _content_type = content_type
                _reason = reason

            return web.Response(status=_status, body=_body, headers=_headers, reason=_reason, content_type=_content_type)
        
        if repeat is True:
            if isinstance(url, Pattern):
                self.patterns_handler[url] = handler
                return
            path = url.path or "/"
            self.handlers[path, self._method] = handler
        else:
            if repeat is False:
                repeat = 1
            handlers = [handler] * repeat
            if isinstance(url, Pattern):
                if self.patterns_handler.get((url, self._method)):
                    self.patterns_handler[url, self._method] += handlers
                else:
                    self.patterns_handler[url, self._method] = handlers
                return
            path = url.path or "/"
            if self.handlers.get((path, self._method)):
                self.handlers[path, self._method] += handlers
            else:
                self.handlers[path, self._method] = handlers
            

    def get(self, url, **kwargs):
        self.add(url, method=hdrs.METH_GET, **kwargs)

    def post(self, url, **kwargs):
        self.add(url, method=hdrs.METH_POST, **kwargs)

    def put(self, url, **kwargs):
        self.add(url, method=hdrs.METH_PUT, **kwargs)

    def patch(self, url, **kwargs):
        self.add(url, method=hdrs.METH_PATCH, **kwargs)

    def delete(self, url, **kwargs):
        self.add(url, method=hdrs.METH_DELETE, **kwargs)

    def head(self, url, **kwargs):
        self.add(url, method=hdrs.METH_HEAD, **kwargs)

    def options(self, url, **kwargs):
        self.add(url, method=hdrs.METH_OPTIONS, **kwargs)

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
        data: "str | bytes | dict | None" = None,
        headers: "dict | None" = None,
    ):
        url = normalize_url(merge_params(url, params))
        key = (method.upper(), url)
        if key not in self.requests:
            raise AssertionError(f"No calls to {method.upper()} {url}")
        request = self.requests[key][0]  # check the first call
        if data is not None:
            actual_body = getattr(request, "_captured_body", b"")
            if isinstance(data, dict):
                # aiohttp may send dicts as form-encoded or JSON; try both.
                from urllib.parse import urlencode, parse_qs
                form_encoded = urlencode(data).encode()
                json_encoded = _json.dumps(data).encode()
                # Also accept order-insensitive form comparison
                actual_qs = parse_qs(actual_body.decode(errors="replace"))
                expected_qs = parse_qs(urlencode(data))
                match = (
                    actual_body == form_encoded
                    or actual_body == json_encoded
                    or actual_qs == expected_qs
                )
                assert match, (
                    f"Expected body {data!r} (form or JSON encoded), got {actual_body!r}"
                )
            else:
                if isinstance(data, str):
                    expected_body = data.encode()
                else:
                    expected_body = data
                assert actual_body == expected_body, (
                    f"Expected body {expected_body!r}, got {actual_body!r}"
                )
        actual_headers = dict(request.headers)
        # we remove the headers added by aiohttp if there are not specified in the expected headers
        for header in ("Content-Length", "Content-Type", "Host", "Accept","Accept-Encoding", "User-Agent"):
            if header not in (headers or {}):
                # this should be deprecated in the future, but for now we want to avoid breaking existing tests that don't specify these headers
                actual_headers.pop(header, None)
        expected_headers = headers or {}
        assert expected_headers == actual_headers, (
            f"Expected headers {expected_headers!r}, got {actual_headers!r}"
        )

    def assert_called_once_with(
        self,
        url: "URL | str",
        method: str = hdrs.METH_GET,
        params: "dict | None" = None,
        data: "str | bytes | dict | None" = None,
        headers: "dict | None" = None,
    ):
        self.assert_called_once()
        self.assert_called_with(url, method, params, data, headers)
