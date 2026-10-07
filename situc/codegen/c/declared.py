"""Which accessors the C build actually declares, for the harnesses.

`gen-checks`, `gen-fuzz` and `gen-tamper` emit C that is compiled against
the generated headers, so every `situ_` function they call has to be one
those headers declare. All three decided that by ENUMERATING the shapes
the emitter writes -- and an enumeration of somebody else's branches is
short by one the moment that somebody grows a branch.
`test_generated_c_calls_only_accessors_the_headers_declare` exists because
it happened twice: a delimited arm gained `_ptr`/`_len` where the harness
still called a one-byte `_get` (26.423), and a nested obligation is named
for its path where the harness used its leaf (26.464).

It happened a third time from the other direction (26.575). The emitter
learned to DECLINE an accessor -- a length only a transform can produce
(26.570), a sealed interior reached without the gate (26.571) -- and three
harnesses went on calling what it no longer writes.

So they ask the headers instead. One assembly of them, here, because the
test asked the same question and a second copy of this list is how the
two stop agreeing: a checks suite reaches `situ_msg_*` through
`<name>_frame.h` and the setters through `<name>_edit.h`, so reading
`<name>.h` alone reports the others' accessors as undeclared -- an
instrument manufacturing the finding it went looking for.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import defined_symbols
from situc.resolve import ResolvedSchema


def declared(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str) -> frozenset[str]:
	"""Every `situ_` name the C build's headers define, across every rung.

	Imported inside the function: these modules import the emitter and the
	harnesses import this, so a module-level import here would close a
	cycle.
	"""
	from situc.codegen.c import converse, edit, emit, frame, relate

	text = emit.generate(schema, resolved, basename).header
	for rung in (edit, relate, frame, converse):
		text += "\n" + "\n".join(
			rung.generate(schema, resolved, basename).values())
	return defined_symbols(text)
