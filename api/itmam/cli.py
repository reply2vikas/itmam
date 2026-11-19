"""Command line: `python -m itmam.cli check [dir]` validates every pathway file;
`python -m itmam.cli schema` prints the pathway JSON Schema."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from itmam.domain.pathway import default_pathways_dir, json_schema, load_pathways


def main(argv: list[str]) -> int:
    if argv[:1] == ["schema"]:
        print(json.dumps(json_schema(), indent=2))
        return 0
    if argv[:1] == ["check"]:
        directory = Path(argv[1]) if len(argv) > 1 else default_pathways_dir()
        try:
            pathways = load_pathways(directory)
        except (ValidationError, ValueError) as exc:
            print(f"invalid pathway configuration:\n{exc}", file=sys.stderr)
            return 1
        for pathway in pathways.values():
            print(f"ok  {pathway.id} v{pathway.version}  {pathway.name}")
        return 0
    print("usage: python -m itmam.cli [schema | check [dir]]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
