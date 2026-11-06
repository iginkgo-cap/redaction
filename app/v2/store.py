"""映射库读写（ADR-007）：控制库 redaction_meta + 每个 namespace 一个 database rs_<id>。

namespace 的库在首次请求时创建并迁移；psycopg 的错误由 api 层统一转成 storage_unavailable。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from psycopg_pool import AsyncConnectionPool

from ..placeholder import PLACEHOLDER_FMT
from .errors import ApiError

logger = logging.getLogger(__name__)

_MIGRATIONS = Path(__file__).resolve().parent / "migrations"
_MIGRATION_LOCK = 6874

Key = tuple[str, bytes]  # (kind, value_hmac)


@dataclass(frozen=True)
class Value:
    kind: str
    value_hmac: bytes
    value_enc: bytes
    source: str

    @property
    def key(self) -> Key:
        return (self.kind, self.value_hmac)


_KEYS_SQL = "(kind, value_hmac) IN (SELECT * FROM unnest(%s::text[], %s::bytea[]))"


def _key_params(keys: list[Key]) -> list:
    return [[k for k, _ in keys], [h for _, h in keys]]


def database_name(namespace: str) -> str:
    return f"rs_{namespace}"


async def _migrate(pool: AsyncConnectionPool, kind: str) -> None:
    async with pool.connection() as conn, conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", [_MIGRATION_LOCK])
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
        )
        cur = await conn.execute("SELECT version FROM schema_migrations")
        applied = {r[0] for r in await cur.fetchall()}
        for f in sorted((_MIGRATIONS / kind).glob("*.sql")):
            if f.name not in applied:
                await conn.execute(f.read_text())
                await conn.execute("INSERT INTO schema_migrations (version) VALUES (%s)", [f.name])


class NamespaceStore:
    """一个 namespace 的库：表里没有 namespace 列，库本身就是隔离边界。"""

    def __init__(self, pool: AsyncConnectionPool):
        self._pool = pool

    async def suppressed(self, keys: list[Key]) -> set[Key]:
        if not keys:
            return set()
        async with self._pool.connection() as conn:
            cur = await conn.execute(f"SELECT kind, value_hmac FROM entity WHERE suppressed AND {_KEYS_SQL}",
                                     _key_params(keys))
            return {(k, bytes(h)) for k, h in await cur.fetchall()}

    async def apply(
        self,
        *,
        suppress: list[Value],
        unsuppress: list[Key],
        allocate: list[Value],
    ) -> dict[Key, tuple[str, bool]]:
        """一个事务里置/清抑制并分配占位符，返回 key → (placeholder, 是否新分配)。"""
        out: dict[Key, tuple[str, bool]] = {}
        # 行锁按 key 排序获取，避免并发请求以相反顺序锁同一批行而死锁
        async with self._pool.connection() as conn, conn.transaction():
            for kind, h in sorted(unsuppress):
                await conn.execute(
                    "UPDATE entity SET suppressed = false WHERE kind = %s AND value_hmac = %s AND suppressed", [kind, h]
                )
            for v in sorted(suppress, key=lambda v: v.key):
                await conn.execute(
                    "INSERT INTO entity (kind, value_hmac, value_enc, source, suppressed) VALUES (%s, %s, %s, %s, true) "
                    "ON CONFLICT (kind, value_hmac) DO UPDATE SET suppressed = true WHERE NOT entity.suppressed",
                    [v.kind, v.value_hmac, v.value_enc, v.source],
                )
            if not allocate:
                return out
            out.update(await self._existing(conn, [v.key for v in allocate]))
            for kind in sorted({v.kind for v in allocate if v.key not in out}):
                await conn.execute("INSERT INTO counter (kind, seq) VALUES (%s, 0) ON CONFLICT DO NOTHING", [kind])
                cur = await conn.execute("SELECT seq FROM counter WHERE kind = %s FOR UPDATE", [kind])
                seq = (await cur.fetchone())[0]
                todo = [v for v in allocate if v.kind == kind and v.key not in out]
                # 拿到计数器行锁后重查：并发请求可能刚分配过同一个值
                out.update(await self._existing(conn, [v.key for v in todo]))
                for v in todo:
                    if v.key in out:
                        continue
                    seq += 1
                    ph = PLACEHOLDER_FMT.format(kind=kind, n=seq)
                    await conn.execute(
                        "INSERT INTO entity (kind, value_hmac, value_enc, placeholder, seq, source) "
                        "VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (kind, value_hmac) DO UPDATE SET placeholder = EXCLUDED.placeholder, seq = EXCLUDED.seq",
                        [kind, v.value_hmac, v.value_enc, ph, seq, v.source],
                    )
                    out[v.key] = (ph, True)
                await conn.execute("UPDATE counter SET seq = %s WHERE kind = %s", [seq, kind])
        return out

    async def _existing(self, conn, keys: list[Key]) -> dict[Key, tuple[str, bool]]:  # noqa: ANN001
        if not keys:
            return {}
        cur = await conn.execute(
            f"SELECT kind, value_hmac, placeholder FROM entity WHERE placeholder IS NOT NULL AND {_KEYS_SQL}",
            _key_params(keys),
        )
        return {(k, bytes(h)): (ph, False) for k, h, ph in await cur.fetchall()}

    async def lookup(self, placeholders: list[str]) -> dict[str, tuple[str, bytes]]:
        """placeholder → (kind, value_enc)。"""
        if not placeholders:
            return {}
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT placeholder, kind, value_enc FROM entity WHERE placeholder = ANY(%s)", [placeholders]
            )
            return {ph: (k, bytes(enc)) for ph, k, enc in await cur.fetchall()}


class Stores:
    """控制库 + 按 namespace 取库。每个 namespace 一个连接池（平时 0 连接、最多 2 个）。

    墓碑集合 removed 只在这里维护，Registry 引用同一个集合：写墓碑后立即生效，已删除的 namespace 不再建库、取池。
    """

    def __init__(self, url: str, meta: AsyncConnectionPool, auto_create: bool, removed: set[str]):
        self._url = url
        self._meta = meta
        self._auto_create = auto_create
        self.removed = removed
        self._stores: dict[str, NamespaceStore] = {}
        self._pools: dict[str, AsyncConnectionPool] = {}
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, url: str, auto_create: bool = True) -> "Stores":
        meta = AsyncConnectionPool(url, min_size=1, max_size=2, timeout=5, open=False)
        await meta.open(wait=True, timeout=10)
        await _migrate(meta, "meta")
        async with meta.connection() as conn:
            cur = await conn.execute("SELECT id FROM namespace")
            removed = {r[0] for r in await cur.fetchall()}
        return cls(url, meta, auto_create, removed)

    async def close(self) -> None:
        for pool in self._pools.values():
            await pool.close()
        await self._meta.close()

    async def get(self, namespace: str, slug: str) -> NamespaceStore:
        store = self._stores.get(namespace)
        if store is not None:
            return store
        async with self._lock:
            # 删除可能在本请求鉴权之后、拿到锁之前完成：再查一次墓碑，绝不重建已删除的库
            if namespace in self.removed:
                raise ApiError("invalid_request", f"namespace {namespace!r} has been removed")
            if namespace not in self._stores:
                await self._ensure_database(namespace, slug)
                pool = AsyncConnectionPool(make_conninfo(self._url, dbname=database_name(namespace)),
                                           min_size=0, max_size=2, timeout=5, open=False)
                await pool.open()
                try:
                    await _migrate(pool, "namespace")
                except BaseException:
                    await pool.close()
                    raise
                self._pools[namespace] = pool
                self._stores[namespace] = NamespaceStore(pool)
            return self._stores[namespace]

    async def _admin(self) -> psycopg.AsyncConnection:
        # CREATE / DROP DATABASE 不能在事务里跑，用独立的自动提交连接
        return await psycopg.AsyncConnection.connect(self._url, autocommit=True)

    async def _exists(self, conn: psycopg.AsyncConnection, db: str) -> bool:
        cur = await conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", [db])
        return await cur.fetchone() is not None

    async def _ensure_database(self, namespace: str, slug: str) -> None:
        db = database_name(namespace)
        async with await self._admin() as conn:
            if not await self._exists(conn, db):
                if not self._auto_create:
                    raise ApiError("storage_unavailable", f"database {db} does not exist (auto create disabled)")
                try:
                    await conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
                    logger.info(f"created database {db} for namespace {slug}")
                except psycopg.errors.DuplicateDatabase:
                    pass
            try:
                await conn.execute(sql.SQL("COMMENT ON DATABASE {} IS {}").format(sql.Identifier(db), sql.Literal(slug)))
            except psycopg.Error as exc:  # 客户预建的库可能不归服务账号所有，注释写不进不影响使用
                logger.warning(f"cannot comment database {db}: {exc!r}")

    async def remove(self, namespace: str, slug: str) -> int:
        """写墓碑并立即禁用，再关池、删库（ADR-007 §1）。返回删掉的实体数；已删除的再调返回 0。

        删库失败时 namespace 已禁用，重调即补删（或由 DBA 手工删库）。
        """
        db = database_name(namespace)
        async with self._lock:
            entities = 0
            async with await self._admin() as conn:
                exists = await self._exists(conn, db)
            if exists:  # 计数出错（连不上等）在写墓碑之前抛出，什么都不改
                try:
                    async with await psycopg.AsyncConnection.connect(make_conninfo(self._url, dbname=db)) as conn:
                        cur = await conn.execute("SELECT count(*) FROM entity")
                        entities = (await cur.fetchone())[0]
                except psycopg.errors.UndefinedTable:
                    pass  # 建了库还没迁移
            async with self._meta.connection() as conn:
                await conn.execute(
                    "INSERT INTO namespace (id, slug, removed_at) VALUES (%s, %s, now()) ON CONFLICT DO NOTHING",
                    [namespace, slug],
                )
            self.removed.add(namespace)
            self._stores.pop(namespace, None)
            pool = self._pools.pop(namespace, None)
            if pool is not None:
                await pool.close()
            async with await self._admin() as conn:
                await conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(db)))
            return entities
