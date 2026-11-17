"""Backend plugins shipped with the project (one subpackage per plugin).

Each subpackage defines ``register() -> backend.pluginhost.BackendPlugin``; the
plugin host imports them at startup. Core never imports a module of this package
directly, so any plugin here can be removed or moved to an external plugin
directory (``BACKEND_PLUGIN_DIRS``). See docs/backend-plugins.md.
"""
