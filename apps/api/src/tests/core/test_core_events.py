import logging
from types import SimpleNamespace
import pytest
import src.core.ee_hooks as ee_hooks
import src.core.events.autoinstall as autoinstall
import src.core.events.content as content
import src.core.events.logs as logs

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

class _FakeAsyncSession:
    """Stands in for a session from the application-wide session factory."""

    def __init__(self, row, opened, error=None):
        self.row = row
        self.error = error
        self._opened = opened

    async def execute(self, statement):
        if self.error is not None:
            raise self.error
        return _FakeResult(self.row)

    async def __aenter__(self):
        self._opened.append(self)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

def _patch_session_factory(monkeypatch, row, error=None):
    opened = []
    monkeypatch.setattr(autoinstall, '_async_session_factory', lambda: _FakeAsyncSession(row, opened, error))
    return opened

async def test_auto_install_triggers_install_when_no_orgs_exist(monkeypatch):
    installs = []
    refreshes = []

    async def fake_install_async(short=False):
        installs.append(short)
    _patch_session_factory(monkeypatch, None)
    monkeypatch.setattr(autoinstall, '_install_async', fake_install_async)
    monkeypatch.setattr(autoinstall, 'install_default_elements', lambda db: refreshes.append(db))
    await autoinstall.auto_install()
    assert installs == [True]
    assert refreshes == []

async def test_auto_install_refreshes_default_roles_when_any_org_exists(monkeypatch):
    installs = []
    refreshes = []

    async def fake_install_async(short=False):
        installs.append(short)

    async def fake_install_default_elements(db):
        refreshes.append(db)
    opened = _patch_session_factory(monkeypatch, object())
    monkeypatch.setattr(autoinstall, '_install_async', fake_install_async)
    monkeypatch.setattr(autoinstall, 'install_default_elements', fake_install_default_elements)
    await autoinstall.auto_install()
    assert installs == []
    assert len(refreshes) == 1
    assert len(opened) == 2

@pytest.mark.asyncio
async def test_check_content_directory_creates_directory_when_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(content.os.path, 'exists', lambda path: False)
    monkeypatch.setattr(content.os, 'makedirs', lambda path: calls.append(path))
    await content.check_content_directory()
    assert calls == ['content']

@pytest.mark.asyncio
async def test_check_content_directory_skips_existing_directory(monkeypatch):
    calls = []
    monkeypatch.setattr(content.os.path, 'exists', lambda path: True)
    monkeypatch.setattr(content.os, 'makedirs', lambda path: calls.append(path))
    await content.check_content_directory()
    assert calls == []

@pytest.mark.asyncio
async def test_create_logs_dir_creates_missing_directory(monkeypatch):
    calls = []
    monkeypatch.setattr(logs.os.path, 'exists', lambda path: False)
    monkeypatch.setattr(logs.os, 'mkdir', lambda path: calls.append(path))
    await logs.create_logs_dir()
    assert calls == ['logs']

@pytest.mark.asyncio
def test_init_logging_attaches_a_stdout_handler():
    """Until this runs the root logger has no handler, so Python's fallback
    drops everything below WARNING and every logger.info in the codebase is
    invisible in production."""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        root.handlers = []
        logs.init_logging()
        ours = [h for h in root.handlers if getattr(h, '_learnhouse', False)]
        assert len(ours) == 1
        assert isinstance(ours[0], logging.StreamHandler)
        assert root.level == logging.INFO
    finally:
        root.handlers = before

def test_init_logging_is_idempotent():
    """Startup can run more than once in a process; a second call must not
    double every log line."""
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        root.handlers = []
        logs.init_logging()
        logs.init_logging()
        assert len([h for h in root.handlers if getattr(h, '_learnhouse', False)]) == 1
    finally:
        root.handlers = before

def test_init_logging_honours_an_explicit_level(monkeypatch):
    monkeypatch.setenv('LEARNHOUSE_LOG_LEVEL', 'WARNING')
    root = logging.getLogger()
    before, before_level = (list(root.handlers), root.level)
    try:
        root.handlers = []
        logs.init_logging()
        assert root.level == logging.WARNING
    finally:
        root.handlers, root.level = (before, before_level)

def test_init_logging_leaves_uvicorn_handlers_alone():
    """basicConfig(force=True) would tear these down and take the access log
    with it."""
    root = logging.getLogger()
    before = list(root.handlers)
    sentinel = logging.NullHandler()
    try:
        root.handlers = [sentinel]
        logs.init_logging()
        assert sentinel in root.handlers
    finally:
        root.handlers = before

def test_is_ee_available(monkeypatch):
    monkeypatch.setenv('LEARNHOUSE_DISABLE_EE', '1')
    assert ee_hooks.is_ee_available() is False
    monkeypatch.delenv('LEARNHOUSE_DISABLE_EE', raising=False)
    monkeypatch.setattr(ee_hooks.os.path, 'isdir', lambda path: True)
    monkeypatch.setattr(ee_hooks.os.path, 'isfile', lambda path: True)
    assert ee_hooks.is_ee_available() is True
    monkeypatch.setattr(ee_hooks.os.path, 'isfile', lambda path: False)
    assert ee_hooks.is_ee_available() is False
    monkeypatch.setattr(ee_hooks.os.path, 'isdir', lambda path: False)
    assert ee_hooks.is_ee_available() is False

def test_get_ee_hooks_handles_missing_module_and_import_errors(monkeypatch, caplog):
    monkeypatch.setattr(ee_hooks, 'is_ee_available', lambda: False)
    assert ee_hooks.get_ee_hooks() is None
    monkeypatch.setattr(ee_hooks, 'is_ee_available', lambda: True)
    monkeypatch.setattr(ee_hooks.importlib.util, 'find_spec', lambda name: None)
    assert ee_hooks.get_ee_hooks() is None

    class _BoomImport:

        def __call__(self, name):
            raise ImportError('boom')
    monkeypatch.setattr(ee_hooks.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setattr(ee_hooks.importlib, 'import_module', _BoomImport())
    with caplog.at_level(logging.ERROR):
        assert ee_hooks.get_ee_hooks() is None
    assert 'Failed to import EE hooks' in caplog.text

def test_get_ee_hooks_returns_module_when_available(monkeypatch):
    sentinel = SimpleNamespace(name='hooks')
    monkeypatch.setattr(ee_hooks, 'is_ee_available', lambda: True)
    monkeypatch.setattr(ee_hooks.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setattr(ee_hooks.importlib, 'import_module', lambda name: sentinel)
    assert ee_hooks.get_ee_hooks() is sentinel

def test_get_ee_hooks_handles_unexpected_errors(monkeypatch, caplog):
    monkeypatch.setattr(ee_hooks, 'is_ee_available', lambda: True)
    monkeypatch.setattr(ee_hooks.importlib.util, 'find_spec', lambda name: object())
    monkeypatch.setattr(ee_hooks.importlib, 'import_module', lambda name: (_ for _ in ()).throw(RuntimeError('boom')))
    with caplog.at_level(logging.ERROR):
        assert ee_hooks.get_ee_hooks() is None
    assert 'Unexpected error loading EE hooks' in caplog.text

def test_register_ee_helpers_and_startup(monkeypatch):
    calls = []
    hooks = SimpleNamespace(register_middlewares=lambda app: calls.append(('middlewares', app)), register_routers=lambda router: calls.append(('routers', router)), on_startup=lambda app: calls.append(('startup', app)))
    monkeypatch.setattr(ee_hooks, 'get_ee_hooks', lambda: hooks)
    app = object()
    router = object()
    ee_hooks.register_ee_middlewares(app)
    ee_hooks.register_ee_routers(router)
    ee_hooks.run_ee_startup(app)
    assert calls == [('middlewares', app), ('routers', router), ('startup', app)]

def test_register_ee_helpers_skip_when_hook_missing(monkeypatch):
    monkeypatch.setattr(ee_hooks, 'get_ee_hooks', lambda: SimpleNamespace())
    ee_hooks.register_ee_middlewares(object())
    ee_hooks.register_ee_routers(object())
    ee_hooks.run_ee_startup(object())

def test_is_multi_org_allowed(monkeypatch):
    monkeypatch.setattr('src.core.deployment_mode.get_deployment_mode', lambda: 'ee')
    assert ee_hooks.is_multi_org_allowed() is True
    monkeypatch.setattr('src.core.deployment_mode.get_deployment_mode', lambda: 'local')
    assert ee_hooks.is_multi_org_allowed() is False
