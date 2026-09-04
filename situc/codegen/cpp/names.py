"""Which C++ classes cannot be called what the schema calls them.

Decision 0013 is about two schema constructs meeting in one C identifier. This
is the hazard C++ adds on top of it, and it is a different one: a class's own
name is declared inside the class, so no member may take it. `struct framed`
gets a framing method called `framed()` and is not C++. Neither is

    struct option { u8 option; u8 length; }

which is a class `option` with an accessor `option()` -- a shape a real
protocol produces without trying. C flattens the whole path into
`situ_option_option` and never meets the rule; `impl option { fn option }` and
`class option: def option` are both fine. This is the one place where a schema
three backends accept is rejected by the fourth's *compiler*, and
`test/schema/edges.situ` carried one for weeks in the file whose whole
purpose is awkward shapes (26.31).

Refusing such a schema was the other option and it is the worse one: it makes
`framed`, `validate`, `extent` and `at` reserved words in one backend and
outlaws the struct above for a reason that has nothing to do with its bytes.
So the *class* moves instead, and the schema's name becomes an alias for it:
every accessor keeps its name, every other class goes on naming this one the
way the schema does, and the rename is visible only in a debugger.

Which classes move is decided from the schema, not by reading the emitted text
back -- section 25 is explicit that a pass reads the AST and never its own
output. That costs completeness, so the rule over-approximates on purpose: a
class whose name is a member's under any affix this file lists is renamed,
whether or not the emitter happens to generate that particular accessor for
that particular member. A false positive costs one alias nobody reads. A false
negative costs a header that does not compile, which is what this file exists
to prevent, and `test_the_affixes_match_the_emitter` holds the lists to the
emitter so a phase that adds an accessor shape cannot leave them behind.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import KEYWORDS, bare_name, c_name
from situc.diagnostics import Diagnostic, Label, Severity, SituError, Span
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import local_name

#: Members every generated class has, whatever the schema says. A struct named
#: for one of these collides with it even if the struct has no members at all,
#: which is why they are listed rather than derived from anything.
STRUCTURAL = frozenset({
	# Ordinary views: the factory, the size constant it uses, the framing
	# question, the streaming form of it, the constraint check, and the extent
	# a container asks an element for.
	"at", "size_bytes", "size_min", "framed", "required", "validate",
	"extent", "dirty_mask",
	# Registers (section 15): the word type, the two bus transactions, the
	# address and width constants, and the pointer behind them.
	"word", "read", "write", "address", "width", "block_",
})

#: What the emitter puts in front of a member's name.
PREFIXES = frozenset({"clear", "dirty", "recompute", "set", "trigger", "with"})

#: What it puts after one. Some of these are halves of others -- `span` and
#: `span_from` both appear -- because the check below matches a whole affix
#: rather than a prefix of one.
SUFFIXES = frozenset({
	"at", "big", "count", "covered", "decode", "decode_spans", "decoded_max",
	"digits", "encode_spans", "eq",
	"extent", "finalize", "find", "first", "from", "gate", "host", "index",
	"indexed", "is_dirty", "is_little", "is_stale", "item", "len", "little",
	"next", "of_host", "offset", "raw_len", "read", "self_span", "span",
	"span_from", "spans", "t",
	"terminated", "terminated_from", "valid", "value",
	# The exported value domain (26.125): a member named x makes x_value_min
	# and x_value_max class constants, so a schema field of either name would
	# collide with them the same way x_len collides with a getter.
	"value_min", "value_max",
	# A checksum naming the codec that computes it makes x_compute and
	# x_check (0053), so a schema field of either name would collide with
	# them the same way.
	"compute", "check",
})


def member_names(struct: ResolvedStruct) -> set[str]:
	"""Every name a member of this class could reach.

	Every entry rather than only the struct's own: a variant's arms and a
	sealed region's interior generate accessors on the parent, and their local
	names carry a dot, which `c_name` flattens the way the emitter does.
	"""
	found: set[str] = set()

	for entry in struct.entries:
		name = bare_name(local_name(struct, entry.placement))
		if not name:
			continue
		found.add(name)
		found.update(f"{prefix}_{name}" for prefix in PREFIXES)
		found.update(f"{name}_{suffix}" for suffix in SUFFIXES)

	return found | STRUCTURAL


def class_name(struct: ResolvedStruct) -> str:
	"""What to call the class. The schema's name, unless something has it.

	A member's name is one way to collide and a keyword is the other, and the
	second was missed for as long as this file existed: `bare_name` has
	mangled a *member* called `class` or `operator` since decision 0025, and
	the class itself went out verbatim -- so `struct class` emitted
	`class class : public ::situ::rt::view` and g++ reported six errors naming
	neither the schema nor situc.

	The same answer as the member case, for the same reason this file gives at
	the top: the class moves and the schema keeps its name. Refusing would
	make `class` and `operator` reserved words in one backend, and DNS has
	fields called both.
	"""
	name = c_name(struct.name)
	collides = name in member_names(struct) or name in KEYWORDS
	return f"{name}_" if collides else name


def renamed(struct: ResolvedStruct) -> bool:
	return class_name(struct) != c_name(struct.name)


def check_collisions(schema: ast.Schema, resolved: ResolvedSchema) -> None:
	"""Refuse a schema where the renamed class lands on a declared type.

	The suffix is one underscore, which is free in every case but one: a schema
	holding both `framed` and `framed_` would have the alias for the first and
	the class for the second reach the same name. Two names one character apart
	is a coincidence rather than a construct, so this says so and stops instead
	of inventing a second escape nobody could predict.
	"""
	declared: dict[str, tuple[str, str, Span]] = {
		**{c_name(decl.name): ("struct", decl.name, decl.span)
		   for decl in schema.structs()},
		**{c_name(decl.name): ("enum", decl.name, decl.span)
		   for decl in schema.enums()},
	}

	for name, struct in resolved.structs.items():
		if not renamed(struct):
			continue
		held = declared.get(class_name(struct))
		if held is None:
			continue

		kind, taken, span = held
		source = declared.get(c_name(name))
		raise SituError(Diagnostic(
			severity = Severity.ERROR,
			message  = f"struct `{name}` needs another name for its C++ class, "
			           f"and `{class_name(struct)}` is taken",
			primary  = Label(source[2] if source else span,
			                 f"a member of this generates `{c_name(name)}`, so "
			                 f"the class cannot be called that"),
			labels   = [Label(span, f"{kind} `{taken}` is already "
			                        f"`{class_name(struct)}`")],
			notes    = [
				"C++ declares a class's own name inside the class, so no member "
				"may take it; the class is renamed and the schema's name becomes "
				"an alias for it",
				"rename either construct, or put them in separate namespaces",
			],
		))
