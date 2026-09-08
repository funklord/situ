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

from collections.abc import Callable

from situc.parser import parse, parse_text
from situc.resolve import ResolvedSchema, resolve

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


def layout(body: str) -> ResolvedSchema:
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


MUTUAL = ("struct expr [depth = 8] {{\n"
          "\tu8    kind;\n"
          "\tu16   n;\n"
          "\titem  items[n];\n"
          "}}\n"
          "\n"
          "struct item{item_attrs} {{\n"
          "\tu8    tag;\n"
          "\texpr  body;\n"
          "}}\n")


def test_a_mutual_cycle_is_described() -> None:
	"""0054 left this open and its own reasoning had already covered it:
	nothing in the mechanism needs the recursion to be single, and the bound
	applies to a strongly connected component rather than to a struct. What
	it left open was the diagnostic -- naming one struct of a two-struct
	cycle sends a reader to whichever happened to be listed first -- and the
	answer is to require the bound of every struct in the cycle and to name
	them all."""
	resolved = layout(MUTUAL.format(item_attrs=" [depth = 8]"))
	assert {"expr", "item"} <= set(resolved.structs)


@pytest.mark.parametrize(("body", "why"), [
	# No bound at all on one member of the cycle.
	(MUTUAL.format(item_attrs=""), "recursive through"),
	# Two numbers for one cycle: a walk entering at either end would
	# spend a different one, which is the ambiguity 17.0 makes an error.
	(MUTUAL.format(item_attrs=" [depth = 4]"), "disagrees"),
	# `[limit]` on some of the cycle and not the rest, which is the same
	# ambiguity wearing the other attribute.
	(MUTUAL.replace("struct expr [depth = 8]",
	                "struct expr [depth = 8, limit = 4]")
	        .format(item_attrs=" [depth = 8]"), "some of its structs"),
	# A cycle through a variant arm with no bound on it. The arm shape is
	# permitted -- it is how a tagged tree is written and `SHAPES["arm"]`
	# builds one -- and what is refused here is the same thing refused of
	# every other shape: a recursion nobody bounded.
	("struct node {\n"
	 "\tu8   tag;\n"
	 "\tvariant body switch (tag) {\n"
	 "\t\tcase 1:  node kid;\n"
	 "\t\tdefault: u8 pad;\n"
	 "\t}\n"
	 "}\n", "contains itself"),
])
def test_a_cycle_states_one_bound_for_all_of_it(body: str, why: str) -> None:
	"""`depth` bounds the cycle, not a struct of it.

	Each of these is a schema that would generate code, and each would
	generate code that is wrong in a way nothing downstream could see.
	"""
	with pytest.raises(SituError, match=why):
		layout(body)


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


def test_every_backend_generates_for_a_recursive_type() -> None:
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
		# All four generate: each extent carries a depth and stops at the
		# declared bound, so the recursion is bounded by the schema rather
		# than by the message's own length.
		for target in ("c", "cpp", "rust", "python"):
			assert main(["build", str(path), "--target", target,
			             "--out", tmp]) == 0, target


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
	# Past it, the extent stops -- at 33 levels of three bytes rather than
	# 32, because it reaches one level past the declared depth so that a
	# message which nests too far is still measurable and can therefore be
	# refused. Capped at 32 exactly, the offending level is invisible and
	# `validate` sees a conforming message.
	used, extent = rows[3]
	assert used == 192 and extent == 99, rows


def test_every_backend_carries_the_depth_into_its_walk() -> None:
	"""The bound is only a bound if the counter survives the descent.

	Each backend's extent calls its run's span and the span calls the
	element's extent back. If the plain form is used there, the counter
	restarts one level down and bounds nothing -- which is 26.112's "a bound
	with a public entry point that restarts it is not a bound", met in a
	generator rather than in the walker.

	Asserted on the four emitted sources rather than by running them, because
	three of the four need a toolchain and this is the property that has to
	hold in all four. The executable half is
	`test_the_generated_c_bounds_the_recursion_by_the_schema`, and the
	measured agreement -- 3, 93, 96, 96 bytes at 1, 31, 32 and 40 levels of
	nesting, identical in C, C++, Rust and Python -- is in 26.283.
	"""
	from situc.codegen.c import generate as gen_c
	from situc.codegen.cpp import generate as gen_cpp
	from situc.codegen.python import generate as gen_py
	from situc.codegen.rust import generate as gen_rs

	schema   = parse_text(PREAMBLE + NODE)
	resolved = resolve(schema, solve(schema))

	built = {
		"c":      gen_c(schema, resolved, "unit").header,
		"cpp":    gen_cpp(schema, resolved, "unit").header,
		"python": gen_py(schema, resolved, "unit").module,
		"rust":   gen_rs(schema, resolved, "unit").module,
	}
	for name, text in built.items():
		# 33: one past the declared depth, so "at" and "past" differ.
		assert "depth >= 33" in text or "depth >= 33u" in text, name
		# The descent passes the counter on rather than starting a new one.
		assert "depth + 1" in text, name
		# And the entry point starts it at zero, so a caller never sees it.
		assert "_at(0" in text or "_at(view, 0u)" in text \
			or "extent_at(0)" in text, name


def test_every_backend_refuses_a_message_that_nests_too_deep() -> None:
	"""`validate` is where the refusal lives, and the extent cannot host it.

	At its cap an extent returns zero, and zero is what an absent run
	returns too -- so a message nesting too far measures short and validates
	clean. That is 26.113's "wrong values indistinguishable from right ones"
	with a limit folded into a length, and it is why the two questions are
	answered by two functions.

	The two verdicts are different verdicts. Past the format's own `[depth]`
	a message is malformed, as a thirteenth month is. Past this build's
	`[limit]` it is well formed and refused anyway -- a statement about the
	reader, which is why it is not a constraint error.
	"""
	from situc.codegen.c import generate as gen_c
	from situc.codegen.cpp import generate as gen_cpp
	from situc.codegen.python import generate as gen_py
	from situc.codegen.rust import generate as gen_rs

	def built(body: str) -> dict[str, str]:
		schema   = parse_text(PREAMBLE + body)
		resolved = resolve(schema, solve(schema))
		return {
			"c":      gen_c(schema, resolved, "unit").source,
			"cpp":    gen_cpp(schema, resolved, "unit").header,
			"python": gen_py(schema, resolved, "unit").module,
			"rust":   gen_rs(schema, resolved, "unit").module,
		}

	# `[depth]` alone: past it the message is malformed.
	refused = {
		"c":      "return SITU_ERR_CONSTRAINT;",
		"cpp":    "return ::situ::rt::err::constraint;",
		"rust":   "return Err(Error::Constraint);",
		"python": "raise ConstraintError(",
	}
	for name, text in built(NODE).items():
		assert "nesting" in text, name
		assert refused[name] in text, name

	# `[limit]` below it: past the limit this build refuses, under its own
	# class, and does not go on to judge the format's depth.
	capped = NODE.replace("[depth = 32]", "[depth = 32, limit = 8]")
	# **The raise, not the name.** Python's generated module imports
	# `DepthError` whenever the schema has a recursive type at all, so a
	# substring test for the bare name is satisfied by the import and passes
	# with the check deleted -- which it did, and which is the third time in
	# one day an import line has made an assertion vacuous (26.274 has the
	# first two).
	raised = {
		"c":      "return SITU_ERR_DEPTH;",
		"cpp":    "return ::situ::rt::err::depth;",
		"rust":   "return Err(Error::Depth);",
		"python": "raise DepthError(",
	}
	for name, text in built(capped).items():
		assert raised[name] in text, name


def test_the_extent_is_capped_by_the_format_and_not_by_the_build() -> None:
	"""A cap on a length can only truncate, so it may not be the smaller
	number.

	The view a caller gets for an element is sized by the extent. Capped at
	this build's `[limit]`, the bytes of a too-deep level become invisible:
	every walk over them comes back short and `validate` sees a conforming
	message rather than refusing one. Measured while building this -- with
	the extent capped at the limit, a 33-level message reported a nesting of
	8 and validated clean.

	So the extent caps at the format's `[depth]`, plus one, because "at the
	limit" and "past the limit" have to be distinguishable by something.
	"""
	from situc.codegen.c import generate as gen_c

	schema   = parse_text(PREAMBLE
	                      + NODE.replace("[depth = 32]", "[depth = 32, limit = 8]"))
	resolved = resolve(schema, solve(schema))
	header   = gen_c(schema, resolved, "unit").header

	assert "depth >= 33u" in header, "the extent must reach one past the depth"
	assert "depth >= 8u" not in header, "a length may not be capped by `[limit]`"


def _packed(body: str) -> bytes:
	"""The runtime image for a schema, as `situc pack` writes it.

	With its metadata tail, so a struct can be found by name: a mutual pair
	is two shapes and which one the packer wrote first is not something a
	test should be pinning.
	"""
	from situc import pack as pack_mod

	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	blob, _coverage = pack_mod.pack(schema, resolved, metadata=True)
	return blob


WHILE_NODE = ("struct node [depth = 32, limit = {limit}] {{\n"
              "\tu8    more;\n"
              "\tnode  kids[] while (more != 0);\n"
              "}}\n")

#: The other way a struct holds a run of itself, and the one a recursive
#: type takes most naturally -- a tree node says how many children it has.
#: It is a separate shape all the way down: `while` asks a predicate after
#: each element and this asks a length program once, so they meet at
#: `struct_extent` and share nothing above it. Held to the same numbers
#: because the schema's are the same numbers.
COUNTED_NODE = ("struct node [depth = 32, limit = {limit}] {{\n"
                "\tu8    count;\n"
                "\tnode  kids[count];\n"
                "}}\n")


def test_the_image_carries_the_declared_depth() -> None:
	"""A side table, keyed by shape, for the reason `image_version` is one:
	almost no struct names itself and `image_struct` is 16 bytes with none
	spare. A new section rather than a wider record, so a walker built
	before it skips it by tag -- which is what `stride` on `image_section`
	has always been for."""
	from walker.image import load

	image = load(_packed(WHILE_NODE.format(limit=5)))
	assert image.depths, "no depth table in the image"
	shape, (declared, limit) = next(iter(image.depths.items()))
	assert (declared, limit) == (32, 5), image.depths


def _chain(levels: int) -> bytes:
	"""A message nested `levels` deep, for either run shape.

	One byte per level, and the byte means "another one follows" in both:
	the `while` run reads it as its predicate and the counted run reads it
	as a count of one. So the same bytes exercise two constructs that share
	nothing above `struct_extent`, which is what makes comparing them worth
	anything.
	"""
	return bytes(1 if i + 1 < levels else 0 for i in range(levels))


@pytest.mark.parametrize("body", [WHILE_NODE, COUNTED_NODE],
                         ids=["while", "counted"])
@pytest.mark.parametrize("limit", [2, 8])
def test_the_python_walker_follows_the_schema_not_its_own_ceiling(
		body: str, limit: int) -> None:
	"""The point of the whole exercise.

	`WALK_DEPTH_MAX` was a number chosen for this corpus and compared
	against nothing the schema declared, so a schema saying 32 met a walker
	allowing 8 and the walk stopped early in silence. The schema's `[limit]`
	decides now, capped by the build's own ceiling -- the arena is this
	program's, not the schema's.

	Parametrised on two limits rather than asserted once, because a single
	limit cannot tell "the schema decides" from "some fixed number decides".
	What makes this a test is that raising the schema's number raises what
	the walker follows.

	And on two run shapes, because it was asserted on one and the other was
	wrong. `_while_walk` re-raises the ceiling and breaks on everything
	else, under a comment saying why: "breaking here would call a too-deep
	message a short run". The counted run beside it wrote `except Refused:
	break`, and `Unplaceable` subclasses `Refused` -- so a twelve-level
	chain under `[limit = 8]` came back eight bytes long instead of
	refused. One shape refusing correctly is not evidence about the other,
	and the two are asserted together now for that reason rather than for
	symmetry.

	`TooDeep` by name rather than `Refused`: the ceiling, a frame that ran
	out, and a member nothing can place are three answers, and only the
	narrowest says the bound fired. The name exists because `validate` has
	to tell the third from this one -- it breaks on the third and must not
	on this (26.287).

	Before this the Python walker had no bound at all: Python's recursion
	limit was the only thing stopping a hostile message, and a
	`RecursionError` is a traceback where a refusal belongs.
	"""
	from walker.image import load
	from walker.walk import TooDeep, acquire, struct_extent

	image = load(_packed(body.format(limit=limit)))

	# The boundary itself, rather than a level either side of it. A
	# conservative pair -- walks 6, refuses 12 -- passes for a walker that
	# stops anywhere between, which is what let the C walker spend two
	# levels per level of struct without this file noticing.
	#
	# `limit + 1` structs, because `[depth = N]` counts EDGES and the root
	# is zero -- the four backends' `validate` refuses `nesting > N`, and
	# `nesting` is 0 for a message that holds one struct. The walkers
	# compared `depth >= N` and followed one level fewer than the generated
	# code did, for every recursive schema, until a mutual pair put the six
	# descriptions side by side (26.288).
	ok = struct_extent(acquire(image, _chain(limit + 1), 0))
	assert ok == limit + 1, (limit, ok)

	with pytest.raises(TooDeep):
		struct_extent(acquire(image, _chain(limit + 2), 0))


#: The three shapes a recursive type takes, the message that nests one `k`
#: deep in each, and the top-level struct. Held together because every
#: recursion test below wants all three and each was written for one:
#: `[depth]` shipped exercised on the counted run alone, so the `while` run
#: emitted a `_span_at` nothing defined and the record run the same, in two
#: backends each -- neither found by the suite, because no schema in it or
#: in the corpus recurses through those two constructs.
SHAPES: dict[str, tuple[str, Callable[[int], bytes], str]] = {
	"counted": (
		"struct node [depth = 8] {\n\tu8 kind;\n\tu16 n;\n"
		"\tnode kids[n];\n}\n",
		lambda k: bytes(b"".join(bytes([0, 0, 1]) for _ in range(k))
		                + bytes([0, 0, 0])),
		"node"),
	"while": (
		"struct node [depth = 8] {\n\tu8 more;\n"
		"\tnode kids[] while (more != 0);\n}\n",
		lambda k: bytes([1] * k + [0]),
		"node"),
	"mutual": (
		"struct expr [depth = 8] {\n\tu8 kind;\n\tu16 n;\n"
		"\titem items[n];\n}\n"
		"struct item [depth = 8] {\n\tu8 tag;\n\texpr body;\n}\n",
		lambda k: bytes(b"".join(bytes([0, 0, 1, 0]) for _ in range(k))
		                + bytes([0, 0, 0])),
		"expr"),
	# A tagged tree: the recursion is the arm a discriminant selects, which
	# is how JSON reaches its object from its value and how every format
	# with a "kind" byte is written. Refused for a day on the reasoning that
	# a variant with no maximum is unbounded -- true of an `opaque` arm and
	# not of this one, whose maximum has no closed form for the same reason
	# a recursive run's does not.
	"arm": (
		"struct node [depth = 8] {\n\tu8 tag;\n"
		"\tvariant body switch (tag) {\n"
		"\t\tcase 1:  node kid;\n\t\tdefault: u8 pad;\n\t}\n}\n",
		lambda k: bytes([1] * k + [0, 0]),
		"node"),
}

#: What each shape answers at `k` turns, as (extent, nesting, valid).
#:
#: A turn of a mutual cycle is TWO structs, so `[depth = 8]` admits four
#: turns where the other two admit eight levels -- which is the whole of
#: what "depth counts nested structs, not turns of the cycle" means, stated
#: as numbers rather than as prose.
EXPECTED = {
	"counted": {8: (27, 8, True), 9: (27, 9, False)},
	"while":   {8: (9,  8, True), 9: (10, 9, False)},
	"mutual":  {4: (19, 8, True), 5: (19, 10, False)},
	"arm":     {8: (10, 8, True), 9: (9,  9, False)},
}


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_generated_python_bounds_every_recursive_shape(
		tmp_path: Path, shape: str) -> None:
	"""Run rather than read, and all three shapes rather than the one.

	Python is the backend that needs no toolchain, so this is the arm of the
	agreement that runs everywhere. `test_the_four_agree_about_a_mutual_cycle`
	is the same table in C, C++ and Rust.

	The numbers are pinned, not merely compared between shapes: two
	descriptions agreeing on a bound neither of them applies is agreement.
	"""
	import importlib.util
	import sys as _sys

	body, build, top = SHAPES[shape]
	from situc.codegen.python import generate as gen_py

	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.py").write_text(gen_py(schema, resolved, "unit").module,
	                                  encoding="ascii")

	root = Path(__file__).resolve().parents[2] / "runtime" / "python"
	_sys.path.insert(0, str(root))
	try:
		spec = importlib.util.spec_from_file_location(
			f"unit_{shape}", tmp_path / "unit.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		_sys.path.remove(str(root))

	held = getattr(module, top)
	for k, (extent, nesting, valid) in EXPECTED[shape].items():
		data = build(k)
		one  = held(module.Message(bytearray(data)), 0, len(data))
		assert one._extent == extent, (shape, k, one._extent)
		assert one.nesting == nesting, (shape, k, one.nesting)
		if valid:
			one.validate()
		else:
			with pytest.raises(Exception):
				one.validate()


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_four_agree_about_every_recursive_shape(
		tmp_path: Path, shape: str) -> None:
	"""C, C++ and Rust against the same table Python is held to.

	This is what found everything. Each backend was self-consistent and each
	looked right alone; the counted run was the only shape anybody had
	compiled, and the mutual cycle was the only case where a turn costs two
	levels -- which is what made the `nesting` probe's dependence on the
	bounded extent visible at all. Skipped per toolchain rather than
	altogether, because a machine with only one compiler still checks that
	one against Python.
	"""
	from situc.cli import main

	body, build, top = SHAPES[shape]
	schema = tmp_path / "r.situ"
	schema.write_text(PREAMBLE + body, encoding="ascii")

	rows = "\n".join(
		f"\t\t{{ {k}u, {len(build(k))}u, {e}u, {n}u, {int(v)} }},"
		for k, (e, n, v) in EXPECTED[shape].items())

	ran = 0
	if shutil.which("gcc") is not None:
		_run_c(tmp_path, schema, top, rows, build, shape)
		ran += 1
	if shutil.which("g++") is not None:
		_run_cpp(tmp_path, schema, top, rows, build, shape)
		ran += 1
	if shutil.which("rustc") is not None:
		_run_rust(tmp_path, schema, top, build, shape)
		ran += 1
	if ran == 0:
		pytest.skip("no C, C++ or Rust toolchain")


#: The driver each compiled backend runs, as a table of cases it checks
#: itself. Written once per language rather than once per shape: the shapes
#: differ in their bytes and not in the questions asked of them.
def _cases(build: Callable[[int], bytes], shape: str) -> str:
	"""The messages, as C array initialisers -- bytes and length per case."""
	return ",\n".join(
		"\t{ " + ", ".join(f"0x{byte:02x}" for byte in build(k)) + " }"
		for k in EXPECTED[shape])


def _run_c(tmp_path: Path, schema: Path, top: str, rows: str,
		build: Callable[[int], bytes], shape: str) -> None:
	from situc.cli import main

	out = tmp_path / "c"
	out.mkdir(exist_ok=True)
	assert main(["build", str(schema), "--target", "c", "--out", str(out)]) == 0

	(out / "main.c").write_text(f'''#include <stdio.h>
#include "r.h"
static const uint8_t cases[][64] = {{
{_cases(build, shape)}
}};
static const uint32_t want[][5] = {{
{rows}
}};
int main(void)
{{
	unsigned i;
	for (i = 0; i < sizeof want / sizeof want[0]; i++) {{
		uint8_t     buf[64];
		situ_msg_t  msg;
		situ_view_t view;
		unsigned    b;

		for (b = 0; b < want[i][1]; b++) {{ buf[b] = cases[i][b]; }}
		situ_msg_init(&msg, buf, want[i][1]);
		if (situ_{top}_view(&msg, 0u, want[i][1], &view) != SITU_OK) {{
			return 1;
		}}
		printf("%u %u %d\\n", situ_{top}_extent(view),
		       situ_{top}_nesting(view),
		       situ_{top}_validate(view) == SITU_OK);
	}}
	return 0;
}}
''', encoding="ascii")

	runtime = Path(__file__).resolve().parents[2] / "runtime" / "c"
	built = subprocess.run(
		["gcc", "-std=c11", "-Os", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{runtime}", f"-I{out}",
		 str(out / "main.c"), str(out / "r.c"), str(runtime / "situ.c"),
		 "-o", str(out / "r")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	_compare(subprocess.run([str(out / "r")], capture_output=True, text=True),
	         shape, "c")


def _run_cpp(tmp_path: Path, schema: Path, top: str, rows: str,
		build: Callable[[int], bytes], shape: str) -> None:
	from situc.cli import main

	out = tmp_path / "cpp"
	out.mkdir(exist_ok=True)
	assert main(["build", str(schema), "--target", "cpp",
	             "--out", str(out)]) == 0

	(out / "main.cpp").write_text(f'''#include <cstdio>
#include "r.hpp"
static const std::uint8_t cases[][64] = {{
{_cases(build, shape)}
}};
static const std::uint32_t want[][5] = {{
{rows}
}};
int main()
{{
	for (unsigned i = 0; i < sizeof want / sizeof want[0]; i++) {{
		std::uint8_t buf[64];
		for (unsigned b = 0; b < want[i][1]; b++) {{ buf[b] = cases[i][b]; }}

		situ_msg_t msg;
		situ_msg_init(&msg, buf, want[i][1]);
		situ_view_t view;
		situ_view_at(&msg, 0u, want[i][1], &view);
		const ::situ::{top} one(view);
		std::printf("%u %u %d\\n", one.extent(), one.nesting(),
		            one.validate() == ::situ::rt::err::ok);
	}}
	return 0;
}}
''', encoding="ascii")

	root  = Path(__file__).resolve().parents[2] / "runtime"
	built = subprocess.run(
		["g++", "-std=c++17", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{root / 'c'}", f"-I{root / 'cpp'}", f"-I{out}",
		 str(out / "main.cpp"), str(root / "c" / "situ.c"),
		 "-o", str(out / "r")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	_compare(subprocess.run([str(out / "r")], capture_output=True,
	                        text=True), shape, "cpp")


def _run_rust(tmp_path: Path, schema: Path, top: str,
		build: Callable[[int], bytes], shape: str) -> None:
	from situc.cli import main

	out = tmp_path / "rust"
	out.mkdir(exist_ok=True)
	assert main(["build", str(schema), "--target", "rust",
	             "--out", str(out)]) == 0

	root = Path(__file__).resolve().parents[2] / "runtime" / "rust"
	(out / "situ_rt.rs").write_text(
		(root / "situ_rt.rs").read_text(encoding="utf-8"), encoding="utf-8")
	pascal = top[:1].upper() + top[1:]
	cases  = ",\n".join(
		"\tvec![" + ", ".join(str(byte) for byte in build(k)) + "]"
		for k in EXPECTED[shape])
	(out / "main.rs").write_text(f'''mod situ_rt;
mod r;
fn main() {{
    let cases: Vec<Vec<u8>> = vec![
{cases}
    ];
    for d in cases {{
        let one = r::{pascal}::new(&d).unwrap();
        println!("{{}} {{}} {{}}", one.extent(), one.nesting(),
                 if one.validate().is_ok() {{ 1 }} else {{ 0 }});
    }}
}}
''', encoding="ascii")

	built = subprocess.run(
		["rustc", "--edition", "2021", "-O", "-o", str(out / "r"),
		 str(out / "main.rs")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	_compare(subprocess.run([str(out / "r")], capture_output=True, text=True),
	         shape, "rust")


def _compare(ran: "subprocess.CompletedProcess[str]", shape: str,
		language: str) -> None:
	"""One backend's table against the pinned one."""
	assert ran.returncode == 0, ran.stderr
	rows = [tuple(int(n) for n in line.split())
	        for line in ran.stdout.split("\n") if line]
	want = [(e, n, int(v)) for e, n, v in EXPECTED[shape].values()]
	assert rows == want, f"{language} {shape}: {rows} != {want}"


@pytest.mark.parametrize("shape", sorted(SHAPES))
def test_the_walkers_stop_where_the_generated_code_does(shape: str) -> None:
	"""The sixth and seventh descriptions, against the same table.

	Nothing compared these two layers. `test_walker_c.py` holds the walkers
	to each other and this file holds the four backends to each other, so a
	walker and a backend could disagree about the same schema for as long as
	each stayed self-consistent -- and they did, by one, for every recursive
	schema: the walkers refused at `depth >= N` where the generated code
	admits `nesting == N`, the root counting zero in both.

	What is compared is the LAST MESSAGE EACH ACCEPTS, not the extent past
	the bound: a generated extent truncates and returns a number, a walker
	refuses by name, and both are right for their own design (26.284). Where
	they must agree is on which messages are inside the format.
	"""
	from walker.image import load
	from walker.walk import TooDeep, acquire, struct_extent

	body, build, top = SHAPES[shape]
	image = load(_packed(body))
	names = [image.struct_name(i) for i in range(len(image.structs))]

	for k, (extent, _nesting, valid) in EXPECTED[shape].items():
		view = acquire(image, build(k), names.index(top))
		if valid:
			assert struct_extent(view) == extent, (shape, k)
		else:
			with pytest.raises(TooDeep):
				struct_extent(view)



#: `[limit]` strictly below `[depth]`, which no schema in the corpus states.
#: The two attributes are the same number everywhere else, so every place
#: that reads the wrong one of the pair is correct by coincidence until a
#: schema separates them -- and this is the schema that separates them.
SPLIT = ("struct node [depth = 8, limit = 3] {\n"
         "\tu8    tag;\n"
         "\tnode  more[] while (tag == 1);\n"
         "}\n")


def _emitted_split() -> dict[str, str]:
	from situc.codegen.c import generate as gen_c
	from situc.codegen.cpp import generate as gen_cpp
	from situc.codegen.python import generate as gen_py
	from situc.codegen.rust import generate as gen_rs

	schema   = parse_text(PREAMBLE + SPLIT)
	resolved = resolve(schema, solve(schema))
	return {
		"c":      gen_c(schema, resolved, "unit").header,
		"cpp":    gen_cpp(schema, resolved, "unit").header,
		"python": gen_py(schema, resolved, "unit").module,
		"rust":   gen_rs(schema, resolved, "unit").module,
	}


def test_the_prototype_names_the_bound_its_own_code_enforces() -> None:
	"""A C-only sentence, and it named the wrong member of the pair.

	`_recursive_prototypes` read `[limit]` and the `_at` form it describes
	is bounded by `[depth] + 1`, so with `[depth] = 8, [limit] = 3` the
	header said "bounded by 3" over code reading `depth >= 9u`. Every schema
	stating only `[depth]` makes the two spellings one number, which is why
	it read correctly from the day `[limit]` arrived.

	Asserted as a RELATIONSHIP rather than against the literal 9: the guard
	is found in the emitted text and the sentence is held to it, so a schema
	with different numbers cannot make this pass for the wrong reason.
	"""
	import re

	header = _emitted_split()["c"]
	guard  = re.search(r"if \(depth >= (\d+)u\)", header)
	said   = re.search(r"bounded by (\d+) rather", header)

	assert guard is not None and said is not None, header[:400]
	assert said.group(1) == guard.group(1), (
		f"the prototype says the `_at` form is bounded by {said.group(1)} "
		f"and the code it describes reads `depth >= {guard.group(1)}u`")


def test_the_four_state_one_saturation_for_the_nesting_probe(
		tmp_path: Path) -> None:
	"""All four say "capped at N", and three of them said the guard.

	The probe stops descending past `min(limit, depth) + 1` and therefore
	SATURATES one higher, so C printed 5 where C++, Rust and Python printed
	4 for identical code. Both readings of "capped at" are defensible and
	that is exactly the problem: four descriptions of one layout stating two
	numbers for one quantity.

	Settled by measuring rather than by picking. The generated Python module
	is run against chains of increasing depth and the number every backend
	prints is held to the value it actually returns -- so the sentence
	cannot drift from the behaviour again, in any of the four, and a schema
	with different bounds cannot make this pass for the wrong reason.
	"""
	import importlib.util
	import re
	import sys as _sys

	built = _emitted_split()
	said  = {}
	for name, text in built.items():
		found = re.search(r"nests, capped at (\d+)", text)
		assert found is not None, f"{name} says nothing about the cap"
		said[name] = int(found.group(1))

	assert len(set(said.values())) == 1, f"four backends, {said}"

	(tmp_path / "unit.py").write_text(built["python"], encoding="ascii")
	root = Path(__file__).resolve().parents[2] / "runtime" / "python"
	_sys.path.insert(0, str(root))
	try:
		spec = importlib.util.spec_from_file_location(
			"unit_split", tmp_path / "unit.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		_sys.path.remove(str(root))

	# A chain of `tag == 1` bytes and a terminator: nesting is one more than
	# the chain's length until the probe stops descending. Nine is well past
	# the declared depth, so the highest value seen IS the saturation.
	seen = set()
	for deep in range(0, 10):
		data = bytearray(b"\x01" * deep + b"\x00\x00")
		one  = module.node.at(module.Message(data), 0, len(data))
		seen.add(one.nesting)

	assert max(seen) == said["c"], (
		f"the backends say the probe caps at {said['c']} and it returns "
		f"{sorted(seen)}")
