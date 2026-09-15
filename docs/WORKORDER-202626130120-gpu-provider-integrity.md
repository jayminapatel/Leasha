# Work order (One thread): one onnxruntime, and it says which one it is

**Doc version:** 1.0 · **Updated:** 2026-09-13 · **Applies to:** app v0.3.3
**Thread:** One thread (`requirements.txt` + `install.ps1` + `doctor.py` +
the Indexing tab's notice line)
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

- [ ] A fresh install on a DirectML-capable machine ends with
      `DmlExecutionProvider` in `onnxruntime.get_available_providers()`, without
      anyone answering a prompt.
- [ ] A second run of `install.ps1` over an existing venv leaves the provider
      exactly as it found it. No silent downgrade to CPU.
- [ ] `doctor.py` fails, not merely narrates, when the venv holds two
      onnxruntime distributions or a half-removed one.
- [ ] A run that has lost the provider says so in the window, not only at INFO
      in a log file.

## 2. Pin it

- [ ] Add explicit pins for both distributions to `requirements.txt`, or a
      `constraints.txt` the installer passes with `-c`. Unpinned is how 1.30.0
      arrived on a venv whose DirectML wheel is 1.24.4.
- [ ] **(UNCONFIRMED — needs the network and the Windows venv)** Establish the
      newest version for which *both* wheels exist for CPython 3.12 on Windows.
      `onnxruntime-directml` lags the main wheel; if 1.24.4 is the newest
      DirectML build then the CPU pin comes back to 1.24.x, and the pins must
      match. Record the pair and the date checked in a comment beside the pins.
- [ ] Confirm the chosen version satisfies `fastembed`'s exclusions above
      (`!=1.20.0`, `!=1.24.0`, `!=1.24.1`) — `1.24.4` does; do not assume the
      next one up will.

## 3. Make the install deterministic

`install.ps1` only. Non-negotiable #7 applies: ASCII-only, saved UTF-8 **with**
a BOM.

- [ ] Install the DirectML wheel as the last package step, unconditionally on a
      Windows machine with an adapter, using
      `--force-reinstall --no-deps onnxruntime-directml==<pinned>`.
      `--no-deps` stops it re-resolving; `--force-reinstall` guarantees the
      DirectML binaries land on top whatever ran before.
- [ ] Decide what remains of the `[y/N]` prompt at `install.ps1:649`. A prompt
      whose "no" leaves the machine five times slower with no later reminder is
      the wrong shape; if it stays, it must be answerable once and remembered.
- [ ] Verify the provider immediately after installing it and fail the step
      loudly if it is absent — the installer currently never checks that the
      wheel it just installed did anything.

## 4. Repair, not just install

The owner's venv is in the state this section exists to handle: `pip uninstall`
hit `WinError 5` because the running application held `onnxruntime.dll` open,
leaving stash directories `~nnxruntime\` and `~nnxruntime-1.30.0.dist-info\`, an
`onnxruntime\` package with no `__init__.py`, and therefore an `import
onnxruntime` that succeeds as an empty namespace package. `pip install` then
reported "already satisfied" from the surviving DirectML `.dist-info` and wrote
nothing.

- [ ] Before touching packages, the installer checks whether a Leasha process
      is holding the venv and stops with the instruction to close it, rather
      than starting an uninstall that cannot finish.
- [ ] Detect and clear `site-packages\~*` stash directories left by a failed
      uninstall, naming them in the output.
- [ ] After any repair, re-assert §3's force-reinstall rather than trusting
      pip's "already satisfied".

## 5. Make `doctor.py` able to fail on it

`doctor.py:300` already prints `DirectML available` / `no DirectML provider`,
and `_backend_choice` at `:317` explains the CPU fallback — both as profile
text, which cannot fail a run.

- [ ] A required check: exactly one onnxruntime distribution in site-packages.
      Two, or a `~`-prefixed remnant, is a fault with the repair command in its
      `Fix`, per the error contract (non-negotiable #2).
- [ ] A check comparing intent with reality: an adapter present and
      `DmlExecutionProvider` absent is a warning that names the wheel to
      install. Distinguish it from *no adapter*, which is not a fault — the
      distinction `_gpus` already draws at `compute_profile.py:391-434`.
- [ ] Neither check may import the models or run an index; `doctor` is read-only
      and quick.

## 6. Say it where somebody is looking

- [ ] When a stored compute profile recorded a DirectML-capable adapter and the
      current process has no provider, the Indexing tab says so in plain words
      before the run starts: what changed, what it costs, and the one command
      that fixes it.
- [ ] It must not fire for a machine that never had a GPU, and must not fire
      from a *failed* adapter probe — `_gpus` returns `probe_failed` for exactly
      this reason (`compute_profile.py:399-407`), and "could not look" is not
      "it is gone".
- [ ] If a new error code is needed, **append** to `ERROR_REGISTRY` in
      `app/core/errors.py`; never reorder it (CONVENTIONS §2).
- [ ] Nothing below `app/ui/` imports from `app/ui/` to do this.

## 7. Tests

- [ ] A unit test that reads `requirements.txt` (or the constraints file) and
      asserts both onnxruntime pins are present and equal in version. It fails
      the day somebody unpins one, which is the day this recurs.
- [ ] A test for the doctor checks in §5, against a fabricated site-packages
      listing — two dist-infos, a `~` remnant, and the healthy single case. No
      real venv, no network.
- [ ] A test for §6's three-way decision: provider present, provider lost,
      probe failed. The middle one warns; the other two are silent.
- [ ] Run the whole suite, not only these. Establish the baseline first if the
      run happens in a Linux sandbox — 17-22 failures there are pre-existing and
      are not this work.

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
