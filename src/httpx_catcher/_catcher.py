import logging
import ssl
from contextlib import contextmanager, suppress
from os import PathLike
from typing import Literal

import httpx

from ._db import TransactionDatabase

type VerifyType = ssl.SSLContext | str | bool
type PathType = PathLike | str | bytes
type ContentKey = tuple[str, str, bytes]
type ModeType = Literal["store", "use", "hybrid", "passive"]

DEFAULT_LOGGER = logging.getLogger(__name__)
_installed = False
_httpc_installed = False


class AsyncCatcherTransport(httpx.AsyncHTTPTransport):
    logger = DEFAULT_LOGGER
    valid_modes = "store", "use", "hybrid", "passive"

    def __init__(
        self,
        db: TransactionDatabase,
        mode: ModeType,
        *,
        verify: VerifyType | None = None,
        **kwargs,
    ) -> None:
        if mode not in self.valid_modes:
            raise ValueError(f"mode should be within {self.valid_modes}, not {mode!r}.")

        verify = ssl.create_default_context() if verify is None else verify
        super().__init__(verify=verify, **kwargs)
        self.db = db
        self.mode = mode

    @classmethod
    @contextmanager
    def with_db(cls, db_path: PathType, mode: ModeType, table_name: str = "transactions"):
        with TransactionDatabase(db_path, table_name, flag="c") as db:
            yield cls(db=db, mode=mode)

    async def store_async_requests(self, request: httpx.Request, response: httpx.Response) -> None:
        # content에 대한 fetching이 무조건 끝나도록 강제함.
        # 대부분의 경우에는 flushing만으로도 충분하지만
        # content와 await 사이가 remote한 아주 일부 경우 (썸네일 다운로드 등) flushing으로 부족함.
        request.read()
        await response.aread()
        self.db[request] = response

    def find_request(self, request: httpx.Request, *, _comprehensive_error: bool = True) -> httpx.Response:
        try:
            response = self.db[request]
        except KeyError:
            if not _comprehensive_error:
                raise ValueError(request)

            method = "" if request.method == "GET" else request.method + " "
            content = request.content
            if not content:
                raise ValueError(
                    f"Could not find a {method}response for {request.url}")
            elif len(content) <= 20:
                raise ValueError(
                    f"Could not find a {method}response for {request.url} (with content: {content!r})")
            else:
                raise ValueError(
                    f"Could not find a {method}response for {request.url} (with {len(content)} length content)")

        response._request = request
        # response.stream = None
        return response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.mode == "use":
            await request.aread()
            return self.find_request(request)

        if self.mode == "hybrid":
            with suppress(ValueError):
                await request.aread()
                return self.find_request(request)

        response = await super().handle_async_request(request)
        if self.mode != "passive":
            await self.store_async_requests(request, response)

        return response


def install(db_path: PathType, mode: ModeType):
    global _installed
    if _installed:
        return

    import atexit
    import httpx

    transport_initializer = AsyncCatcherTransport.with_db(db_path, mode)
    transport = transport_initializer.__enter__()
    atexit.register(transport_initializer.__exit__, None, None, None)
    # monkey patching transport
    httpx.AsyncClient.__init__.__kwdefaults__["transport"] = transport

    _installed = True


def install_httpc(db_path: PathType, mode: ModeType):
    global _httpc_installed
    if _httpc_installed:
        return

    import atexit
    import httpc

    transport_initializer = AsyncCatcherTransport.with_db(db_path, mode)
    transport = transport_initializer.__enter__()
    atexit.register(transport_initializer.__exit__, None, None, None)
    # monkey patching transport
    httpc.AsyncClient.__init__.__kwdefaults__["transport"] = transport

    _httpc_installed = True
