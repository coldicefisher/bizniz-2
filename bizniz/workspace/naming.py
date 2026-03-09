"""
Workspace naming utilities.

Converts human-readable names like "Fraydit Solutions" or "Dog Breeder App"
into workspace-friendly slugs like "fraydit_solutions" or "dog_breeder_app".
"""
import re
import unicodedata


def slugify(name: str) -> str:
    """Convert a human-readable name to a workspace-friendly slug.

    Examples:
        slugify("Fraydit Solutions") -> "fraydit_solutions"
        slugify("Dog Breeder App") -> "dog_breeder_app"
        slugify("My Cool Project!") -> "my_cool_project"
        slugify("cafe-systeme") -> "cafe_systeme"
    """
    # Normalize unicode (remove accents)
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    # Lowercase
    name = name.lower()
    # Replace hyphens and spaces with underscores
    name = re.sub(r"[-\s]+", "_", name)
    # Remove anything that isn't alphanumeric or underscore
    name = re.sub(r"[^a-z0-9_]", "", name)
    # Collapse multiple underscores
    name = re.sub(r"_+", "_", name)
    # Strip leading/trailing underscores
    name = name.strip("_")
    return name or "workspace"
