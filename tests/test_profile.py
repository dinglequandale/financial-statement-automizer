"""Unit tests for fsa.mapping.profile: YAML round-trip of a ClientProfile."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pytest

from fsa.mapping.profile import PROFILE_VERSION, ProfileError, load_profile, save_profile
from fsa.model.mapping import (
    ClientProfile,
    Decider,
    Exclusion,
    MappingRule,
    MappingSet,
    Target,
    TargetKind,
)
from fsa.model.schema import StatementType


def _full_profile() -> ClientProfile:
    """A profile exercising every field, every Decider, every TargetKind, both signs."""
    profile = ClientProfile(
        client_name="TS Distributors, Inc.",
        template_source="Konrad Project/BVAL Model (Weaver Template).xlsx",
        created_at=date(2026, 1, 15),
        updated_at=date(2026, 8, 8),
        notes=["Seeded from FY2025 review.", "Second note, ASCII only."],
    )

    bs = profile.get(StatementType.BS)
    bs.rules.append(
        MappingRule(
            client_account="Petty Cash",
            norm_account="petty cash",
            target=Target(
                statement=StatementType.BS,
                label="Cash and Cash Equivalents",
                kind=TargetKind.ASSIGN,
                block="Total Current Assets",
            ),
            sign=1,
            confidence=1.0,
            decided_by=Decider.HUMAN,
            rationale="Confirmed by analyst in FY2025 review.",
            decided_at=datetime(2026, 8, 8, 14, 30, 0),
            decided_for_year=2025,
            verified=True,
        )
    )
    bs.rules.append(
        MappingRule(
            client_account="Accumulated Depreciation & Amort.",
            norm_account="accumulated depreciation and amortization",
            target=Target(
                statement=StatementType.BS,
                label="Accumulated Depreciation",
                kind=TargetKind.ASSIGN,
            ),
            sign=1,
            confidence=0.95,
            decided_by=Decider.ALIAS,
            rationale=None,
            decided_at=None,
            decided_for_year=None,
            verified=None,
        )
    )
    bs.rules.append(
        MappingRule(
            client_account="Revenue Component 1",
            norm_account="revenue component 1",
            target=Target(
                statement=StatementType.BS,
                label="Widget Sales",
                kind=TargetKind.NAME_SLOT,
                slot_label="Revenue Component 1",
            ),
            sign=1,
            confidence=0.8,
            decided_by=Decider.LLM,
            verified=False,
        )
    )
    bs.rules.append(
        MappingRule(
            client_account="Consulting Fees",
            norm_account="consulting fees",
            target=Target(
                statement=StatementType.BS,
                label="Consulting Revenue",
                kind=TargetKind.INSERT,
                block="Total Current Assets",
            ),
            sign=-1,
            confidence=0.6,
            decided_by=Decider.UNRESOLVED,
        )
    )
    bs.exclusions.append(
        Exclusion(
            client_account="Intercompany Elimination",
            norm_account="intercompany elimination",
            reason="Consolidation plug, not a client account.",
            decided_by=Decider.HUMAN,
            decided_at=datetime(2026, 8, 1, 9, 0, 0),
        )
    )

    is_set = profile.get(StatementType.IS)
    is_set.rules.append(
        MappingRule(
            client_account="Interest Expense",
            norm_account="interest expense",
            target=Target(
                statement=StatementType.IS,
                label="Interest (Expense)",
                kind=TargetKind.ASSIGN,
            ),
            sign=-1,
            confidence=1.0,
            decided_by=Decider.PROFILE,
            decided_for_year=2024,
        )
    )

    return profile


def test_round_trip_preserves_every_field(tmp_path: Path) -> None:
    profile = _full_profile()
    path = tmp_path / "ts_distributors.yaml"
    save_profile(profile, path)
    loaded = load_profile(path)

    assert loaded.client_name == profile.client_name
    assert loaded.template_source == profile.template_source
    assert loaded.created_at == profile.created_at
    assert loaded.updated_at == profile.updated_at
    assert loaded.notes == profile.notes

    for stmt in ("BS", "IS"):
        orig_set = profile.sets[stmt]
        new_set = loaded.sets[stmt]
        assert len(new_set.rules) == len(orig_set.rules)
        for orig_rule, new_rule in zip(orig_set.rules, new_set.rules):
            assert new_rule == orig_rule
        assert len(new_set.exclusions) == len(orig_set.exclusions)
        for orig_excl, new_excl in zip(orig_set.exclusions, new_set.exclusions):
            assert new_excl == orig_excl


def test_round_trip_preserves_sign_explicitly(tmp_path: Path) -> None:
    profile = _full_profile()
    path = tmp_path / "p.yaml"
    save_profile(profile, path)
    loaded = load_profile(path)

    is_rule = loaded.sets["IS"].rules[0]
    assert is_rule.sign == -1
    bs_rule = loaded.sets["BS"].rules[0]
    assert bs_rule.sign == 1


def test_saved_file_is_ascii_and_stable_key_order(tmp_path: Path) -> None:
    profile = _full_profile()
    path = tmp_path / "p.yaml"
    save_profile(profile, path)

    raw = path.read_bytes()
    raw.decode("ascii")  # must not raise

    text = path.read_text(encoding="ascii")
    assert text.startswith("version:")
    # BS must be written before IS regardless of dict insertion order.
    assert text.index("BS:") < text.index("IS:")


def test_save_creates_parent_directories(tmp_path: Path) -> None:
    profile = _full_profile()
    path = tmp_path / "nested" / "dir" / "profile.yaml"
    save_profile(profile, path)
    assert path.exists()


def test_load_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_profile(tmp_path / "does_not_exist.yaml")


def test_load_rejects_wrong_version(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(
        "version: 99\nclient_name: X\nstatements: {}\n", encoding="ascii"
    )
    with pytest.raises(ProfileError):
        load_profile(path)


def test_load_rejects_malformed_yaml(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="ascii")
    with pytest.raises(ProfileError):
        load_profile(path)


def test_load_rejects_missing_client_name(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(f"version: {PROFILE_VERSION}\nstatements: {{}}\n", encoding="ascii")
    with pytest.raises(ProfileError):
        load_profile(path)


def test_empty_profile_round_trips(tmp_path: Path) -> None:
    profile = ClientProfile(client_name="Empty Co")
    path = tmp_path / "empty.yaml"
    save_profile(profile, path)
    loaded = load_profile(path)
    assert loaded.client_name == "Empty Co"
    assert loaded.created_at is None
    assert loaded.notes == []


def test_profile_lookup_works_after_round_trip(tmp_path: Path) -> None:
    profile = _full_profile()
    path = tmp_path / "p.yaml"
    save_profile(profile, path)
    loaded = load_profile(path)

    hit = loaded.lookup(StatementType.BS, "petty cash")
    assert hit is not None
    assert hit.target.label == "Cash and Cash Equivalents"
    assert hit.decided_by is Decider.HUMAN
