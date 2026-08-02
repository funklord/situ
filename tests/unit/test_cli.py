"""CLI tests (project.md section 21)."""

from __future__ import annotations

import json
from pathlib import Path

import argparse

import pytest

from every_schema import SCHEMAS as ALL_SCHEMAS
from every_schema import ids
from situc.cli import build_parser, main

ROOT = Path(__file__).resolve().parent.parent.parent

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


def test_an_unplanned_command_is_refused() -> None:
	"""FUTURE_COMMANDS is empty now, so the mechanism it feeds has nothing to
	explain. What must still hold is that a name nobody defined is an error
	rather than a traceback."""
	with pytest.raises(SystemExit):
		main(["gen-telepathy", HEADER])


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
	assert "not checked by this build" in payload["diagnostics"][0]["message"]


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


# -- the advisor (phase 9) --------------------------------------------------


BADLY_ORDERED = ("endian big;\n"
	"struct bad { u16 n [max = 8]; u8 opts[n]; u32 seq; }\n")


def test_advise_prints_ranked_suggestions(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	schema = tmp_path / "bad.situ"
	schema.write_text(BADLY_ORDERED, encoding="ascii")

	assert main(["advise", str(schema)]) == 0

	out = capsys.readouterr().out
	assert "highest yield first" in out
	assert "bad.opts: move this variable-length member after the fixed ones" in out
	assert "cost: nothing" in out


def test_advise_never_fails_the_build(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""A suggestion is advice about a design, not a verdict on one.

	A build that failed on advice would teach people to stop reading it.
	"""
	schema = tmp_path / "bad.situ"
	schema.write_text(BADLY_ORDERED, encoding="ascii")

	assert main(["advise", str(schema)]) == 0
	capsys.readouterr()


def test_advise_emits_json(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	schema = tmp_path / "bad.situ"
	schema.write_text(BADLY_ORDERED, encoding="ascii")

	assert main(["advise", str(schema), "--format=json"]) == 0

	payload = json.loads(capsys.readouterr().out)
	first   = payload["suggestions"][0]
	assert first["rule"] == "move-dynamic-to-tail"
	assert first["cost"]["worst"] == 0


def test_advise_says_so_when_there_is_nothing_to_say(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	schema = tmp_path / "fine.situ"
	schema.write_text("endian big;\nstruct fine { u32 a; u16 b; }\n", encoding="ascii")

	assert main(["advise", str(schema)]) == 0
	assert "No suggestions" in capsys.readouterr().out


# -- map --check (section 18.1) ---------------------------------------------


def _with_map(tmp_path: Path, body: str) -> Path:
	schema = tmp_path / "s.situ"
	schema.write_text(body, encoding="ascii")
	main(["map", str(schema)])
	return schema


def test_map_check_passes_on_a_current_map(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	schema = _with_map(tmp_path, "endian big;\nstruct s { u32 a; }\n")
	(tmp_path / "s.situ.map").write_text(capsys.readouterr().out, encoding="ascii")

	assert main(["map", str(schema), "--check"]) == 0
	assert "is current" in capsys.readouterr().err


def test_map_check_fails_on_an_uncommitted_capability_change(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""The point of committing the map: a regression is a reviewable diff at
	the moment of editing rather than a surprise months later."""
	schema = _with_map(tmp_path, "endian big;\nstruct s { u32 a; }\n")
	(tmp_path / "s.situ.map").write_text(capsys.readouterr().out, encoding="ascii")

	schema.write_text("endian big;\nstruct s { u32 a; u16 b; }\n", encoding="ascii")

	assert main(["map", str(schema), "--check"]) == 1

	captured = capsys.readouterr()
	assert "the capability map of s.situ has changed" in captured.err
	assert "+  s.b " in captured.out or "+  s.b" in captured.out


def test_map_check_says_what_to_do_when_no_map_is_committed(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	schema = tmp_path / "s.situ"
	schema.write_text("endian big;\nstruct s { u32 a; }\n", encoding="ascii")

	assert main(["map", str(schema), "--check"]) == 1
	assert "no committed map" in capsys.readouterr().err


# -- diff (section 18.3) ----------------------------------------------------


def test_diff_exits_non_zero_on_a_regression(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	old = tmp_path / "old.situ"
	new = tmp_path / "new.situ"
	old.write_text("endian big;\nstruct r { u16 n [max = 8]; u8 body[8]; }\n",
	               encoding="ascii")
	new.write_text("endian big;\nstruct r { u16 n [max = 8]; u8 body[n]; }\n",
	               encoding="ascii")

	assert main(["diff", str(old), str(new)]) == 1
	assert "mutate InPlaceFixed -> Shifting" in capsys.readouterr().out


def test_diff_exits_zero_when_nothing_regressed(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	old = tmp_path / "old.situ"
	new = tmp_path / "new.situ"
	old.write_text("endian big;\nstruct r { u32 a; }\n", encoding="ascii")
	new.write_text("endian big;\nstruct r { u32 a; u32 b; }\n", encoding="ascii")

	assert main(["diff", str(old), str(new)]) == 0
	capsys.readouterr()


def test_diff_describes_a_revision_whose_budget_is_blown(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""Requirements are not discharged for a diff.

	A revision that broke its budget is exactly the one worth diffing, and
	refusing to describe it would withhold the explanation when it is wanted.
	"""
	old = tmp_path / "old.situ"
	new = tmp_path / "new.situ"
	old.write_text("endian big;\nstruct r { u32 a; }\nrequire size(r) == 4;\n",
	               encoding="ascii")
	new.write_text("endian big;\nstruct r { u32 a; u16 b; }\nrequire size(r) == 4;\n",
	               encoding="ascii")

	assert main(["diff", str(old), str(new)]) == 0
	assert "+ r.b" in capsys.readouterr().out


def test_doc_writes_a_file(tmp_path: Path) -> None:
	"""`--out` names a directory; the format decides the suffix."""
	schema = ROOT / "examples" / "udp" / "udp.situ"

	assert main(["doc", str(schema), "--out", str(tmp_path)]) == 0
	assert (tmp_path / "udp.txt").read_text(encoding="ascii").count("+-+-+") > 0

	assert main(["doc", str(schema), "--out", str(tmp_path),
	             "--format", "markdown"]) == 0
	assert (tmp_path / "udp.md").read_text(encoding="ascii").startswith("# udp.situ")


def test_every_planned_command_has_landed() -> None:
	"""Section 21 named the CLI surface up front. Nothing on it is pending."""
	from situc.cli import FUTURE_COMMANDS

	assert FUTURE_COMMANDS == {}


def test_gen_dissector_writes_lua(tmp_path: Path) -> None:
	schema = ROOT / "examples" / "udp" / "udp.situ"

	assert main(["gen-dissector", str(schema), "--out", str(tmp_path)]) == 0
	lua = (tmp_path / "udp.lua").read_text(encoding="ascii")

	assert 'Proto("udp_header"' in lua
	assert "ProtoField.uint16" in lua


# -- the CLI section of the specification (21) ------------------------------

SPEC = ROOT / "project.md"


def spec_block() -> str:
	text = SPEC.read_text(encoding="utf-8")
	section = text[text.index("## 21. CLI surface"):text.index("## 22.")]
	# From after the opening fence to the closing one. Searching from index 4
	# found the opening fence again, and the block came out empty -- a test
	# that compared nothing to everything and failed for the right reason by
	# luck rather than by design.
	start = section.index("```") + 3
	return section[start:section.index("```", start)]


def test_the_cli_section_lists_every_command() -> None:
	"""Section 21 is what a reader types from, and it had drifted: it named a
	`--strict` that never existed and called three per-subcommand flags
	global. The 11.3 table had drifted the same way for the same reason --
	nothing was checking."""
	listed = {line.split()[1] for line in spec_block().splitlines()
	          if line.startswith("situc ") and len(line.split()) > 1}

	actions = [action for action in build_parser()._actions
	           if isinstance(action, argparse._SubParsersAction)]
	assert actions, "the parser has subcommands"

	assert listed == set(actions[0].choices)


def test_it_names_only_the_flags_that_are_global() -> None:
	"""`--out` and `--target` read like global options and are not; a reader
	who believes the old line writes `situc --target=rust build` and gets a
	usage error."""
	globals_ = {option
	            for action in build_parser()._actions
	            for option in action.option_strings
	            if option.startswith("--")}
	text = SPEC.read_text(encoding="utf-8")
	section = text[text.index("## 21. CLI surface"):text.index("## 22.")]
	claimed = section[section.index("One global flag"):]

	# `--help` is argparse's, not situ's, and the section does not list it.
	assert globals_ - {"--help"} == {"--diagnostics"}
	assert "--diagnostics" in claimed
	for local in ("--out", "--target", "--prefix"):
		assert f"`{local}" in claimed or f"`{local}=" in claimed


# -- every subcommand, over every schema (26.35) ----------------------------

#: The subcommands that take a schema and produce a document. Split from the
#: ones below only by whether they write into a directory.
READS  = ("map", "advise", "wire", "doc", "dump-ast")
WRITES = ("gen-fuzz", "gen-checks", "gen-dissector", "gen-derived",
	"gen-codec-tests")


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=ids(ALL_SCHEMAS))
@pytest.mark.parametrize("command", READS + WRITES)
def test_every_subcommand_runs_on_every_schema(
		command: str, schema: Path, tmp_path: Path) -> None:
	"""A subcommand is a product surface, and a schema is what it is for.
	Every pair had been exercised by *some* test on *some* schema, which is
	not the same claim.

	It found `situc dump-ast` dying with a Python traceback -- `TypeError:
	cannot dump Invariant` -- on any schema carrying an invariant.
	`tests/schemas/edges.situ` has carried one since invariants landed, and
	the phase 1 deliverable had never been pointed at it (26.35).

	This asks only that the command succeeds. What each one *says* is the
	business of the tests above; a crash is the failure that makes those
	moot.
	"""
	argv = [command, str(schema)]
	if command in WRITES:
		argv += ["--out", str(tmp_path)]

	assert main(argv) == 0
