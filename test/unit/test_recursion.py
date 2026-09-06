"""Recursive types, bounded by a declared depth (`doc/decision/0054`).

Section 2 refused every cycle for as long as situ existed, on the grounds
that recursion makes size and capability computation non-terminating. That
reasoning was sound and none of it needed a prohibition: each of the four
things it protects -- the size, the lattice walk, `--owned`, and the walkers'
bounded stacks -- needs a *bound*, and a declared depth is one.

What the refusal meant concretely is worth knowing, because it is the whole
of what these tests hold: `layout_of` re-entered for the same struct name and
died in `Scope.narrow` at Python's recursion limit. One guard stops it, and
the placeholder it returns is a variable-length nested member, which every
backend already understands.

**Describable is not generable, and that line is tested here too.** `map`,
`wire` and `verify` are correct for a recursive type; `build` refuses one,
because every backend would emit an `extent` that calls itself -- run-time
recursion in generated code that 20.1 promises has none, with neither
`depth` nor `limit` enforced anywhere. A header that does not compile is
worse than a refusal (26.69).
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

PREAMBLE = "target buffer;\nendian big;\n"

NODE = ("struct node [depth = 32] {\n"
        "\tu8   kind;\n"
        "\tu16  count;\n"
        "\tnode children[count];\n"
        "}\n")


def layout(body: str):
	schema = parse_text(PREAMBLE + body)
	return resolve(schema, solve(schema))


def test_a_struct_that_names_itself_is_described() -> None:
	"""The solver terminates, and the member it could not measure before is
	an ordinary variable-length one."""
	resolved = layout(NODE)
	paths    = {entry.placement.path for entry in resolved.structs["node"].entries}
	assert {"node.kind", "node.count", "node.children"} <= paths

	children = next(e.placement for e in resolved.structs["node"].entries
	                if e.placement.path == "node.children")
	assert children.type_name == "node"


def test_recursion_without_a_depth_is_still_refused() -> None:
	"""The prohibition survives where the bound is absent: a format whose
	nesting is unbounded is one whose worst case nobody has considered, which
	is a finding rather than a gap in the language. And the diagnostic carries
	the remedy now, which it could not before there was one."""
	with pytest.raises(SituError) as caught:
		layout(NODE.replace(" [depth = 32]", ""))
	rendered = str(caught.value) + "".join(getattr(caught.value, "notes", []))
	assert "contains itself" in rendered


def test_a_mutual_cycle_is_still_refused() -> None:
	"""0054 leaves it open deliberately. Nothing in the mechanism needs the
	recursion to be single -- the bound applies to a strongly connected
	component as readily as to a struct -- but the diagnostic wants thought,
	since naming one struct of a two-struct cycle sends a reader to whichever
	happened to be listed first. Pinned so that permitting it is a decision
	rather than a side effect."""
	with pytest.raises(SituError):
		layout("struct a [depth = 8] { u8 k; b inner; }\n"
		       "struct b [depth = 8] { u8 k; a inner; }\n")


@pytest.mark.parametrize(("attrs", "why"), [
	("[depth = 32, limit = 99]", "above"),
	("[depth = 0]",              "not a depth"),
])
def test_the_two_depths_are_held_to_what_they_can_mean(
		attrs: str, why: str) -> None:
	"""`limit` above `depth` can never fire, so it sits in a schema looking
	like a defence; a depth of zero describes a type that cannot contain
	itself; and a `limit` alone caps a recursion the format does not bound.
	Each is refused rather than warned, because a schema stating what the
	generated code does not enforce is worse than one stating nothing."""
	with pytest.raises(SituError, match=why):
		layout(NODE.replace("[depth = 32]", attrs))


def test_a_limit_without_a_depth_is_refused() -> None:
	"""`limit` caps a recursion that `depth` bounds, so alone it caps nothing.

	Tested on a struct that does NOT name itself, because a recursive one
	without `depth` is refused for the recursion first -- which is the more
	fundamental answer and the right order. The case this check is actually
	about is a schema that reached for a safety cap on a type that has no
	recursion to be unsafe about.
	"""
	with pytest.raises(SituError, match="without"):
		layout("struct s [limit = 4] { u8 a; }\n")


def test_a_recursive_type_is_described_but_not_generated() -> None:
	"""The line this phase draws, and the refusal names which half works.

	Every backend would emit an `extent` that calls itself. In C that does not
	compile without a forward declaration, and with one it is run-time
	recursion in generated code that 20.1 says has none -- with `depth` and
	`limit` enforced nowhere. So the specification path ships and the
	generation path says so.
	"""
	from situc.cli import main

	import tempfile, pathlib
	with tempfile.TemporaryDirectory() as tmp:
		path = pathlib.Path(tmp) / "r.situ"
		path.write_text(PREAMBLE + NODE, encoding="ascii")

		assert main(["map", str(path)]) == 0
		assert main(["wire", str(path)]) == 0
		assert main(["build", str(path), "--target", "c",
		             "--out", tmp]) != 0
