"""Tests for the cross-process port reservation registry."""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bizniz.provisioner.port_reservation import (
    active_reservations,
    release_ports,
    reserve_ports,
)


@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Point the registry at a temp dir so each test starts fresh."""
    monkeypatch.setenv("BIZNIZ_PROJECTS_ROOT", str(tmp_path))
    yield tmp_path


class TestReserveAndRead:
    def test_active_reservations_empty_on_missing_file(self, isolated_registry):
        assert active_reservations() == {}

    def test_reserve_and_read_back(self, isolated_registry):
        reserve_ports("project_a", [8000, 8001, 9011])
        assert active_reservations() == {
            8000: "project_a",
            8001: "project_a",
            9011: "project_a",
        }

    def test_second_project_sees_first_project_ports(self, isolated_registry):
        reserve_ports("project_a", [8000, 9011])
        # Reading from a fresh "process" — just verifies the file is read.
        active = active_reservations()
        assert active == {8000: "project_a", 9011: "project_a"}

    def test_release_drops_reservations(self, isolated_registry):
        reserve_ports("project_a", [8000, 9011])
        release_ports("project_a")
        assert active_reservations() == {}

    def test_release_one_project_keeps_others(self, isolated_registry):
        reserve_ports("project_a", [8000])
        reserve_ports("project_b", [9000])
        release_ports("project_a")
        assert active_reservations() == {9000: "project_b"}


class TestConflictDetection:
    def test_reserve_conflicting_port_raises(self, isolated_registry):
        reserve_ports("project_a", [9011])
        with pytest.raises(ValueError, match="9011"):
            reserve_ports("project_b", [9011])

    def test_same_project_can_re_reserve_own_port(self, isolated_registry):
        reserve_ports("project_a", [8000, 9011])
        # Re-reserving for the same slug just refreshes — not a conflict.
        reserve_ports("project_a", [8000, 9011, 5432])
        assert set(active_reservations().keys()) == {8000, 9011, 5432}

    def test_conflict_does_not_partially_commit(self, isolated_registry):
        reserve_ports("project_a", [9011])
        with pytest.raises(ValueError):
            reserve_ports("project_b", [8000, 9011, 5432])
        # project_b's 8000 + 5432 must NOT have been written.
        assert active_reservations() == {9011: "project_a"}


class TestTTLExpiry:
    def test_expired_entries_pruned_on_read(self, isolated_registry, monkeypatch):
        # Reserve with a very short TTL.
        reserve_ports("project_a", [8000], ttl_s=0.05)
        time.sleep(0.1)
        assert active_reservations() == {}

    def test_expired_entries_dont_block_new_reservations(self, isolated_registry):
        reserve_ports("project_a", [9011], ttl_s=0.05)
        time.sleep(0.1)
        # project_b can now grab 9011 because project_a's TTL expired.
        reserve_ports("project_b", [9011])
        assert active_reservations() == {9011: "project_b"}

    def test_unparseable_entries_pruned_silently(self, isolated_registry, tmp_path):
        # Hand-write a malformed registry.
        registry = tmp_path / ".port_reservations.json"
        registry.write_text(json.dumps({
            "reservations": [
                {"port": 8000, "project_slug": "x"},  # no expires_at
                {"port": 9011, "project_slug": "y", "expires_at": "not-a-date"},
            ],
        }))
        # Both entries are unparseable → registry shows empty.
        assert active_reservations() == {}


class TestEmptyAndEdgeCases:
    def test_reserve_empty_list_is_noop(self, isolated_registry):
        reserve_ports("project_a", [])
        assert active_reservations() == {}

    def test_release_nonexistent_project_is_noop(self, isolated_registry):
        release_ports("never_reserved")
        assert active_reservations() == {}

    def test_duplicate_ports_in_input_dedupe(self, isolated_registry):
        reserve_ports("project_a", [8000, 8000, 8000])
        assert active_reservations() == {8000: "project_a"}
