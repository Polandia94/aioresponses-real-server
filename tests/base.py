import asyncio
from unittest import IsolatedAsyncioTestCase


def fail_on(**kw):  # noqa
    def outer(fn):
        def inner(*args, **kwargs):
            return fn(*args, **kwargs)

        return inner

    return outer


class AsyncTestCase(IsolatedAsyncioTestCase):
    """Asynchronous test case class that covers up differences in usage
    between unittest (starting from Python 3.8) and asynctest.

    `setup` and `teardown` is used to be called before each test case
    (note: that they are in lowercase)
    """

    async def setup(self):
        pass

    async def teardown(self):
        pass

    async def asyncSetUp(self):
        self.loop = asyncio.get_event_loop()
        await self.setup()

    async def asyncTearDown(self):
        await self.teardown()
