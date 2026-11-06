"""共享 fixture。"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.canonical import CanonicalMap

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(scope="session")
def canonical_map() -> CanonicalMap:
    return CanonicalMap(CONFIG_DIR / "canonical.yaml")


@pytest.fixture(scope="session")
def en_rules_path() -> Path:
    return CONFIG_DIR / "en_regex_rules.yaml"



def reset_test_databases(url: str) -> None:
    """删掉测试库里的 rs_* 与控制库表。库名不含 test 时拒绝执行：指错实例会删光所有客户的映射库。"""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import conninfo_to_dict

    if "test" not in conninfo_to_dict(url).get("dbname", ""):
        raise RuntimeError(f"refusing to reset non-test database: {conninfo_to_dict(url).get('dbname')!r}")

    with psycopg.connect(url, autocommit=True) as conn:
        for (db,) in conn.execute("SELECT datname FROM pg_database WHERE datname LIKE 'rs\\_%'").fetchall():
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(db)))
        conn.execute("DROP TABLE IF EXISTS namespace, entity, counter, schema_migrations")


@pytest.fixture()
async def stores():
    """真 Postgres（REDACTION_TEST_DATABASE_URL 指向控制库），每个测试前清空；没配就跳过。"""
    import os

    from app.v2.store import Stores

    url = os.environ.get("REDACTION_TEST_DATABASE_URL")
    if not url:
        pytest.skip("REDACTION_TEST_DATABASE_URL 未设置")
    reset_test_databases(url)
    s = await Stores.open(url)
    yield s
    await s.close()
