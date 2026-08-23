"""A window, for the person this tool is actually for.

Everything else in this project is built for someone who can type a quoted
path. The analyst it exists to help is a valuation professional, and a command
line is not a reasonable thing to put between them and their work.

Three deliberate choices:

* **The job directory is not a question.** It goes under Documents, named after
  the client, and both buttons that need it find it themselves. Asking an
  analyst to invent a working directory is asking them to do bookkeeping for
  the software.

* **Findings arrive in English.** `fsa.explain` turns each code into what
  happened, why it matters and what to do next. A stopped run has to be
  actionable by the person who ran it, not by the person who wrote it.

* **The long work runs off the main thread**, so the window never greys out.
  Progress is posted back through a queue and drained on a timer, which is the
  ordinary Tk way of doing this and keeps every widget touch on one thread.

Built on tkinter because it ships with Python: no installer, no second
dependency, and nothing to go wrong on a machine that already runs the CLI.
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import date
from pathlib import Path

APP_NAME = "Financial Statement Automizer"
SETTINGS = Path.home() / ".fsa-settings.json"


def jobs_root() -> Path:
    docs = Path.home() / "Documents"
    return (docs if docs.is_dir() else Path.home()) / "BVAL Models"


def load_settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(d: dict) -> None:
    try:
        SETTINGS.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass  # a settings file we cannot write is not worth an error dialog


def reveal(path: Path) -> None:
    """Open a file or folder the way the operating system would."""
    try:
        if sys.platform == "win32":
            import os

            os.startfile(str(path))  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except Exception:
        pass


#: The small files that describe an engagement: what was read, what the tool
#: proposed, what the analyst decided, and every note raised along the way.
#: Deliberately not the finished model or the review workbook -- those are the
#: analyst's work product and are large; none of them is needed to learn how
#: the tool should have behaved.
FEEDBACK_FILES = (
    "job.json",
    "findings.json",
    "proposed.yaml",
    "decisions.yaml",
    "review_log.json",
)


def package_job(job_dir: Path, client: str) -> Path:
    """Zip an engagement's record so it can be sent back in one piece.

    The difference between `proposed.yaml` and `decisions.yaml` is the record
    of every correction the analyst made, and `review_log.json` adds how sure
    the tool had been about each one. Together they are what turns a real
    engagement into evidence; the point of a button is that nobody has to
    remember which five files that means.
    """
    import zipfile

    safe = "".join(c for c in client if c.isalnum() or c in " -_.").strip() or "job"
    out = job_dir.parent / f"{safe} - {job_dir.name} - feedback.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FEEDBACK_FILES:
            f = job_dir / name
            if f.is_file():
                z.write(f, arcname=name)
    return out


@dataclass
class Job:
    client: str
    inputs: str
    template: str
    profile: str
    scope: str
    #: Files the analyst unticked. Named rather than pruned from disk: the
    #: client's folder is theirs, and a run should never require editing it.
    excludes: tuple[str, ...] = ()

    @property
    def directory(self) -> Path:
        safe = "".join(c for c in self.client if c.isalnum() or c in " -_.").strip()
        return jobs_root() / (safe or "Unnamed client") / date.today().isoformat()


def run_gui() -> int:  # noqa: C901 - a form is a form
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    settings = load_settings()
    events: queue.Queue = queue.Queue()
    state: dict = {"job": None, "busy": False}

    root = tk.Tk()
    root.title(APP_NAME)
    root.geometry("880x680")
    root.minsize(760, 560)

    style = ttk.Style(root)
    try:
        style.theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    style.configure("Head.TLabel", font=("Segoe UI Semibold", 13))
    style.configure("Step.TLabel", font=("Segoe UI Semibold", 10))
    style.configure("Hint.TLabel", foreground="#5a6672")
    style.configure("Go.TButton", font=("Segoe UI Semibold", 10))

    outer = ttk.Frame(root, padding=16)
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="Turn a client's statements into a BVAL model",
              style="Head.TLabel").pack(anchor="w")
    ttk.Label(
        outer,
        text="Read the statements, correct the mapping in Excel, then write the model.",
        style="Hint.TLabel",
    ).pack(anchor="w", pady=(2, 12))

    form = ttk.LabelFrame(outer, text=" Step 1 — Read the statements ", padding=12)
    form.pack(fill="x")
    form.columnconfigure(1, weight=1)

    fields: dict[str, tk.StringVar] = {}

    def row(label: str, key: str, hint: str, picker=None, default: str = "") -> None:
        r = form.grid_size()[1]
        ttk.Label(form, text=label).grid(row=r, column=0, sticky="w", pady=(0, 2))
        var = tk.StringVar(value=settings.get(key, default))
        fields[key] = var
        entry = ttk.Entry(form, textvariable=var)
        entry.grid(row=r, column=1, sticky="ew", padx=(10, 6), pady=(0, 2))
        if picker:
            ttk.Button(form, text="Browse…", width=10,
                       command=lambda: picker(var)).grid(row=r, column=2, pady=(0, 2))
        ttk.Label(form, text=hint, style="Hint.TLabel").grid(
            row=r + 1, column=1, sticky="w", padx=(10, 0), pady=(0, 8)
        )

    def pick_folder(var: tk.StringVar) -> None:
        p = filedialog.askdirectory(title="Choose the folder holding the client's statements")
        if p:
            var.set(p)

    def pick_template(var: tk.StringVar) -> None:
        p = filedialog.askopenfilename(
            title="Choose the blank BVAL template",
            filetypes=[("Excel workbook", "*.xlsx *.xlsm"), ("All files", "*.*")],
        )
        if p:
            var.set(p)

    def pick_profile(var: tk.StringVar) -> None:
        p = filedialog.askopenfilename(
            title="Choose last year's saved decisions",
            filetypes=[("Saved decisions", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if p:
            var.set(p)

    row("Client", "client", "The name that appears on the model.")
    row("Statements folder", "inputs",
        "Everything the client sent. Anything that is not a statement is ignored.",
        pick_folder)
    row("BVAL template", "template",
        "The blank Weaver model. Remembered for next time.", pick_template)
    row("Last year's decisions", "profile",
        "Optional, and the single biggest time saver on a repeat client.",
        pick_profile)
    row("Reporting entity", "scope",
        "Only needed when one file holds several companies. Leave blank at first.")

    # Which files to read. A client folder routinely holds things that are not
    # the client's statements -- a prior year's working consolidation, a draft
    # schedule, a cover letter -- and two of them can claim the same year. The
    # analyst is the only one who knows which is authoritative, so show what was
    # found and let them untick it. Typing a filename would be worse.
    files_frame = ttk.LabelFrame(outer, text=" Files found — untick anything that is not the client's statements ",
                                 padding=(10, 8))
    files_frame.pack(fill="x", pady=(12, 0))
    files_canvas = tk.Canvas(files_frame, height=104, highlightthickness=0,
                             background=root.cget("background"))
    files_scroll = ttk.Scrollbar(files_frame, orient="vertical", command=files_canvas.yview)
    files_inner = ttk.Frame(files_canvas)
    files_inner.bind("<Configure>",
                     lambda e: files_canvas.configure(scrollregion=files_canvas.bbox("all")))
    files_canvas.create_window((0, 0), window=files_inner, anchor="nw")
    files_canvas.configure(yscrollcommand=files_scroll.set)
    files_canvas.pack(side="left", fill="both", expand=True)
    files_scroll.pack(side="right", fill="y")
    file_vars: dict[str, tk.BooleanVar] = {}

    def refresh_files(*_a) -> None:
        for child in files_inner.winfo_children():
            child.destroy()
        file_vars.clear()
        folder = Path(fields["inputs"].get().strip() or ".")
        if not folder.is_dir():
            ttk.Label(files_inner, text="Choose a folder above to see what is in it.",
                      style="Hint.TLabel").pack(anchor="w")
            return
        from fsa.job import READABLE

        found = sorted(
            f for f in folder.iterdir()
            if f.is_file() and f.suffix.lower() in READABLE and not f.name.startswith("~$")
        )
        if not found:
            ttk.Label(files_inner, text="No statements found in that folder.",
                      style="Hint.TLabel").pack(anchor="w")
            return
        for f in found:
            var = tk.BooleanVar(value=True)
            file_vars[f.name] = var
            size = f.stat().st_size / 1024
            ttk.Checkbutton(files_inner, variable=var,
                            text=f"{f.name}   ({size:,.0f} KB)").pack(anchor="w")

    fields["inputs"].trace_add("write", refresh_files)
    refresh_files()

    buttons = ttk.Frame(outer)
    buttons.pack(fill="x", pady=(12, 8))
    go = ttk.Button(buttons, text="Read the statements", style="Go.TButton")
    go.pack(side="left")
    progress = ttk.Progressbar(buttons, mode="indeterminate", length=190)

    log_frame = ttk.LabelFrame(outer, text=" What happened ", padding=(10, 8))
    log_frame.pack(fill="both", expand=True)
    text = tk.Text(log_frame, wrap="word", height=14, relief="flat",
                   font=("Consolas", 9), background="#fbfcfc", borderwidth=0)
    scroll = ttk.Scrollbar(log_frame, command=text.yview)
    text.configure(yscrollcommand=scroll.set, state="disabled")
    text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    text.tag_configure("stop", foreground="#a4262c", font=("Consolas", 9, "bold"))
    text.tag_configure("ok", foreground="#0b6a3a", font=("Consolas", 9, "bold"))
    text.tag_configure("head", font=("Consolas", 9, "bold"))

    finish = ttk.Frame(outer)
    finish.pack(fill="x", pady=(10, 0))
    ttk.Label(finish, text="Step 2 — correct column D in Excel, save and close, "
              "then write the model.", style="Hint.TLabel").pack(anchor="w")
    finish_buttons = ttk.Frame(finish)
    finish_buttons.pack(anchor="w", pady=(6, 0))
    open_review = ttk.Button(finish_buttons, text="Open review sheet", state="disabled")
    open_review.pack(side="left")
    build_btn = ttk.Button(finish_buttons, text="Write the model",
                           style="Go.TButton", state="disabled")
    build_btn.pack(side="left", padx=(8, 0))
    send_btn = ttk.Button(finish_buttons, text="Package this job to send",
                          state="disabled")
    send_btn.pack(side="left", padx=(8, 0))

    # ---------------------------------------------------------------- output

    def say(line: str = "", tag: str | None = None) -> None:
        text.configure(state="normal")
        text.insert("end", line + "\n", tag or ())
        text.see("end")
        text.configure(state="disabled")

    def clear() -> None:
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.configure(state="disabled")

    def busy(on: bool) -> None:
        state["busy"] = on
        go.configure(state="disabled" if on else "normal")
        build_btn.configure(state="disabled" if on else build_btn.cget("state"))
        if on:
            progress.pack(side="left", padx=(12, 0))
            progress.start(12)
        else:
            progress.stop()
            progress.pack_forget()

    # ---------------------------------------------------------------- work

    def prepare_worker(job: Job) -> None:
        try:
            from fsa.explain import render_text, summarize
            from fsa.job import JobError, prepare

            job.directory.mkdir(parents=True, exist_ok=True)
            res = prepare(
                [job.inputs],
                Path(job.template),
                job.directory,
                client_name=job.client,
                profile_path=Path(job.profile) if job.profile else None,
                use_llm=True,
                scope=job.scope or None,
                excludes=list(job.excludes),
            )
            summary = summarize(res.report)
            lines = []
            for st, table in res.tables.items():
                lines.append(
                    f"  {st.value}: {len(table.rows)} accounts, years "
                    f"{', '.join(str(y) for y in table.years)}"
                )
            detail = (
                f"  {res.n_accounts} rows to review: {res.n_proposed} already filled in, "
                f"{res.n_excluded} excluded, "
                f"{res.n_unresolved - res.n_unresolved_subtotal} still need you"
            )
            events.put(("prepared", (summary, "\n".join(lines), detail,
                                     res.review_path, render_text(summary))))
        except Exception as exc:  # noqa: BLE001
            from fsa.job import JobError

            friendly = str(exc) if isinstance(exc, JobError) else None
            events.put(("failed", (friendly, traceback.format_exc())))

    def build_worker(job: Job) -> None:
        try:
            from fsa.job import build

            out = job.directory / f"{job.client or 'Client'} BVAL Model.xlsx"
            res = build(job.directory, out, save_profile_to=job.directory / "decisions.yaml",
                        overwrite=True)
            events.put(("built", (res, out)))
        except Exception as exc:  # noqa: BLE001
            from fsa.job import JobError

            friendly = str(exc) if isinstance(exc, JobError) else None
            events.put(("failed", (friendly, traceback.format_exc())))

    def start_prepare() -> None:
        job = Job(
            **{k: fields[k].get().strip() for k in
               ("client", "inputs", "template", "profile", "scope")},
            excludes=tuple(n for n, v in file_vars.items() if not v.get()),
        )
        if not job.inputs or not Path(job.inputs).is_dir():
            messagebox.showwarning(APP_NAME, "Choose the folder holding the client's statements.")
            return
        if not job.template or not Path(job.template).is_file():
            messagebox.showwarning(APP_NAME, "Choose the blank BVAL template.")
            return
        if not job.client:
            messagebox.showwarning(APP_NAME, "Enter the client's name — it goes on the model.")
            return

        save_settings({k: v.get() for k, v in fields.items()})
        state["job"] = job
        open_review.configure(state="disabled")
        build_btn.configure(state="disabled")
        clear()
        say("Reading the statements. This takes under a minute.")
        busy(True)
        threading.Thread(target=prepare_worker, args=(job,), daemon=True).start()

    def package() -> None:
        job = state["job"]
        if job is None:
            return
        try:
            out = package_job(job.directory, job.client)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning(APP_NAME, "Could not package the job: " + str(exc))
            return
        say()
        say(f"Packaged for sending: {out}", "ok")
        say("    It holds what was read, what the tool suggested, what you")
        say("    changed, and the notes raised along the way. Not the model.")
        reveal(out.parent)

    def start_build() -> None:
        job = state["job"]
        if job is None:
            return
        clear()
        say("Writing the model through Excel. Do not touch Excel while this runs.")
        busy(True)
        threading.Thread(target=build_worker, args=(job,), daemon=True).start()

    go.configure(command=start_prepare)
    build_btn.configure(command=start_build)
    send_btn.configure(command=package)
    open_review.configure(
        command=lambda: reveal(state["job"].directory / "review.xlsx") if state["job"] else None
    )

    # ---------------------------------------------------------------- events

    def drain() -> None:
        try:
            while True:
                kind, payload = events.get_nowait()

                if kind == "prepared":
                    summary, tables, detail, review_path, rendered = payload
                    busy(False)
                    say()
                    say("Statements read.", "ok")
                    say(tables)
                    say(detail)
                    if rendered.strip():
                        say(rendered)
                    open_review.configure(state="normal")
                    if summary.ok:
                        say()
                        say("Next: open the review sheet, correct column D, "
                            "save and close, then write the model.", "head")
                        build_btn.configure(state="normal")
                    else:
                        say()
                        say("The model was not written because the figures above "
                            "cannot be trusted yet. Resolve them and run again.", "stop")
                    reveal(Path(review_path))

                elif kind == "built":
                    res, out = payload
                    busy(False)
                    say()
                    if res.write_report.balanced and not res.stats.unresolved_left:
                        say("Model written and every year balances.", "ok")
                    elif res.stats.unresolved_left:
                        say(f"Model written, but {res.stats.unresolved_left} account(s) "
                            f"were left blank in review and are missing from it.", "stop")
                    else:
                        say("Model written, but at least one year does not balance.", "stop")
                    for year, diff in sorted((res.write_report.balance_check or {}).items()):
                        ok = abs(diff) < 0.01
                        say(f"    {year}: {'balances' if ok else f'out by {diff:,.2f}'}",
                            None if ok else "stop")
                    say(f"    {out}")
                    send_btn.configure(state="normal")
                    reveal(out.parent)

                elif kind == "failed":
                    friendly, tb = payload
                    busy(False)
                    say()
                    if friendly:
                        say("Could not continue.", "stop")
                        say(f"    {friendly}")
                    else:
                        say("Something went wrong that should not have.", "stop")
                        say(tb.strip().splitlines()[-1] if tb else "")
                        say("    Send this window's text to whoever maintains the tool.")
        except queue.Empty:
            pass
        root.after(120, drain)

    root.after(120, drain)
    root.mainloop()
    return 0


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except Exception:
        print(
            "This build of Python has no windowing support, so the window cannot open.\n"
            "Use the command line instead:  python -m fsa.cli prepare --help",
            file=sys.stderr,
        )
        return 2
    from fsa.env import load_dotenv

    load_dotenv()
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
