"""v2 映射库：每个 namespace 一个 database；分配、抑制、隔离、删除（连真 Postgres）。"""
from __future__ import annotations

import asyncio
import base64
import os
import re

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app.v2.crypto import Cipher, Keys
from app.v2.store import Stores, Value

CIPHER = Cipher(Keys(b"e" * 32, b"h" * 32))


def _v(ns: str, kind: str, value: str, source: str = "cn_regex") -> Value:
    return Value(kind, CIPHER.digest(ns, kind, value), CIPHER.encrypt(value), source)


def _databases() -> dict[str, str | None]:
    """rs_* 库 → COMMENT"""
    with psycopg.connect(os.environ["REDACTION_TEST_DATABASE_URL"]) as conn:
        rows = conn.execute(
            "SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database WHERE datname LIKE 'rs\\_%'"
        ).fetchall()
    return dict(rows)


async def test_同值同占位符_新值递增(stores) -> None:
    s = await stores.get("n1", "tenant-one")
    a, b = _v("n1", "PERSON", "张三"), _v("n1", "PERSON", "李四")
    r1 = await s.apply(suppress=[], unsuppress=[], allocate=[a, b])
    assert r1[a.key] == ("[[PERSON_1]]", True)
    assert r1[b.key] == ("[[PERSON_2]]", True)
    r2 = await s.apply(suppress=[], unsuppress=[], allocate=[_v("n1", "PERSON", "张三")])
    assert r2[a.key] == ("[[PERSON_1]]", False)


async def test_每个_namespace_一个库_首次使用时创建并写_slug(stores) -> None:
    assert _databases() == {}
    s1, s2 = await stores.get("n1", "tenant-one"), await stores.get("n2", "tenant-two")
    assert _databases() == {"rs_n1": "tenant-one", "rs_n2": "tenant-two"}
    await s1.apply(suppress=[], unsuppress=[], allocate=[_v("n1", "PERSON", "张三")])
    r = await s2.apply(suppress=[], unsuppress=[], allocate=[_v("n2", "PERSON", "王五")])
    assert list(r.values()) == [("[[PERSON_1]]", True)]  # 编号各自独立
    kind, enc = (await s2.lookup(["[[PERSON_1]]"]))["[[PERSON_1]]"]
    assert CIPHER.decrypt(enc) == "王五"
    # 数据物理上只在各自的库里，表上没有 namespace 列
    url = os.environ["REDACTION_TEST_DATABASE_URL"]
    for db in ("rs_n1", "rs_n2"):
        with psycopg.connect(make_conninfo(url, dbname=db)) as conn:
            assert conn.execute("SELECT count(*) FROM entity").fetchone()[0] == 1
            cols = {r[0] for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'entity'").fetchall()}
            assert "namespace" not in cols


async def test_库已存在时复用_不重复迁移(stores) -> None:
    s = await stores.get("n1", "tenant-one")
    await s.apply(suppress=[], unsuppress=[], allocate=[_v("n1", "PERSON", "张三")])
    other = await Stores.open(os.environ["REDACTION_TEST_DATABASE_URL"])  # 模拟重启
    try:
        r = await (await other.get("n1", "renamed")).lookup(["[[PERSON_1]]"])
        assert set(r) == {"[[PERSON_1]]"}
        assert _databases()["rs_n1"] == "renamed"  # slug 改了，注释跟着刷新
    finally:
        await other.close()


async def test_关闭自动建库时缺库报_storage_unavailable(stores) -> None:
    from app.v2.errors import ApiError

    strict = await Stores.open(os.environ["REDACTION_TEST_DATABASE_URL"], auto_create=False)
    try:
        with pytest.raises(ApiError) as e:
            await strict.get("n9", "nine")
        assert e.value.code == "storage_unavailable"
        assert "rs_n9" not in _databases()
    finally:
        await strict.close()


async def test_并发分配不撞号(stores) -> None:
    s = await stores.get("n1", "tenant-one")

    async def one(i: int):
        vals = [_v("n1", "PHONE", f"1380000{i:04d}"), _v("n1", "PHONE", "shared")]
        return await s.apply(suppress=[], unsuppress=[], allocate=vals)

    results = await asyncio.gather(*(one(i) for i in range(30)))
    shared = {r[_v("n1", "PHONE", "shared").key][0] for r in results}
    assert len(shared) == 1
    phs = {ph for r in results for ph, _ in r.values()}
    seqs = sorted(int(re.search(r"_(\d+)\]\]", p).group(1)) for p in phs)
    assert seqs == list(range(1, 32))


async def test_并发首次使用同一_namespace_只建一次(stores) -> None:
    got = await asyncio.gather(*(stores.get("n1", "tenant-one") for _ in range(10)))
    assert len({id(s) for s in got}) == 1
    assert list(_databases()) == ["rs_n1"]


async def test_抑制只在本_namespace_且_include_可清除(stores) -> None:
    s1, s2 = await stores.get("n1", "tenant-one"), await stores.get("n2", "tenant-two")
    v = _v("n1", "PERSON", "周大福", "hanlp")
    await s1.apply(suppress=[v], unsuppress=[], allocate=[])
    await s1.apply(suppress=[v], unsuppress=[], allocate=[])  # 重试不报错
    assert await s1.suppressed([v.key]) == {v.key}
    assert await s2.suppressed([_v("n2", "PERSON", "周大福").key]) == set()
    r = await s1.apply(suppress=[], unsuppress=[v.key], allocate=[v])
    assert r[v.key] == ("[[PERSON_1]]", True)
    assert await s1.suppressed([v.key]) == set()


async def test_并发置抑制_顺序相反不死锁(stores) -> None:
    s = await stores.get("n1", "tenant-one")
    vals = [_v("n1", "PERSON", f"name{i}", "hanlp") for i in range(20)]
    for _ in range(10):
        await asyncio.gather(*(s.apply(suppress=o, unsuppress=[], allocate=[])
                               for o in [vals, vals[::-1], vals, vals[::-1]]))
    assert len(await s.suppressed([v.key for v in vals])) == 20


async def test_删除_namespace_删库留墓碑且幂等(stores) -> None:
    s1 = await stores.get("n1", "tenant-one")
    await s1.apply(suppress=[], unsuppress=[], allocate=[_v("n1", "PERSON", "张三"), _v("n1", "PHONE", "1")])
    s2 = await stores.get("n2", "tenant-two")
    await s2.apply(suppress=[], unsuppress=[], allocate=[_v("n2", "PERSON", "张三")])
    assert await stores.remove("n1", "tenant-one") == 2
    assert await stores.remove("n1", "tenant-one") == 0
    assert await stores.remove("never-used", "never") == 0
    assert stores.removed == {"n1", "never-used"}
    assert set(_databases()) == {"rs_n2"}
    assert set(await s2.lookup(["[[PERSON_1]]"])) == {"[[PERSON_1]]"}


async def test_删除期间排队的请求不会重建已删的库(stores) -> None:
    from app.v2.errors import ApiError

    await stores.get("n1", "tenant-one")
    stores._stores.clear()  # 这个请求要走慢路径（本进程首次使用，或刚好在删除弹出缓存之后）
    remove = asyncio.create_task(stores.remove("n1", "tenant-one"))
    await asyncio.sleep(0)  # 删除先拿到锁；已过鉴权的请求排在锁后面
    get = asyncio.create_task(stores.get("n1", "tenant-one"))
    await remove
    with pytest.raises(ApiError) as e:
        await get
    assert e.value.code == "invalid_request"
    assert "rs_n1" not in _databases()


async def test_删库失败时_namespace_已禁用且可重试(stores) -> None:
    """私有化：客户预建的库不归服务账号所有，DROP 报 must be owner。"""
    from app.v2.errors import ApiError

    admin_url = os.environ["REDACTION_TEST_DATABASE_URL"]
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute("DROP DATABASE IF EXISTS svc_test_meta WITH (FORCE)")
        conn.execute("DROP ROLE IF EXISTS svc_test")
        conn.execute("CREATE ROLE svc_test LOGIN PASSWORD 'svc' CREATEDB")
        conn.execute("CREATE DATABASE svc_test_meta OWNER svc_test")
        conn.execute("CREATE DATABASE rs_pre")
        conn.execute("GRANT ALL ON DATABASE rs_pre TO svc_test")
    with psycopg.connect(make_conninfo(admin_url, dbname="rs_pre"), autocommit=True) as conn:
        conn.execute("GRANT ALL ON SCHEMA public TO svc_test")
    svc = await Stores.open(make_conninfo(admin_url, user="svc_test", password="svc", dbname="svc_test_meta"),
                            auto_create=False)
    try:
        await svc.get("pre", "pre-co")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            await svc.remove("pre", "pre-co")
        assert "pre" in svc.removed  # 墓碑已写、立即禁用
        with pytest.raises(ApiError):
            await svc.get("pre", "pre-co")
        assert "rs_pre" in _databases()  # 库还在，等重试或 DBA 手工删
    finally:
        await svc.close()
        with psycopg.connect(admin_url, autocommit=True) as conn:
            conn.execute("DROP DATABASE IF EXISTS svc_test_meta WITH (FORCE)")
            conn.execute("DROP DATABASE IF EXISTS rs_pre WITH (FORCE)")
            conn.execute("DROP OWNED BY svc_test")
            conn.execute("DROP ROLE IF EXISTS svc_test")


def test_测试清理拒绝非测试库() -> None:
    from .conftest import reset_test_databases

    with pytest.raises(RuntimeError, match="non-test"):
        reset_test_databases("postgresql://redaction:x@127.0.0.1:1/redaction_meta")


def test_hmac_含_namespace_与_kind() -> None:
    assert CIPHER.digest("n1", "PERSON", "x") != CIPHER.digest("n2", "PERSON", "x")
    assert CIPHER.digest("n1", "PERSON", "x") != CIPHER.digest("n1", "ORG", "x")


def test_加密每次_nonce_不同且可解() -> None:
    a, b = CIPHER.encrypt("张三"), CIPHER.encrypt("张三")
    assert a != b and a[:1] == b"\x01"
    assert CIPHER.decrypt(a) == "张三"


def test_load_keys_本地环境变量与文件(tmp_path) -> None:
    import json

    from app.settings import Settings
    from app.v2.crypto import KeyLoadError, load_keys

    k = base64.b64encode(b"k" * 32).decode()
    assert load_keys(Settings(encryption_key=k, hmac_key=k)).hmac_key == b"k" * 32
    f = tmp_path / "keys.json"
    f.write_text(json.dumps({"encryption_key": k, "hmac_key": k}))
    assert load_keys(Settings(key_file=f)).encryption_key == b"k" * 32
    with pytest.raises(KeyLoadError):
        load_keys(Settings())
    with pytest.raises(KeyLoadError):
        load_keys(Settings(encryption_key=base64.b64encode(b"short").decode(), hmac_key=k))
