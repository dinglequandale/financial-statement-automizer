"""Persist and reload a `ClientProfile` as human-readable, diffable YAML.

This is the cache that makes year 2 free (PLAN.md 7.1, L0): every decision --
human, profile-replayed, aliased, structurally inferred, fuzzy-matched, or
LLM-proposed -- gets written here once confirmed, and `matcher.propose()`
reads it back as the highest-trust layer next time.

Format notes:

  - Stable key order (never `sort_keys=True`) so a diff shows only what
    actually changed, not a reshuffle -- git-friendliness is the whole point
    of choosing YAML over a pickle or a database.
  - `allow_unicode=False` on write: this project's console is cp1252 and a
    stray em-dash or curly quote in a client's raw account label has already
    proven fatal elsewhere (see fsa/ingest/normalize.py's docstring), so the
    profile file itself stays pure ASCII, with any non-ASCII character
    backslash-escaped by PyYAML rather than written literally.
  - `version: 1` up front so a future format change has something to branch
    on instead of guessing from the shape of the file.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import yaml

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

PROFILE_VERSION = 1

# Statements are always written in this order regardless of dict insertion
# order, so two profiles for the same client diff cleanly.
_STATEMENT_ORDER = [s.value for s in StatementType]


class ProfileError(RuntimeError):
    pass


def _target_to_dict(t: Target) -> dict:
    return {
        "statement": t.statement.value,
        "label": t.label,
        "kind": t.kind.value,
        "block": t.block,
        "slot_label": t.slot_label,
    }


def _target_from_dict(d: dict) -> Target:
    try:
        return Target(
            statement=StatementType(d["statement"]),
            label=d["label"],
            kind=TargetKind(d.get("kind", "assign")),
            block=d.get("block"),
            slot_label=d.get("slot_label"),
        )
    except KeyError as exc:
        raise ProfileError(f"profile target missing required field: {exc}") from exc


def _rule_to_dict(r: MappingRule) -> dict:
    return {
        "client_account": r.client_account,
        "norm_account": r.norm_account,
        "target": _target_to_dict(r.target),
        "sign": r.sign,
        "confidence": r.confidence,
        "decided_by": r.decided_by.value,
        "rationale": r.rationale,
        "decided_at": r.decided_at,
        "decided_for_year": r.decided_for_year,
        "verified": r.verified,
    }


def _rule_from_dict(d: dict) -> MappingRule:
    try:
        return MappingRule(
            client_account=d["client_account"],
            norm_account=d["norm_account"],
            target=_target_from_dict(d["target"]),
            sign=int(d.get("sign", 1)),
            confidence=float(d.get("confidence", 1.0)),
            decided_by=Decider(d.get("decided_by", "unresolved")),
            rationale=d.get("rationale"),
            decided_at=_as_datetime(d.get("decided_at")),
            decided_for_year=d.get("decided_for_year"),
            verified=d.get("verified"),
        )
    except KeyError as exc:
        raise ProfileError(f"profile rule missing required field: {exc}") from exc


def _exclusion_to_dict(e: Exclusion) -> dict:
    return {
        "client_account": e.client_account,
        "norm_account": e.norm_account,
        "reason": e.reason,
        "decided_by": e.decided_by.value,
        "decided_at": e.decided_at,
    }


def _exclusion_from_dict(d: dict) -> Exclusion:
    try:
        return Exclusion(
            client_account=d["client_account"],
            norm_account=d["norm_account"],
            reason=d["reason"],
            decided_by=Decider(d.get("decided_by", "human")),
            decided_at=_as_datetime(d.get("decided_at")),
        )
    except KeyError as exc:
        raise ProfileError(f"profile exclusion missing required field: {exc}") from exc


def _as_datetime(value: object) -> datetime | None:
    """PyYAML parses a date-only scalar as `datetime.date`, not `datetime`.

    `MappingRule.decided_at` / `Exclusion.decided_at` are typed `datetime |
    None`, so promote a bare date to midnight rather than leaving a `date`
    object where a `datetime` is expected.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    raise ProfileError(f"cannot parse {value!r} as a date/datetime")


def _mapping_set_to_dict(ms: MappingSet) -> dict:
    return {
        "rules": [_rule_to_dict(r) for r in ms.rules],
        "exclusions": [_exclusion_to_dict(e) for e in ms.exclusions],
    }


def _mapping_set_from_dict(statement: StatementType, d: dict) -> MappingSet:
    return MappingSet(
        statement=statement,
        rules=[_rule_from_dict(x) for x in (d.get("rules") or [])],
        exclusions=[_exclusion_from_dict(x) for x in (d.get("exclusions") or [])],
    )


def _profile_to_dict(profile: ClientProfile) -> dict:
    statements: dict[str, dict] = {}
    for key in _STATEMENT_ORDER:
        if key in profile.sets:
            statements[key] = _mapping_set_to_dict(profile.sets[key])
    # Any statement key present in the profile but outside the known enum
    # (should not happen, but never silently drop data) goes last.
    for key, ms in profile.sets.items():
        if key not in statements:
            statements[key] = _mapping_set_to_dict(ms)

    return {
        "version": PROFILE_VERSION,
        "client_name": profile.client_name,
        "template_source": profile.template_source,
        "created_at": profile.created_at,
        "updated_at": profile.updated_at,
        "notes": list(profile.notes),
        "statements": statements,
    }


def save_profile(profile: ClientProfile, path: Path) -> None:
    """Write `profile` to `path` as stable-ordered, ASCII-only YAML."""
    path = Path(path)
    data = _profile_to_dict(profile)
    text = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,
        width=100,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="ascii")


def load_profile(path: Path) -> ClientProfile:
    """Read a `ClientProfile` back from `path`. Round-trips `save_profile()`."""
    path = Path(path)
    raw = path.read_text(encoding="ascii")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ProfileError(f"{path}: not a mapping profile (empty or malformed YAML)")

    version = data.get("version")
    if version != PROFILE_VERSION:
        raise ProfileError(
            f"{path}: unsupported profile version {version!r} "
            f"(this build understands version {PROFILE_VERSION})"
        )

    client_name = data.get("client_name")
    if not client_name:
        raise ProfileError(f"{path}: profile has no client_name")

    profile = ClientProfile(
        client_name=client_name,
        template_source=data.get("template_source"),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        notes=list(data.get("notes") or []),
    )

    for key, sdict in (data.get("statements") or {}).items():
        try:
            statement = StatementType(key)
        except ValueError as exc:
            raise ProfileError(f"{path}: unknown statement {key!r}") from exc
        profile.sets[key] = _mapping_set_from_dict(statement, sdict)

    return profile
