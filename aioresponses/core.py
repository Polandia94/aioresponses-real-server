import socket
from functools import wraps
from re import Pattern
import typing
from unittest.mock import patch
import json as json_module
import inspect
import warnings
import gc
from urllib.parse import parse_qs


import aiohttp
from aiohttp import ClientRequest, ClientResponse, web, hdrs
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.connector import SSLContext, TCPConnector
from aiohttp.formdata import FormData
from aiohttp.resolver import ThreadedResolver, AsyncResolver
from aiohttp.test_utils import TestServer
from aiohttp.web_request import Request
from yarl import URL
from typing import Any, Awaitable, Callable, Type
from .compat import merge_params, normalize_url


class CallbackResult:
    """Result object return by a callback"""

    def __init__(
        self,
        method: str = hdrs.METH_GET,
        status: int = 200,
        body: str | bytes = "",
        content_type: str = "application/json",
        payload: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        response_class: Type[ClientResponse] | None = None,
        reason: str | None = None,
    ):
        self.method = method
        self.status = status
        self.body = body
        self.content_type = content_type
        self.payload = payload
        self.headers = headers
        self.response_class = response_class
        self.reason = reason


handler_type = Callable[[web.Request], Awaitable[web.Response]]


class aioresponses:
    """
    Mock aiohttp requests by redirecting DNS to a local aiohttp.web test server.
    """

    def __init__(
        self,
        passthrough: list[str] | None = None,
        passthrough_unmatched: bool = False,
        param: str | None = None,
        **kwargs: dict[str, Any],
    ) -> None:
        if kwargs:
            warnings.warn(
                "Passing extra parameters to aioresponses via kwargs is deprecated and will be removed in a future release.",
                DeprecationWarning,
                stacklevel=2,
            )

        self._passthrough_urls = passthrough or []
        self._passthrough_hosts: list[str] = []

        for p in self._passthrough_urls:
            try:
                host = URL(p).host
                self._passthrough_hosts.append(host if host else p)
            except Exception:
                self._passthrough_hosts.append(p)

        self.param = param
        self.passthrough_unmatched = passthrough_unmatched

        self._host_list: list[str] = []
        self._patterns_list: list[Pattern[str]] = []

        # handler are (path, method) → handler or list of handlers (if repeat != True)
        self.handlers: dict[tuple[str, str], handler_type | list[handler_type]] = {}
        # patterns_handler are (pattern, method) → handler or list of handlers (if repeat != True)
        self.patterns_handler: dict[
            tuple[Pattern[str], str], handler_type | list[handler_type]
        ] = {}

        # recorded requests: {(METHOD, URL): [web.Request, ...]}
        self.requests: dict[tuple[str, URL], list[Request]] = {}

        self.server: TestServer | None = None
        self._patchers: list[Any] = []

    async def __aenter__(self) -> "aioresponses":
        app = web.Application()
        # we add every route to the app.
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self.server = TestServer(app)
        await self.server.start_server()

        assert isinstance(self.server.host, str) and isinstance(self.server.port, int)  # pyright: ignore[reportUnknownMemberType]
        self.server_host = self.server.host
        self.server_port = self.server.port

        # Patch resolve() on BOTH resolver classes at the class level.
        # This affects every existing and future instance automatically.
        self._originals_resolver: dict[
            Type[AbstractResolver],
            Callable[[Any, str, int, Any], Awaitable[list[ResolveResult]]],
        ] = {}

        for resolver_cls in (ThreadedResolver, AsyncResolver):
            # Capture the original class method
            original_resolve = resolver_cls.resolve
            self._originals_resolver[resolver_cls] = original_resolve

            # Use a closure to capture the correct 'self' (aioresponses instance)
            # while receiving 'resolver_self' (the resolver instance).
            async def mock_resolve(
                resolver_self: AbstractResolver,
                host: str,
                port: int = 0,
                family: socket.AddressFamily = socket.AF_INET,
            ) -> list[ResolveResult]:
                return await self._fake_resolve(resolver_self, host, port, family)

            p = patch.object(resolver_cls, "resolve", mock_resolve)
            p.start()
            self._patchers.append(p)

        # Patch _get_ssl_context so that https:// requests to mocked hosts
        # connect to our plain-HTTP TestServer without TLS.
        original_get_ssl_context = TCPConnector._get_ssl_context  # pyright: ignore[reportPrivateUsage]
        self._original_ssl_context = original_get_ssl_context

        def mock_get_ssl_context(connector_self: TCPConnector, req: ClientRequest):
            return self._fake_ssl_context(connector_self, req)

        p_ssl = patch.object(TCPConnector, "_get_ssl_context", mock_get_ssl_context)
        p_ssl.start()
        self._patchers.append(p_ssl)

        # Clear the DNS cache on every open connector so cached entries
        # from before our patch was applied cannot bypass us.
        self._clear_all_connector_caches()

        return self

    async def __aexit__(
        self,
        exc_type: Type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        for p in self._patchers:
            p.stop()
        self._patchers.clear()
        if self.server:
            await self.server.close()
            self.server = None
        self._host_list.clear()
        self._patterns_list.clear()
        self.handlers.clear()

    # Decorator support
    def __call__(
        self, f: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        @wraps(f)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
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

    def _fake_ssl_context(
        self, connector_self: TCPConnector, req: ClientRequest
    ) -> SSLContext | None:
        """Return None (no TLS) for mocked hosts, real SSL context otherwise."""
        host = req.url.raw_host
        if host in self._host_list or self._match_pattern(str(req.url)):
            # Our TestServer is plain HTTP — disable TLS for mocked hosts.
            return None
        # For unmocked hosts, use the original method to get the correct context.
        original = self._original_ssl_context
        return original(connector_self, req)

    def _match_pattern(self, url: str) -> bool:
        for pattern in self._patterns_list:
            if pattern.match(url):
                return True
        return False

    async def _fake_resolve(
        self,
        resolver_self: AbstractResolver,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        """Replacement for resolver.resolve() on both resolver classes."""
        # if there is pattern, we always match, because we dont have full url
        if host in self._host_list or self._patterns_list:
            return [
                ResolveResult(
                    hostname=host,
                    host=self.server_host,
                    port=self.server_port,
                    family=family,
                    proto=0,
                    flags=0,
                )
            ]

        # Not mocked — check if it's a passthrough host or we allow unmatched.
        if host in self._passthrough_hosts or self.passthrough_unmatched:
            original = self._originals_resolver[type(resolver_self)]
            return await original(resolver_self, host, port, family)

        # if no passthrough and no match, we return a redirection to localhost
        # but will not be any handler registered for it, so it will raise a ClientConnectionError on the other side
        return [
            ResolveResult(
                hostname=host,
                host=self.server_host,
                port=self.server_port,
                family=family,
                proto=0,
                flags=0,
            )
        ]

    @staticmethod
    def _clear_all_connector_caches() -> None:
        """
        Walk every TCPConnector referenced by a live ClientSession and clear
        its DNS cache.  This ensures pre-patch resolutions are not reused.
        """
        for obj in gc.get_objects():
            if isinstance(obj, aiohttp.TCPConnector):
                try:
                    obj.clear_dns_cache()
                except Exception:
                    pass

    async def _dispatch(self, request: web.Request) -> web.Response:
        key = (request.method.upper(), normalize_url(request.url))
        self.requests.setdefault(key, [])
        request.kwargs = {"headers": request.headers, "query": dict(request.query)}
        # Read body eagerly before the handler runs, because aiohttp sets
        # PayloadAccessError on the stream once the response cycle completes.
        request._captured_body = await request.read() if request.can_read_body else b""
        self.requests[key].append(request)
        selected_handler = self.handlers.get((request.path, request.method))
        if isinstance(selected_handler, list):
            if not selected_handler:
                handler: handler_type | None = None
            else:
                handler = typing.cast(handler_type, selected_handler.pop(0))

        else:
            handler = selected_handler
        if handler is None:
            # Reconstruct the original URL: aiohttp always sends the original Host header,
            # but request.url reflects the local TestServer address. Try both schemes.
            original_host = request.headers.get("Host", request.url.host)
            original_urls = [
                f"https://{original_host}{request.path_qs}",
                f"http://{original_host}{request.path_qs}",
            ]
            # Check if there's a pattern handler for this request
            for (pattern, method), pattern_handler in self.patterns_handler.items():
                if (
                    any(pattern.match(u) for u in original_urls)
                    and method == request.method
                ):
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
            return web.Response(
                status=502, text="No handler registered for this request."
            )
        return await handler(request)

    def add(
        self,
        url: URL | str | Pattern[str],
        method: str = hdrs.METH_GET,
        status: int = 200,
        body: str | bytes = b"",
        json: Any = None,
        payload: dict | None = None,
        headers: dict | None = None,
        repeat: bool | int = False,
        content_type: str | None = None,
        callback: Callable[[URL | Pattern[str]], CallbackResult] | None = None,
        reason: str | None = None,
        exception: Exception | None = None,
        **kwargs,
    ) -> None:
        if exception is not None:
            # if there is an excpetion, dont add handler, will return a clientDisconnectionError
            # add some deprecation or similar
            return
        method = method.upper()
        if isinstance(url, str):
            url = URL(url)

        if isinstance(url, Pattern):
            self._patterns_list.append(url)

        assert self.server is not None, (
            "Server not started — use `async with aioresponses() as m:` first."
        )
        if isinstance(url, URL):
            host = url.host
            assert host, f"Cannot extract host from {url!r}"

            # Map this host → our test server
            self._host_list.append(host)

        if json is not None:
            body = json_module.dumps(json).encode()
            content_type = "application/json"
        elif payload is not None:
            body = json_module.dumps(payload).encode()
        elif isinstance(body, str):
            body = body.encode()
            if content_type is None:
                content_type = "application/json"

        resp_headers = dict(headers or {})
        if payload is not None and "Content-Type" not in resp_headers:
            content_type = "application/json"

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
                    _body = json_module.dumps(result.payload).encode()
                _content_type = result.content_type
                _reason = result.reason
            else:
                _status = status
                _body = body
                _headers = headers
                _content_type = content_type
                _reason = reason

            return web.Response(
                status=_status,
                body=_body,
                headers=_headers,
                reason=_reason,
                content_type=_content_type,
            )

        if repeat is True:
            if isinstance(url, Pattern):
                self.patterns_handler[url, method] = handler
                return
            path = url.path or "/"
            self.handlers[path, method] = handler
        else:
            if repeat is False:
                repeat = 1
            handlers: list[handler_type] = [handler] * repeat
            if isinstance(url, Pattern):
                if (url, method) in self.patterns_handler:
                    list_pattern_handler = self.patterns_handler[(url, method)]
                    if isinstance(list_pattern_handler, list):
                        list_pattern_handler = typing.cast(
                            list[handler_type], list_pattern_handler
                        )
                        list_pattern_handler += handlers
                    else:
                        raise ValueError(
                            f"Existing handler for pattern {url} {method} has repeat=True, cannot add more handlers to it."
                        )

                else:
                    self.patterns_handler[url, method] = handlers
                return
            path = url.path or "/"
            if (path, method) in self.handlers:
                handlers_list = self.handlers[(path, method)]
                if isinstance(handlers_list, list):
                    handlers_list = typing.cast(list[handler_type], handlers_list)
                    handlers_list += handlers
                else:
                    raise ValueError(
                        f"Existing handler for {path} {method} has repeat=True, cannot add more handlers to it."
                    )
            else:
                self.handlers[path, method] = handlers

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
        url: URL | str,
        method: str = hdrs.METH_GET,
        params: dict[str, str] | None = None,
    ):
        url = normalize_url(merge_params(url, params))
        key = (method.upper(), url)
        if key not in self.requests:
            raise AssertionError(f"No calls to {method.upper()} {url}")

    def assert_called_with(
        self,
        url: URL | str,
        method: str = hdrs.METH_GET,
        params: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ):
        url = normalize_url(merge_params(url, params))
        key = (method.upper(), url)
        if key not in self.requests:
            raise AssertionError(f"No calls to {method.upper()} {url}")
        request = self.requests[key][0]  # check the first call
        actual_body = getattr(request, "_captured_body", b"")
        if json is not None:
            # aiohttp sends json= as JSON-encoded bytes with application/json
            expected_body = json_module.dumps(json).encode()
            assert actual_body == expected_body, (
                f"Expected JSON body {json!r}, got {actual_body!r}"
            )
        elif data is not None:
            if isinstance(data, dict):
                # aiohttp sends data=dict via FormData as application/x-www-form-urlencoded.
                # Use FormData to produce the exact same encoding aiohttp does.
                form_encoded = FormData(data)()._value  # type: ignore[attr-defined]
                # Accept order-insensitive comparison via parse_qs
                actual_qs = parse_qs(actual_body.decode(errors="replace"))
                expected_qs = parse_qs(form_encoded.decode())
                match = actual_body == form_encoded or actual_qs == expected_qs
                assert match, (
                    f"Expected body {data!r} (form encoded), got {actual_body!r}"
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
        # Strip headers that aiohttp adds automatically, unless the caller
        # explicitly wants to assert them.
        for header in (
            "Content-Length",
            "Content-Type",
            "Transfer-Encoding",
            "Host",
            "Accept",
            "Accept-Encoding",
            "User-Agent",
        ):
            if header not in (headers or {}):
                # this should be deprecated in the future, but for now we want to avoid breaking existing tests that don't specify these headers
                actual_headers.pop(header, None)
        expected_headers = headers or {}
        assert expected_headers == actual_headers, (
            f"Expected headers {expected_headers!r}, got {actual_headers!r}"
        )

    def assert_called_once_with(
        self,
        url: URL | str,
        method: str = hdrs.METH_GET,
        params: dict[str, str] | None = None,
        data: str | bytes | dict[str, Any] | None = None,
        json: Any = None,
        headers: dict[str, str] | None = None,
    ):
        self.assert_called_once()
        self.assert_called_with(url, method, params, data, json, headers)
