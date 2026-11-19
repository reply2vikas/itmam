from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from itmam.cli import main as cli
from itmam.domain.pathway import PathwayConfig, default_pathways_dir, json_schema, load_pathways

from support import P1, P2, P3, PATHWAYS


def p1_data() -> dict[str, Any]:
    with (default_pathways_dir() / f"{P1}.yaml").open(encoding="utf-8") as handle:
        data: dict[str, Any] = yaml.safe_load(handle)
    return data


def test_all_three_pathways_load() -> None:
    assert set(PATHWAYS) == {P1, P2, P3}
    assert PATHWAYS[P2].subjects.count == 2
    assert PATHWAYS[P3].subjects.contact == "guardian"


def test_cli_check_and_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli(["check"]) == 0
    assert P1 in capsys.readouterr().out
    assert cli(["schema"]) == 0
    assert "stages" in json_schema()["properties"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["stages"]["book"].update(owner="sender"), "owner must be receiver"),
        (lambda d: d["stages"]["notify"].update(warn="5wd"), "warn must come before breach"),
        (lambda d: d["stages"]["accept"].pop("on_reject"), "on_reject is required"),
        (lambda d: d["stages"].pop("close"), "missing stages"),
        (lambda d: d.pop("watch"), "creates_watch needs a 'watch' block"),
        (
            lambda d: d["closures"]["unreachable"].pop("min_contact_attempts"),
            "needs min_contact_attempts",
        ),
        (lambda d: d["care_clock"].update(target="200d"), "target must come before hard_limit"),
        (lambda d: d["roles"].update(receiver="screening_coordinator"), "must be distinct"),
        (lambda d: d["stages"]["refer"].update(breach="5 days"), "not a duration"),
        (lambda d: d.update(colour="blue"), "Extra inputs are not permitted"),
    ],
)
def test_linter_rejects_unsafe_configuration(change: Any, message: str) -> None:
    data = p1_data()
    change(data)
    with pytest.raises(ValidationError, match=message):
        PathwayConfig.model_validate(data)


def test_result_codes_must_be_unique_across_pathways(tmp_path: Path) -> None:
    data = p1_data()
    for name in ("a", "b"):
        data["id"] = f"copy-{name}"
        (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError, match="is used by both"):
        load_pathways(tmp_path)
