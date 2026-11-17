import logging
import importlib.util
import os
logger = logging.getLogger(__name__)

def is_ee_available():
    """
    Check if the Enterprise Edition code is present on disk and not disabled.

    NOTE: This does NOT indicate the deployment mode is 'ee'.
    SaaS deployments also ship with the EE folder present.
    Use get_deployment_mode() from src.core.deployment_mode to determine the actual mode.
    """
    if os.environ.get('LEARNHOUSE_DISABLE_EE') == '1':
        return False
    return os.path.isdir('ee') and os.path.isfile(os.path.join('ee', 'hooks.py'))

def get_ee_hooks():
    """Safely import and return the EE hooks module if available."""
    if not is_ee_available():
        return None
    try:
        spec = importlib.util.find_spec('ee.hooks')
        if spec is None:
            return None
        module = importlib.import_module('ee.hooks')
        return module
    except ImportError as e:
        logger.error(f'Failed to import EE hooks: {e}')
        return None
    except Exception as e:
        logger.error(f'Unexpected error loading EE hooks: {e}')
        return None

def register_ee_middlewares(app):
    """Call EE to register its middlewares."""
    from config.config import get_learnhouse_config
    if get_learnhouse_config().general_config.saas_mode:
        return
    hooks = get_ee_hooks()
    if hooks and hasattr(hooks, 'register_middlewares'):
        hooks.register_middlewares(app)

def register_ee_routers(v1_router):
    """Call EE to register its routers."""
    hooks = get_ee_hooks()
    if hooks and hasattr(hooks, 'register_routers'):
        hooks.register_routers(v1_router)

def run_ee_startup(app):
    """Call EE to run its startup tasks."""
    hooks = get_ee_hooks()
    if hooks and hasattr(hooks, 'on_startup'):
        hooks.on_startup(app)

async def run_ee_shutdown(app):
    """Let EE stop its background tasks and write what it still holds (e.g.
    queued analytics events). Never raises: shutdown carries on regardless."""
    hooks = get_ee_hooks()
    if hooks and hasattr(hooks, 'on_shutdown'):
        try:
            await hooks.on_shutdown(app)
        except Exception:
            logger.exception('EE shutdown hook failed')


def is_multi_org_allowed() -> bool:
    """Check if multi-org mode is allowed (requires EE or SaaS)."""
    from src.core.deployment_mode import get_deployment_mode
    mode = get_deployment_mode()
    return mode in ('ee', 'saas')

def notify_ee_course_content_changed(course_id: int) -> None:
    """Tell EE a course's content changed (title, activities, publication,
    assignment due dates) so LTI can update the LMS gradebook columns placed
    from it. Fire-and-forget, like notify_ee_learner_progress."""
    hooks = get_ee_hooks()
    if hooks and hasattr(hooks, 'on_course_content_changed'):
        try:
            hooks.on_course_content_changed(course_id)
        except Exception:
            logger.exception('EE course-content hook failed')

def notify_ee_learner_progress(user_id: int, course_id: int) -> None:
    """Tell EE a learner's progress in a course changed (e.g. so LTI can push
    the new score to the LMS gradebook). Fire-and-forget: EE schedules its own
    background work and failures never reach the caller."""
    hooks = get_ee_hooks()
    if hooks and hasattr(hooks, 'on_learner_progress'):
        try:
            hooks.on_learner_progress(user_id, course_id)
        except Exception:
            logger.exception('EE learner-progress hook failed')

async def track_ee_analytics_event(**event) -> None:
    """Hand an analytics event (the arguments of
    ``src.services.analytics.analytics.track``) to EE, which stores it. Org
    analytics is an EE feature: without EE this does nothing."""
    hooks = get_ee_hooks()
    hook = getattr(hooks, 'track_analytics_event', None) if hooks else None
    if hook is not None:
        await hook(**event)

async def get_ee_user_behavior(user_id: int, org_id: int, days: int) -> dict:
    """One member's behavioural analytics in an org (time per course, views,
    searches...), keyed by section, for the per-student audit. Comes from EE
    analytics; empty without EE."""
    hooks = get_ee_hooks()
    hook = getattr(hooks, 'user_analytics_behavior', None) if hooks else None
    if hook is None:
        return {}
    return await hook(user_id, org_id, days)
