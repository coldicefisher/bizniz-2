import pytest
from bizniz.workspace.naming import slugify

def test_basic_slugify():
    assert slugify("Fraydit Solutions") == "fraydit_solutions"

def test_slugify_with_special_chars():
    assert slugify("My Cool Project!") == "my_cool_project"

def test_slugify_with_hyphens():
    assert slugify("dog-breeder-app") == "dog_breeder_app"

def test_slugify_unicode():
    assert slugify("cafe systeme") == "cafe_systeme"

def test_slugify_empty():
    assert slugify("") == "workspace"
    assert slugify("!!!") == "workspace"

def test_slugify_already_clean():
    assert slugify("my_project") == "my_project"
