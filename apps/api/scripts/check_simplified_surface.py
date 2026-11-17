"""Check the reduced API contract without starting a server or changing data.

Run from apps/api: uv run python scripts/check_simplified_surface.py
"""
import os
import re
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0, str(root))

from app import app  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402
from src.db.users import User, UserReadPublic, UserUpdate  # noqa: E402
from src.db.roles import Rights  # noqa: E402
from src.db.organization_config import OrgFeatureConfig  # noqa: E402
from src.services.email.translations import SUPPORTED_UI_LANGUAGES  # noqa: E402

retired = {'podcast', 'podcastepisode', 'board', 'boardmember', 'playground',
           'playgroundreaction', 'folder', 'foldercontent', 'media', 'mediasharetoken',
           'courseupdate', 'orgpack', 'organization_plan_history', 'aigeneration', 'course_embedding', 'agent', 'messagereminder', 'mcp_oauth_client', 'mcp_oauth_grant', 'mcp_oauth_code', 'mcp_oauth_token'}
assert not retired.intersection(SQLModel.metadata.tables), retired.intersection(SQLModel.metadata.tables)
for model in (User, UserReadPublic, UserUpdate):
    assert not {'bio', 'details', 'profile'}.intersection(model.model_fields), model.__name__
assert not {'podcasts', 'boards', 'playgrounds', 'folders', 'media', 'payments'}.intersection(Rights.model_fields)
assert not {'podcasts', 'boards', 'playgrounds', 'folders', 'payments', 'collaboration'}.intersection(OrgFeatureConfig.model_fields)
paths = app.openapi()['paths']
for path in paths:
    assert not re.search(r'/(podcasts|boards|playgrounds|folders|media|calendar|payments|store|ai|mcp|agents|reminders)(/|$)', path), path
    assert not path.endswith('/updates') and '/update/{courseupdate_uuid}' not in path, path
assert '/api/v1/auth/login' in paths
assert '/api/v1/courses/{course_uuid}/meta' in paths
assert set(SUPPORTED_UI_LANGUAGES) == {'en', 'he', 'ar'}
print(f'PASS: {len(paths)} API paths, retained course/auth endpoints, retired models and profile fields absent; languages en/he/ar')
