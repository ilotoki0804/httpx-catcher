import logging
import random
import ssl
import string
from collections import deque
from collections.abc import MutableMapping
from contextlib import contextmanager, suppress
from os import PathLike
from shelve import Shelf
from typing import Literal

import httpx

from ._dbm_sqlite import open as dbm_open

type PathType = PathLike | str | bytes
type ContentKey = tuple[str, str, bytes]
type ModeType = Literal["store", "use", "hybrid", "passive"]

DEFAULT_LOGGER = logging.getLogger(__name__)
_installed = False
_httpc_installed = False


class RejectDict(dict):
    def __setitem__(self, item):
        pass

    def __getitem__(self, item):
        raise KeyError(item)


class AsyncCatcherTransport(httpx.AsyncHTTPTransport):
    logger = DEFAULT_LOGGER

    def __init__(self, *args, shelf: MutableMapping, mode: ModeType, flush_limit: int | None = None, **kwargs) -> None:
        super().__init__(*args[1:], verify=ssl.create_default_context(), **kwargs)
        self.shelf = shelf
        self.mode = mode
        self.flush_immediately: bool = False
        self.flush_limit: int | None = flush_limit
        self._keys: dict[ContentKey, str] = self.shelf.setdefault("__keys__", {})
        self._waiting_flush: deque[tuple[httpx.Request, httpx.Response]] = deque([])

    def close(self):
        self.flush()
        self.shelf["__keys__"] = self._keys

    def flush(self):
        i = 0
        while self._waiting_flush and (pair := self._waiting_flush.popleft()):
            i += 1
            request, response = pair
            content_key = self.generate_content_key(request)

            if content_key in self._keys:
                method, url, content = content_key
                self.logger.warning(f"{url} already present in requests. override it.")

            shelf_key = self.generate_random_name()
            self._keys[content_key] = shelf_key
            self.shelf[shelf_key] = request, response
        if i:
            self.logger.info(f"{i} item{'s' * (i != 1)} flushed!")

    @staticmethod
    def generate_content_key(request: httpx.Request) -> ContentKey:
        method = str(request.method)
        url = str(request.url)
        request.read()
        content = request.content
        return method, url, content

    @staticmethod
    def generate_random_name() -> str:
        return ''.join(random.choices(string.printable, k=30))

    async def store_requests(self, request: httpx.Request, response: httpx.Response) -> None:
        # content에 대한 fetching이 무조건 끝나도록 강제함.
        # 대부분의 경우에는 flushing만으로도 충분하지만
        # content와 await 사이가 remote한 아주 일부 경우 (썸네일 다운로드 등) flushing으로 부족함.
        await response.aread()
        self._waiting_flush.append((request, response))
        if self.flush_immediately or self.flush_limit is not None and len(self._waiting_flush) >= self.flush_limit:
            self.flush()

    def find_request(self, request: httpx.Request) -> httpx.Response:
        content_key = self.generate_content_key(request)
        key = self._keys[content_key]
        request, response = self.shelf[key]
        response._request = request
        # response.stream = None
        return response

    async def handle_async_request(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        if self.mode == "use":
            return self.find_request(request)

        if self.mode == "hybrid":
            with suppress(KeyError):
                return self.find_request(request)

        response = await super().handle_async_request(request)
        if self.mode != "passive":
            await self.store_requests(request, response)

        return response


@contextmanager
def init_transport(db_path: PathType, mode: ModeType, table_name: str = "Dict"):
    with dbm_open(db_path, "c", table=table_name) as db:
        with Shelf(db, writeback=False) as shelf:
            shelf.cache = RejectDict()  # type: ignore # no caching on shelf
            transport = AsyncCatcherTransport(shelf=shelf, mode=mode)
            try:
                yield transport
            finally:
                transport.close()


def install(db_path: PathType, mode: ModeType, flush_limit: int | None = 20):
    global _installed
    if _installed:
        return

    import atexit
    import httpx

    transport_initializer = init_transport(db_path, mode)
    transport = transport_initializer.__enter__()
    atexit.register(transport_initializer.__exit__, None, None, None)
    transport.flush_limit = flush_limit
    # monkey patching transport
    httpx.AsyncClient.__init__.__kwdefaults__["transport"] = transport

    _installed = True


def install_httpc(db_path: PathType, mode: ModeType, flush_limit: int | None = 20):
    global _httpc_installed
    if _httpc_installed:
        return

    import atexit
    import httpc

    transport_initializer = init_transport(db_path, mode)
    transport = transport_initializer.__enter__()
    atexit.register(transport_initializer.__exit__, None, None, None)
    transport.flush_limit = flush_limit
    # monkey patching transport
    httpc.AsyncClient.__init__.__kwdefaults__["transport"] = transport

    _httpc_installed = True
