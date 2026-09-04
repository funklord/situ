"""CLI tests (project.md section 21)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import argparse

import pytest

from every_schema import ROOT
from every_schema import SCHEMAS as ALL_SCHEMAS
from every_schema import ids
import situc
from situc import layers
from situc.cli import COPYRIGHT, build_parser, main
from situc.parser import parse_text

ROOT = Path(__file__).resolve().parent.parent.parent

SCHEMAS = Path(__file__).resolve().parents[1] / "schema"
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


def test_gen_fuzz_says_how_many_targets_it_emitted(
	tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
	"""A harness with no target is invisible from outside, so it is counted.

	`std/kernels.situ` declares no struct, and a register is a bus
	transaction rather than bytes off a wire, so both produce an
	`LLVMFuzzerTestOneInput` that is `(void)data; (void)size; return 0;`.
	Each compiles, runs, and returns zero for every input -- 16 million
	executions at coverage 1 is what that looks like from the outside, which
	the generator's own comment records having cost once already.

	Neither is a fault. What was missing is that nobody was told: a fuzz run
	over 36 harnesses of which two cannot fail reports 34 tests and two
	tautologies. The other four `gen-*` commands already print a count in
	parentheses; this one printed a filename.
	"""
	root = Path(__file__).resolve().parents[2]

	assert main(["gen-fuzz", str(root / "std" / "kernels.situ"),
	             "--out", str(tmp_path)]) == 0
	assert "(0 fuzz target(s))" in capsys.readouterr().err

	assert main(["gen-fuzz", HEADER, "--out", str(tmp_path)]) == 0
	said = capsys.readouterr().err
	assert "(0 fuzz target(s))" not in said, said
	assert "fuzz target(s))" in said, said

	# The count comes from the artifact, so it cannot drift from it.
	text = (tmp_path / "header_fuzz.c").read_text(encoding="ascii")
	assert f"({text.count('static void fuzz_')} fuzz target(s))" in said


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
	assert "highest-yield class first" in out
	assert "bad.opts: move this variable-length member after the fixed ones" in out
	assert "cost: nothing" in out


def test_the_readme_advise_sample_is_what_the_advisor_prints(
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""The README shows `advise` output. It was hand-maintained, and drifted
	three ways at once: the heading it quoted no longer existed, the cost line
	still read "and every deployed peer" from before that caveat was moved
	after "but", and the yield still read "1 member return ... their
	accessors" from before the verb agreed.

	Every one was a change to the advisor that nobody carried into the
	document, and nothing could have said so: the sample is program output
	pasted into prose, which is a copy that cannot be told from a current one
	by reading it. `test_examples` already holds the README's *counts* to what
	the tree contains, on the same ground.

	Compared with whitespace flattened, since the README wraps the cost line
	to fit its column and the terminal does not.
	"""
	assert main(["advise", str(ROOT / "example/http/http.situ")]) == 0
	printed = capsys.readouterr().out

	readme = (ROOT / "README.md").read_text(encoding="ascii").splitlines()
	start  = next(i for i, line in enumerate(readme)
	              if line.startswith("6 suggestion(s)"))
	end    = next(i for i, line in enumerate(readme[start:], start)
	              if line.startswith("```"))

	def flat(text: str) -> str:
		return " ".join(text.split())

	assert flat(printed).startswith(flat("\n".join(readme[start:end]))), (
		"the README's advise sample is not what the advisor prints")


#: A README block quoting a tool's output: the argv that produces it, the
#: exact line it opens with, and whether its lines can be matched one for
#: one. `explain`'s cannot -- its blame text runs past the page and the
#: README hard-wraps it -- so that one is held to containment, which cannot
#: see an omitted line. The two that are lists are held line for line, which
#: can, and that is where an omission would matter.
QUOTED_OUTPUT = [
	(["explain", "example/http/http.situ", "request_line.version"],
	 "$ situc explain example/http/http.situ request_line.version", False),
	# A prefix, not the whole line: the rest of it is what the check is for.
	(["map", "example/udp/udp.situ"], "struct udp_header size=", True),
	(["doc", "example/udp/udp.situ"],
	 "struct udp_header", True),
]


def readme_block(first: str) -> list[str]:
	"""The fenced block opened by `first`: that line exactly, or, where no
	block opens with exactly it, the one block that opens with it as a
	prefix.

	Both, because neither alone works. `struct udp_header` opens two blocks
	-- the capability map and the `doc` diagram -- so a prefix match takes
	whichever comes first; and an exact anchor is a *copy of the line it is
	checking*, so changing that line breaks the anchor instead of reporting
	the drift. It did: the map's struct line grew its size range and this
	stopped finding the block rather than saying the sample was stale.

	Exact wins where it matches, which is what keeps `struct udp_header`
	pointing at the shorter of the two.
	"""
	lines = (ROOT / "README.md").read_text(encoding="ascii").splitlines()
	opens = [i for i, line in enumerate(lines)
	         if i and lines[i - 1].startswith("```")]

	start = [i for i in opens if lines[i] == first]
	if not start:
		start = [i for i in opens if lines[i].startswith(first)]
	assert len(start) == 1, (
		f"{len(start)} README blocks open with {first!r}; the anchor has to "
		f"name exactly one")

	end = next(i for i, line in enumerate(lines[start[0]:], start[0])
	           if line.startswith("```"))
	return lines[start[0]:end]


def flat(text: str) -> str:
	"""Whitespace collapsed, and a table rule reduced to a token.

	The README narrows its columns to fit the page, so the same row carries
	different padding and the `doc` table's rule is twenty dashes where the
	tool prints forty. Neither is content. What the columns hold is, and
	that is compared as it stands.
	"""
	return re.sub(r"-{3,}", "---", " ".join(text.split()))


@pytest.mark.parametrize("argv,first,line_for_line", QUOTED_OUTPUT,
                         ids=[argv[0] for argv, _, _ in QUOTED_OUTPUT])
def test_a_quoted_output_block_is_what_the_tool_prints(
	argv: list[str], first: str, line_for_line: bool,
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""What a README block shows has to be what the tool prints, and what it
	leaves out has to say so.

	These are program output pasted into prose, and a stale copy cannot be
	told from a current one by reading it -- 26.166, where the `advise`
	sample had drifted three ways at once. Two more had, in the other
	direction: they elided without marking it. The capability map's struct
	line dropped `mutate`, `atomic` and `auth` while the field lines under
	it marked their own cut with `...`, and the `doc` field table dropped
	`destination_port`, which the diagram directly above it draws.

	An unmarked cut is the worse of the two, because a reader takes a
	complete-looking line for a complete one. So the two marks mean two
	different things and are checked differently:

	    `...` alone on a line   lines are omitted here
	    a trailing `..`         this line is cut short, and the next line
	                            shown is the next line printed

	The first version of this test flattened the block and looked for each
	piece in order, which passes a block with a row deleted: containment
	cannot see an absence. Found by sabotage -- deleting `destination_port`
	again left it green -- which is the whole reason the marks are now told
	apart.
	"""
	assert main(argv) == 0
	printed = [flat(line) for line in capsys.readouterr().out.splitlines()]
	printed = [line for line in printed if line]

	shown = [line for line in readme_block(first) if not line.startswith("$ ")]

	if not line_for_line:
		joined, at = " ".join(printed), 0
		for chunk in " \n".join(shown).split("..."):
			text = flat(chunk)
			if not text:
				continue
			found = joined.find(text, at)
			assert found >= 0, f"{argv[0]} does not print:\n{text}"
			at = found + len(text)
		return

	# Line for line, from where the block starts in the real output. The
	# opening line may itself be cut, so it is located by the same rule.
	head  = flat(shown[0])
	head  = head.rstrip(".").rstrip() if head.endswith("..") else head
	start = [i for i, line in enumerate(printed) if line.startswith(head)]
	assert start, f"{argv[0]} prints no line opening the README's block: {head}"
	at    = start[0]
	skip = False
	for line in shown:
		text = flat(line)
		if not text:
			continue
		if text == "...":
			skip = True
			continue

		# A cut is spelled with two dots or three; strip whichever, and do
		# not mistake a sentence's full stop for one.
		cut  = text.endswith("..")
		want = text.rstrip(".").rstrip() if cut else text
		while skip and at < len(printed) and not printed[at].startswith(want):
			at += 1
		skip = False

		assert at < len(printed), f"{argv[0]} stops before the README does: {want}"
		got = printed[at]
		assert got.startswith(want) if cut else got == want, (
			f"the README shows a line {argv[0]} does not print here.\n"
			f"  README: {text}\n  {argv[0]:<7}: {got}\n"
			f"mark an omission with `...` on its own line, and a cut line "
			f"with a trailing `..`")
		at += 1


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
	schema = ROOT / "example" / "udp" / "udp.situ"

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
	schema = ROOT / "example" / "udp" / "udp.situ"

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


def test_the_readme_lists_every_requirement_predicate() -> None:
	"""A table of the language's vocabulary, held to the vocabulary.

	The README's predicate table is generated prose: it says what each
	`require` demands and which axis it reads, and nothing stopped a
	predicate arriving without a row. That is the shape 26.166 found in the
	`advise` sample and 26.172 in the `map` block -- a stale copy of
	program-derived content cannot be told from a current one by reading it.

	Names and axes rather than the summaries. The summaries are one wording
	of the same fact and the README is free to wrap them; which predicates
	exist, and which axis each reads, is not free.
	"""
	from situc.requirements import DEFERRED_PREDICATES, PREDICATES

	readme = (ROOT / "README.md").read_text(encoding="ascii")

	for name, predicate in PREDICATES.items():
		row = f"| `{name}` | {predicate.axis.value} |"
		assert row in readme, f"the README's predicate table has no row for {name}"

	for name in DEFERRED_PREDICATES:
		assert f"| `{name}` |" in readme, \
			f"the README does not say {name} is undecided by this build"


def test_the_readme_lists_every_placed_attribute() -> None:
	"""The same, for the attribute vocabulary.

	`PLACED_ATTRS` is the one source of truth for what may appear in
	brackets, kept that way so a name moving out of `UNPLACED_ATTRS` fails a
	test rather than opening a silent gap. The README documents that
	vocabulary, and six of it were missing when this was written.

	Containment, not a table: some attributes are named in prose and some in
	a table, and which is right depends on the attribute rather than on a
	rule worth enforcing. What must not happen is one going unmentioned.
	"""
	from situc.wellformed import PLACED_ATTRS

	readme  = (ROOT / "README.md").read_text(encoding="ascii")
	missing = sorted(name for name in PLACED_ATTRS if name not in readme)

	assert not missing, f"the README does not mention {', '.join(missing)}"


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


def test_the_version_names_the_copyright_holder(
	capsys: pytest.CaptureFixture[str],
) -> None:
	"""`harmonization.md`: every private project names the holder in its
	`--version`, its About window, and its README. situ has two of the three.

	On two lines, which needed a custom action: argparse's own `version`
	action hands the string to `HelpFormatter`, which wraps it and folds
	whitespace, so `situc 1.0\nCopyright ...` printed as one line with a
	space. It looked close enough to pass a glance.

	Attribution rather than licensing. Naming the holder grants nothing, and
	situ has no licence by decision -- `packaging/copyright` records that,
	and this test exists to keep the *name* present, not to acquire a
	`License:` line beside it.
	"""
	with pytest.raises(SystemExit) as exit_:
		main(["--version"])
	assert exit_.value.code == 0

	printed = capsys.readouterr().out.splitlines()
	assert printed[0] == f"situc {situc.__version__}", printed
	assert printed[1] == COPYRIGHT, printed
	assert "Nabeel Sowan <nabeel@vibes.se>" in COPYRIGHT

	# The README is the other surface, and the same line.
	readme = (Path(__file__).resolve().parents[2] / "README.md").read_text(
		encoding="utf-8")
	assert COPYRIGHT in readme, "the README does not name the holder"


def test_it_names_only_the_flags_that_are_global() -> None:
	"""`--out` and `--target` read like global options and are not; a reader
	who believes the old line writes `situc --target=rust build` and gets a
	usage error.

	`--version` is global and is listed, because a packaged tool that cannot
	say which version it is cannot be evaluated."""
	globals_ = {option
	            for action in build_parser()._actions
	            for option in action.option_strings
	            if option.startswith("--")}
	text = SPEC.read_text(encoding="utf-8")
	section = text[text.index("## 21. CLI surface"):text.index("## 22.")]
	claimed = section[section.index("Two global flags"):]

	# `--help` is argparse's, not situ's, and the section does not list it.
	assert globals_ - {"--help"} == {"--diagnostics", "--version"}
	assert "--diagnostics" in claimed
	assert "--version" in claimed
	for local in ("--out", "--target", "--prefix"):
		assert f"`{local}" in claimed or f"`{local}=" in claimed


# -- every subcommand, over every schema (26.35) ----------------------------

#: The subcommands that take a schema and produce a document. Split from the
#: ones below only by whether they write into a directory.
READS  = ("map", "advise", "wire", "doc", "dump-ast")
WRITES = ("gen-fuzz", "gen-checks", "gen-dissector", "gen-derived",
	"gen-codec-tests", "build", "gen-tamper")

#: `pack` alone means a *file* by `--out`, where the eight above mean a
#: directory. Adding it to `WRITES` failed on every schema with
#: `IsADirectoryError`, which is how the difference was found: one flag
#: spelled the same way in nine subcommands and meaning two things.
WRITES_A_FILE = ("pack",)

#: Subcommands this sweep cannot drive from a schema alone, and what each
#: needs instead. Named rather than absent, because the list above claimed
#: "every subcommand" while covering ten of the parser's nineteen -- and
#: three of the nine it left out took a schema and nothing else.
#:
#: `build` was one of them. It is the primary surface of the whole compiler,
#: and the sweep written to stop a subcommand going unexercised had never run
#: it. `pack` and `gen-tamper` were the other two.
#:
#: Their absence cost nothing measurable: all three run clean over every
#: schema in the tree, and `build` does so on all four targets. That is the
#: point rather than a reprieve -- nothing here knew that, and the test whose
#: name says otherwise was the reason nobody looked.
NEEDS_MORE_THAN_A_SCHEMA = {
	"gen-tests":    "takes a vectors file as well as a schema",
	"verify":       "takes a vectors file as well as a schema",
	"explain":      "takes a path expression as well as a schema",
	"diff":         "takes two schemas, an old and a new",
	"import-proto": "takes a `.proto`, which is not a schema",
	"lsp":          "takes no schema: it serves a protocol on stdin",
}


def test_a_short_out_flag_marks_a_file() -> None:
	"""`--out` means a directory in nine subcommands and a file in two.

	Section 21 said `--out=DIR` flatly, and it is how adding `pack` to the
	sweep above failed on all 37 schemas with `IsADirectoryError`: one flag,
	spelled the same everywhere, meaning two things and documented as one.

	The parser already told them apart and nobody had noticed. `pack` and
	`import-proto` each produce a single artifact rather than a set, and they
	are exactly the two that also accept `-o`. Asserting the correlation is
	what turns an accident into a convention: a subcommand that starts
	writing a file without offering `-o`, or offers `-o` while writing into a
	directory, fails here rather than surprising somebody at a prompt.
	"""
	actions = [action for action in build_parser()._actions
	           if isinstance(action, argparse._SubParsersAction)]
	assert actions, "the parser has subcommands"

	short: set[str] = set()
	takes_out: set[str] = set()
	for name, parser in actions[0].choices.items():
		for action in parser._actions:
			if "--out" not in action.option_strings:
				continue
			takes_out.add(name)
			if "-o" in action.option_strings:
				short.add(name)

	assert takes_out, "no subcommand takes --out, so this checks nothing"
	assert short == set(WRITES_A_FILE) | {"import-proto"}, (
		f"`-o` is offered by {sorted(short)}; the subcommands that write a "
		f"single file are `pack` and `import-proto`. One of the two has "
		f"moved, and section 21 describes the pairing")


def test_every_subcommand_is_swept_or_excused() -> None:
	"""The sweep's population comes from the parser, not from this file.

	`READS + WRITES` was ten names typed here, under a test called
	`test_every_subcommand_runs_on_every_schema`. The parser has nineteen.
	Three of the nine missing needed only a schema and were simply absent,
	`build` among them.

	A twentieth subcommand now fails here until it is swept or excused, which
	is the property the name was already claiming.
	"""
	actions = [action for action in build_parser()._actions
	           if isinstance(action, argparse._SubParsersAction)]
	assert actions, "the parser has subcommands"
	available = set(actions[0].choices)

	classified = (set(READS) | set(WRITES) | set(WRITES_A_FILE)
	              | set(NEEDS_MORE_THAN_A_SCHEMA))
	assert classified == available, (
		f"unclassified: {sorted(available - classified)}; "
		f"named but absent from the parser: {sorted(classified - available)}")


@pytest.mark.parametrize("schema", ALL_SCHEMAS, ids=ids(ALL_SCHEMAS))
@pytest.mark.parametrize("command", READS + WRITES + WRITES_A_FILE)
def test_every_subcommand_runs_on_every_schema(
		command: str, schema: Path, tmp_path: Path) -> None:
	"""A subcommand is a product surface, and a schema is what it is for.
	Every pair had been exercised by *some* test on *some* schema, which is
	not the same claim.

	It found `situc dump-ast` dying with a Python traceback -- `TypeError:
	cannot dump Invariant` -- on any schema carrying an invariant.
	`test/schema/edges.situ` has carried one since invariants landed, and
	the phase 1 deliverable had never been pointed at it (26.35).

	This asks only that the command succeeds. What each one *says* is the
	business of the tests above; a crash is the failure that makes those
	moot.
	"""
	argv = [command, str(schema)]
	if command in WRITES:
		argv += ["--out", str(tmp_path)]
	elif command in WRITES_A_FILE:
		argv += ["--out", str(tmp_path / "packed.bin")]

	assert main(argv) == 0


#: A relation that is validated, carried in the committed contract, and
#: generates nothing: two arrays ordered with `>=`, where a key answers only
#: equality. This is the shape `suggestion/fuzznet.md` walked into twice.
UNGENERATED = """target buffer;
endian big;

struct msg {
	u8  tag[8];
	u16 seq;
}

relation answers(query: msg, reply: msg) {
	must reply.tag >= query.tag;
}
"""


#: A schema whose codec expands without a bound, which is 0031's case E: the
#: measure pass *is* the work, so rung 1 has nowhere to put the output.
#:
#: **No committed schema has one.** `layers.floor` returns "view" for every
#: one of the tree's schemas, so its `edit` branch, `allocating()` returning
#: non-empty, and `allocates()` answering True had never run -- and neither
#: had the refusal below, which is the gate that makes the ladder mean
#: anything (26.221).
ALLOCATING = """target buffer;
endian big;
bit_order msb_first;

codec squash {
	expansion = unbounded;
	granularity = stream;
	not seekable;
	invertible;
}

impl squash extern "my_squash";

struct payload {
	u8  n;
	coded body(squash) {
		u8 content[n];
	}
	u8  trailer;
}
"""


def test_the_lowest_rung_refuses_what_it_cannot_put_anywhere(
		tmp_path: Path) -> None:
	"""The ladder's one enforcement, produced for the first time.

	`--layer view` is rung 1 and emits no storage. A region behind a codec
	with unbounded expansion has an extent nothing knows until the transform
	has run, so rung 1 cannot hold the result -- which is the whole reason
	decision 0032 has a second rung.

	The diagnostic is asserted rather than only the exit, because this one
	had never been produced: it names the member, why the rung cannot take
	it, and which rung can. A refusal nobody has read is a sentence nobody
	has checked.
	"""
	schema = tmp_path / "a.situ"
	schema.write_text(ALLOCATING, encoding="ascii")

	with pytest.raises(SystemExit) as refused:
		main(["build", str(schema), "--target", "c", "--layer", "view",
		      "--out", str(tmp_path / "out")])

	said = str(refused.value)
	assert "--layer view cannot emit `payload.body`" in said
	assert "expands without a bound" in said
	assert "--layer edit" in said


def test_the_rung_above_it_takes_the_same_schema(tmp_path: Path) -> None:
	"""The other half, without which the refusal could be unconditional.

	A gate that refused at every rung would pass the test above and be
	useless, which is 154's shape: two states rendered alike.
	"""
	schema = tmp_path / "a.situ"
	schema.write_text(ALLOCATING, encoding="ascii")
	out = tmp_path / "out"

	assert main(["build", str(schema), "--target", "c", "--layer", "edit",
	             "--out", str(out)]) == 0
	assert (out / "a.h").exists()


def test_the_floor_is_the_rung_the_schema_needs() -> None:
	"""`floor` and `allocating` themselves, since the CLI is one caller of
	three -- `capmap` prints the answer and `require no_alloc(X)` is
	discharged from it, and a wrong answer there is silent.

	`allocates` is asked about a member, its struct, and two paths that are
	neither. The prefix match answers the first two without the caller
	saying which it meant; `payload.bod` is what holds it to a *path* prefix
	rather than a string one, and it is the case that discriminates.
	Sabotaged: replacing `one == path or one.startswith(f"{path}.")` with a
	plain `one.startswith(path)` leaves `payload.trailer` answering
	correctly and only `payload.bod` goes wrong, so a test without it passes
	over the defect it was written for.
	"""
	schema = parse_text(ALLOCATING)

	assert layers.unbounded_codecs(schema) == {"squash"}
	assert layers.allocating(schema) == {"payload.body"}
	assert layers.floor(schema) == "edit"

	assert layers.allocates(schema, None, "payload.body") is True   # type: ignore[arg-type]
	assert layers.allocates(schema, None, "payload") is True        # type: ignore[arg-type]
	assert layers.allocates(schema, None, "payload.trailer") is False  # type: ignore[arg-type]
	assert layers.allocates(schema, None, "payload.bod") is False      # type: ignore[arg-type]


def test_an_ungenerated_relation_is_a_notice_by_default(
		tmp_path: Path) -> None:
	"""The default does not move. Such a schema is not wrong -- refusing it
	would stop one that is otherwise fine -- so it stays a notice and a
	zero exit, which is what the asker explicitly did not want changed."""
	schema = tmp_path / "u.situ"
	schema.write_text(UNGENERATED, encoding="ascii")

	assert main(["build", str(schema), "--target", "c", "--layer", "relate",
	             "--out", str(tmp_path / "out")]) == 0


def test_refuse_ungenerated_fails_on_a_relation_that_emits_nothing(
		tmp_path: Path) -> None:
	"""And with the flag it is a failure, naming the relation.

	A declaration that is validated, appears in the contract and compiles to
	nothing leaves its only evidence on stdout during a build, which is how
	one consumer shipped past it twice.
	"""
	schema = tmp_path / "u.situ"
	schema.write_text(UNGENERATED, encoding="ascii")

	out = tmp_path / "out"
	with pytest.raises(SystemExit) as refused:
		main(["build", str(schema), "--target", "c", "--layer", "relate",
		      "--refuse-ungenerated", "--out", str(out)])

	assert "answers" in str(refused.value)
	assert "no predicate" in str(refused.value)
	# Nothing on disk: a build that is going to fail must not leave half an
	# answer behind for the next reader to trust.
	assert not out.exists() or not list(out.iterdir())


@pytest.mark.parametrize("schema", ["example/packet/packet.situ",
                                    "test/schema/edges.situ"])
def test_refuse_ungenerated_does_not_refuse_a_shape_that_has_no_owned_form(
		schema: str, tmp_path: Path) -> None:
	"""The discriminating case, and the one a naive flag gets wrong.

	`packet` reports one refusal and `edges` eight, all of them "no owned
	form for X" -- a fact about a shape the data decides, where the schema
	declared nothing that then vanished. Folding those into the flag would
	refuse two schemas that are entirely fine, so what it counts is
	relations only: no predicate, no conversation table, no driver.
	"""
	assert main(["build", str(ROOT / schema), "--target", "c",
	             "--layer", "drive", "--refuse-ungenerated",
	             "--out", str(tmp_path / "out")]) == 0
