Test-only backend plugins for core tests. `backend/tests/conftest.py` adds this
directory to `BACKEND_PLUGIN_DIRS`, so core tests exercise core behaviour through
fake providers and presets and pass with every shipped vendor plugin removed.
