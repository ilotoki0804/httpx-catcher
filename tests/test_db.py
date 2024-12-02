import asyncio
from pathlib import Path
import httpc
import httpx
from httpx_catcher import AsyncCatcherTransport, TransactionDatabase
import pytest

RESOURCE_DIR = Path(__file__).parent.joinpath("resource")


def test_db():
    RESOURCE_DIR.mkdir(exist_ok=True)

    db_path = RESOURCE_DIR / "test.db"
    db = TransactionDatabase(db_path, "Test")
    try:
        req = httpx.Request("GET", "https://hello.world", content=b"hello world content")
        res = httpx.Response(200, text="hello, world!")
        db[req] = res
        fetched_res = db[req]
        # 일관성을 유지하기 어려운 파라미터 제거
        del res._decoder, fetched_res.stream, res.stream
        assert vars(fetched_res) == vars(res)

        assert len(db) == 1
        del db[req]
        assert not db
    finally:
        db.close()
        db_path.unlink(missing_ok=True)


@pytest.mark.skip
def test_catcher():
    asyncio.run(async_test_catcher())


async def async_test_catcher():
    db_path = RESOURCE_DIR / "test.db"
    with AsyncCatcherTransport.with_db(db_path, "hybrid") as transport:
        async with httpc.AsyncClient(transport=transport) as client:
            await client.get("https://www.google.com", headers={"hello": "world"})
            req = httpx.Request("GET", "https://www.google.com", headers={"hello": "world"})
            assert transport.db[req]
    db_path.unlink(missing_ok=True)
