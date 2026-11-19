import pytest

from itmam.demo import main


def test_narrated_demo_runs_clean(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    out = capsys.readouterr().out
    assert "Invariant violations: 0" in out
    assert "{'received': 2, 'open': 1, 'closed': 1}" in out
    assert "chain problems: 0" in out
