"""Tests for contract_guard.validate_form_field_contract.

The guard catches form-field drift between the AI's test submissions
and the backend's OpenAPI request body schemas. The motivating bug:
test fills `name="username"` but POST /auth/login expects `email`.
"""
from bizniz.integration.contract_guard import validate_form_field_contract


# Backend OpenAPI: login expects {email, password}, register expects
# {email, password, first_name}.
BACKEND_DOC = {
    "paths": {
        "/api/v1/auth/login": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "email": {"type": "string"},
                                    "password": {"type": "string"},
                                },
                            }
                        }
                    }
                }
            }
        },
        "/api/v1/auth/register": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "email": {"type": "string"},
                                    "password": {"type": "string"},
                                    "first_name": {"type": "string"},
                                },
                            }
                        }
                    }
                }
            }
        },
    }
}
CONTRACTS = {"backend": BACKEND_DOC}


def test_correct_field_names_pass():
    test = """
await page.fill('input[name="email"]', 'a@b.com');
await page.fill('input[name="password"]', 'pw');
"""
    r = validate_form_field_contract(test, CONTRACTS)
    assert r.ok


def test_username_drift_caught():
    """The exact M1 bug: AI uses `username` when backend wants `email`."""
    test = """
await page.fill('input[name="username"]', 'a@b.com');
await page.fill('input[name="password"]', 'pw');
"""
    r = validate_form_field_contract(test, CONTRACTS)
    assert not r.ok
    assert "username" in r.drifted


def test_corrective_message_suggests_alternative():
    test = """
await page.fill('input[name="user_email"]', 'a@b.com');
"""
    r = validate_form_field_contract(test, CONTRACTS)
    msg = r.message()
    assert "user_email" in msg
    # Should suggest "email" since "email" is a substring of "user_email"
    assert "email" in msg


def test_ui_only_fields_pass():
    test = """
await page.fill('input[name="confirm_password"]', 'pw');
await page.fill('input[name="remember_me"]', 'true');
"""
    r = validate_form_field_contract(test, CONTRACTS)
    assert r.ok


def test_no_contracts_means_ok():
    test = """
await page.fill('input[name="anything"]', 'value');
"""
    r = validate_form_field_contract(test, {})
    assert r.ok


def test_camelcase_selector_matches():
    test = """
await page.locator('input[name="firstName"]').fill('Alice');
"""
    # firstName isn't in our schema (we have first_name), so should drift
    r = validate_form_field_contract(test, CONTRACTS)
    assert not r.ok
    assert "firstname" in r.drifted


def test_resolves_schema_refs():
    """If the schema uses $ref, the guard should still find the field
    names by following the reference."""
    doc_with_ref = {
        "paths": {
            "/api/v1/auth/login": {
                "post": {
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/LoginRequest"}
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "LoginRequest": {
                    "type": "object",
                    "properties": {
                        "email": {"type": "string"},
                        "password": {"type": "string"},
                    },
                }
            }
        },
    }
    test = """
await page.fill('input[name="email"]', 'a@b.com');
"""
    r = validate_form_field_contract(test, {"backend": doc_with_ref})
    assert r.ok
    # Sanity: a wrong name still drifts
    test_bad = """
await page.fill('input[name="username"]', 'a@b.com');
"""
    r2 = validate_form_field_contract(test_bad, {"backend": doc_with_ref})
    assert not r2.ok
