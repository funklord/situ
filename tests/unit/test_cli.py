"""CLI tests (project.md section 21)."""

from __future__ import annotations

from pathlib import Path

import pytest

from situc.cli import main

SCHEMAS = Path(__file__).resolve().parents[1] / "schemas"
HEADER  = str(SCHEMAS / "header.situ")


def test_dump_ast_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["dump-ast", HEADER]) == 0
	assert capsys.readouterr().out.startswith("schema\n")


def test_dump_ast_source_format(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["dump-ast", "--format=source", HEADER]) == 0
	assert capsys.readouterr().out.startswith("target buffer;\n")


def test_parse_error_is_rendered_and_exits_nonzero(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	schema = tmp_path / "bad.situ"
	schema.write_text("struct S { u8 a }\n", encoding="ascii")

	assert main(["dump-ast", str(schema)]) == 1

	captured = capsys.readouterr()
	assert captured.out == ""
	assert "expected `;`" in captured.err
	assert "--> " in captured.err


def test_missing_file_reports_cleanly(tmp_path: Path) -> None:
	with pytest.raises(SystemExit) as caught:
		main(["dump-ast", str(tmp_path / "absent.situ")])
	assert "cannot read" in str(caught.value)


@pytest.mark.parametrize(("command", "phase"), [
	("build",	4),
	("gen-tests",	4),
	("advise",	9),
	("explain",	9),
	("diff",	9),
	("import-proto", 13),
])
def test_future_commands_name_their_phase(
	command: str, phase: int, capsys: pytest.CaptureFixture[str],
) -> None:
	assert main([command, HEADER]) == 2
	assert f"phase {phase}" in capsys.readouterr().err


def test_json_diagnostics_deferred_to_phase_three(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["--diagnostics=json", "dump-ast", HEADER]) == 2
	assert "phase 3" in capsys.readouterr().err


def test_unknown_command_is_an_argparse_error() -> None:
	with pytest.raises(SystemExit):
		main(["nonsense", HEADER])
