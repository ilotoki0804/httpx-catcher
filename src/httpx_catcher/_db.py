from __future__ import annotations

import json
import os
import pickle
import sqlite3
from pathlib import Path
from contextlib import suppress, closing
from collections.abc import MutableMapping
import typing

import httpx


class DBError(OSError):
    pass


_ERR_CLOSED = "DBM object has already been closed"
_ERR_REINIT = "DBM object does not support reinitialization"

if typing.TYPE_CHECKING:
    RequestTuple = tuple[str, str, str, bytes]
    NoHeadersRequestTuple = tuple[str, str, bytes]


class TransactionDatabase(MutableMapping[httpx.Request, httpx.Response]):
    def __init__(
        self,
        path: os.PathLike | str | bytes,
        table: str,
        *,
        flag: typing.Literal["r", "w", "c", "n"] = "c",
        mode: int = 0o666,
        protocol: int = pickle.DEFAULT_PROTOCOL,
    ) -> None:
        self._protocol = protocol

        if hasattr(self, "_cx"):
            raise DBError(_ERR_REINIT)

        path = Path(os.fsdecode(path))
        match flag:
            case "r":
                flagged = "ro"
            case "w":
                flagged = "rw"
            case "c":
                flagged = "rwc"
                path.touch(mode=mode, exist_ok=True)
            case "n":
                flagged = "rwc"
                path.unlink(missing_ok=True)
                path.touch(mode=mode)
            case _:
                raise ValueError("Flag must be one of 'r', 'w', 'c', or 'n', "
                                 f"not {flag!r}")

        # We use the URI format when opening the database.
        uri = self._normalize_uri(path)
        uri = f"{uri}?mode={flagged}"

        try:
            self._cx = sqlite3.connect(uri, autocommit=True, uri=True)
        except sqlite3.Error as exc:
            raise DBError(str(exc))

        # This is an optimization only; it's ok if it fails.
        with suppress(sqlite3.OperationalError):
            self._cx.execute("PRAGMA journal_mode = wal")

        self._build_queries(table)

        if flagged == "rwc":
            self._execute(self._build_table)

    def _execute(self, *args, **kwargs):
        if not self._cx:
            raise DBError(_ERR_CLOSED)
        try:
            return closing(self._cx.execute(*args, **kwargs))
        except sqlite3.Error as exc:
            raise DBError(str(exc))

    def _build_queries(self, table: str) -> None:
        if not table.isidentifier():
            raise ValueError(f"Table name must be an identifier, not {table!r}")
        if table.startswith("sqlite_"):
            raise ValueError(
                "Table name should not start with 'sqlite_', "
                "since it's reserved for internal use in sqlite"
            )

        self._build_table = f"""
        CREATE TABLE IF NOT EXISTS {table} (
            method TEXT NOT NULL,
            url TEXT NOT NULL,
            headers TEXT NOT NULL,
            content BLOB NOT NULL,
            response BLOB NOT NULL
        )
        """
        self._get_size = f"SELECT COUNT (url) FROM {table}"
        self._lookup_key = f"""
        SELECT response FROM {table} WHERE (
            method = CAST(? AS TEXT)
            AND url = CAST(? AS TEXT)
            AND content = CAST(? AS BLOB)
        )
        """
        self._store_kv = f"""
        INSERT INTO {table} (method, url, headers, content, response) VALUES (
            CAST(? AS TEXT), CAST(? AS TEXT), CAST(? AS TEXT), CAST(? AS BLOB), CAST(? AS BLOB)
        )
        """
        self._delete_key = f"""DELETE FROM {table} WHERE (
            method = CAST(? AS TEXT)
            AND url = CAST(? AS TEXT)
            AND content = CAST(? AS BLOB)
        )"""
        self._iter_keys = f"SELECT (method, url, headers, content) FROM {table}"
        self._drop_table = f"DROP TABLE {table}"

    @staticmethod
    def _normalize_uri(path: Path) -> str:
        uri = path.absolute().as_uri()
        while "//" in uri:
            uri = uri.replace("//", "/")
        return uri

    @staticmethod
    def _disassemble_request(request: httpx.Request) -> RequestTuple:
        return request.method, str(request.url), json.dumps(dict(request.headers)), request.content

    @staticmethod
    def _disassemble_request_without_headers(request: httpx.Request) -> NoHeadersRequestTuple:
        return request.method, str(request.url), request.content

    @staticmethod
    def _assemble_request(request_tuple: RequestTuple) -> httpx.Request:
        method, url, headers, content = request_tuple
        return httpx.Request(method, url, headers=json.loads(headers), content=content)

    def __len__(self) -> int:
        with self._execute(self._get_size) as cu:
            row = cu.fetchone()
        return row[0]

    def __getitem__(self, request: httpx.Request) -> httpx.Response:
        with self._execute(self._lookup_key, self._disassemble_request_without_headers(request)) as cu:
            row = cu.fetchone()
        if not row:
            raise KeyError(request)
        return pickle.loads(row[0])

    def __setitem__(self, request: httpx.Request, response: httpx.Response) -> None:
        with suppress(KeyError):
            del self[request]
        self._execute(self._store_kv, (*self._disassemble_request(request), pickle.dumps(response, protocol=self._protocol)))

    def __delitem__(self, request: httpx.Request) -> None:
        with self._execute(self._delete_key, self._disassemble_request_without_headers(request)) as cu:
            if not cu.rowcount:
                raise KeyError(request)

    def __iter__(self) -> typing.Iterator[httpx.Request]:
        try:
            with self._execute(self._iter_keys) as cu:
                for row in cu:
                    yield self._assemble_request(row)
        except sqlite3.Error as exc:
            raise DBError(str(exc))

    def close(self) -> None:
        if self._cx:
            self._cx.close()
            self._cx = None

    def drop(self) -> None:
        try:
            self._execute(self._drop_table)
        except sqlite3.Error as exc:
            raise DBError(str(exc))

    def keys(self) -> list[httpx.Request]:
        return list(super().keys())

    def __enter__(self) -> typing.Self:
        return self

    def __exit__(self, *_) -> None:
        self.close()
