"""The orchestration seam: files in, review workbook out, model out.

Everything below `fsa.job` already had unit tests. What went untested until now
was that the pieces actually compose -- and composing them is where the real
failures live: a path that moves between commands, an LLM key that is absent, a
review workbook whose accounts no longer match the ingest.

The pure-plumbing tests run everywhere. The two end-to-end ones are marked
`sample` (real client data) and skip cleanly without it or without Excel.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fsa.env import load_dotenv
from fsa.job import (
    READABLE,
    JobError,
    JobState,
    resolve_inputs,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_S1 = _PROJECT_ROOT / "Konrad Project" / "sample_1_TS"
_S2 = _PROJECT_ROOT / "Konrad Project" / "sample_2_flooring"
_TEMPLATE = _S1 / "BVAL Model (Weaver Template).xlsx"


# --------------------------------------------------------------------------
# resolve_inputs
# --------------------------------------------------------------------------


def test_resolve_inputs_expands_a_directory(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x")
    (tmp_path / "b.xlsx").write_bytes(b"x")
    found = resolve_inputs([str(tmp_path)])
    assert {p.name for p in found} == {"a.pdf", "b.xlsx"}


def test_resolve_inputs_ignores_unreadable_extensions(tmp_path):
    """Clients send read-me's and logos; those are not an error condition."""
    (tmp_path / "a.pdf").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("hi")
    (tmp_path / "logo.png").write_bytes(b"x")
    assert [p.name for p in resolve_inputs([str(tmp_path)])] == ["a.pdf"]


def test_resolve_inputs_skips_excel_lock_files(tmp_path):
    """`~$foo.xlsx` appears whenever the client has the workbook open."""
    (tmp_path / "real.xlsx").write_bytes(b"x")
    (tmp_path / "~$real.xlsx").write_bytes(b"x")
    assert [p.name for p in resolve_inputs([str(tmp_path)])] == ["real.xlsx"]


def test_resolve_inputs_honours_excludes(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"x")
    (tmp_path / "b.pdf").write_bytes(b"x")
    found = resolve_inputs([str(tmp_path)], ["b.pdf"])
    assert [p.name for p in found] == ["a.pdf"]


def test_resolve_inputs_raises_when_nothing_readable(tmp_path):
    (tmp_path / "notes.txt").write_text("hi")
    with pytest.raises(JobError, match="no readable files"):
        resolve_inputs([str(tmp_path)])


def test_resolve_inputs_raises_on_missing_path(tmp_path):
    with pytest.raises(JobError, match="not found"):
        resolve_inputs([str(tmp_path / "nope.pdf")])


def test_resolve_inputs_accepts_explicit_files(tmp_path):
    f = tmp_path / "a.ods"
    f.write_bytes(b"x")
    assert resolve_inputs([str(f)]) == [f]


def test_readable_covers_every_format_the_reader_seam_dispatches():
    assert {".pdf", ".xlsx", ".ods"} <= READABLE


# --------------------------------------------------------------------------
# JobState
# --------------------------------------------------------------------------


def test_job_state_round_trips(tmp_path):
    state = JobState(
        client_name="Acme",
        template="T.xlsx",
        inputs=["a.pdf"],
        used_llm=True,
        created="2026-01-01",
    )
    state.save(tmp_path)
    back = JobState.load(tmp_path)
    assert back == state


def test_job_state_is_readable_json(tmp_path):
    """A job directory should be inspectable without running the tool."""
    JobState(client_name="Acme", template="T.xlsx").save(tmp_path)
    data = json.loads((tmp_path / "job.json").read_text())
    assert data["client_name"] == "Acme"


def test_job_state_load_explains_a_non_job_directory(tmp_path):
    with pytest.raises(JobError, match="job directory"):
        JobState.load(tmp_path)


# --------------------------------------------------------------------------
# .env loading
# --------------------------------------------------------------------------


def test_load_dotenv_sets_missing_keys(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("FSA_TEST_KEY=abc123\n")
    monkeypatch.delenv("FSA_TEST_KEY", raising=False)
    assert load_dotenv(env) == ["FSA_TEST_KEY"]
    import os

    assert os.environ["FSA_TEST_KEY"] == "abc123"


def test_load_dotenv_never_overrides_the_real_environment(tmp_path, monkeypatch):
    """`.env` is a default. A key set in the analyst's own shell must win."""
    env = tmp_path / ".env"
    env.write_text("FSA_TEST_KEY=from_file\n")
    monkeypatch.setenv("FSA_TEST_KEY", "from_shell")
    assert load_dotenv(env) == []
    import os

    assert os.environ["FSA_TEST_KEY"] == "from_shell"


def test_load_dotenv_strips_quotes_and_ignores_comments(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# a comment\n\nFSA_Q="quoted value"\nFSA_S=\'single\'\nbad line\n')
    for k in ("FSA_Q", "FSA_S"):
        monkeypatch.delenv(k, raising=False)
    load_dotenv(env)
    import os

    assert os.environ["FSA_Q"] == "quoted value"
    assert os.environ["FSA_S"] == "single"


def test_load_dotenv_is_a_no_op_when_absent(tmp_path):
    assert load_dotenv(tmp_path / "nothing-here") == []


# --------------------------------------------------------------------------
# end to end -- real sample data
# --------------------------------------------------------------------------


def _excel_available() -> bool:
    try:
        import pythoncom
        import win32com.client as win32
    except ImportError:
        return False
    try:
        pythoncom.CoInitialize()
        app = win32.DispatchEx("Excel.Application")
        app.Quit()
        del app
        return True
    except Exception:
        return False
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


@pytest.mark.sample
def test_prepare_produces_a_complete_job_directory(tmp_path):
    """No LLM: proves the deterministic path composes and degrades cleanly."""
    from fsa.job import PROPOSED_FILE, REVIEW_FILE, prepare

    if not (_S2 / "orig_pdfs").exists() or not _TEMPLATE.exists():
        pytest.skip("sample data not present")

    res = prepare(
        [str(_S2 / "orig_pdfs")],
        _TEMPLATE,
        tmp_path / "job",
        client_name="Commercial Flooring, Inc.",
        use_llm=False,
    )

    assert (tmp_path / "job" / REVIEW_FILE).exists()
    assert (tmp_path / "job" / PROPOSED_FILE).exists()
    assert (tmp_path / "job" / "job.json").exists()
    assert (tmp_path / "job" / "findings.json").exists()
    # Both statements read, and the review sheet has a row per account.
    assert set(res.tables) == {
        __import__("fsa.model.schema", fromlist=["StatementType"]).StatementType.BS,
        __import__("fsa.model.schema", fromlist=["StatementType"]).StatementType.IS,
    }
    assert res.n_accounts > 50


@pytest.mark.sample
def test_prepare_survives_a_missing_api_key(tmp_path, monkeypatch):
    """A firm laptop without a key must still get the deterministic mapping."""
    from fsa.job import prepare

    if not (_S2 / "orig_pdfs").exists() or not _TEMPLATE.exists():
        pytest.skip("sample data not present")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    res = prepare(
        [str(_S2 / "orig_pdfs")],
        _TEMPLATE,
        tmp_path / "job",
        use_llm=True,  # asked for, unavailable
    )
    assert (res.job_dir / "review.xlsx").exists()
    assert any(f.code == "llm_unavailable" for f in res.report.findings)


@pytest.mark.sample
def test_build_writes_a_model_from_a_reviewed_job(tmp_path):
    """The whole loop: prepare -> (analyst accepts) -> build -> .xlsx on disk."""
    from fsa.job import build, prepare

    if not (_S2 / "orig_pdfs").exists() or not _TEMPLATE.exists():
        pytest.skip("sample data not present")
    if not _excel_available():
        pytest.skip("Excel COM not available")

    prepare(
        [str(_S2 / "orig_pdfs")],
        _TEMPLATE,
        tmp_path / "job",
        client_name="Commercial Flooring, Inc.",
        use_llm=False,
    )
    res = build(
        tmp_path / "job",
        tmp_path / "model.xlsx",
        save_profile_to=tmp_path / "profile.yaml",
    )
    assert res.out_path.exists()
    assert res.out_path.stat().st_size > 0
    # The profile is the whole point of doing this again next year.
    assert res.profile_path is not None and res.profile_path.exists()

    from fsa.mapping.profile import load_profile

    saved = load_profile(res.profile_path)
    assert saved.client_name == "Commercial Flooring, Inc."


@pytest.mark.sample
def test_build_refuses_when_a_client_file_has_moved(tmp_path):
    """Re-ingesting from recorded paths is only safe if we check they exist."""
    from fsa.job import build, prepare

    if not (_S2 / "orig_pdfs").exists() or not _TEMPLATE.exists():
        pytest.skip("sample data not present")

    prepare([str(_S2 / "orig_pdfs")], _TEMPLATE, tmp_path / "job", use_llm=False)

    state = JobState.load(tmp_path / "job")
    state.inputs = [str(tmp_path / "gone.pdf")]
    state.save(tmp_path / "job")

    with pytest.raises(JobError, match="moved or been deleted"):
        build(tmp_path / "job", tmp_path / "model.xlsx")


def test_build_refuses_a_job_directory_without_a_review(tmp_path):
    from fsa.job import build

    JobState(client_name="Acme", template=str(_TEMPLATE)).save(tmp_path)
    with pytest.raises(JobError, match="nothing to apply"):
        build(tmp_path, tmp_path / "model.xlsx")
