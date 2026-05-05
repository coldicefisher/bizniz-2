"""Tests for the hallucination guard.

The guard's job is binary discrimination on egregious cases: a property-
manager test should NOT mention pet-grooming. Edge cases (mostly-
correct tests with one or two false-positive English words) should
not trip the guard.
"""
from bizniz.integration.hallucination_guard import validate_test_grounding


PROPERTY_MANAGER = """
Property management for landlords. Landlord can add properties
(address, units, description), assign tenants, record rent
payments, and respond to maintenance requests. Tenants log in to
see lease details and submit maintenance requests.
"""


def test_clean_property_manager_test_passes():
    test = """
import pytest, httpx

def test_landlord_creates_and_reads_property(client, landlord):
    r = client.post('/properties', json={'address': 'X', 'units': 5}, headers=landlord)
    assert r.status_code == 201
    prop_id = r.json()['id']
    r2 = client.get(f'/properties/{prop_id}', headers=landlord)
    assert r2.status_code == 200

def test_unauthenticated_cannot_list_properties(client):
    r = client.get('/properties')
    assert r.status_code == 401
"""
    report = validate_test_grounding(PROPERTY_MANAGER, test)
    assert report.ok, f"clean test was flagged: {report.suspicious}"


def test_grooming_contamination_caught():
    """The exact failure mode we saw: AI hallucinates pet-grooming
    domain into a property-manager test."""
    test = """
def test_unauthenticated_user_can_view_grooming_services(client):
    r = client.get('/services')
    assert r.status_code == 200

def test_book_an_appointment(client):
    r = client.post('/appointments', json={'service': 'haircut'})
    assert r.status_code == 201
"""
    report = validate_test_grounding(PROPERTY_MANAGER, test)
    assert not report.ok
    # The egregious confabulation tokens must surface. ``services`` is
    # NOT in this list — it's a generic framework folder name (every
    # FastAPI project has app/services/), and reliable bleed-through
    # detection comes from domain-specific words (grooming, haircut,
    # appointment) instead.
    flagged = set(report.suspicious)
    assert "grooming" in flagged
    assert "appointments" in flagged or "appointment" in flagged
    assert "haircut" in flagged


def test_corrective_message_is_actionable():
    # Need enough hallucinated tokens to exceed default threshold (2)
    test = """
def test_grooming_appointment_booking(): pass
def test_haircut_service_listing(): pass
"""
    report = validate_test_grounding(PROPERTY_MANAGER, test)
    msg = report.message()
    assert "grooming" in msg
    assert "Re-generate" in msg or "re-generate" in msg.lower()


def test_extra_allowed_suppresses_service_names():
    """Service/component names from the architecture pass through."""
    test = """
def test_widget_service_renders(): pass
"""
    # Without the allowlist, "widget" might be suspicious
    report = validate_test_grounding(PROPERTY_MANAGER, test, extra_allowed={"widget"})
    # "widget" is now allowed; other terms in this tiny test are generic
    assert "widget" not in report.suspicious


def test_camelcase_compound_split():
    """``landlordHeaders`` should be allowed when ``landlord`` is in the
    problem statement and ``headers`` is generic vocab."""
    test = """
def test_helper(landlordHeaders, tenantToken): pass
"""
    report = validate_test_grounding(PROPERTY_MANAGER, test)
    # landlord and tenant are in the problem; headers and token are generic
    assert "landlord" not in report.suspicious
    assert "tenant" not in report.suspicious


def test_snake_case_compound_split():
    test = """
def test_helper(landlord_headers, tenant_token): pass
"""
    report = validate_test_grounding(PROPERTY_MANAGER, test)
    assert "landlord" not in report.suspicious
    assert "tenant" not in report.suspicious

