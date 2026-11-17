"""Runtime coverage for core event helpers and EE hooks."""
import inspect
import logging
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import src.core.ee_hooks as ee_hooks
import src.core.events.autoinstall as autoinstall
import src.core.events.content as content_events
import src.core.events.logs as logs_events

class _FakeScalars:

    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row

class _FakeResult:

    def __init__(self, row):
        self._row = row

    def scalars(self):
        return _FakeScalars(self._row)

    def first(self):
        return self._row

class _FakeAsyncSession:
    """Stands in for a session handed out by ``_async_session_factory``."""

    def __init__(self, row, opened):
        self.row = row
        self._opened = opened

    async def execute(self, statement):
        return _FakeResult(self.row)

    async def __aenter__(self):
        self._opened.append(self)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

def _patch_session_factory(monkeypatch, row):
    """Point autoinstall at a fake application session factory.

    The real one is the app-wide engine, and the whole point of the module is that
    it must not build a second one.
    """
    opened = []
    monkeypatch.setattr(autoinstall, '_async_session_factory', lambda: _FakeAsyncSession(row, opened))
    return opened

@pytest.mark.asyncio
async def test_auto_install_bootstraps_when_no_org_exists(monkeypatch):
    installs = []
    refreshes = []
    opened = _patch_session_factory(monkeypatch, None)

    async def fake_install_async(short=True):
        installs.append(short)

    async def fake_install_default_elements(db):
        refreshes.append(db)
    monkeypatch.setattr(autoinstall, '_install_async', fake_install_async)
    monkeypatch.setattr(autoinstall, 'install_default_elements', fake_install_default_elements)
    await autoinstall.auto_install()
    assert installs == [True]
    assert refreshes == []
    assert len(opened) == 1

@pytest.mark.asyncio
async def test_auto_install_refreshes_roles_when_org_exists(monkeypatch):
    installs = []
    refreshes = []
    opened = _patch_session_factory(monkeypatch, SimpleNamespace(slug='anything'))

    async def fake_install_async(short=True):
        installs.append(short)

    async def fake_install_default_elements(db):
        refreshes.append(db)
    monkeypatch.setattr(autoinstall, '_install_async', fake_install_async)
    monkeypatch.setattr(autoinstall, 'install_default_elements', fake_install_default_elements)
    await autoinstall.auto_install()
    assert installs == []
    assert len(refreshes) == 1
    assert len(opened) == 2

@pytest.mark.asyncio
async def test_auto_install_role_refresh_failure_is_non_fatal(monkeypatch):
    _patch_session_factory(monkeypatch, SimpleNamespace(slug='anything'))

    async def boom(db):
        raise RuntimeError('db went away mid-refresh')
    monkeypatch.setattr(autoinstall, 'install_default_elements', boom)
    await autoinstall.auto_install()

@pytest.mark.asyncio
async def test_auto_install_does_not_create_its_own_engine():
    """Regression guard for the duplicate-pool startup crash loop.

    A second engine here doubled each pod's connection count against the
    Postgres pooler; when the pooler ran out of clients the extra pool raised
    during startup, the boot aborted, and the restart opened even more
    connections.
    """
    source = inspect.getsource(autoinstall)
    assert 'create_engine' not in source
    assert 'create_async_engine' not in source

@pytest.mark.asyncio
async def test_content_and_logs_helpers(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    created_dirs = []
    monkeypatch.setattr(content_events.os.path, 'exists', lambda path: path == 'content')
    monkeypatch.setattr(content_events.os, 'makedirs', lambda path: created_dirs.append(path))
    await content_events.check_content_directory()
    assert created_dirs == []
    monkeypatch.setattr(content_events.os.path, 'exists', lambda path: False)
    await content_events.check_content_directory()
    assert created_dirs == ['content']
    mkdir_calls = []
    monkeypatch.setattr(logs_events.os.path, 'exists', lambda path: path == 'logs')
    monkeypatch.setattr(logs_events.os, 'mkdir', lambda path: mkdir_calls.append(path))
    await logs_events.create_logs_dir()
    assert mkdir_calls == []
    monkeypatch.setattr(logs_events.os.path, 'exists', lambda path: False)
    await logs_events.create_logs_dir()
    assert mkdir_calls == ['logs']

def test_ee_hooks_availability_and_loading(monkeypatch, caplog):
    monkeypatch.delenv('LEARNHOUSE_DISABLE_EE', raising=False)
    monkeypatch.setattr(ee_hooks.os.path, 'isdir', lambda path: path == 'ee')
    monkeypatch.setattr(ee_hooks.os.path, 'isfile', lambda path: True)
    assert ee_hooks.is_ee_available() is True
    monkeypatch.setenv('LEARNHOUSE_DISABLE_EE', '1')
    assert ee_hooks.is_ee_available() is False
    assert ee_hooks.get_ee_hooks() is None
    monkeypatch.delenv('LEARNHOUSE_DISABLE_EE', raising=False)
    monkeypatch.setattr(ee_hooks.os.path, 'isdir', lambda path: True)
    monkeypatch.setattr(ee_hooks.importlib.util, 'find_spec', lambda name: None)
    assert ee_hooks.get_ee_hooks() is None
    monkeypatch.setattr(ee_hooks.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setattr(ee_hooks.importlib, 'import_module', lambda name: SimpleNamespace(register_middlewares=Mock()))
    hooks = ee_hooks.get_ee_hooks()
    assert hooks is not None
    monkeypatch.setattr(ee_hooks.importlib, 'import_module', lambda name: (_ for _ in ()).throw(ImportError('missing hooks')))
    with caplog.at_level(logging.ERROR):
        assert ee_hooks.get_ee_hooks() is None
    assert 'Failed to import EE hooks' in caplog.text
    monkeypatch.setattr(ee_hooks.importlib, 'import_module', lambda name: (_ for _ in ()).throw(RuntimeError('unexpected')))
    with caplog.at_level(logging.ERROR):
        assert ee_hooks.get_ee_hooks() is None
    assert 'Unexpected error loading EE hooks' in caplog.text

@pytest.mark.asyncio
async def test_connect_to_db_retries_transient_failure(monkeypatch):
    import src.core.events.database as db_events
    app = SimpleNamespace()
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError('(EMAXCONNSESSION) max clients reached in session mode - max clients are limited to pool_size: 15')
    monkeypatch.setattr(db_events, '_bootstrap_schema', flaky)
    monkeypatch.setattr(db_events, '_STARTUP_CONNECT_BACKOFF_SECONDS', 0)
    await db_events.connect_to_db(app)
    assert len(attempts) == 3
    assert app.db_engine is db_events.engine

@pytest.mark.asyncio
async def test_connect_to_db_fails_fast_on_bad_credentials(monkeypatch):
    import src.core.events.database as db_events
    app = SimpleNamespace()
    attempts = []

    async def bad_password():
        attempts.append(1)
        raise RuntimeError('password authentication failed for user "postgres"')
    monkeypatch.setattr(db_events, '_bootstrap_schema', bad_password)
    with pytest.raises(RuntimeError):
        await db_events.connect_to_db(app)
    assert len(attempts) == 1
