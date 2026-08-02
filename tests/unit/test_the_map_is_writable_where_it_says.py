"""Every member the capability map calls writable can be written, in all four.

The map is the product (section 1). `mutate = InPlaceFixed` on a field is a
claim that the generated API lets a caller write it where it sits -- and a
backend that emits no setter for such a field has not implemented the schema,
it has implemented a narrower one. The absence is not even visible: nothing
fails, the accessor simply is not there.

This is 26.31's pair of agreement checks on the other axis. Those ask what each
backend *refuses* and fail where the four disagree; this asks what the map
*promises* and fails where a backend does not deliver it. The difference
matters, because the three gaps found this session were all silent in a way
backend-versus-backend cannot see:

  * Rust emitted nothing for a scalar at a dynamic offset -- no setter and no
    note -- while the other three wrote it and the map called it writable;
  * three backends emitted no covered write for a field of a nested struct, so
    the only path to it marked no tag;
  * and each was found by a differential run that had to compile a driver
    first, which is a slow and indirect way to learn that a function is
    missing.

What is *not* asked is written down beside each exclusion. Every one is a
member whose write the map does not promise in the first place, or one whose
setter belongs to another type.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import pytest

from every_schema import SCHEMAS, ids
from situc import ast
from situc.capability import Axis
from situc.cli import analyse
from situc.codegen.c import generate as generate_c
from situc.codegen.c.names import c_name
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.parser import parse
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import local_name


class Emitted(Protocol):
	"""What every backend's `generate` returns. Four different `Generated`
	types, so a bare mapping of them infers as `object` and checks nothing."""

	def files(self) -> dict[str, str]: ...


Emit = Callable[[ast.Schema, ResolvedSchema, str], Emitted]

BACKENDS: dict[str, Emit] = {
	"c":      generate_c,
	"cpp":    generate_cpp,
	"rust":   generate_rs,
	"python": generate_py,
}


def spellings(backend: str, struct: str, local: str) -> list[str]:
	"""How each language writes one field. Four surfaces, one operation.

	Python's is a property assignment rather than a method, which is the
	backend's own convention (decision 0022) -- and the reason this asks for
	spellings rather than one name.
	"""
	return {
		"c":      [f"situ_{c_name(struct)}_{local}_set("],
		"cpp":    [f"set_{local}("],
		"rust":   [f"set_{local}(", f"set_r#{local}("],
		"python": [f"@{local}.setter", f"def set_{local}("],
	}[backend]


def promised(struct: ResolvedStruct) -> list[str]:
	"""The struct's own fields the map says are writable in place."""
	found: list[str] = []

	for entry in struct.entries:
		placement = entry.placement

		# A nested struct's field belongs to the nested type, which emits its
		# own setter; an element of a run belongs to the element type. Both
		# appear here under a dotted path because the map names them.
		#
		# The exception is a *covered* nested field, whose write has to mark a
		# bit the nested type cannot know about. That one is held to the
		# parent by `test_invariants.py`, which asks for the marking too --
		# the operation is not the same operation.
		if "." in local_name(struct, placement) or "[]" in placement.path:
			continue

		if placement.kind != "field" or placement.scalar is None:
			continue
		# An array is reached through its pointer; a setter writing one
		# element would be a worse API than the pointer and an explicit mark.
		if placement.array_count is not None or placement.sized_by is not None:
			continue
		# A text number is written as digits, and a longer number needs more
		# of them: there is no store that leaves the bytes after it in place.
		if placement.radix is not None:
			continue
		# A marker resolves byte order rather than holding a value.
		if placement.marker is not None:
			continue
		# A sealed interior is reached through the gate, whose type every
		# accessor on it takes.
		if placement.sealed_by:
			continue
		# `[secret]` gets no accessor at all, which is the point (14.6).
		if any(attr.name == "secret" for attr in placement.attrs):
			continue

		if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
			continue

		found.append(c_name(local_name(struct, placement)))

	return found


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_every_writable_member_has_a_setter(schema: Path) -> None:
	source, resolved, _ = analyse(schema)
	parsed = parse(source)

	emitted = {name: "\n".join(generate(parsed, resolved, "unit").files().values())
	           for name, generate in BACKENDS.items()}

	missing: list[str] = []
	for struct in resolved.structs.values():
		# A register is a bus transaction rather than bytes off a wire, and
		# its access modes are section 15.2's rather than `mutate`'s.
		if struct.layout.register is not None:
			continue

		for local in promised(struct):
			for backend, text in emitted.items():
				if not any(one in text
				           for one in spellings(backend, struct.name, local)):
					missing.append(f"{backend}: {struct.name}.{local}")

	assert not missing, (
		f"{schema.name}: the map calls these writable and the backend emits "
		"no setter:\n  " + "\n  ".join(missing))
