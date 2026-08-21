"""The window builds, and the pieces around it behave.

A GUI cannot be meaningfully unit tested, but the failure that actually ships
is the window not opening at all -- a typo in a widget option, a missing style,
a bad grid call. Constructing every widget catches that, and it costs a second.

`mainloop` is replaced so the test does not block; everything before it is the
part that can be wrong.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fsa import gui


def test_job_directory_is_derived_not_asked_for():
    """An analyst should never have to invent a working directory."""
    job = gui.Job(client="Acme Flooring, Inc.", inputs="x", template="y",
                  profile="", scope="")
    assert job.directory.parent.name == "Acme Flooring Inc."
    assert job.directory.parent.parent == gui.jobs_root()


def test_an_unnamed_client_still_gets_somewhere_to_put_things():
    job = gui.Job(client="", inputs="x", template="y", profile="", scope="")
    assert job.directory.parent.name == "Unnamed client"


def test_a_client_name_of_pure_punctuation_does_not_escape_the_jobs_root():
    job = gui.Job(client="../../etc", inputs="x", template="y", profile="", scope="")
    assert gui.jobs_root() in job.directory.parents


def test_settings_round_trip_and_survive_a_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(gui, "SETTINGS", tmp_path / "nope.json")
    assert gui.load_settings() == {}
    gui.save_settings({"template": "C:/x.xlsx"})
    assert gui.load_settings()["template"] == "C:/x.xlsx"


def test_unwritable_settings_are_not_an_error(monkeypatch, tmp_path):
    """Losing a remembered path is a nuisance; a crash on startup is not."""
    monkeypatch.setattr(gui, "SETTINGS", tmp_path / "no-such-dir" / "s.json")
    gui.save_settings({"a": "b"})  # must not raise


def test_corrupt_settings_are_ignored(tmp_path, monkeypatch):
    bad = tmp_path / "s.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(gui, "SETTINGS", bad)
    assert gui.load_settings() == {}


def test_the_window_builds_every_widget():
    tk = pytest.importorskip("tkinter")
    try:
        probe = tk.Tk()
        probe.destroy()
    except tk.TclError:
        pytest.skip("no display available")

    built = {}
    real_mainloop = tk.Tk.mainloop

    def stop_immediately(self):
        built["ok"] = True
        self.destroy()

    tk.Tk.mainloop = stop_immediately
    try:
        assert gui.run_gui() == 0
    finally:
        tk.Tk.mainloop = real_mainloop
    assert built.get("ok"), "run_gui returned without ever reaching mainloop"
