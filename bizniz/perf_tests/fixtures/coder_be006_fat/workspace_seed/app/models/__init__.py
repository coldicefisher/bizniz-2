"""Auto-discover every model module so SQLAlchemy registers all
tables before the engine starts using ``Base.metadata``.

Drop a new file ``app/models/<feature>.py`` defining ORM classes
that inherit from ``app.db.base.Base``, and they're automatically
imported here — no edit to this file or any other shipped model
file is required. See SKELETON.md.
"""
import importlib
import pkgutil

for _mod_info in pkgutil.iter_modules(__path__):
    if _mod_info.name.startswith("_"):
        continue
    importlib.import_module(f"{__name__}.{_mod_info.name}")
