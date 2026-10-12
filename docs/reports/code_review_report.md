# Leasha - Comprehensive 360 Code Review

**Doc version:** 1.0 · **Updated:** 2026-10-12 · **Applies to:** app v1.0.3

## Executive Summary

Leasha is an impressive local, offline-first search application tailored for Windows desktops. The codebase demonstrates a high level of maturity, rigorous adherence to guiding principles, and a deep understanding of performance constraints and user experience.

The architecture is tightly defined around an embedded engine that guarantees privacy without sacrificing capability. The decision to move from a multi-process architecture to a single-process (with optional helper processes) is well-executed and load-bearing.

## 1. Machinery and Architecture

### Strengths
- **Single Source of Truth**: SQLite serves as the undeniable authority. The integration of LanceDB for vector search as a derived store is a masterclass in resilient design. If the two disagree, LanceDB can always be rebuilt from SQLite. This ensures data integrity and simplifies crash recovery.
- **Embedded ML Stack**: FastEmbed via ONNX Runtime provides meaning-based search without the fragility of external services. The deliberate choice to keep inference on the CPU or seamlessly switch to DirectML via `gpu_serialize` protects against driver faults and ensures the application runs on consumer hardware.
- **Robust Error Handling**: The `AppError` contract is strictly followed. Failing to parse a 100GB corpus due to one corrupted file is a common pitfall in similar applications; Leasha gracefully logs, marks, and continues.
- **Resumability**: Writing progress (cursor) to the `files` table and committing chunks *before* marking a file as `INDEXED` is highly defensive and ensures crashes cost seconds, not hours.

### Areas for Improvement / Recommendations
- **SQLite Concurrency**: While WAL mode is enabled and write groups (`WRITE_GROUP_MAX_DOCS`) mitigate lock contention, heavy concurrent reading during index writes might still face locking issues. Further optimizing the `busy_timeout` or investigating connection pooling for the UI reads could future-proof the application against more aggressive indexing threads.
- **Native Crash Visibility**: The integration of `faulthandler` (`catch_native_crashes`) is brilliant, but parsing `crash.log` manually can be tedious. A mechanism to surface these crashes elegantly in the UI upon next launch (e.g., "Leasha recovered from an unexpected crash") would improve user trust.

## 2. UI/UX (PySide6)

### Strengths
- **Responsive by Design**: The strict separation of the UI thread and background workers (`CallableWorker`) is strictly enforced. Heavy lifting (database reads, hashing, parsing) never blocks the event loop.
- **Progressive Disclosure**: The UI respects the user's focus. The two-tier search (BM25 for as-you-type, full hybrid on `Enter` or idle) is a brilliant UX pattern that saves CPU cycles and provides instant feedback.
- **Clear Communication**: Search degraded states (e.g., `NOTICE_NO_VECTORS`) are surfaced as plain-text notices. The user is never left wondering why results seem poor.
- **Native Integration**: Windows-specific integrations (taskbar progress, native notifications, tray minimization) make Leasha feel like a first-class citizen on the OS.

### Areas for Improvement / Recommendations
- **View Complexity**: Some views (`settings_view.py`, `indexing_view.py`) are nearing the 250-line complexity guard. Continuing to decompose these into smaller, reusable widgets will improve maintainability.
- **Cross-Platform Readiness**: While macOS and Linux are supported in source, subtle UI behaviors (like `Enter` key handling in lists) required specific workarounds (`app/ui/enter_key.py`). Expanding automated UI testing on these platforms will be necessary if they ever become official release targets.

## 3. Performance

### Strengths
- **Intelligent Batching**: Batching embedding calls (`EMBED_BATCH`) and SQLite commits (`WRITE_GROUP_MAX_DOCS`) drastically improves throughput. The dynamic feeder queue is perfectly tuned to keep the model fed without overwhelming memory.
- **Resource Governance**: The `ResourceGovernor` is a standout feature. Throttling based on disk space, battery, and overall CPU load ensures Leasha is a good neighbor to other applications.
- **Vector Deduping**: Reusing embeddings for identical passages (`chunks_deduped`) saves significant compute on repetitive documents (like email signatures or boilerplate).
- **Fast Startup**: Deferring the construction of non-critical tabs (Mail, Code, Settings) until after the first paint (`QTimer.singleShot`) is an elegant solution to keep the time-to-interactive under the 3-second budget.

### Areas for Improvement / Recommendations
- **Model Quantization**: The adoption of `int8` quantization for the embedding model is excellent for throughput but introduces a slight recall penalty. Continuing to benchmark newer, more efficient models (like smaller parameter Qwen versions or updated BAAI models) could yield a better speed/accuracy tradeoff.
- **FTS5 Optimization**: The query planning around FTS5 (`_bounded` queries) is smart, but searching for extremely common words can still take seconds. Continuously reviewing `sqlite_stat1` and exploring whether `trigram` tokenizers could replace `porter` for specific columns might alleviate some edge-case latencies.

## Conclusion

Leasha is exceptionally well-engineered. The codebase reflects a mature development process driven by measurable performance budgets and a clear understanding of desktop constraints. The strict adherence to offline-first principles, coupled with a seamless hybrid search pipeline, positions it as a robust solution in the local search space.
