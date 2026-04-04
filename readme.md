Proof of concept for an aioresponses-like library that uses real aiohttp server
related to this discussion: https://github.com/orgs/aio-libs/discussions/45

This is a proof of concept of how a linrary could expose aioresponses-like API
while using real aiohttp server under the hood. It works only with GET requests,
only compare the url, param and method, and only implements assert_called_with.

The breaking changes will be:
- The context manager will be async, as it will need to start the server
- the assert_called_* methods will not work with arbitrary kwargs, instead they will only work with specific args that the request was made with (e.g. url, method, headers, params, etc.)
- the fragments (after #) are not being passed on the request, so will not be compared
- on a connector exception, aiohttp could retry, so the number of request will not be the same
- pass timeout don't work
- you cant raise exceptions exception clienterror
- the decorathor need to decorate an async function
- as this mock DNS will not work to mock request to external IPs


Added a perf test to compare the performance of the POC with the original aioresponses implementation.

Results:

aioresponses (main)       :  0.7223 seconds for 1000 iter (0.72 ms/iter)
aioresponses_2 (POC)      :  0.8272 seconds for 1000 iter (0.83 ms/iter)

Probablly this overheard is tolerable, and could be optimized.
However, once we start adding more features, the performance will degrade.