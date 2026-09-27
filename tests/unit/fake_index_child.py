"""A stand-in for `app.cli index --events jsonl`, for the supervisor's tests.

Layer: L3 (test support)

Run as `python fake_index_child.py MODE`. It speaks the event protocol of
`app/index/run_events.py` - but writes its lines by hand where a test needs
the pipe to misbehave (a line split across two writes, a burst, garbage, a
death halfway through a line), which the real writer never does on purpose.

Deliberately imports nothing heavy (no pipeline, no Qt), so it starts in a
fraction of a second and the tests stay quick.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time


def line(event: str, **payload) -> str:
    return json.dumps({"event": event, "t": time.monotonic(), **payload}) + "\n"


def out(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def progress(**stats) -> str:
    return line("progress", stats=stats)


def main(mode: str) -> int:
    out(line("hello", protocol=1, pid=os.getpid()))

    if mode == "normal":
        out(progress(phase="model", indexed=0))
        # **One event, two writes**, with a pause between: the reader must
        # wait for the end of the line, not parse half of it.
        whole = progress(phase="reading", indexed=1, current="a.txt")
        out(whole[:25])
        time.sleep(0.3)
        out(whole[25:])
        out("a library printed this to stdout\n")
        out(line("heartbeat"))
        # A burst: three hundred events in one write.
        out("".join(progress(phase="reading", indexed=n) for n in range(2, 302)))
        out(line("finished", exit=0, stats={"indexed": 301, "seen": 301,
                                            "phase": "word_index"}))
        return 0

    if mode == "commands":
        # Echo every command back in the final stats, in the order received.
        out(progress(phase="reading", indexed=1))
        heard = []
        for raw in sys.stdin:
            word = raw.strip()
            heard.append(word)
            if word == "pause":
                out(progress(phase="reading", indexed=1, paused=True,
                             paused_by_person=True))
            if word == "stop":
                break
        out(line("finished", exit=0, stats={"indexed": 1, "notices": heard}))
        return 0

    if mode == "crash":
        out(progress(phase="reading", indexed=5, current="big.pst", current_item=812))
        sys.stderr.write("Fatal Python error: the reader ran out of memory\n")
        sys.stderr.flush()
        out('{"event": "progress", "stats": {"ind')      # dies mid-line
        os._exit(3)

    if mode == "error":
        from app.core.errors import make_error
        from app.index.run_events import to_json_safe

        error = make_error("ERR_INDEX_RUNNING", "core.run_lock",
                           holder="the command line, since 14:02")
        out(line("finished", exit=1, error=to_json_safe(error)))
        return 1

    if mode == "silent_exit":
        # Ends cleanly, exit code 0, without ever saying it finished.
        out(progress(phase="reading", indexed=2, current="half.docx"))
        return 0

    if mode == "stop_ok":
        out(progress(phase="reading", indexed=1, current="slow.pdf"))
        for raw in sys.stdin:
            if raw.strip() == "stop":
                break
        out(line("finished", exit=1, stats={"indexed": 1}))
        return 1

    if mode == "ignore_stop":
        # A child wedged inside one file: takes no notice of anything.
        out(progress(phase="reading", indexed=1, current="wedged.pst"))
        threading.Event().wait(600)
        return 0

    if mode == "exit_on_eof":
        # What the real child does when its window disappears: its input ends.
        out(progress(phase="reading", indexed=1))
        for _raw in sys.stdin:
            pass
        return 0

    raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
