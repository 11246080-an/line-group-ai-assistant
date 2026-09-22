"""Compatibility wrapper for the process-local feature database.

Some feature flows import ``in_memory_feature_db`` directly when
``USE_IN_MEMORY_FEATURE_DB=true``.  The implementation lives under
``database/`` with the rest of the database handoff files, so this wrapper keeps
both import paths working regardless of the Flask working directory.
"""

from database.in_memory_feature_db import *  # noqa: F401,F403
