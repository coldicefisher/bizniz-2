"""Distribution-name → import-name mapping for common packages where
the two differ.

This is the same data symbol_validator already uses (line 230-239 of
coder/symbol_validator.py); we duplicate here so the WorkspaceContext
can render an "import as" column without coupling to the validator.

Keep this list in sync with the validator's table when adding entries.
"""
from __future__ import annotations


# Python: distribution-name → import-name.
PYTHON_IMPORT_ALIASES = {
    "pyjwt": "jwt",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "python-dateutil": "dateutil",
    "python-dotenv": "dotenv",
    "python-jose": "jose",
    "python_jose": "jose",
    "beautifulsoup4": "bs4",
    "scikit-learn": "sklearn",
    "scikit_learn": "sklearn",
    "opencv-python": "cv2",
    "opencv_python": "cv2",
    "msgpack-python": "msgpack",
    "protobuf": "google.protobuf",
    "google-auth": "google.auth",
    "google-cloud-storage": "google.cloud.storage",
}


def python_import_for(distribution_name: str) -> str:
    """Return the import name for a distribution name, falling back
    to the distribution name itself when no alias is known."""
    norm = distribution_name.lower().replace("_", "-")
    if norm in PYTHON_IMPORT_ALIASES:
        return PYTHON_IMPORT_ALIASES[norm]
    # Try underscored variant.
    norm2 = norm.replace("-", "_")
    if norm2 in PYTHON_IMPORT_ALIASES:
        return PYTHON_IMPORT_ALIASES[norm2]
    # Default: same as distribution name (most common case).
    return distribution_name


# npm: usually package name IS the import name. A few exceptions:
NPM_IMPORT_ALIASES = {
    # @scope/name → @scope/name (no change). Most are 1:1.
}


def npm_import_for(distribution_name: str) -> str:
    """Return the import name for an npm package (usually the same)."""
    return NPM_IMPORT_ALIASES.get(distribution_name, distribution_name)
