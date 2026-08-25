"""`--force` overrules change detection, which nothing could do before."""
from __future__ import annotations

from app.index.pipeline import PipelineConfig, WalkConfig


def test_force_defaults_to_off():
    """Every run re-reading every file would be a different product."""
    assert PipelineConfig(walk=WalkConfig(roots=[])).force is False


def test_force_is_settable():
    assert PipelineConfig(walk=WalkConfig(roots=[]), force=True).force is True


def test_the_cli_exposes_it():
    """**The escape hatch that was missing.**

    A file whose row says INDEXED but which produced no chunks is skipped for
    ever - reported as unchanged, totals healthy, content not searchable. With
    no way to overrule the detector the only recovery was deleting the database.
    """
    import app.cli as cli

    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        import inspect
        source = inspect.getsource(cli)
        assert '"--force"' in source
        assert 'force=bool(' in source, "the flag is parsed but never reaches the pipeline"
        return
    args = parser.parse_args(["index", "--force", "."])
    assert args.force is True
