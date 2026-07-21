"""Standalone core-wheel smoke for research-tool discovery and immutable sessions."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from installed_study_smoke import _seed_database, _specification

from wartosc_perp_research import cli


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=False)
    database = root / "research.db"
    study = root / "study.json"
    session_spec = root / "session-spec.json"
    request = root / "request.json"
    session = root / "session"
    first_export = root / "session-export-a.json"
    second_export = root / "session-export-b.json"

    _seed_database(database)
    _write_json(study, _specification())
    _write_json(
        session_spec,
        {
            "objective": "Exercise the deterministic installed-wheel research-tool vertical.",
            "schema_version": 1,
            "session_id": "installed-research-session",
        },
    )
    _write_json(
        request,
        {
            "arguments": {
                "database": "research.db",
                "output": "study-bundle",
                "specification": "study.json",
            },
            "schema_version": 1,
            "tool_name": "historical_study.run",
        },
    )

    assert cli.main(["research", "tools", "list"]) == 0
    assert cli.main(["research", "tools", "describe", "historical_study.run"]) == 0
    assert (
        cli.main(
            [
                "research",
                "session",
                "create",
                "--spec",
                str(session_spec),
                "--output",
                str(session),
            ]
        )
        == 0
    )
    invoke = [
        "research",
        "session",
        "invoke",
        "--session",
        str(session),
        "--request",
        str(request),
    ]
    assert cli.main(invoke) == 0
    segment_bytes = {path.name: path.read_bytes() for path in (session / "events").iterdir()}
    assert cli.main(invoke) == 0
    assert {
        path.name: path.read_bytes() for path in (session / "events").iterdir()
    } == segment_bytes
    assert cli.main(["research", "session", "inspect", "--session", str(session)]) == 0
    assert cli.main(["research", "session", "verify", "--session", str(session)]) == 0
    for output in (first_export, second_export):
        assert (
            cli.main(
                [
                    "research",
                    "session",
                    "export",
                    "--session",
                    str(session),
                    "--output",
                    str(output),
                ]
            )
            == 0
        )
    assert first_export.read_bytes() == second_export.read_bytes()
    exported = json.loads(first_export.read_text(encoding="utf-8"))
    assert exported["session"]["session_id"] == "installed-research-session"
    assert exported["events"][-1]["event_type"] in {
        "output_artifact_references",
        "tool_warning",
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: installed_research_session_smoke.py OUTPUT_ROOT")
    main(Path(sys.argv[1]))
