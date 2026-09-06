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

**The C backend generates for one; the other three do not yet.** Its extent
carries a depth and stops at the declared bound, so the recursion is bounded
by the schema rather than by the message's own length -- which is what 20.1's
"bounded stack" needs. `build` refuses the other three targets by name.

**And the claim about `verify` in this docstring was wrong for a day.** It
said "map, wire and verify are correct for a recursive type" while nothing in
this file called `verify` -- it imported `solve`, `parse_text` and `resolve`
and nothing else. openmlx4 found the gap by using it: a recursive *located*
member crashed the Python backend, and `verify` rendered the crash as "does
not conform ... from an implementation that is not this schema", which sent
them to their own bytes for two probes. An untested claim in a docstring is
the same shape as a signal with no artifact, and it reached HEAD because the
sentence was easier to write than the call.
"""

from __future__ import annotations

import pytest

from situc.diagnostics import SituError
from situc.layout import solve
import shutil
import subprocess
from pathlib import Path

from situc.parser import parse, parse_text
from situc.resolve import resolve

PREAMBLE = "target buffer;\nendian big;\n"

NODE = ("struct node [depth = 32] {\n"
        "\tu8   kind;\n"
        "\tu16  count;\n"
        "\tnode children[count];\n"
        "}\n")


def _verify(schema: Path, vectors: Path) -> str:
	"""What `situc verify` would print, without going through the CLI."""
	from situc import verify as verify_mod
	from situc.diagnostics import Source

	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	found    = verify_mod.check(
		parsed, resolved, schema.stem,
		Source(str(vectors), vectors.read_text(encoding="ascii")))
	return verify_mod.render(found, str(schema), str(vectors))


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


def test_only_c_generates_for_a_recursive_type_so_far() -> None:
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
		# C generates for one: its extent carries a depth and stops at the
		# declared bound, so the recursion is bounded by the schema rather
		# than by the message's own length.
		assert main(["build", str(path), "--target", "c", "--out", tmp]) == 0
		# The other three do not yet, and say so rather than emitting a
		# header that does not compile (26.69).
		for target in ("cpp", "rust", "python"):
			assert main(["build", str(path), "--target", target,
			             "--out", tmp]) != 0, target


def test_verify_reports_a_compiler_fault_as_one(tmp_path: Path) -> None:
	"""The crash is the smaller half; the verdict is the bug.

	`verify` caught every exception from the generated module and rendered it
	as "does not conform ... from an implementation that is not this schema".
	An `AttributeError` from a construct the Python backend does not emit is
	situc failing, and telling a reader their bytes are wrong sends them to
	the bytes -- which is where openmlx4 went, for two probes, before doubting
	the compiler.

	A recursive *located* member is the case that produces it. Pinned on the
	rendering rather than on the crash, because the crash is a gap that will
	close and the rendering is a rule that must not.
	"""
	schema = tmp_path / "chain.situ"
	schema.write_text(PREAMBLE + "struct hdr { u32 size; u32 next; }\n"
	                  "struct chain [depth = 8] {\n"
	                  "\thdr   head;\n"
	                  "\tu8    data[head.size];\n"
	                  "\tchain following at head.next;\n"
	                  "}\n", encoding="ascii")
	vectors = tmp_path / "chain.vectors"
	vectors.write_text("chain one 00 00 00 00 00 00 00 00\n", encoding="ascii")

	text = _verify(schema, vectors)
	assert "situc failed" in text, text
	assert "not a verdict on the bytes" in text, text
	assert "does not conform" not in text, text


def test_verify_still_calls_a_refusal_a_refusal(tmp_path: Path) -> None:
	"""The control on the test above: separating a compiler fault from a
	verdict is only worth anything if a verdict still reads as one. Without
	this, renaming every failure "situc failed" would pass."""
	schema = tmp_path / "s.situ"
	schema.write_text(PREAMBLE + "struct s { u8 v [must_eq = 7]; }\n",
	                  encoding="ascii")
	vectors = tmp_path / "s.vectors"
	vectors.write_text("s wrong 09\n", encoding="ascii")

	text = _verify(schema, vectors)
	assert "does not conform" in text, text
	assert "situc failed" not in text, text


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_the_generated_c_bounds_the_recursion_by_the_schema(
		tmp_path: Path) -> None:
	"""Compiled and run, because the point of the depth is what it does at
	run time and a header that merely compiles proves nothing about that.

	Nesting up to the declared depth measures whole; past it the extent stops
	at the bound rather than following the message. That is what 20.1's
	bounded stack means for a type whose nesting the data decides -- without
	it a hostile message nests as deep as its own bytes allow.

	The extent answers a short length rather than an error because it returns
	a length and has no error channel; `validate` is where the refusal
	belongs. A test that only checked the short answer would pass against a
	generator that had lost the recursion entirely, so both sides of the
	bound are asserted.
	"""
	from situc.cli import main

	schema = tmp_path / "r.situ"
	schema.write_text(PREAMBLE + NODE, encoding="ascii")
	assert main(["build", str(schema), "--target", "c",
	             "--out", str(tmp_path)]) == 0

	(tmp_path / "main.c").write_text('''#include <stdio.h>
#include "r.h"
static uint32_t build(uint8_t *buf, uint32_t levels)
{
	uint32_t i;
	for (i = 0; i < levels; i++) {
		buf[i * 3u]      = (uint8_t)i;
		buf[i * 3u + 1u] = 0u;
		buf[i * 3u + 2u] = (uint8_t)(i + 1u < levels ? 1u : 0u);
	}
	return levels * 3u;
}
int main(void)
{
	uint8_t buf[4096];
	uint32_t want[] = { 1u, 31u, 32u, 64u };
	unsigned k;
	for (k = 0; k < 4u; k++) {
		situ_msg_t msg; situ_view_t view;
		uint32_t used = build(buf, want[k]);
		situ_msg_init(&msg, buf, used);
		if (situ_node_view(&msg, 0u, used, &view) != SITU_OK) { return 1; }
		printf("%u %u\\n", used, situ_node_extent(view));
	}
	return 0;
}
''', encoding="ascii")

	runtime = Path(__file__).resolve().parents[2] / "runtime" / "c"
	built = subprocess.run(
		["gcc", "-std=c11", "-Os", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{runtime}", f"-I{tmp_path}",
		 str(tmp_path / "main.c"), str(tmp_path / "r.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "r")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "r")], capture_output=True, text=True)
	assert ran.returncode == 0, ran.stderr
	rows = [tuple(int(n) for n in line.split())
	        for line in ran.stdout.split("\n") if line]
	assert len(rows) == 4, ran.stdout

	# Inside the declared 32, the extent is the whole chain.
	for used, extent in rows[:3]:
		assert extent == used, rows
	# Past it, the extent stops at the bound: 32 levels of three bytes.
	used, extent = rows[3]
	assert used == 192 and extent == 96, rows
