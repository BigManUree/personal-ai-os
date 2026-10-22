"""Pytest safety guards for Personal AI OS.

The project has a real local development database under ``data/``. Force test
collection into a temporary database before application modules are imported, and
verify that individual tests have retained their explicit temp-DB isolation.
"""

from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
REAL_DATABASE_PATH = (ROOT / "data" / "personal_ai_os.db").resolve()
ALLOW_REAL_DATABASE = os.environ.get("PAOS_TEST_ALLOW_REAL_DATABASE") == "1"
STATIC_DIR = ROOT / "app" / "static"
FRONTEND_SRC = ROOT / "frontend" / "src"


def _is_under_system_temp(path: Path) -> bool:
    try:
        path.resolve().relative_to(Path(tempfile.gettempdir()).resolve())
        return True
    except (OSError, ValueError):
        return False


def pytest_configure(config: pytest.Config) -> None:
    """Prevent an inherited developer environment from reaching real data."""
    if ALLOW_REAL_DATABASE:
        return

    configured_db = os.environ.get("DATABASE_PATH")
    if not configured_db or not _is_under_system_temp(Path(configured_db)):
        test_dir = Path(tempfile.mkdtemp(prefix="paos-pytest-"))
        os.environ["DATABASE_PATH"] = str(test_dir / "collection-isolation.db")

    configured_key = os.environ.get("PAOS_SECRET_KEY_PATH")
    if not configured_key or not _is_under_system_temp(Path(configured_key)):
        os.environ["PAOS_SECRET_KEY_PATH"] = str(
            Path(os.environ["DATABASE_PATH"]).parent / "test-secret.key"
        )

    # Do not inherit cloud or credential-encryption secrets during normal tests.
    # Tests that exercise cloud paths set explicit fake values and mock HTTP.
    os.environ.pop("LLM_API_KEY", None)
    os.environ.pop("PAOS_SECRET_KEY", None)


@pytest.fixture(autouse=True)
def _assert_temporary_database() -> None:
    if ALLOW_REAL_DATABASE:
        return
    env_db = os.environ.get("DATABASE_PATH")
    assert env_db, "DATABASE_PATH must point at a temporary SQLite file in tests"
    assert Path(env_db).resolve() != REAL_DATABASE_PATH
    assert _is_under_system_temp(Path(env_db)), (
        f"refusing to run tests against non-temp DATABASE_PATH: {env_db}"
    )

    from app import main

    assert Path(main.DB_PATH).resolve() != REAL_DATABASE_PATH


@pytest.fixture
def rollback_journal():
    """Switch a freshly created test database off WAL before multi-threaded writes.

    A brand-new WAL database has to bootstrap its ``-shm``/``-wal`` pair. When
    several threads connect for their very first write at the same instant, Windows
    can fail one of them with ``attempt to write a readonly database`` (reproduced
    outside pytest: 2/8 rounds on WAL, 0/8 on the rollback journal). The concurrency
    tests below exercise unique constraints and lease fencing, not WAL bootstrap,
    so they run on the rollback journal. Production keeps WAL.
    """

    def _apply(target: sqlite3.Connection | str | Path) -> None:
        if isinstance(target, sqlite3.Connection):
            target.execute("PRAGMA journal_mode=DELETE")
            return
        conn = sqlite3.connect(str(target), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=DELETE")
        finally:
            conn.close()

    return _apply


@pytest.fixture(scope="session")
def frontend_bundle() -> str:
    """Text of the single JS bundle referenced by ``app/static/index.html``.

    The legacy hand-written scripts under ``app/static`` were removed on
    2026-10-22, so frontend assertions must target the Vite build output (or the
    Vue sources) rather than files that no longer exist.
    """
    index_html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    match = re.search(r'src="/static/(assets/[^"]+\.js)"', index_html)
    assert match, "app/static/index.html 未引用 JS bundle"
    return (STATIC_DIR / match.group(1)).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def frontend_sources() -> str:
    """Concatenated Vue/TS sources, for safety guards on the real UI code."""
    files = sorted(FRONTEND_SRC.rglob("*.vue")) + sorted(FRONTEND_SRC.rglob("*.ts"))
    assert files, "frontend/src 下没有源码文件"
    return "\n".join(path.read_text(encoding="utf-8") for path in files)
