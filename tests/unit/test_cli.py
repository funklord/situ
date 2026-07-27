"""CLI tests (project.md section 21)."""

from __future__ import annotations

import json
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
	("advise",	9),
	("diff",	9),
	("import-proto", 13),
])
def test_future_commands_name_their_phase(
	command: str, phase: int, capsys: pytest.CaptureFixture[str],
) -> None:
	assert main([command, HEADER]) == 2
	assert f"phase {phase}" in capsys.readouterr().err


def test_json_diagnostics_are_emitted(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Machine-readable diagnostics so CI and editors need not parse prose."""
	schema = tmp_path / "d.situ"
	schema.write_text(
		"endian big;\nstruct S { u8 a; }\nrequire no_realloc(S);\n",
		encoding="ascii")

	assert main(["--diagnostics=json", "map", str(schema)]) == 0

	payload = json.loads(capsys.readouterr().err)
	assert len(payload["diagnostics"]) == 1
	assert payload["diagnostics"][0]["severity"] == "note"
	assert "phase 5" in payload["diagnostics"][0]["message"]


def test_json_diagnostics_on_failure(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	schema = tmp_path / "d.situ"
	schema.write_text(
		"endian big;\nstruct S { u8 a; }\nrequire size(S) == 4;\n", encoding="ascii")

	assert main(["--diagnostics=json", "map", str(schema)]) == 1

	payload = json.loads(capsys.readouterr().err)
	assert payload["diagnostics"][0]["severity"] == "error"
	assert payload["diagnostics"][0]["primary"]["line"] == 3


def test_explain_prints_a_vector_and_blame(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["explain", HEADER, "header.seq"]) == 0

	out = capsys.readouterr().out
	assert "offset     AbsoluteStatic(0x05)" in out
	assert "repr       ValueConverted  <- weakened" in out
	assert "blame:" in out


def test_explain_on_a_struct(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["explain", HEADER, "header"]) == 0
	assert "struct header" in capsys.readouterr().out


def test_explain_on_an_unknown_path(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["explain", HEADER, "header.nope"]) == 1
	assert "unknown path" in capsys.readouterr().err


def test_gen_fuzz_writes_a_harness(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	assert main(["gen-fuzz", HEADER, "--out", str(tmp_path)]) == 0
	text = (tmp_path / "header_fuzz.c").read_text(encoding="ascii")
	assert "LLVMFuzzerTestOneInput" in text
	assert "wrote" in capsys.readouterr().err


def test_gen_tests_writes_a_vector_suite(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	vectors = str(Path(HEADER).with_suffix(".vectors"))
	assert main(["gen-tests", HEADER, vectors, "--out", str(tmp_path)]) == 0

	text = (tmp_path / "header_vectors.c").read_text(encoding="ascii")
	assert "cmocka_run_group_tests" in text
	assert "4 vectors" in capsys.readouterr().err


def test_gen_tests_reports_a_stale_vector(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	bad = tmp_path / "bad.vectors"
	bad.write_text("header short 01 02\n", encoding="ascii")

	assert main(["gen-tests", HEADER, str(bad), "--out", str(tmp_path)]) == 1
	assert "is 2 bytes, but `header` is 9" in capsys.readouterr().err


def test_build_writes_the_generated_pair(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	assert main(["build", HEADER, "--out", str(tmp_path)]) == 0

	header = (tmp_path / "header.h").read_text(encoding="ascii")
	source = (tmp_path / "header.c").read_text(encoding="ascii")
	assert "#define SITU_HEADER_SIZE_FIXED 9u" in header
	assert '#include "header.h"' in source
	assert "wrote" in capsys.readouterr().err


def test_map_command_emits_the_map(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["map", HEADER]) == 0
	assert capsys.readouterr().out.startswith("# situ capability map")


def test_map_summary_format(capsys: pytest.CaptureFixture[str]) -> None:
	assert main(["map", "--format=summary", HEADER]) == 0
	assert "header: 9 bytes" in capsys.readouterr().out


def test_unknown_command_is_an_argparse_error() -> None:
	with pytest.raises(SystemExit):
		main(["nonsense", HEADER])
