"""Golden-vector tests over the Rust accessors (sections 1 and 22).

Section 1 names four artifacts and four languages, and until this file the
fourth artifact reached one language: `gen-tests` took no `--target` at all,
so a user who generated Rust accessors got no golden-vector suite for them.
`gen-fuzz` reaches C and C++; `gen-checks` reaches C. Measured from the CLI
rather than from a record, and nothing in the tree marked it open.

**One vector file, one parser, one validator.** `parse_vectors` and `_check`
below are imported from the C backend rather than reimplemented: the vector
file is language-independent, and two copies of "is this vector well formed"
is the shape this tree keeps paying for -- a question answered in two places
from different text. They live under `c/` because C was their only consumer,
which is a reason to import across rather than to copy.

**What this emits, and what it does not.** Per case: the bytes take a view,
and validation accepts them. That is what a vector says before it says
anything about a field, and it catches the two faults a Rust accessor can
have that no cross-backend comparison would -- refusing bytes the layout
admits, and admitting a length it should not.

Per-field expectations are NOT compared here and `--target c` compares them.
They are still READ and still checked against the schema, because `_check`
does that and is shared, so a vectors file naming a field that does not
exist is refused for Rust exactly as for C. What is missing is the
comparison, and the generated file says so with the number of expectations
it is not checking -- silence there would read as coverage, which is this
family's own rule.
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen import (byte_run, comparable, enum_constants,
                           refuse_parameters, tally)
from situc.codegen.c.vectors import Case, _check, _symbol
from situc.codegen.rust.emit import _ident, _pascal
from situc.resolve import ResolvedSchema


def generate(schema: ast.Schema, resolved: ResolvedSchema, cases: list[Case],
		basename: str, prefix: str = "situ") -> str:
	"""A test crate root for `basename.rs`, from `basename`'s vectors.

	`prefix` is accepted and unused: Rust names its accessors by type and
	method rather than by a flat symbol, so there is nothing for a prefix to
	disambiguate. Taken anyway so the two backends have one signature and
	`cmd_gen_tests` needs no special case -- which is the same reason the C
	one takes a schema it only uses for `refuse_parameters`.
	"""
	# The family's bargain, and first for the C backend's reason: a schema
	# with a `parameter` is declined rather than served badly, because
	# `validate` takes the argument tail and the accessors for a parameter
	# are deliberately not emitted. The refusal is about the schema, so it
	# comes before any vector is read.
	refuse_parameters(schema, {case.struct for case in cases})

	seen: dict[str, Case] = {}
	for case in cases:
		symbol = _symbol(case)
		if symbol in seen:
			raise ValueError(
				f"vector `{case.struct} {case.name}` is declared twice")
		seen[symbol] = case

	for case in cases:
		_check(resolved, case)

	# The shared tally classifies the vector FILE; Rust declines one class
	# more, and the count has to say so or it overstates what this suite
	# compares. See `_enum_typed`.
	# The constants are resolved here too, even though Rust declines every
	# expectation they unlock: without them the shared classifier reports a
	# C enum constant as "not an integer literal", which is true and is not
	# the reason. The obstacle is the getter's type, and a reader fixing one
	# of these needs to be told which (26.828's shape, met in a reason
	# string rather than a claim).
	enums     = {decl.name for decl in schema.enums()}
	variants  = _variants(schema)
	constants = enum_constants(schema, prefix)
	# `nested=True`: Rust reaches a nested field by chaining through the
	# accessor's `Result` -- `view.head().expect(..).magic()` -- which is
	# one expression because `expect` yields an owned view whose borrow of
	# `view` outlives the call on it.
	compared, declined = tally(resolved, cases, constants, nested=True)
	for case in cases:
		for path, value in case.expectations:
			literal = comparable(resolved, case, path, value, constants,
			                     nested=True)
			if literal is None or not _enum_typed(resolved, enums, case, path):
				continue
			# An enum-typed field is comparable after all, against the
			# VARIANT rather than the number: the getter returns
			# `Option<T>`, so `Some(PacketId::Out)` is the right-hand side
			# and the number is how we find which variant that is. Where
			# the value names no variant the getter would return `None`,
			# which is a vector claiming a value the enum does not have --
			# declined rather than asserted, since `None` is what a wrong
			# vector and an unknown value look like alike.
			if _variant_of(basename, resolved, variants, case, path,
			               literal) is None:
				compared -= 1
				why = ("an enum-typed field whose value names no variant,"
				       " where the Rust getter returns `None`")
				declined[why] = declined.get(why, 0) + 1

	lines = _preamble(basename, compared, declined)

	for case in cases:
		lines.extend(_case_body(basename, resolved, enums, variants,
		                        constants, case))

	lines.append("}")
	return "\n".join(lines) + "\n"


def _preamble(basename: str, compared: int,
		declined: dict[str, int]) -> list[str]:
	lines = [
		f"//! Generated by situc {__version__} from {basename}.situ and its"
		f" vectors -- do not edit.",
		"//!",
		"//! Golden-vector tests over the Rust accessors, read from the same",
		"//! vector file `--target c` reads. One file, so the two backends",
		"//! cannot be asked different questions about one schema.",
		"//!",
		"//! A test crate root. It expects the generated module and the",
		"//! runtime beside it:",
		"//!",
		f"//!     rustc --edition 2021 --test {basename}_vectors.rs \\",
		f"//!         -o {basename}_vectors",
		"//!",
		f"//! which reads `{basename}.rs` and `situ_rt.rs` from the same",
		"//! directory.",
		"//!",
		"//! Each case asserts that the bytes take a view and that validation",
		"//! accepts them -- what a vector says before it says anything about",
		"//! a field, and the two faults a Rust accessor can have that no",
		"//! cross-backend comparison would notice: refusing bytes the layout",
		"//! admits, and admitting a length it should not.",
	]

	# Said with numbers rather than omitted, and with a reason per class: a
	# suite that checks fewer things than its input states, and does not say
	# which, reads as coverage of the input.
	if compared:
		lines.extend([
			"//!",
			f"//! {compared} field expectation(s) are compared -- a scalar",
			"//! against an integer and a byte run against its bytes,",
			"//! through their getters.",
		])

	if declined:
		lines.extend([
			"//!",
			f"//! {sum(declined.values())} are NOT compared, and",
			"//! `situc gen-tests --target c` compares them:",
			"//!",
		])
		for why, count in sorted(declined.items()):
			lines.append(f"//!   {count} -- {why}")
		lines.extend([
			"//!",
			"//! Each is still read and checked against the schema, so a",
			"//! vectors file naming a field this schema does not have is",
			"//! refused here too.",
		])

	lines.extend([
		"",
		"mod situ_rt;",
		f"mod {basename};",
		"",
		"#[cfg(test)]",
		"mod vectors {",
	])
	return lines


def _enum_typed(resolved: ResolvedSchema, enums: set[str],
		case: Case, path: str) -> bool:
	"""Whether this field's Rust getter returns an enum rather than a number.

	The shared classifier answers a question about the vector FILE and is
	right for every backend; this one is Rust's alone. An enum-typed field's
	getter returns `Option<T>` -- unknown values are not a variant -- so
	`assert_eq!(view.pid(), 0x2D)` does not typecheck:

	    expected `Option<PacketId>`, found integer

	`example/usb` and `example/netlink` are the two schemas it fires on, and
	they are the two that failed to compile before this existed. Caught by
	the corpus gate rather than by any unit test, which is the whole reason
	that gate builds the generated code instead of reading it.

	Python does not need this, because an `IntEnum` compares equal to its
	number; C++ does not, because `static_cast` from an enum class to an
	integer is legal. One shared question, three different vetoes, and this
	is the only one that is not empty.
	"""
	found = resolved.find(f"{case.struct}.{path}")
	return found is not None and found.placement.type_name in enums


def _variants(schema: ast.Schema) -> dict[str, dict[int, str]]:
	"""Each enum's members by value, for the variant a number names.

	Keyed on the value rather than the name because that is the question an
	expectation asks: a vector gives `0x2D` or `SITU_PACKET_ID_SETUP`, both
	of which resolve to a number, and the Rust right-hand side needs
	`PacketId::Setup`.
	"""
	found: dict[str, dict[int, str]] = {}
	for decl in schema.enums():
		by_value: dict[int, str] = {}
		for member in decl.members:
			value = getattr(member.value, "value", None)
			if isinstance(value, int) and value not in by_value:
				by_value[value] = member.name
		found[decl.name] = by_value
	return found


def _variant_of(basename: str, resolved: ResolvedSchema,
		variants: dict[str, dict[int, str]],
		case: Case, path: str, literal: str) -> str | None:
	"""`Some(crate::usb::PacketId::Setup)` for a field and a number, or None.

	Qualified by the module, as the view beside it is. Unqualified gave
	`error[E0433]: failed to resolve: use of undeclared type PacketId` for
	eight of the corpus's schemas: the cases live in `mod vectors` and the
	generated module is a sibling, so nothing of its is in scope by its bare
	name. Caught by the corpus gate compiling the output, which is the only
	thing that could have.
	"""
	found = resolved.find(f"{case.struct}.{path}")
	if found is None:
		return None

	members = variants.get(found.placement.type_name)
	if members is None:
		return None

	try:
		number = int(literal, 0)
	except ValueError:
		return None

	name = members.get(number)
	if name is None:
		return None
	return (f"Some(crate::{basename}::"
	        f"{_pascal(found.placement.type_name)}::{_pascal(name)})")


def _case_body(basename: str, resolved: ResolvedSchema, enums: set[str],
		variants: dict[str, dict[int, str]],
		constants: dict[str, int], case: Case) -> list[str]:
	name = _symbol(case)
	body = ", ".join(f"0x{byte:02X}" for byte in case.data)

	return [
		"\t#[test]",
		f"\tfn {name}() {{",
		f"\t\tconst BYTES: [u8; {len(case.data)}] = [{body}];",
		"",
		f"\t\tlet view = crate::{basename}::{_pascal(case.struct)}"
		f"::new(&BYTES)",
		f"\t\t\t.expect(\"`{case.name}` must take a view\");",
		"",
		f"\t\tview.validate()",
		f"\t\t\t.expect(\"`{case.name}` must validate\");",
		*_expectations(basename, resolved, enums, variants, constants, case),
		"\t}",
		"",
	]


def _expectations(basename: str, resolved: ResolvedSchema, enums: set[str],
		variants: dict[str, dict[int, str]],
		constants: dict[str, int], case: Case) -> list[str]:
	"""What the vector says about each field, where this target can say it.

	`assert_eq!` carries the field name as its message, so a failure names
	the expectation and not only the two numbers -- the difference between a
	test that says what broke and one that says something did.
	"""
	lines: list[str] = []
	for path, value in case.expectations:
		raw = byte_run(resolved, case, path, value, nested=True)
		if raw is not None:
			if not lines:
				lines.append("")
			body = ", ".join(f"0x{byte:02X}" for byte in raw)
			# `[..]` so the comparison is slice against slice: the getter
			# returns `&[u8]` and an array literal is `[u8; N]`, which do
			# not compare directly.
			lines.append(
				f"\t\tassert_eq!({_reach(path, case)}, &[{body}][..],"
				f" \"{path}\");")
			continue

		literal = comparable(resolved, case, path, value, constants,
		                     nested=True)
		if literal is None:
			continue

		right = literal
		if _enum_typed(resolved, enums, case, path):
			variant = _variant_of(basename, resolved, variants, case, path,
			                      literal)
			if variant is None:
				continue
			right = variant

		if not lines:
			lines.append("")
		# `_ident` and not `c_name`: a field called `type` is a Rust keyword
		# and its accessor is `r#type`, which the Rust backend escapes and
		# the C name function knows nothing about. `test/schema/header` has
		# one, and it was the last of eight schemas this broke.
		lines.append(
			f"\t\tassert_eq!({_reach(path, case)}, {right},"
			f" \"{path}\");")
	return lines


def _reach(path: str, case: Case) -> str:
	"""`head.magic` as `view.head().expect(..).magic()`.

	A nested struct's accessor returns `Result<Inner>`, so each step but the
	last is unwrapped: `expect` yields an owned view whose borrow of `view`
	outlives the call on it, which is what lets this be one expression
	rather than a let-binding per level.

	The message names the step that failed rather than the case, because a
	vector whose bytes take a view can still fail to reach a nested one --
	a bounds check per level is what section 12.3 asks for -- and "`one`
	failed" would not say which level.
	"""
	parts = path.split(".")
	reached = "view"
	for step in parts[:-1]:
		reached += (f".{_ident(step)}()"
		            f".expect(\"`{case.struct}.{step}` must be reachable\")")
	return f"{reached}.{_ident(parts[-1])}()"
