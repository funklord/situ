"""The source conventions of project.md section 25 are checked by the test
suite, not only by `make lint`, so a violation fails CI wherever it enters."""

from __future__ import annotations

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
