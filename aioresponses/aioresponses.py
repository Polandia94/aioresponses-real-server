import socket
from aiohttp import web, hdrs
from aiohttp.resolver import DefaultResolver
from aiohttp.test_utils import TestServer
from yarl import URL
from unittest.mock import patch
import aiohttp
from multidict import MultiDict
from functools import wraps
import asyncio
import sys
from re import Pattern
import warnings



_mapping = {}
_passthrough = []
class FakeResolver(DefaultResolver):
    def __init__(self, passthrough=None, *args, **kwargs):
        _mapping = {}
        _passthrough = []
        if passthrough:
            for p in passthrough:
                try:
                    host = URL(p).host
                    if host:
                        _passthrough.append(host)
                    else:
                        _passthrough.append(p)
                except:
                    _passthrough.append(p)
        super().__init__(*args, **kwargs)

    def add_mapping(self, host, ip, port, repeat: int | bool =False):
        _mapping[host] = (ip, port, repeat)

    async def resolve(self, host, port=0, family=socket.AF_INET):
        if host in _passthrough:
            return await super().resolve(host, port, family)

        # Redirect host and port if it matches our mapping
        target = _mapping.get(host)


        if target:
            target_host, target_port, repeat = target
            if isinstance(repeat, bool):
                if repeat is False:
                    del _mapping[host]
            else:
                repeat -= 1
                if repeat == 0:
                    del _mapping[host]
            return [
                {
                    "hostname": host,
                    "host": target_host,
                    "port": target_port,
                    "family": family,
                    "proto": 0,
                    "flags": 0,
                }
            ]

        return await super().resolve(host, port, family)




def normalize_url(url: URL | str) -> URL:
    """Normalize url to make comparisons."""
    url = URL(url)
    if url.fragment:
        url = url.with_fragment(None) # NOTE: this is a breaking change on fragment managment
    return url.with_query(sorted(url.query.items()))


def merge_params(url: URL | str, params: dict | None = None) -> URL:
    url = URL(url)
    if params:
        query_params = MultiDict(url.query)
        query_params.extend(url.with_query(params).query)
        return url.with_query(query_params)
    return url


class aioresponses:
    def __init__(self, passthrough=None, **kwargs):
        self.passthrough = passthrough or []
        self._kwargs = kwargs
        self.param = kwargs.pop('param', None)
        self._resolver = None
        self.handlers = {}
        self.requests = {}
        self._patcher = None
        original_init = aiohttp.TCPConnector.__init__

        def patched_init(self_conn, *args, **patched_kwargs):
            if "resolver" not in patched_kwargs:
                patched_kwargs["resolver"] = self.resolver
            return original_init(self_conn, *args, **patched_kwargs)

        self._patcher = patch("aiohttp.connector.TCPConnector.__init__", patched_init)
        self._patcher.start()

    @property
    def resolver(self):
        if self._resolver == None:
            self._resolver = FakeResolver(passthrough=self.passthrough)
        return self._resolver

    def __enter__(self):
        return self.loop.run_until_complete(self.__aenter__())

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.loop.run_until_complete(self.__aexit__(exc_type, exc_val, exc_tb))

    @property
    def loop(self):
        return asyncio.get_event_loop()

    async def _dispatch(self, request):
        handler = self.handlers.get(request.path)
        if handler:
            return await handler(request)
        return web.Response(status=404, text="Not Found")

    async def __aenter__(self, **kwargs):
        config = {**self._kwargs, **kwargs}
        app = web.Application()
        # Add a catch-all route that can handle dynamic paths
        app.router.add_route("*", "/{tail:.*}", self._dispatch)

        self.server = TestServer(app, **config)
        await self.server.start_server()
        self.app = app

        # Patch TCPConnector to use our resolver by default


        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._patcher:
            self._patcher.stop()
        await self.server.close()

    def __call__(self, f):
        from functools import wraps
        import asyncio

        @wraps(f)
        async def wrapper(*args, **kwargs):
            async with self as m:
                if self.param:
                    kwargs[self.param] = m
                else:
                    # check if we are in a method
                    if args and hasattr(args[0], f.__name__):
                        # likely a method, pass m as second arg after self
                        new_args = (args[0], m) + args[1:]
                        return await f(*new_args, **kwargs)
                    else:
                        args = args + (m,)
                if asyncio.iscoroutinefunction(f):
                    return await f(*args, **kwargs)
                else:
                    return f(*args, **kwargs)
        return wrapper

    def get(self, url: URL | str, status=200, body="OK", repeat: int | bool = False, response_class=None):
        if response_class:
            # we do a deprecation warning
            warnings.warn("response_class is not needed", DeprecationWarning)
        async def handler(request):
            key = (request.method.upper(), request.url)
            self.requests.setdefault(key, [])
            self.requests[key].append(request)
            return web.Response(status=status, body=body)

        if isinstance(url, str):
            url = URL(url)

        # we map the host of the url to the site host AND port
        self.resolver.add_mapping(url.host, self.server.host, self.server.port, repeat)

        # we add the handler to our dynamic map
        self.handlers[url.path] = handler

    def assert_any_call(self, url: URL | str | Pattern, method: str = hdrs.METH_GET, params: dict | None = None):
        url = normalize_url(merge_params(url, params))
        method = method.upper()
        key = (method, url)
        print(self.requests)
        print(key)
        try:
            expected = self.requests[key]
        except KeyError:
            raise AssertionError(f"No calls to {method} {url}")

    def assert_called_once(self):
        """assert that the mock was called only once."""
        # call count is the len of the sum of every list on self.requests.values()
        print(self.requests)
        call_count = sum(len(v) for v in self.requests.values())
        if not call_count == 1:
            msg = "Expected '{}' to have been called once. Called {} times.".format(self.__class__.__name__, call_count)

            raise AssertionError(msg)

    def assert_called_once_with(self, url: URL | str, method: str = "GET", params: dict | None = None):
        """assert that the mock was called once with the specified arguments.
        Raises an AssertionError if the args and keyword args passed in are
        different to the only call to the mock."""
        self.assert_called_once()
        self.assert_called_with(url, method, params)

    def assert_called(self):
        """assert that the mock was called at least once."""
        if len(self.requests) == 0:
            msg = "Expected '{}' to have been called.".format(self.__class__.__name__)
            raise AssertionError(msg)
    
    def assert_not_called(self):
        """assert that the mock was not called."""
        if len(self.requests) > 0:
            msg = "Expected '{}' to have not been called. Called {} times.".format(self.__class__.__name__, len(self.requests))
            raise AssertionError(msg)

    def assert_called_with(
        self, url: URL | str, method: str = "GET", params: dict | None = None
    ):
        """assert that the last call was made with the specified arguments.

        Raises an AssertionError if the args and keyword args passed in are
        different to the last call to the mock."""
        url = normalize_url(merge_params(url, params))
        method = method.upper()
        key = (method, url)
        try:
            expected = self.requests[key][-1]
        except KeyError:
            raise AssertionError(f"No calls to {method} {url}")

        # we need to create a request object to match the expected one
        assert isinstance(expected, web.Request)
        assert expected.method == method
        assert expected.url == url
