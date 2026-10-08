r"""What this machine actually is. **Detection only — no policy.**

Layer: L0 — read by the envelope (§3), the backends (§2) and `doctor`.

Every bound in the index-tuning order derives from these facts, which is why
this file contains no opinion about any of them: it does not say how many
workers to run, only how many cores there are and what kind. The moment a
number here becomes a choice, two places decide the same thing.

**The hybrid-core problem, from the owner's own machine.** A 13th-gen i7-1365U
reports ten cores, and the flat count is a lie for scheduling: two are P-cores
with hyper-threading and eight are E-cores, and an ONNX thread on an E-core
delivers a fraction of a P-core's throughput. So the profile records the split,
and §3's formulas weight it rather than counting.

**A fingerprint, so a new machine notices itself.** The same index folder
carried to another box must not keep the first one's numbers. The fingerprint
is over the facts that change scheduling - cores, RAM, the GPU - and not over
the free-space figure, which changes hourly and would invalidate the cache for
no reason.

Nothing here raises. A machine that will not answer a question is recorded as
not having answered it, which is a fact about the machine and is what `doctor`
needs to print.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from app.core.logging import logger

__all__ = [
    "ComputeProfile",
    "GpuAdapter",
    "detect",
    "cached_profile",
    "stored_profile",
    "reset_detection_cache",
    "OVERRIDE_ENV",
    "PROFILE_STATE_KEY",
]

_log = logger.bind(component="core.compute")

#: The one file-edited tunable in this order, and it exists because every other
#: bound derives from detection: when detection is wrong, nothing downstream can
#: be argued with until it can be replaced.
OVERRIDE_ENV = "COMPUTE_PROFILE_OVERRIDE"

#: Where the cached profile lives in `index_state`.
PROFILE_STATE_KEY = "compute:profile"


@dataclass(frozen=True)
class GpuAdapter:
    """One display adapter, as DXGI describes it."""

    name: str = ""
    vram_mb: int = 0
    #: `"12_1"`, `"11_0"`, or `""` when D3D12 would not create a device.
    feature_level: str = ""
    #: Whether onnxruntime offers DirectML in this installation. A capable
    #: adapter with no provider built in is not a usable backend.
    directml: bool = False


@dataclass(frozen=True)
class ComputeProfile:
    """The machine, as detected. Every field is a fact, none is a decision."""

    logical_processors: int = 0
    physical_cores: int = 0
    #: Performance and efficiency cores, where the platform will say.
    #: `(0, 0)` means "not a hybrid, or could not tell" - which `doctor` prints
    #: as such rather than guessing a split.
    performance_cores: int = 0
    efficiency_cores: int = 0
    ram_mb: int = 0
    avx2: bool = False
    #: `"ssd"`, `"hdd"` or `""`. The index volume specifically - a corpus on a
    #: spinning disk and an index on NVMe is an ordinary arrangement.
    index_disk: str = ""
    gpus: tuple[GpuAdapter, ...] = ()
    platform: str = ""
    #: Set when `OVERRIDE_ENV` supplied this rather than detection.
    overridden: bool = False
    #: Anything that could not be answered, and why. `doctor` prints it: a
    #: silent gap in a profile is a wrong number waiting to be believed.
    unknowns: tuple[str, ...] = field(default_factory=tuple)
    #: 2026-09-08. True when the display-adapter check did not run to
    #: completion - the PowerShell probe timed out or errored, which happens
    #: under exactly the CPU load the resource governor pauses for. **An empty
    #: `gpus` with this set is not "no graphics card"**; it is "nobody looked".
    #: `backends.why_unavailable` says so. Absent from a profile written by
    #: older code, so it defaults to False on load - a stored profile's `gpus`
    #: were always the result of a probe that ran.
    gpu_probe_failed: bool = False

    @property
    def hybrid(self) -> bool:
        """True when the platform reported both P-cores and E-cores."""
        return bool(self.performance_cores and self.efficiency_cores)

    @property
    def directml_available(self) -> bool:
        """True when some adapter can be used by onnxruntime's DirectML provider.

        A card that DXGI lists but no provider can drive reads as False here.
        """
        return any(gpu.directml for gpu in self.gpus)

    def fingerprint(self) -> str:
        """A hash over what changes scheduling, and nothing else.

        Free space is deliberately not in it: it changes hourly, and a cache
        invalidated by a download is a cache that is never warm.
        """
        material = json.dumps({
            "logical": self.logical_processors,
            "physical": self.physical_cores,
            "p": self.performance_cores,
            "e": self.efficiency_cores,
            "ram_mb": self.ram_mb,
            "avx2": self.avx2,
            "disk": self.index_disk,
            "gpus": [(g.name, g.vram_mb) for g in self.gpus],
            "platform": self.platform,
        }, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]

    def as_dict(self) -> dict[str, Any]:
        """The profile as plain data, with its fingerprint attached.

        The fingerprint is written out so a stored profile can be compared
        without rebuilding it; `from_dict` ignores the key on the way back in.
        """
        out = asdict(self)
        out["fingerprint"] = self.fingerprint()
        return out

    @classmethod
    def from_dict(cls, payload: Any) -> Optional["ComputeProfile"]:
        """Rebuild from `as_dict`, or None if it is not one. Never raises."""
        if not isinstance(payload, dict):
            return None
        try:
            gpus = tuple(
                GpuAdapter(**{k: v for k, v in gpu.items()
                              if k in GpuAdapter.__dataclass_fields__})
                for gpu in payload.get("gpus", []) or ()
            )
            fields = {k: v for k, v in payload.items()
                      if k in cls.__dataclass_fields__ and k != "gpus"}
            fields["unknowns"] = tuple(fields.get("unknowns", ()) or ())
            return cls(gpus=gpus, **fields)
        except Exception:                        # noqa: BLE001 - see module doc
            return None


# --- detection ---------------------------------------------------------------


def detect(index_path: Any = None, *, refresh: bool = False) -> ComputeProfile:
    """Look at the machine. **Never raises**; gaps are recorded as unknowns.

    **The two PowerShell probes run once per process, not once per call.**
    The embedder, OCR, the image model, the reranker, the Settings card and the
    start-of-run resolve each call this for themselves, and every call used to
    spawn PowerShell twice (the index-disk type, then the display adapters) -
    up to 25 seconds of waiting per call when the machine is busy, and the
    exposure behind the 2026-09-08 "no display adapter was detected" log. What
    was learned the first time is kept (`_gpu_cache`, `_disk_cache`) and
    everything cheap - core counts, RAM, AVX2, whether DirectML is offered -
    is still read fresh every time.

    `refresh=True` is the way round it: the Settings "Re-detect" button and the
    tests use it, and it asks the machine again. A probe that **failed** is not
    kept for good - it is served for `_RETRY_AFTER_FAILURE` seconds (so a busy
    machine is not made busier by every subsystem retrying at once) and then
    asked again, and it is still reported as "could not run", never as "no
    graphics card".
    """
    override = _from_override()
    if override is not None:
        return override

    unknowns: list[str] = []
    logical = os.cpu_count() or 0
    physical, performance, efficiency = _cores(unknowns)
    gpus, gpu_probe_failed = _gpus(unknowns, refresh=refresh)
    return ComputeProfile(
        logical_processors=logical,
        physical_cores=physical or logical,
        performance_cores=performance,
        efficiency_cores=efficiency,
        ram_mb=_ram_mb(unknowns),
        avx2=_avx2(unknowns),
        index_disk=_disk_kind(index_path, unknowns, refresh=refresh),
        gpus=gpus,
        platform=f"{platform.system()} {platform.release()}".strip(),
        unknowns=tuple(unknowns),
        gpu_probe_failed=gpu_probe_failed,
    )


def _from_override() -> Optional[ComputeProfile]:
    """`COMPUTE_PROFILE_OVERRIDE=path.json`, **logged loudly** when it applies.

    Loudly because a machine running on a hand-written profile and behaving
    oddly is a mystery that costs an afternoon unless the log says so on every
    start.
    """
    path = os.environ.get(OVERRIDE_ENV, "").strip()
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:                     # noqa: BLE001
        _log.warning("{} points at {} which could not be read ({}); "
                     "detecting normally", OVERRIDE_ENV, path, exc)
        return None
    profile = ComputeProfile.from_dict(payload)
    if profile is None:
        _log.warning("{} at {} is not a profile; detecting normally",
                     OVERRIDE_ENV, path)
        return None
    from dataclasses import replace

    _log.warning("COMPUTE PROFILE OVERRIDDEN by {} - every derived bound comes "
                 "from that file, not from this machine", path)
    return replace(profile, overridden=True)


def _cores(unknowns: list[str]) -> tuple[int, int, int]:
    """`(physical, performance, efficiency)`.

    The P/E split comes from Windows' own topology API, which is the only
    source that knows: `psutil` reports a flat count, and a flat count is what
    made the envelope's arithmetic too crude on a 2P+8E laptop.
    """
    physical = 0
    try:
        import psutil

        physical = int(psutil.cpu_count(logical=False) or 0)
    except Exception:                            # noqa: BLE001
        unknowns.append("physical core count")

    if sys.platform != "win32":
        return physical, 0, 0

    performance, efficiency = _windows_core_kinds(unknowns)
    return physical, performance, efficiency


#: `RelationProcessorCore` in `LOGICAL_PROCESSOR_RELATIONSHIP`.
_RELATION_PROCESSOR_CORE = 0


def _windows_core_kinds(unknowns: list[str]) -> tuple[int, int]:
    r"""P-cores and E-cores, via `GetLogicalProcessorInformationEx`.

    `PROCESSOR_RELATIONSHIP.EfficiencyClass` is the field: higher is faster,
    and on a hybrid part there are exactly two classes. Windows 10 1903 and
    later populate it; earlier ones return zero for every core, which reads
    correctly as "not a hybrid" rather than as a wrong split.
    """
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        size = ctypes.c_ulong(0)
        kernel32.GetLogicalProcessorInformationEx(
            _RELATION_PROCESSOR_CORE, None, ctypes.byref(size))
        buffer = (ctypes.c_ubyte * size.value)()
        if not kernel32.GetLogicalProcessorInformationEx(
                _RELATION_PROCESSOR_CORE, buffer, ctypes.byref(size)):
            unknowns.append("processor topology")
            return 0, 0

        classes: dict[int, int] = {}
        offset = 0
        while offset < size.value:
            # SYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX: Relationship (DWORD),
            # Size (DWORD), then the union. For a core, EfficiencyClass is the
            # second byte of PROCESSOR_RELATIONSHIP.
            entry_size = int.from_bytes(
                bytes(buffer[offset + 4:offset + 8]), sys.byteorder)
            if entry_size <= 0:
                break
            efficiency_class = buffer[offset + 9]
            classes[efficiency_class] = classes.get(efficiency_class, 0) + 1
            offset += entry_size

        if len(classes) < 2:
            return 0, 0                          # one class: not a hybrid
        fastest = max(classes)
        performance = classes[fastest]
        efficiency = sum(count for cls, count in classes.items()
                         if cls != fastest)
        return performance, efficiency
    except Exception as exc:                     # noqa: BLE001
        _log.debug("could not read processor topology: {}", exc)
        unknowns.append("processor topology")
        return 0, 0


def _ram_mb(unknowns: list[str]) -> int:
    try:
        import psutil

        return int(psutil.virtual_memory().total / (1024 * 1024))
    except Exception:                            # noqa: BLE001
        unknowns.append("installed memory")
        return 0


def _avx2(unknowns: list[str]) -> bool:
    """AVX2, which is what the ONNX runtime's fast kernels want."""
    try:
        if sys.platform == "win32":
            #: `PF_AVX2_INSTRUCTIONS_AVAILABLE`
            return bool(ctypes.windll.kernel32  # type: ignore[attr-defined]
                        .IsProcessorFeaturePresent(40))
        flags = Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="ignore")
        return " avx2 " in f" {flags} " or "\navx2 " in flags or " avx2\n" in flags
    except Exception:                            # noqa: BLE001
        unknowns.append("AVX2 support")
        return False


def _disk_kind(index_path: Any, unknowns: list[str], *,
               refresh: bool = False) -> str:
    """`"ssd"`, `"hdd"`, or `""` for the volume holding the index."""
    if index_path is None:
        return ""
    try:
        if sys.platform == "win32":
            return _windows_disk_kind(Path(index_path), unknowns, refresh=refresh)
        return _linux_disk_kind(Path(index_path), unknowns)
    except Exception:                            # noqa: BLE001
        unknowns.append("index disk type")
        return ""


def _windows_disk_kind(path: Path, unknowns: list[str], *,
                       refresh: bool = False) -> str:
    """Seek penalty, via PowerShell's storage cmdlets. **Once per drive letter
    per process** (see `detect`); a probe that failed is retried after
    `_RETRY_AFTER_FAILURE`.

    `IOCTL_STORAGE_QUERY_PROPERTY` would avoid the subprocess, but it needs a
    handle to the physical drive, which needs elevation on some systems - and a
    detection that fails without administrator rights is a detection that fails
    on most machines. `Get-PhysicalDisk` answers without any.
    """
    letter = str(path.resolve().drive).rstrip(":")
    if not letter:
        unknowns.append("index disk type")
        return ""
    with _probe_lock:
        known = _disk_cache.get(letter.upper())
        if known is not None and not refresh and not _stale(known[3], known[2]):
            unknowns.extend(known[1])
            return known[0]
        mine: list[str] = []
        answer, failed = _probe_windows_disk(letter, mine)
        _disk_cache[letter.upper()] = (answer, tuple(mine), failed, _now())
    unknowns.extend(mine)
    return answer


def _probe_windows_disk(letter: str, unknowns: list[str]) -> tuple[str, bool]:
    """The subprocess itself: `(kind, failed)`. `failed` is True only when the
    probe did not run to an answer (timeout, no PowerShell) - an answer that is
    neither SSD nor HDD is a real answer and is not asked again."""
    script = (
        f"$p = Get-Partition -DriveLetter {letter} -ErrorAction Stop; "
        "(Get-PhysicalDisk -ErrorAction Stop | "
        "Where-Object DeviceId -eq $p.DiskNumber).MediaType"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:                            # noqa: BLE001
        unknowns.append("index disk type")
        return "", True
    answer = (done.stdout or "").strip().lower()
    if "ssd" in answer:
        return "ssd", False
    if "hdd" in answer:
        return "hdd", False
    unknowns.append("index disk type")
    return "", False


def _linux_disk_kind(path: Path, unknowns: list[str]) -> str:
    """`/sys/block/<device>/queue/rotational`, which is the whole answer."""
    try:
        device = os.stat(path).st_dev
        major, minor = os.major(device), os.minor(device)
        link = Path(f"/sys/dev/block/{major}:{minor}")
        node = link.resolve()
        for candidate in (node, *node.parents):
            rotational = candidate / "queue" / "rotational"
            if rotational.is_file():
                return "hdd" if rotational.read_text().strip() == "1" else "ssd"
    except Exception:                            # noqa: BLE001
        # A container, a network mount or a missing /sys entry: the module
        # rule is that detection never raises, so the gap is recorded below
        # as an unknown and `doctor` prints it as such.
        pass
    unknowns.append("index disk type")
    return ""


# --- the GPU half ------------------------------------------------------------


#: 2026-09-08. The adapters from the last display-adapter probe **in this
#: process** that ran to completion - including an honest empty answer. The
#: embedder, OCR and the image model each call `detect()` for themselves, so
#: when the probe worked for the embedder at startup and then timed out for
#: OCR twenty minutes later under load, this is what lets OCR get the same
#: answer instead of a false "no display adapter was detected".
_last_known_adapters: Optional[tuple[GpuAdapter, ...]] = None

#: 2026-09-20. **Detect once per process.** Every subsystem calls `detect()`
#: for itself and each call used to spawn PowerShell twice. The two probes'
#: answers are kept here: `"probe"` -> `(adapters, probe_failed, unknowns,
#: taken_at)` for the display adapters, and per upper-case drive letter
#: `(kind, unknowns, failed, taken_at)` for the index disk. One lock, held
#: across the subprocess, so two subsystems asking at the same moment share
#: one probe rather than each spawning their own.
_probe_lock = threading.RLock()
_gpu_cache: dict[str, tuple] = {}
_disk_cache: dict[str, tuple] = {}

#: A probe that could not run is served from memory for this long, then asked
#: again. Long enough that a machine under load is not hammered by every
#: subsystem retrying, short enough that one bad moment at startup does not
#: stand for the whole session.
_RETRY_AFTER_FAILURE = 120.0

_now = time.monotonic


def _stale(taken_at: float, failed: bool) -> bool:
    """A cached answer is stale only when it is a failure old enough to retry.
    An answer the machine really gave - including "no adapters" - stands."""
    return bool(failed) and (_now() - taken_at) >= _RETRY_AFTER_FAILURE


def reset_detection_cache() -> None:
    """Forget everything the two probes learned, including the last known
    adapters. For tests; a caller that only wants a fresh look uses
    `detect(refresh=True)`, which keeps the last-known fall-back."""
    global _last_known_adapters
    with _probe_lock:
        _gpu_cache.clear()
        _disk_cache.clear()
        _last_known_adapters = None


#: Seconds the display-adapter probe is allowed. Deliberately not lengthened
#: as a fix: a longer wait under load is still a wait that can fail.
_DXGI_TIMEOUT = 15


def _gpus(unknowns: list[str], *,
          refresh: bool = False) -> tuple[tuple[GpuAdapter, ...], bool]:
    """Display adapters, and whether DirectML can actually be used.

    **Two separate questions, deliberately.** An adapter that DXGI reports and
    an execution provider that onnxruntime offers are different facts, and a
    machine can easily have the first without the second - which is exactly the
    case that would otherwise be read as "the GPU does not work".

    2026-09-08: returns `(adapters, probe_failed)`. **A probe that did not run
    is not a probe that found nothing.** Under CPU load the PowerShell probe
    can time out, and until now that came back as an empty tuple - the same
    value as "this machine has no graphics card" - so `why_unavailable` stated
    a hardware fact from a measurement that had not happened, on a machine
    whose embedder was running on the GPU at that moment. When the probe fails
    and an earlier one in this process succeeded, that answer is reused and
    said so at WARNING; when there is none, the empty tuple comes back flagged
    so the sentence downstream can be truthful.

    2026-09-20: **the probe itself runs once per process** (`_gpu_cache`); only
    the DirectML check, which is in-process and cheap, is asked every time - so
    a lost provider is still noticed (`resolve._gpu_regression` depends on it).
    """
    directml = _directml_provider_available()
    if sys.platform != "win32":
        return (), False

    with _probe_lock:
        cached = _gpu_cache.get("probe")
        if cached is not None and not refresh and not _stale(cached[3], cached[1]):
            adapters, probe_failed = cached[0], cached[1]
            unknowns.extend(cached[2])
        else:
            mine: list[str] = []
            adapters, probe_failed = _probe_gpus(mine)
            _gpu_cache["probe"] = (adapters, probe_failed, tuple(mine), _now())
            unknowns.extend(mine)

    if not adapters:
        return (), probe_failed
    from dataclasses import replace

    return (tuple(replace(adapter, directml=directml) for adapter in adapters),
            probe_failed)


def _probe_gpus(unknowns: list[str]) -> tuple[tuple[GpuAdapter, ...], bool]:
    """Ask the machine (one PowerShell call) and apply the fall-back rules."""
    global _last_known_adapters

    adapters, failure = _dxgi_adapters(unknowns)
    if failure:
        if _last_known_adapters is None:
            _log.warning("graphics card check {} - no earlier answer to fall "
                         "back on, so the graphics card is not known", failure)
            return (), True
        _log.warning("graphics card check {} - using the last known answer "
                     "({} adapters)", failure, len(_last_known_adapters))
        return _last_known_adapters, True
    _last_known_adapters = adapters
    return adapters, False


def _directml_provider_available() -> bool:
    try:
        import onnxruntime

        return "DmlExecutionProvider" in onnxruntime.get_available_providers()
    except Exception:                            # noqa: BLE001
        return False


def _dxgi_adapters(unknowns: list[str]) -> tuple[tuple[GpuAdapter, ...], str]:
    """Name and dedicated VRAM per adapter, from PowerShell's CIM data.

    DXGI through `ctypes` would be exact and is a page of COM vtable
    arithmetic that fails differently on every Windows build. The video
    controller's CIM class gives the name and the memory, which is what the
    profile records and what a person reads in `doctor`; the question that
    actually gates the backend - can onnxruntime use it - is answered
    separately and definitively above.

    2026-09-08: returns `(adapters, failure)`. `failure` is `""` when the
    probe ran - even if it found nothing - and otherwise a short phrase saying
    why it did not (`timed out after 15s`, `failed (OSError)`), which is the
    difference between "no graphics card" and "could not look".
    """
    script = (
        "Get-CimInstance Win32_VideoController -ErrorAction Stop | "
        "Select-Object Name, AdapterRAM | ConvertTo-Json -Compress"
    )
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=_DXGI_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if done.returncode != 0:
            # `-ErrorAction Stop` makes a CIM failure a non-zero exit with
            # empty stdout - indistinguishable from "no adapters" without this.
            unknowns.append("display adapters")
            return (), f"failed (PowerShell exit {done.returncode})"
        payload = json.loads((done.stdout or "").strip() or "null")
    except subprocess.TimeoutExpired:
        unknowns.append("display adapters")
        return (), f"timed out after {_DXGI_TIMEOUT}s"
    except Exception as exc:                     # noqa: BLE001
        unknowns.append("display adapters")
        return (), f"failed ({type(exc).__name__})"
    if payload is None:
        # PowerShell answered and the answer was "none" (or unparseable
        # output, which the probe cannot tell apart from none without
        # inventing a distinction). Reported as an honest empty answer.
        unknowns.append("display adapters")
        return (), ""
    if isinstance(payload, dict):
        payload = [payload]

    adapters = []
    for entry in payload:
        try:
            # AdapterRAM is a signed 32-bit field, so anything at or above 4GB
            # comes back negative or clamped. Recorded as 0 rather than as a
            # negative number somebody would have to interpret.
            raw = int(entry.get("AdapterRAM") or 0)
            adapters.append(GpuAdapter(
                name=str(entry.get("Name") or "").strip(),
                vram_mb=max(0, raw) // (1024 * 1024),
            ))
        except Exception:                        # noqa: BLE001
            continue
    return tuple(adapters), ""


# --- the cache ---------------------------------------------------------------


def stored_profile(store: Any) -> Optional[ComputeProfile]:
    """The profile last cached in `store`, read-only, or None.

    Factored out of `cached_profile` so a caller that needs to compare
    "what was true last time" against a fresh, uncached detect() call -
    work order 202626130120 (0t)'s section 6 - can read the old value without
    triggering (or racing) the write `cached_profile` makes when it decides
    the machine has changed. Never raises: a missing or unparseable state
    value reads as "nothing cached yet", the same as a fresh machine.
    """
    try:
        return ComputeProfile.from_dict(
            json.loads(store.get_state(PROFILE_STATE_KEY, "") or "null"))
    except Exception:                            # noqa: BLE001
        return None


def cached_profile(store: Any, index_path: Any = None, *,
                   refresh: bool = False) -> ComputeProfile:
    """The stored profile if this is still the same machine, else a fresh one.

    **The future-machine story in one function.** The same index folder opened
    on a different box has a different fingerprint, so the first launch there
    notices and re-derives everything rather than running on the old machine's
    numbers.
    """
    fresh = detect(index_path, refresh=True) if refresh else detect(index_path)
    if fresh.overridden:
        return fresh                             # an override is never cached

    stored = stored_profile(store)

    if (
        stored is not None
        and fresh.gpu_probe_failed
        and not fresh.gpus
        and stored.gpus
        and not stored.gpu_probe_failed
    ):
        # 2026-09-08. The graphics card check did not run this time, and the
        # stored profile holds an answer from a time it did. Without this the
        # empty `gpus` changed the fingerprint, this read as "a different
        # machine", and the cache was *overwritten* with a GPU-less profile -
        # a timeout under load quietly deleting a hardware fact. Keep the
        # stored adapters; the flag stays set so `doctor` can say they were
        # not re-checked.
        from dataclasses import replace

        _log.warning("graphics card check could not run - using the last known "
                     "answer from the stored profile ({} adapters)",
                     len(stored.gpus))
        fresh = replace(fresh, gpus=stored.gpus)

    if stored is not None and stored.fingerprint() == fresh.fingerprint():
        return stored

    if stored is not None:
        _log.info("this machine is not the one the profile was taken on "
                  "({} -> {}); re-deriving", stored.fingerprint(),
                  fresh.fingerprint())
    try:
        store.set_state(PROFILE_STATE_KEY, json.dumps(fresh.as_dict()))
    except Exception as exc:                     # noqa: BLE001 - a cache
        _log.debug("could not cache the compute profile: {}", exc)
    return fresh
