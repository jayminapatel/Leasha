r"""Order 202626270602 (0n) section 4 - `leasha timeline`, before the window.

Layer: L4 entry point (non-negotiable 8)

The command reaches the same queries the window does, so what is proved here
- the truthful dates, the offline badge, paging by cursor - is proved for both.
"""

from __future__ import annotations

import json

import pytest

from app import cli
from app.storage.sqlite_store import SqliteStore
from tests.unit.timeline_env import june_2015


def env_file(tmp_path):
    data = tmp_path / "data"
    path = tmp_path / ".env"
    path.write_text(
        f"DATA_PATH={data}\nVECTOR_PATH={data / 'vectors'}\nFTS_DB={data / 'index.db'}\n"
        f"CACHE_PATH={data / 'cache'}\nMODEL_CACHE={data / 'models'}\nSTATE_PATH={data / 'state'}\n"
        f"LOG_PATH={tmp_path / 'logs'}\nMIN_FREE_GB=0\nREQUIRED_FREE_GB=0\n", encoding="utf-8")
    return str(path)


def run(argv) -> int:
    args = cli.build_parser().parse_args(argv)
    return int(args.func(args))


@pytest.fixture
def june_index(tmp_path):
    """An index on disk where the CLI will look for it, holding the June 2015 fixture."""
    env = env_file(tmp_path)
    cli.cmd_init(cli.build_parser().parse_args(["init", "--env", env]))
    store, ids = june_2015(tmp_path)
    store.close()
    import shutil

    target = tmp_path / "data" / "index.db"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(tmp_path / "june.db", target)
    return env, ids, tmp_path


def test_the_command_is_registered_with_a_plain_help_line():
    parser = cli.build_parser()
    args = parser.parse_args(["timeline", "--month", "2015-06"])
    assert args.func.__name__ == "cmd_timeline" and args.month == "2015-06"


def test_an_empty_index_says_there_is_nothing_yet_and_is_not_an_error(tmp_path, capsys):
    env = env_file(tmp_path)
    cli.cmd_init(cli.build_parser().parse_args(["init", "--env", env]))
    capsys.readouterr()
    assert run(["timeline", "--env", env]) == cli.EXIT_OK
    assert "There is nothing on the timeline yet" in capsys.readouterr().out


def test_with_no_dates_it_says_what_the_timeline_holds_by_year_and_month(june_index, capsys):
    env, _ids, _ = june_index
    assert run(["timeline", "--env", env]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "6 items from 2015 to 2019." in out
    assert "2015" in out and "06:4" in out and "07:1" in out          # June has four, July one
    assert "2019" in out


def test_a_month_lists_everything_from_it_oldest_first_with_the_drives_badge(june_index, capsys):
    env, _ids, _ = june_index
    assert run(["timeline", "--month", "2015-06", "--env", env]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "June 2015"
    order = [out.index(name) for name in ("lake.jpg", "beach.jpg", "to-the-bank.docx", "Wedding plans")]
    assert order == sorted(order)
    assert "Wednesday 10 June 2015" in out and "Thursday 25 June 2015" in out
    beach = next(line for line in out.splitlines() if "beach.jpg" in line)
    assert "[Old WD - not plugged in]" in beach and "Taken" in beach
    assert "File date" in next(line for line in out.splitlines() if "to-the-bank" in line)
    assert "Sent" in next(line for line in out.splitlines() if "Wedding plans" in line)
    assert "july.docx" not in out and "main.py" not in out and "odd.txt" not in out


def test_a_year_and_a_free_range_and_a_kind_all_reach_the_same_places(june_index, capsys):
    env, _ids, _ = june_index
    run(["timeline", "--year", "2015", "--env", env])
    assert "july.docx" in capsys.readouterr().out
    run(["timeline", "--after", "2015-06-01", "--before", "2015-06-30", "--env", env])
    assert "lake.jpg" in capsys.readouterr().out
    run(["timeline", "--after", "2019", "--env", env])
    assert "new.jpg" in capsys.readouterr().out
    run(["timeline", "--month", "2015-06", "--kind", "mail", "--env", env])
    out = capsys.readouterr().out
    assert "Wedding plans" in out and "lake.jpg" not in out
    run(["timeline", "--month", "2015-06", "--kind", "code", "--env", env])
    assert "main.py" in capsys.readouterr().out


def test_json_is_machine_readable_and_pages_by_cursor_without_repeats(june_index, capsys):
    env, _ids, _ = june_index
    seen, cursor = [], None
    for _ in range(10):
        argv = ["timeline", "--month", "2015-06", "--limit", "1", "--json", "--env", env]
        if cursor:
            argv += ["--cursor", cursor]
        assert run(argv) == cli.EXIT_OK
        out = capsys.readouterr().out
        assert out.lstrip().startswith("{"), f"a human preamble came first: {out[:60]!r}"
        payload = json.loads(out)
        seen += [item["name"] for item in payload["items"]]
        cursor = payload["next_cursor"]
        if not cursor:
            break
    assert seen == ["lake.jpg", "beach.jpg", "to-the-bank.docx", "Wedding plans"]
    item = payload["items"][0]
    assert {"path", "kind", "when", "date_from", "reachable_now", "source"} <= set(item)


def test_json_says_how_each_date_was_arrived_at_and_where_an_offline_item_lives(june_index, capsys):
    env, _ids, _ = june_index
    run(["timeline", "--month", "2015-06", "--json", "--env", env])
    items = {i["name"]: i for i in json.loads(capsys.readouterr().out)["items"]}
    assert items["lake.jpg"]["date_from"] == "taken" and items["lake.jpg"]["source"] is None
    assert items["beach.jpg"]["source"] == "Old WD" and items["beach.jpg"]["reachable_now"] is False
    assert items["to-the-bank.docx"]["date_from"] == "saved"
    assert items["Wedding plans"]["date_from"] == "sent"


def test_something_that_is_not_a_date_is_a_plain_error_and_reads_nothing(june_index, capsys):
    env, _ids, _ = june_index
    assert run(["timeline", "--month", "banana", "--env", env]) != cli.EXIT_OK
    err = capsys.readouterr().err
    assert "not a date Leasha can read" in err and "Traceback" not in err
    assert run(["timeline", "--after", "soon", "--env", env]) != cli.EXIT_OK
    assert run(["timeline", "--month", "2015-06", "--kind", "nonsense", "--env", env]) != cli.EXIT_OK
    assert "photos" in capsys.readouterr().err                     # it lists the real choices
    assert run(["timeline", "--month", "2015-06", "--cursor", "junk", "--env", env]) != cli.EXIT_OK


def test_an_empty_month_says_so_and_no_fold_lists_every_photograph(tmp_path, capsys):
    env = env_file(tmp_path)
    cli.cmd_init(cli.build_parser().parse_args(["init", "--env", env]))
    with SqliteStore(tmp_path / "data" / "index.db") as store:
        base = 0xAAAA5555AAAA5555
        for i in range(4):
            from tests.unit.timeline_env import add_file, noon

            add_file(store, rf"D:\P\b{i}.jpg", mtime=noon(2019, 1, 1), taken=noon(2015, 6, 10, i),
                     phash=f"{base ^ (1 << i):016x}")
    capsys.readouterr()
    run(["timeline", "--month", "2015-06", "--env", env])
    folded = capsys.readouterr().out
    assert "similar photos" in folded and sum(f"b{i}.jpg" in folded for i in range(4)) == 1
    run(["timeline", "--month", "2015-06", "--no-fold", "--env", env])
    flat = capsys.readouterr().out
    assert all(f"b{i}.jpg" in flat for i in range(4)) and "similar photos" not in flat
    run(["timeline", "--month", "1999-01", "--env", env])
    assert "Nothing in Leasha's records is dated January 1999." in capsys.readouterr().out


def test_the_command_leaves_the_index_exactly_as_it_found_it(june_index):
    env, _ids, tmp = june_index
    target = tmp / "data" / "index.db"

    def snapshot():
        with SqliteStore(target) as store:
            return [tuple(store.conn.execute(f"SELECT COUNT(*), COALESCE(SUM(rowid), 0) FROM {t}").fetchone())
                    for t in ("files", "messages", "volumes", "repos", "chunks")]

    before = snapshot()
    run(["timeline", "--env", env])
    run(["timeline", "--month", "2015-06", "--env", env])
    run(["timeline", "--year", "2015", "--json", "--env", env])
    assert snapshot() == before
