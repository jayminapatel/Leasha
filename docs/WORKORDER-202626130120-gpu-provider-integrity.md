# Work order (One thread): one onnxruntime, and it says which one it is

**Doc version:** 1.2 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3
**Thread:** One thread (`requirements.txt` + `install.ps1` + `doctor.py` +
the Indexing tab's notice line)
**Status correction, 2026-09-20:** SHIPPED - 24 of 24 items ticked, closed 2026-09-15. (The
register carried this as "24 / 24", which read as 24 open; it is 24 done, 0 open.) The line
below is the original release text, kept as written.
**Status:** RELEASED by the owner 2026-09-13, raised from a live fault on the
owner's machine. **Gap-schedulable** — §2 and §3 are independent of §5 and §6
and may land first.

**The fault, as observed.** An index run on 2026-09-12 reported `0 files/min`
and an ETA of `about 1823 days`. The ETA arithmetic is a separate defect and is
not this order. The cause underneath it was that the machine had silently
stopped using its graphics card, and nothing anywhere said so.

---

## 0. THE EVIDENCE — read this before changing anything

Every line below was read from an artefact, not inferred.

**The two wheels share one directory.** `onnxruntime` and `onnxruntime-directml`
are different distributions that both unpack into `venv\Lib\site-packages\
onnxruntime\`. Whichever is installed second overwrites the other's
`capi\onnxruntime.dll` and `capi\onnxruntime_pybind11_state.pyd`. Both
`.dist-info` directories survive, so `pip list` shows two packages where there
is one set of binaries.

**On the owner's machine, on 2026-09-12:**

```
onnxruntime-1.30.0.dist-info            installed 21:10:25
onnxruntime_directml-1.24.4.dist-info   installed 2026-09-05 16:52:14
onnxruntime\capi\onnxruntime.dll        21:10:16   <- the CPU build
onnxruntime\capi\DirectML.dll           2026-09-05 16:52:06   <- orphaned
```

**And the logs bracket it exactly**
(`logs\runs\run-20260912-233609-window.log`, `logs\app\app_2026-09-12.log`):

| Time | Line |
|---|---|
| 19:03:45 | `OCR engine loaded in 1.9s on the graphics card` |
| 21:10 | plain `onnxruntime` 1.30.0 written over the DirectML binaries |
| 23:36:28, :28, :42 | `ignoring the stored rates: this is not the machine they were measured on` — three times |
| 23:36:49 | `OCR engine loaded in 1.5s on the processor` … `no DirectML provider` |
| 23:37:57 | `resource governor: pause - Indexing has added 4,119MB (now 4,756MB), above the 4,000MB it is allowed to add` |
| 23:41:19 | `WARNING … Pausing freed nothing, so 5,487MB is resident rather than indexing` (`app/index/resources.py:708`) |

The chain is one link long in each direction: no DirectML provider, so the
embedder, reranker, OCR and image model all load on the CPU; CPU model
residency is larger, so the governor's 4,000MB ceiling is breached by the
models themselves; `ComputeProfile.fingerprint()` hashes the adapter list
(`app/core/compute_profile.py:129`), so losing the provider also discarded
every measured rate the tuning screen had.

**Why it will happen on any machine, not just this one.** `install.ps1:543`
runs `pip install -r requirements.txt`. `fastembed` 0.8.0 requires
`onnxruntime (>=1.17.0,!=1.20.0,!=1.24.0,!=1.24.1)` and
`rapidocr-onnxruntime` 1.4.4 requires `onnxruntime>=1.7.0`; `requirements.txt`
pins neither, so pip fetches the newest CPU wheel. `install.ps1:643-661` then
offers the DirectML wheel behind a `[y/N]` prompt with
`--upgrade-strategy only-if-needed`. GPU support therefore works only because
that install happened to run *second* — and `onnxruntime-directml` does not
satisfy a requirement spelled `onnxruntime`, so the next pip command that
re-resolves either dependency puts the CPU wheel back on top. Silently.

**The invariant this order establishes:** a Leasha venv has exactly one set of
onnxruntime binaries, their provenance is pinned, and if the provider a machine
had yesterday is gone today the application says so where somebody is looking.

---

## 1. What must be true when this is done

- [x] A fresh install on a DirectML-capable machine ends with
      `DmlExecutionProvider` in `onnxruntime.get_available_providers()`, without
      anyone answering a prompt.
  > **2026-09-15:** the `[y/N]` prompt at `install.ps1:649` is gone - see
  > section 3's note. Verified on the real Windows venv this order was built
  > in, which has a genuine Intel Iris Xe adapter: ran the installer's exact
  > command by hand, `pip install --force-reinstall --no-deps
  > onnxruntime-directml==1.24.4`, then `python -c "import onnxruntime as o;
  > print(o.get_available_providers())"` printed
  > `['DmlExecutionProvider', 'CPUExecutionProvider']`. `doctor.py` (full run,
  > not `--quick`) confirms it too: `[PASS] GPU provider matches this
  > machine   an adapter is present and DirectML is available`.
- [x] A second run of `install.ps1` over an existing venv leaves the provider
      exactly as it found it. No silent downgrade to CPU.
  > **2026-09-15:** the force-reinstall step is unconditional but idempotent
  > in outcome - it always lands on the same pinned `onnxruntime-directml`
  > version, whatever ran before. Verified by running the installer's pip
  > command a second time against the already-fixed venv: `Successfully
  > installed onnxruntime-directml-1.24.4` again, and the provider list
  > unchanged (`DmlExecutionProvider` still present). The plain
  > `pip install -r requirements.txt` step that runs first cannot regress it
  > either, now that `onnxruntime==1.24.4` is pinned in requirements.txt - it
  > no longer re-resolves to a newer CPU-only wheel.
- [x] `doctor.py` fails, not merely narrates, when the venv holds two
      onnxruntime distributions or a half-removed one.
  > **2026-09-15:** `check_onnxruntime_integrity()` in `doctor.py`, wired into
  > `run_all()` as a required (non-optional) check. Refined from the literal
  > "exactly one" during implementation - see section 5's note for why two
  > *matching-version* distributions has to be the healthy case, not a
  > fault. A stash remnant or two *different-version* distributions still
  > fails outright, with the repair command in `Fix`. Covered by
  > `tests/unit/test_doctor_onnxruntime.py` against fabricated
  > `site_packages` fixtures (the healthy single case, the healthy matched
  > pair, mismatched versions, a stash remnant, an unrelated stash, and a
  > missing directory - 6 cases).
- [x] A run that has lost the provider says so in the window, not only at INFO
      in a log file.
  > **2026-09-15:** see section 6's note. `Pipeline._report_gpu_regression`
  > appends the notice to `stats.notices` at WARNING and at the very start of
  > `run()`, which the Indexing tab already draws through
  > `IndexingView.show_notices` on every progress tick and on finish - no new
  > UI code needed, the same mechanism `_report_root_problems` and
  > `_say_if_nothing_was_walked` already use for this exact reason.

## 2. Pin it

- [x] Add explicit pins for both distributions to `requirements.txt`, or a
      `constraints.txt` the installer passes with `-c`. Unpinned is how 1.30.0
      arrived on a venv whose DirectML wheel is 1.24.4.
  > **2026-09-15:** `onnxruntime==1.24.4` is now its own pinned line in
  > `requirements.txt`. `onnxruntime-directml==1.24.4` is documented in the
  > same comment block rather than pinned as an install line - it has no
  > Windows-only wheel that would install on this project's Linux test
  > sandboxes, so it cannot be unconditional there; `install.ps1` installs it
  > on a real Windows machine instead (section 3). Reproduced the original
  > fault first, to be sure the fix was needed: a clean
  > `pip install -r requirements.txt` against the *previous* unpinned file
  > pulled `onnxruntime-1.30.0` via `fastembed`'s own unpinned requirement,
  > exactly as section 0's evidence describes.
- [x] **(UNCONFIRMED — needs the network and the Windows venv)** Establish the
      newest version for which *both* wheels exist for CPython 3.12 on Windows.
      `onnxruntime-directml` lags the main wheel; if 1.24.4 is the newest
      DirectML build then the CPU pin comes back to 1.24.x, and the pins must
      match. Record the pair and the date checked in a comment beside the pins.
  > **2026-09-15 — CONFIRMED, was UNCONFIRMED.** Queried PyPI's JSON API
  > directly (`https://pypi.org/pypi/onnxruntime-directml/json` and the same
  > for `onnxruntime`) from this real Windows venv, which has network access.
  > `onnxruntime-directml` publishes no `cp312-win_amd64` wheel newer than
  > `1.24.4` (`1.24.3`, `1.24.2`, ... below it; nothing above except a `.dev`
  > pre-release). `onnxruntime` itself does publish a matching `1.24.4`
  > `cp312-win_amd64` wheel, exactly as predicted. Both pins recorded in
  > `requirements.txt`'s comment, dated 2026-09-15.
- [x] Confirm the chosen version satisfies `fastembed`'s exclusions above
      (`!=1.20.0`, `!=1.24.0`, `!=1.24.1`) — `1.24.4` does; do not assume the
      next one up will.
  > **2026-09-15:** confirmed - `1.24.4` is not in `{1.20.0, 1.24.0, 1.24.1}`.
  > Asserted by `tests/unit/test_onnxruntime_pins.py::
  > test_the_pin_satisfies_fastembeds_exclusions`, which reads the pin from
  > `requirements.txt` itself rather than trusting this note to stay
  > accurate.

## 3. Make the install deterministic

`install.ps1` only. Non-negotiable #7 applies: ASCII-only, saved UTF-8 **with**
a BOM.

- [x] Install the DirectML wheel as the last package step, unconditionally on a
      Windows machine with an adapter, using
      `--force-reinstall --no-deps onnxruntime-directml==<pinned>`.
      `--no-deps` stops it re-resolving; `--force-reinstall` guarantees the
      DirectML binaries land on top whatever ran before.
  > **2026-09-15:** `install.ps1`'s "Optional: DirectML GPU acceleration"
  > section is rewritten. It still detects a display adapter (the same
  > `Get-CimInstance Win32_VideoController` probe as before, simplified to
  > check adapter presence only, since the wheel is now forced regardless of
  > onnxruntime's current provider list), and on a Windows machine with one
  > it runs exactly the command above, last, after the plain
  > `pip install -r requirements.txt` step.
- [x] Decide what remains of the `[y/N]` prompt at `install.ps1:649`. A prompt
      whose "no" leaves the machine five times slower with no later reminder is
      the wrong shape; if it stays, it must be answerable once and remembered.
  > **2026-09-15 — decided: removed entirely.** An adapter-having machine now
  > always gets the DirectML wheel; there is nothing to ask, and therefore
  > nothing a "no" can be forgotten against. A machine with no adapter prints
  > one `DarkGray` line saying the processor is used and moves on - no
  > prompt either way, satisfying section 1's first acceptance box ("without
  > anyone answering a prompt").
- [x] Verify the provider immediately after installing it and fail the step
      loudly if it is absent — the installer currently never checks that the
      wheel it just installed did anything.
  > **2026-09-15:** the install step now runs
  > `python -c "import onnxruntime; print('DmlExecutionProvider' in
  > onnxruntime.get_available_providers())"` immediately after the pip
  > install and throws (which `Invoke-Step` turns into a loud `ERROR` /
  > `FIX` pair, not silence) if the import fails or the check prints
  > anything but `True`. Verified by hand on the real venv: the exact same
  > two commands, run in sequence, produced `Successfully installed
  > onnxruntime-directml-1.24.4` then `['DmlExecutionProvider',
  > 'CPUExecutionProvider']`.

## 4. Repair, not just install

The owner's venv is in the state this section exists to handle: `pip uninstall`
hit `WinError 5` because the running application held `onnxruntime.dll` open,
leaving stash directories `~nnxruntime\` and `~nnxruntime-1.30.0.dist-info\`, an
`onnxruntime\` package with no `__init__.py`, and therefore an `import
onnxruntime` that succeeds as an empty namespace package. `pip install` then
reported "already satisfied" from the surviving DirectML `.dist-info` and wrote
nothing.

- [x] Before touching packages, the installer checks whether a Leasha process
      is holding the venv and stops with the instruction to close it, rather
      than starting an uninstall that cannot finish.
  > **2026-09-15:** new step "Check no Leasha process is holding the venv",
  > immediately before "Install Python packages". `Get-Process -Name python`
  > filtered to this venv's own `python.exe` path; if any match, the step
  > throws with the exact WinError 5 explanation and the instruction to
  > close Leasha first, which `Invoke-Step` turns into a stop-and-explain
  > (or Retry/Continue/Abort under `-OnError Ask`) rather than a silent
  > uninstall attempt.
- [x] Detect and clear `site-packages\~*` stash directories left by a failed
      uninstall, naming them in the output.
  > **2026-09-15:** new step "Clear stash directories left by a failed
  > uninstall", right after the process check. Lists
  > `venv\Lib\site-packages\~*`, names each one as it removes it
  > (`Removing stale stash: ~nnxruntime`), and never fails the run if none
  > exist - an empty match is the common, healthy case.
- [x] After any repair, re-assert §3's force-reinstall rather than trusting
      pip's "already satisfied".
  > **2026-09-15:** the sequence is now: process check, stash cleanup, plain
  > `pip install -r requirements.txt`, then section 3's unconditional
  > `--force-reinstall --no-deps onnxruntime-directml==1.24.4` last -
  > every install.ps1 run, repair or fresh, ends at the same forced step, so
  > there is no path that trusts "already satisfied" for the DirectML
  > wheel.

## 5. Make `doctor.py` able to fail on it

`doctor.py:300` already prints `DirectML available` / `no DirectML provider`,
and `_backend_choice` at `:317` explains the CPU fallback — both as profile
text, which cannot fail a run.

- [x] A required check: exactly one onnxruntime distribution in site-packages.
      Two, or a `~`-prefixed remnant, is a fault with the repair command in its
      `Fix`, per the error contract (non-negotiable #2).
  > **2026-09-15 — implemented as one *coherent* install rather than a bare
  > count.** Found while building it: section 3's own fix installs
  > `onnxruntime-directml` unconditionally, forever, alongside the plain
  > wheel on any machine with a display adapter - so a healthy, correctly
  > pinned DirectML machine (this real venv, after the fix) always carries
  > *two* dist-info folders (`onnxruntime-1.24.4.dist-info` and
  > `onnxruntime_directml-1.24.4.dist-info`), confirmed by inspecting
  > `site-packages` directly. A literal "exactly one" would fail `doctor`
  > forever on the very hardware this order exists for, which contradicts
  > section 1's own first acceptance box. `check_onnxruntime_integrity()`
  > therefore treats matching-version distributions as the sanctioned pair
  > and only fails on a version *mismatch* (the actual section 0 fault -
  > 1.30.0 beside 1.24.4) or a `~` stash remnant. Verified against this
  > venv's real, now-healthy state: `doctor.py` prints `[PASS] onnxruntime
  > install is coherent`.
- [x] A check comparing intent with reality: an adapter present and
      `DmlExecutionProvider` absent is a warning that names the wheel to
      install. Distinguish it from *no adapter*, which is not a fault — the
      distinction `_gpus` already draws at `compute_profile.py:391-434`.
  > **2026-09-15:** `check_gpu_provider_intent()` in `doctor.py`, optional
  > (WARN, not a hard failure - CPU still works). Reuses
  > `ComputeProfile.gpu_probe_failed` and `.gpus`/`.directml_available` to
  > draw exactly the three-way line `why_unavailable` already draws in
  > `app/index/backends.py`: no adapter (silent), adapter with no provider
  > (WARN, names the pinned wheel), and a failed probe (silent - "could not
  > look" is not "it is gone"). Verified against this venv before and after
  > the fix: `[WARN] GPU provider matches this machine   a display adapter
  > is present (Intel(R) Iris(R) Xe Graphics) but this installation has no
  > DirectML provider` beforehand, `[PASS] ... an adapter is present and
  > DirectML is available` afterward.
- [x] Neither check may import the models or run an index; `doctor` is read-only
      and quick.
  > **2026-09-15:** `check_onnxruntime_integrity` touches only
  > `Path.iterdir()` over `site-packages` - no import at all.
  > `check_gpu_provider_intent` calls `compute_profile.detect()`, which the
  > rest of `doctor.py` already calls for the "Machine" section above it;
  > the only onnxruntime touch anywhere in this path is
  > `onnxruntime.get_available_providers()`, a metadata call, never a model
  > load or an index. Both ran in well under a second against this venv, as
  > part of every `doctor.py --quick` and full run above.

## 6. Say it where somebody is looking

- [x] When a stored compute profile recorded a DirectML-capable adapter and the
      current process has no provider, the Indexing tab says so in plain words
      before the run starts: what changed, what it costs, and the one command
      that fixes it.
  > **2026-09-15:** `app.index.backends.gpu_regression_notice(stored, fresh)`
  > is the decision; `app.index.resolve._gpu_regression` reads the profile
  > `store` had cached *before* this call and compares it against a genuinely
  > fresh `detect()` - deliberately never `cached_profile`'s own return
  > value, because a DirectML loss alone does not change
  > `ComputeProfile.fingerprint()` (it hashes adapter name and VRAM, not
  > provider availability), so `cached_profile` can hand back the *old*,
  > still-rosy stored profile on exactly the machine this order exists for.
  > `resolve_for_run` (already the one place both the CLI and the window
  > resolve the machine off the UI thread before a run starts) carries the
  > notice on `Resolved.gpu_regression_notice`; `PipelineConfig` carries it
  > into the Pipeline; `Pipeline.run()` appends it to `stats.notices` as the
  > very first thing, before the embedder loads or a file is read. No new UI
  > code: `IndexingView.show_notices` already draws `stats.notices` on every
  > progress tick and on finish. The sentence names the card, says
  > everything is running on the processor instead, and gives the exact pip
  > command to fix it.
- [x] It must not fire for a machine that never had a GPU, and must not fire
      from a *failed* adapter probe — `_gpus` returns `probe_failed` for exactly
      this reason (`compute_profile.py:399-407`), and "could not look" is not
      "it is gone".
  > **2026-09-15:** all four states are covered in
  > `tests/unit/test_gpu_regression_notice.py` - still has it (silent), had
  > it and lost it (fires), never had it even with no GPU now (silent), never
  > had it even if a GPU shows up now (silent), a failed probe (silent), no
  > stored profile at all (silent), and a missing fresh profile (silent) - 8
  > tests in all, plus the `resolve_for_run` wiring in
  > `tests/unit/test_resolve_gpu_regression.py`.
- [x] If a new error code is needed, **append** to `ERROR_REGISTRY` in
      `app/core/errors.py`; never reorder it (CONVENTIONS §2).
  > **2026-09-15 — decided: not needed.** The notice is a plain string
  > appended to `IndexStats.notices`, the same mechanism
  > `_report_root_problems` and `_say_if_nothing_was_walked` already use for
  > exactly this kind of "not a failure, but somebody needs to know"
  > condition - neither of those raises an `AppError` either, and this
  > order's notice is not an error: search and indexing still work, only
  > slower. `ERROR_REGISTRY` is unchanged.
- [x] Nothing below `app/ui/` imports from `app/ui/` to do this.
  > **2026-09-15:** confirmed by inspection - `app/core/compute_profile.py`,
  > `app/index/backends.py`, `app/index/resolve.py`, `app/index/pipeline.py`
  > and `doctor.py` import none of `app.ui`. `grep -n "app\.ui" ` across
  > those five files returns nothing. `app/ui/shell.py` and `app/cli.py` are
  > the only two call sites that read `tuned.gpu_regression_notice` and hand
  > it to `PipelineConfig` - wiring, not detection logic.

## 7. Tests

- [x] A unit test that reads `requirements.txt` (or the constraints file) and
      asserts both onnxruntime pins are present and equal in version. It fails
      the day somebody unpins one, which is the day this recurs.
  > **2026-09-15:** `tests/unit/test_onnxruntime_pins.py`, 5 tests - the
  > install pin exists, the documented DirectML pin matches it exactly, the
  > pin satisfies fastembed's exclusions, `app.index.backends.DIRECTML_PIN`
  > matches the requirements.txt pin, and `install.ps1` installs that same
  > version. All read the real files rather than a copy of the numbers.
- [x] A test for the doctor checks in §5, against a fabricated site-packages
      listing — two dist-infos, a `~` remnant, and the healthy single case. No
      real venv, no network.
  > **2026-09-15:** `tests/unit/test_doctor_onnxruntime.py`, 12 tests - 6
  > against fabricated `site_packages` (healthy single, nothing installed,
  > mismatched-version pair fails, matched-version pair is healthy, a stash
  > remnant fails, an unrelated stash is ignored, a missing directory reads
  > as nothing installed) and 4 against an injected `ComputeProfile` for
  > `check_gpu_provider_intent` (no adapter, has the provider, adapter with
  > no provider warns, a failed probe stays silent). All parameter-injected,
  > so no real venv and no network are touched.
- [x] A test for §6's three-way decision: provider present, provider lost,
      probe failed. The middle one warns; the other two are silent.
  > **2026-09-15:** `tests/unit/test_gpu_regression_notice.py`, 8 tests
  > against `backends.gpu_regression_notice` directly, plus 4 more in
  > `tests/unit/test_resolve_gpu_regression.py` wiring it through
  > `resolve_for_run` (including that every existing caller passing
  > `store=None` is completely unaffected, and that a broken store never
  > fails the resolve), plus 5 in `tests/unit/test_pipeline_gpu_regression.py`
  > proving the notice reaches `IndexStats.notices` - the same pattern
  > `test_empty_run_says_why.py` already uses for `_report_root_problems`.
- [x] Run the whole suite, not only these. Establish the baseline first if the
      run happens in a Linux sandbox — 17-22 failures there are pre-existing and
      are not this work.
  > **2026-09-15:** ran on the real Windows venv, not a Linux sandbox, so no
  > baseline subtraction was needed. Exact command and pass count are in
  > `HANDOFF.md`'s new trap-guarded note and this session's own final report.

## 8. What cannot be checked from a sandbox

Named here so nobody reasons past the gap.

- The PyPI version matrix in §2 — needs the network.
- Anything that proves the provider is back — needs the Windows venv:
  `venv\Scripts\python.exe -c "import onnxruntime as o; print(o.__version__, o.get_available_providers())"`
- The Indexing tab's notice in §6 — needs Qt on a real desktop, or a
  `widget.grab()` capture per the three-tier method.
- Whether the GPU actually restores the throughput lost on 2026-09-12. Measure
  it (non-negotiable #9): index the same subtree twice, provider on and off,
  and record files/min both ways. "It feels fast" is not a result.

## 9. Not in this order

- The ETA that printed `about 1823 days`. Same evening, different defect:
  `format_eta` (`app/ui/presenter.py:457`) guards only `files_per_minute <= 0`,
  so a rate of 0.067 files/min — which the line beside it prints as
  `0 files/min` — is extrapolated across five years. Fix it separately.
- The resource governor's 4,000MB ceiling. It behaved correctly given CPU
  models; whether the default is right once the GPU is back is a measurement,
  not an assumption.
