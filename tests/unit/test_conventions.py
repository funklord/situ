"""The source conventions of project.md section 25 are checked by the test
suite, not only by `make lint`, so a violation fails CI wherever it enters."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import lint_conventions  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def test_tree_follows_conventions() -> None:
	problems = []
	for path in lint_conventions.iter_sources(ROOT):
		problems.extend(lint_conventions.check_file(path, ROOT))

	assert not problems, "\n" + "\n".join(str(problem) for problem in problems)


def test_linter_flags_space_indent(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("int main(void)\n{\n    return 0;\n}\n", encoding="ascii")

	problems = lint_conventions.check_file(source, tmp_path)

	assert [problem.message for problem in problems] == ["space-indented line; use tabs"]


def test_linter_flags_space_before_tab(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("int main(void)\n{\n \treturn 0;\n}\n", encoding="ascii")

	problems = lint_conventions.check_file(source, tmp_path)

	assert [problem.message for problem in problems] == ["space before tab in indent"]


def test_linter_allows_alignment_after_tab(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("void f(void)\n{\n\tg(a,\n\t  b);\n}\n", encoding="ascii")

	assert lint_conventions.check_file(source, tmp_path) == []


def test_linter_allows_block_comment_continuation(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("/* one\n * two\n */\n", encoding="ascii")

	assert lint_conventions.check_file(source, tmp_path) == []


def test_linter_flags_non_ascii(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	# Spelled as bytes so this file stays ASCII and passes its own check.
	source.write_bytes(b"/* caf\xc3\xa9 */\n")

	problems = lint_conventions.check_file(source, tmp_path)

	assert len(problems) == 1
	assert problems[0].message.startswith("non-ASCII byte")


def test_linter_flags_trailing_whitespace(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("int x;\t\n", encoding="ascii")

	problems = lint_conventions.check_file(source, tmp_path)

	assert [problem.message for problem in problems] == ["trailing whitespace"]


def test_linter_flags_missing_final_newline(tmp_path: Path) -> None:
	source = tmp_path / "sample.c"
	source.write_text("int x;", encoding="ascii")

	problems = lint_conventions.check_file(source, tmp_path)

	assert [problem.message for problem in problems] == ["no newline at end of file"]


def test_linter_ignores_python_string_content(tmp_path: Path) -> None:
	"""Golden diagnostic texts have a space gutter that is content, not indent."""
	source = tmp_path / "sample.py"
	source.write_text('EXPECTED = """\n   |\n41 | require x;\n   | ^^^^^^^^^^\n"""\n',
	                  encoding="ascii")

	assert lint_conventions.check_file(source, tmp_path) == []


# -- the repository layout section (23) -------------------------------------


def test_the_layout_section_lists_every_compiler_module() -> None:
	"""Section 23 is a map of the tree, and `unparse.py` was missing from it.

	A map with a module absent is worse than no map: a reader looking for
	where an expression becomes text again concludes there is nowhere, which
	is how a sixth copy of something gets written.
	"""
	text    = (ROOT / "project.md").read_text(encoding="utf-8")
	section = text[text.index("## 23. Repository layout"):text.index("### 23.1")]
	block   = section[section.index("  situc/"):]
	# To the next entry at exactly two spaces. Slicing at `"\n  "` cut at the
	# first *four*-space line instead, because two spaces is a prefix of four,
	# and the block came out one line long.
	end     = re.search(r"\n  (?=\S)", block[1:])
	block   = block if end is None else block[:end.end()]

	listed = set(re.findall(r"^\s{4}([a-z_0-9]+\.py)\b", block, re.M))
	real   = {path.name for path in (ROOT / "situc").glob("*.py")}

	assert listed == real
