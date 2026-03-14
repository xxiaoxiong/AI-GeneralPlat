import asyncio


class DummyConn:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_build_memory_query_with_followup_context():
    from app.api.v1.agents import _build_memory_query

    history = [
        {"role": "user", "content": "帮我分析订单趋势"},
        {"role": "assistant", "content": "最近7天订单上涨 12%"},
    ]
    query = _build_memory_query("继续", history)
    assert "上下文参考" in query
    assert "订单趋势" in query or "上涨" in query


def test_build_memory_query_for_long_text_keeps_original():
    from app.api.v1.agents import _build_memory_query

    message = "请根据最近三个月的成交数据，给我做按渠道分组的转化率分析"
    assert _build_memory_query(message, []) == message


def test_build_db_sync_url_escapes_password_chars():
    from app.api.v1.agents import _build_db_sync_url

    conn = DummyConn(
        db_type="mysql",
        username="demo",
        password="p@ss:word",
        host="127.0.0.1",
        port=3306,
        database="sales",
    )
    url = _build_db_sync_url(conn)
    assert "p%40ss%3Aword" in url


def test_get_cached_table_names_uses_ttl_cache(monkeypatch):
    from app.api.v1 import agents as agents_api

    calls = {"n": 0}

    def fake_load(sync_url: str, extra_params: str = ""):
        calls["n"] += 1
        return ["orders", "users"]

    conn = DummyConn(
        id=999,
        db_type="mysql",
        username="demo",
        password="pass",
        host="127.0.0.1",
        port=3306,
        database="sales",
        extra_params="",
    )

    monkeypatch.setattr(agents_api, "_load_tables_sync", fake_load)
    agents_api._DB_SCHEMA_CACHE.clear()

    async def run_twice():
        first = await agents_api._get_cached_table_names(conn)
        second = await agents_api._get_cached_table_names(conn)
        return first, second

    first, second = asyncio.get_event_loop().run_until_complete(run_twice())
    assert first == ["orders", "users"]
    assert second == ["orders", "users"]
    assert calls["n"] == 1
