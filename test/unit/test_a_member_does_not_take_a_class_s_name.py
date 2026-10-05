"""A Python class scope is one namespace, members included (26.569).

This backend puts its own names in the class beside every member's --
`at` acquires a view, `validate` checks the constraints, `required`
answers the framing question -- so a member of one of those names is a
second binding of it. Python takes the later one, which binding that is
depends on emission order, and nothing says a word:

	struct record { u8 at; u16 tail; }

	>>> record.at
	<property object at ...>
	>>> record.at(msg, 0, 9)
	TypeError: 'property' object is not callable

The module imports and cannot be used. The three other backends are not
in the same position and are not in the same position as each other: C
suffixes every accessor (`situ_record_at_get` beside `situ_record_view`)
and is unaffected, while C++ refuses `validate` and `size_bytes` and
Rust refuses `validate` and `required` -- loudly, at compile time, which
is why this one is the fault worth a guard.

The guard asks the finished module rather than a list of reserved names,
so it cannot be short by one the next time a class learns a method.
"""

from __future__ import annotations

import ast as pyast
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.python import generate as generate_py
from situc.codegen.python.emit import Emitter
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from every_schema import SCHEMAS, ids, load_schema  # noqa: E402

PREAMBLE = "endian big;\n\n"

#: Measured by generating, not chosen: a one-member fixed-size struct's
#: class binds `SIZE_BYTES`, `at`, `required` and `validate`, and the
#: constant is the one a member cannot reach because situ has no
#: uppercase member names.
COLLIDING = ("at", "validate", "required")

#: Ordinary member names, and the reason the guard is not a word list:
#: each of these is a name this backend uses SOMEWHERE and not in the
#: class a plain struct produces. A list long enough to be safe would
#: refuse all four.
INNOCENT = ("framed", "extent", "word", "read", "write", "nesting",
            "messages", "payload", "check", "size_bytes", "size_min")


def _schema(name: str) -> str:
	return PREAMBLE + f"struct record {{\n\tu8 {name};\n\tu16 tail;\n}}\n"


def _generate(name: str, backend: str = "python"):  # type: ignore[no-untyped-def]
	schema   = parse_text(_schema(name), path="record.situ")
	resolved = resolve(schema, solve(schema))
	make     = generate_py if backend == "python" else generate_c
	return make(schema, resolved, "record")


@pytest.mark.parametrize("name", COLLIDING)
def test_a_member_taking_a_class_s_own_name_is_refused(name: str) -> None:
	"""Named, spanned, and with the thing to change."""
	with pytest.raises(SituError, match="are one name in `class record`") as why:
		_generate(name)

	rendered = str(why.value.diagnostic.message)
	assert f"record.{name}" in rendered, rendered

	# The span, read rather than its line number: a line is a fact about
	# this fixture's blank lines and a slice is a fact about the span.
	primary = why.value.diagnostic.primary
	assert primary is not None, "a diagnostic with nowhere to point"
	span = primary.span
	assert name in span.source.text[span.start:span.end], "the member"


@pytest.mark.parametrize("name", COLLIDING)
def test_the_collision_is_real_and_not_the_guard_s_idea(name: str) -> None:
	"""The positive control, and it has to go through the guard's seam.

	Without this the refusal above is evidence that something raised, not
	that anything was wrong: a list of four names would pass it exactly as
	well. So the guard is replaced by one that does nothing, the module is
	generated, and the duplicate binding is read out of the text.

	That is also what fails if the mangling answer is ever taken instead:
	a backend that renamed the member would bind two names here, this
	assertion would go red, and whoever changed it would have to say so.
	"""
	held = Emitter._refuse_collisions
	try:
		Emitter._refuse_collisions = (  # type: ignore[method-assign]
			lambda self, module: module)
		built = _generate(name)
	finally:
		Emitter._refuse_collisions = held  # type: ignore[method-assign]

	classes = [node for node in pyast.parse(built.module).body
	           if isinstance(node, pyast.ClassDef) and node.name == "record"]
	assert len(classes) == 1, "one class named `record`"
	bound = [one for item in classes[0].body
	         for one in Emitter._bound_by(item)]
	assert bound.count(name) == 2, f"{name} bound {bound.count(name)} times"


@pytest.mark.parametrize("name", INNOCENT)
def test_a_name_this_backend_uses_elsewhere_is_not(name: str) -> None:
	"""The control that keeps the guard from being a word list.

	`framed`, `extent`, `word`, `read`, `write` and `nesting` are all
	names this backend emits for some shape of struct, and none of them is
	in the class a plain fixed-size struct produces. A guard written as a
	list would have to carry them to be safe for the shapes that do, and
	would then refuse these.

	`size_bytes` and `size_min` are here for a sharper reason, and they
	are why the list was measured rather than copied from C++'s: this
	backend spells those constants `SIZE_BYTES` and `SIZE_MIN`, so a
	member takes neither -- while C++ spells them lowercase and a member
	named `size_bytes` stops its header compiling. Two backends, one
	name, opposite answers.
	"""
	built = _generate(name)
	assert f"def {name}" in built.module, "the member's own accessor"


@pytest.mark.parametrize("name", COLLIDING)
def test_c_keeps_the_name_the_schema_chose(name: str) -> None:
	"""And the asymmetry is the point rather than an aside.

	C suffixes every member accessor, so `situ_record_validate_get` sits
	beside `situ_record_validate` and neither moves. A refusal in the front
	end would have outlawed this, which is why the guard is the Python
	backend's and not the compiler's.
	"""
	built = _generate(name, backend="c")
	assert f"situ_record_{name}_get" in built.header
	assert f"situ_record_{name}_set" in built.header


@pytest.mark.parametrize("path", SCHEMAS, ids=ids(SCHEMAS))
def test_no_schema_in_this_repository_collides(path: Path) -> None:
	"""The half that makes it a guard rather than a new refusal.

	A guard added over a corpus it has never read is a guard that may be
	refusing work that was fine. Every schema here generates in all four
	combinations of the two flags that change what a class body holds --
	`--materialize` adds an index per capped run and `--messages` adds
	`messages` and `message_text`, which are two of the names a member
	could reach -- and none is refused.

	It is also the only thing that would notice a FUTURE backend name
	landing on a member some schema already has -- which is the direction
	this fault came from, and the one a fixture cannot cover.
	"""
	schema   = load_schema(path)
	resolved = resolve(schema, solve(schema))
	for materialize in (False, True):
		for messages in (False, True):
			generate_py(schema, resolved, path.stem,
			            materialize=materialize, messages=messages)
