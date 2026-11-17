"""Migrate and bootstrap once, before the API rollout."""
import asyncio
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
os.chdir(root)
sys.path.insert(0, str(root))
from src.core.migrations import ensure_schema  # noqa: E402

ensure_schema()
# The bootstrap Job owns installation; serving replicas have it disabled.
os.environ["LEARNHOUSE_AUTO_INSTALL"] = "true"
from src.core.events.autoinstall import auto_install  # noqa: E402
asyncio.run(auto_install())
