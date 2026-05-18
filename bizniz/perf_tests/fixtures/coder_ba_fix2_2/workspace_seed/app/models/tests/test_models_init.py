"""Verify ``app.models`` package init registers the Recipe model.

The shipped ``app/models/__init__.py`` auto-discovers every sibling
module via ``pkgutil.iter_modules`` so each ORM class is bound to
``Base.metadata`` at import time. This test asserts that contract
holds for Recipe specifically: importing the package alone (without
explicitly importing ``app.models.recipe``) must be enough to make
``recipes`` show up in ``Base.metadata.tables`` and for the Recipe
submodule to be reachable as an attribute of ``app.models``.

Without this, repository / route code that relies on metadata-driven
registration (Alembic autogenerate, ``create_all``, relationship
resolution) would silently miss the table.
"""
import importlib
import sys

# Tables that ``app.models``' auto-discovery is expected to register.
# Listed here so the reload fixture can evict them cleanly without
# leaving a half-registered metadata that breaks re-import with
# "Table already defined".
_AUTO_DISCOVERED_TABLES = ("users", "recipes")


def _reload_models_package():
    """Force a fresh ``app.models`` package import so auto-discovery runs.

    SQLAlchemy refuses to redefine a Table on the same MetaData, so
    every table the package would auto-discover MUST be removed from
    ``Base.metadata`` BEFORE we evict the modules from ``sys.modules``
    and re-import. Otherwise the second pass through ``user.py`` /
    ``recipe.py`` raises ``InvalidRequestError``.
    """
    from app.db.base import Base

    for name in _AUTO_DISCOVERED_TABLES:
        if name in Base.metadata.tables:
            Base.metadata.remove(Base.metadata.tables[name])

    for mod_name in list(sys.modules):
        if mod_name == "app.models" or mod_name.startswith("app.models."):
            del sys.modules[mod_name]

    return importlib.import_module("app.models")


def test_importing_package_registers_recipes_table():
    """Importing ``app.models`` must make ``recipes`` appear in metadata."""
    from app.db.base import Base

    _reload_models_package()

    assert "recipes" in Base.metadata.tables, (
        "importing app.models should auto-discover recipe.py and "
        "register the Recipe model on Base.metadata"
    )


def test_recipe_submodule_reachable_from_package():
    """``app.models.recipe`` must be importable as a side effect of init."""
    models_pkg = _reload_models_package()

    # auto-discovery imports the submodule via importlib, which sets
    # it as an attribute on the parent package.
    assert hasattr(models_pkg, "recipe"), (
        "app.models should expose the ``recipe`` submodule after "
        "auto-discovery"
    )
    from app.models.recipe import Recipe

    assert Recipe.__tablename__ == "recipes"


def test_recipe_and_user_both_registered_together():
    """Both User and Recipe must register from a single package import.

    Guards against a regression where auto-discovery is replaced
    with a hand-maintained list that forgets one of the models.
    """
    from app.db.base import Base

    _reload_models_package()

    assert "users" in Base.metadata.tables
    assert "recipes" in Base.metadata.tables
