"""One question, asked of four backends, about the same bytes.

The suite compares backends on well-formed buffers, and that comparison has
never failed. Every disagreement this project has found was about a *malformed*
message: an offset the message chose sent C out of bounds, C++ past its buffer,
Rust into a panic and Python into a silent clamp, and a frame shorter than the
minimum was a view in two backends and an error in the other two (26.27). Four
answers to one question, in the one place nothing was asking.

`tests/unit/test_backends_agree_under_random_bytes.py` asked it for one schema
with four hand-written drivers. This derives them, so the question is asked of
every schema in the repository and of every construct the probe list below
covers.

**Why the four emitters live in one file.** What matters is that the output
text is identical, line for line, in four languages -- a diff is the whole
test. Splitting the renderers into the four backend packages would put the
thing that has to agree in four files that have already been shown to drift
apart when they answer separately (26.32). The probe list is chosen once, by
`traverse.classify`, and each language spells the same probes.

**Not a CLI command**, unlike `gen-fuzz` and `gen-checks`. Those are artifacts a
*user* wants over their own schema; this one is only useful to somebody holding
all four backends at once, which is this repository. If that changes, it is a
subcommand and a line in section 21.

What is probed is a subset, and the subset is the thing to grow. Now: scalars,
byte arrays, delimited members and delimited text numbers, tags, endian
markers, varints, the counts of runs and of `tlv` and `indexed` regions, a
variant's arms, members present only from a given version, the framing of a
coded region, a sealed region's stage gate *and* the scalars behind it, the
first element of an array of wide scalars, and a nested struct's sub-view --
plus `validate`.

Not yet, and each for a stated reason rather than for want of writing it:

  * an enum-typed field -- C and C++ hand back the value in an enum type, Rust
    an `Option` that is `None` for a value the schema does not name, and Python
    the member or the raw integer. Three answers to "what does this byte say",
    all defensible, and no single line to diff. The underlying integer would
    compare, and no backend exposes it;
  * a fixed-width text number's *value*, a byte run inside a gate, and a coded
    region's decoded contents.

A versioned member needed no new probe shape at all: "is this member in *this*
message?" is the question a variant's arm answers, asked of the version field
instead of a discriminant, and all four spell it the same way. A coded region
is asked where it ends and not what it holds, the decode being C's to run and
absent from Python by decision (0017). A nested struct is the opposite case and
is why it is still missing -- C bounds-checks the sub-view and the other three
cannot fail, so there is no shared answer to compare until three of them grow
one (26.31).

The gate is the probe worth having for its own sake. Section 14.3 claims a
sealed interior cannot be reached before its tag verifies, and the four say
that in four shapes: an out-parameter and an error in C, a callback in C++ --
so that no expression names a gate outside the verified branch -- a `Result` in
Rust, a raise in Python. What they can all answer is whether a failed check is
refused and a passed one admitted, which is the claim itself, and then what the
interior *says* once it is open, which is the half a tag exists to protect.

C++ made that second half awkward in a way worth keeping: its interior is read
during the open, inside the callback, so printing there put the interior ahead
of the summary line the other three print first. Same numbers, different order,
and a diff sees an order. The values are captured and printed after.

A variant's arms are asked the reachability question rather than the value
one: which arm the discriminant selects, and how long it is or what it holds.
Three shapes met there -- an out-parameter and an error in C and C++, a
`Result` in Rust, a property that raises in Python -- and they agree, which is
worth knowing rather than assuming.

Adding a kind is cheap and pays immediately. The four spellings have to be
looked up once, and looking them up is itself the check: `tlv` counts were a
method in Python and a property everywhere else in that same backend, and a
varint's total-value accessor was public in three languages and private in
Python -- so the number every length in the struct derives from was the one
thing a Python caller could not ask for. Neither is a crash; both are one
question with two answers.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from situc import ast
from situc.codegen.c.names import c_name, ident, macro
from situc.codegen.rust.emit import _ident as rust_ident
from situc.codegen.rust.emit import _pascal
from situc.capability import Axis
from situc.layout import BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import (
	Member, arm_members, classify, containment_order, local_name,
	own_entries, own_members,
)


class Probe(Enum):
	"""What to ask about a member. One line of output each."""

	#: `name <integer>`
	SCALAR    = "scalar"
	#: `name len=<n> first=<byte|->`
	BYTES     = "bytes"
	#: `name len=<n> term=<0|1>`
	DELIMITED = "delimited"
	#: `name present=<0|1>`
	TAG       = "tag"
	#: `name count=<n>`
	COUNT     = "count"
	#: `name little=<0|1>` -- an endian marker, whose answer every field it
	#: governs depends on.
	MARKER    = "marker"
	#: `name len=<n> value=<v>` -- a varint, both numbers off the wire.
	VARINT    = "varint"
	#: `name ok=<0|1> len=<n>` -- a variant's byte-run arm, reachable only
	#: when the discriminant selects it.
	ARM_BYTES = "arm_bytes"
	#: `name ok=<0|1> value=<v>` -- a variant's scalar arm.
	ARM_VALUE = "arm_value"
	#: `name refused=<0|1> opened=<0|1>` -- a sealed region's stage gate,
	#: asked to open on a failed check and then on a passed one.
	SEALED    = "sealed"
	#: `name <integer>` -- a scalar *inside* a gate, read through it.
	GATED     = "gated"
	#: `name[0] <integer>` -- the first element of an array of wide scalars,
	#: which is reached by index rather than by pointer.
	ELEMENT   = "element"
	#: `name ok=<0|1> extent=<n>` -- a nested struct's sub-view, which every
	#: backend can now refuse.
	NESTED    = "nested"
	#: `name <- <integer>` -- a *write*, and then the buffer, which is the
	#: half of every backend's surface nothing compared.
	WRITE     = "write"
	#: `name <- <integer> dirty=<0|1>` -- a write to a member a tag covers,
	#: which has to leave the tag stale (14.2). The claim is the security
	#: model's, and four backends spell the marking four ways.
	COVERED   = "covered"


@dataclass(frozen=True)
class Ask:
	probe: Probe
	local: str
	#: A fixed byte count, where the member has one. `BYTES` needs it in C,
	#: which has a macro rather than a length accessor for a counted array.
	count: int | None = None
	#: The width of a scalar arm's out-parameter, which is the field's own
	#: type rather than a wide one: C types the parameter exactly.
	bits: int = 0
	signed: bool = False
	#: For `SEALED`: the plain scalars inside the region, read through the gate
	#: once it opens. The interior is the half a tag exists to protect, so
	#: reading it is the half worth comparing.
	inside: tuple[str, ...] = ()
	#: For `NESTED`: the member's type, which C++ needs to declare the
	#: out-parameter the accessor fills in.
	inner: str = ""


def asks(struct: ResolvedStruct, structs: set[str],
		structs_by_name: dict[str, ResolvedStruct] | None = None) -> list[Ask]:
	"""Which members this struct can be asked about, in declaration order.

	`traverse.classify` decides what kind a member is, so the four drivers ask
	about exactly the same members -- the alternative is four lists that agree
	today.
	"""
	found: list[Ask] = []
	structs_by_name = structs_by_name or {}

	for entry in own_entries(struct):
		placement = entry.placement
		kind      = classify(struct, placement, structs)
		local     = c_name(local_name(struct, placement))

		# The gate, which is the security claim of 14.3: the interior cannot
		# be had without a verification token. What every backend can answer
		# is whether it refuses a failed check and admits a passed one.
		#
		# Before the `sealed_by` skip below, because a sealed region names
		# itself there -- the region is `sealed_by` the region -- so asking
		# that question first skipped the one member this probe exists for.
		if placement.kind == "sealed":
			found.append(Ask(Probe.SEALED, local, None, 0, False,
			                 _gated(struct, placement)))
			continue

		# A member *inside* a sealed region is reached through the gate, which
		# the probe above opens; the interior is asked about there.
		if placement.sealed_by:
			continue

		# A coded region is asked the framing question and not the value one:
		# the bytes are the transform's output, and Python emits no decode at
		# all (0017). Where it ends is still a scan over attacker bytes, and
		# that all four do answer.
		#
		# A region with no delimiter is asked the same question the other way:
		# how many bytes it occupies, which is its interior's extent through
		# the codec's expansion. Only the delimited form was asked, so the
		# other one -- where every backend returned the region's *minimum*,
		# zero, for a region the data sizes -- was compared by nothing
		# (26.35).
		if placement.codec is not None:
			if placement.delimiter is not None:
				found.append(Ask(Probe.DELIMITED, local))
			elif placement.kind == "coded":
				found.append(Ask(Probe.BYTES, local))
			continue

		scalar = placement.scalar

		# A member present only from a given version answers exactly the way a
		# variant's arm does -- an out-parameter and an error in C and C++, a
		# `Result` in Rust, a property that raises in Python -- because it is
		# the same question: is this member in *this* message? The version
		# field is the message's own, so a hostile one decides it.
		if placement.since is not None:
			if scalar is not None and not scalar.is_bit_packed \
					and not scalar.is_bcd \
					and placement.type_name in _SCALAR_TYPES:
				found.append(Ask(Probe.ARM_VALUE, local, None,
				                 max(8, scalar.bits), scalar.signed))
			continue

		# `classify` has no kind for a variant: it has no accessor of its own,
		# and the emitters key on the placement. Its *arms* do have accessors,
		# and which one is reachable is a question about a discriminant the
		# message chose.
		if placement.kind == "variant":
			found.extend(_arms(struct, placement))
			continue

		if kind is Member.SCALAR:
			if placement.type_name in ("", None) or scalar is None:
				continue
			# An enum is a different type in each language; a marker resolves
			# byte order rather than holding a value.
			if placement.marker is not None or placement.radix is not None:
				continue
			if scalar.is_bcd:
				continue
			if placement.type_name not in _SCALAR_TYPES:
				continue
			found.append(Ask(Probe.SCALAR, local))
		elif kind is Member.ARRAY and scalar is not None \
				and placement.array_count is not None:
			if scalar.bits == BITS_PER_BYTE:
				found.append(Ask(Probe.BYTES, local, placement.array_count))
			elif not scalar.is_bit_packed and not scalar.is_bcd \
					and placement.type_name in _SCALAR_TYPES \
					and placement.array_count > 0:
				# An element wider than a byte is `ValueConverted`, so there is
				# no pointer into it and the accessor takes an index. The first
				# element is always there -- the count is the schema's, not the
				# message's -- so this needs no bounds question, which is what
				# keeps it askable where Rust returns a `Result` and C does
				# not.
				found.append(Ask(Probe.ELEMENT, local))
		elif kind is Member.VARIABLE and scalar is not None \
				and scalar.bits == BITS_PER_BYTE:
			# Only where a *field* gives the length. A member sized by
			# arithmetic over other fields -- `u8 data[(len + 1) * 8 - 2]` --
			# gets no `_len` accessor in C, the count being an expression the
			# caller can evaluate, so there is no fourth spelling to compare.
			if placement.sized_by is None:
				continue
			found.append(Ask(Probe.BYTES, local))
		elif kind is Member.NESTED and placement.type_name in structs:
			# A fixed-size nested struct measures its own constant; a
			# variable one is asked. The count carries which.
			inner_struct = structs_by_name.get(placement.type_name or "")
			fixed = (inner_struct.layout.size_bytes
			         if inner_struct is not None
			         and inner_struct.layout.is_fixed_size else None)
			found.append(Ask(Probe.NESTED, local, fixed,
			                 inner=c_name(placement.type_name or "")))
		elif kind is Member.DELIMITED:
			# A text number framed by a delimiter is asked the framing
			# question and not the value one: its value accessor returns an
			# error in three languages and raises in the fourth, which are
			# four shapes rather than one answer.
			found.append(Ask(Probe.DELIMITED, local))
		elif kind is Member.TAG:
			found.append(Ask(Probe.TAG, local, placement.array_count))
		elif kind is Member.MARKER:
			found.append(Ask(Probe.MARKER, local))
		elif kind is Member.VARINT:
			found.append(Ask(Probe.VARINT, local))
		elif kind in (Member.RECORD_RUN, Member.REPEAT_WHILE, Member.TLV,
				Member.INDEXED):
			# Every count is a walk over numbers the message chose: a run's
			# elements, a `tlv` region's items, an `indexed` region's table.
			found.append(Ask(Probe.COUNT, local))

	return found


def writes(struct: ResolvedStruct) -> list[Ask]:
	"""Which members this struct can be *written*, in declaration order.

	Every backend emits setters and nothing compared them: the drivers read,
	and a schema means the same thing in four languages only if it also means
	the same thing when written. A byte order reversed in one setter, a mask
	off by a bit, a bit-packed field written with a read-modify-write that
	clobbers its neighbour -- none of that is visible from a read pass over
	bytes nobody wrote (26.35).

	The subset is narrow on purpose, and each exclusion is a shape whose
	*probe* would differ rather than whose behaviour would:

	  * `mutate` other than `InPlaceFixed` has no setter at all, which the
	    capability map decides and every backend already obeys;
	  * a covered member's setter takes the message or a dirty word, so the
	    four signatures differ by design (14.2);
	  * a versioned one's refuses, and the read probe already asks that;
	  * an enum takes its own type in two languages and an integer in two;
	  * a signed one needs a value in range, and a pattern that fits every
	    width is what makes the comparison worth anything;
	  * BCD and fixed point convert on the way in, so a pattern is not a
	    value they can hold.
	"""
	# A field whose value decides where a later member starts writes through
	# a setter that takes the message and bumps its generation (12.3), so its
	# signature is not the ordinary one. `sqlite`'s `cell_count` is the case:
	# four backends spell that extended setter four ways on purpose.
	drivers = {placement.sized_by for placement in own_members(struct)
	           if placement.sized_by and placement.sized_by != "remaining"}

	found: list[Ask] = []

	for entry in own_entries(struct):
		placement = entry.placement
		scalar    = placement.scalar

		if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
			continue
		if local_name(struct, placement) in drivers:
			continue
		if placement.since is not None:
			continue
		if placement.kind != "field" or scalar is None:
			continue
		if placement.array_count is not None or placement.sized_by is not None:
			continue
		if placement.marker is not None or placement.radix is not None:
			continue
		if scalar.signed or scalar.is_bcd or scalar.is_fixed_point:
			continue
		# `_SCALAR_TYPES` and nothing else, packed or not: a bit-packed
		# *enum* is still an enum, and its setter takes the enum type in two
		# languages and an integer in the other two. `leap_indicator : u2`
		# is what caught the first version of this.
		if placement.type_name not in _SCALAR_TYPES:
			continue

		# A covered write is the same write plus a claim: the tag it covers
		# is stale afterwards, which is 14.2's whole point and which each
		# backend spells its own way -- the message in C, C++ and Python, a
		# dirty word in Rust.
		#
		# A *tag* only. `covered_by` also carries an invariant's obligation,
		# spelled `invariant total`, whose recompute is section 16.1's rather
		# than 14.2's and whose accessors are named after the invariant. One
		# question at a time.
		covers = [one for one in placement.covered_by if " " not in one]
		kind   = Probe.COVERED if covers else Probe.WRITE
		if placement.covered_by and not covers:
			continue		# an invariant's obligation, not a tag's
		found.append(Ask(kind, c_name(local_name(struct, placement)),
		                 _pattern(scalar.bits), 0, False,
		                 inside=tuple(covers)))

	# And a covered field of a *nested* struct, which the loop above cannot
	# see: `own_entries` drops a dotted path. Its setter is on the parent in
	# all four backends -- it was on the parent in C alone until this probe
	# went looking (26.35) -- and the nested type's own setter marks nothing,
	# so this is the only path that keeps the tag honest.
	for entry in struct.entries:
		placement = entry.placement
		scalar    = placement.scalar
		covers    = [one for one in placement.covered_by if " " not in one]

		if "." not in local_name(struct, placement) or not covers:
			continue
		if scalar is None or placement.kind != "field":
			continue
		# A sealed region's interior is reached through the gate, whose type
		# every accessor on it takes. The `SEALED` probe opens one and reads
		# what is inside; writing through a gate is a question of its own.
		if placement.sealed_by:
			continue
		if placement.array_count is not None or placement.sized_by is not None:
			continue
		if placement.type_name not in _SCALAR_TYPES:
			continue
		if entry.vector.get(Axis.MUTATE).base != "InPlaceFixed":
			continue

		found.append(Ask(Probe.COVERED, c_name(local_name(struct, placement)),
		                 _pattern(scalar.bits), 0, False,
		                 inside=tuple(covers)))

	return found


def _pattern(bits: int) -> int:
	"""A value that fits `bits` and shows a byte order when it does not.

	`0x0123456789ABCDEF` truncated: a `u32` written little end first lands as
	`EF CD AB 89` and big end first as `89 AB CD EF`, and the two are not each
	other reversed by accident.
	"""
	return 0x0123456789ABCDEF & ((1 << bits) - 1)


def _arms(struct: ResolvedStruct, variant: Placement) -> list[Ask]:
	"""A variant's arms: is this one the arm the discriminant selects?

	The reachability is the question. Every backend refuses the arm that is
	not present -- an error in three languages and an exception in the fourth
	-- and what has to agree is *which* arm each of them says is there, for a
	discriminant the message chose. `examples/dnsname`'s label is the one
	variant in the tree, and its reserved forms are what a hostile name is
	made of.
	"""
	found: list[Ask] = []

	for _, member in arm_members(struct, variant):
		# `default: error` names no member: there is no arm to reach, and the
		# refusal is `validate`'s to report.
		if member is None:
			continue

		local  = c_name(local_name(struct, member))
		scalar = member.scalar
		if scalar is None:
			continue		# a struct arm: its own accessors are its type's
		if scalar.bits == BITS_PER_BYTE \
				and (member.sized_by is not None
				     or member.array_count is not None):
			found.append(Ask(Probe.ARM_BYTES, local))
		elif not scalar.is_bit_packed and not scalar.is_bcd \
				and member.type_name in _SCALAR_TYPES:
			found.append(Ask(Probe.ARM_VALUE, local, None,
			                 max(8, scalar.bits), scalar.signed))

	return found


def _gated(struct: ResolvedStruct, region: Placement) -> tuple[str, ...]:
	"""Plain scalars inside a sealed region, in declaration order.

	Only the scalars: a `[secret]` member has no debug accessor at all by
	design (14.6), and a byte run inside a gate is spelled four ways that have
	not been checked against each other yet.
	"""
	found: list[str] = []

	for entry in struct.entries:
		placement = entry.placement
		if placement.sealed_by != region.name or placement.kind != "field":
			continue
		if placement.scalar is None or placement.array_count is not None \
				or placement.sized_by is not None:
			continue
		if placement.scalar.is_bit_packed or placement.scalar.is_bcd:
			continue
		if placement.type_name not in _SCALAR_TYPES:
			continue
		if any(attr.name == "secret" for attr in placement.attrs):
			continue

		# The name *inside* the gate, which is the member's local name with
		# the region's stripped: `packet.sealed.inner_kind` is `inner_kind` on
		# the gate in three backends, and `sealed_inner_kind` only in C, where
		# there are no scopes to put it in. The caller spells that difference.
		found.append(c_name(local_name(struct, placement))
		             [len(c_name(region.name)) + 1:])

	return tuple(found)


#: Scalar type names that are one integer in every backend. An enum is not, and
#: neither is a `bit` in the sense of what a getter returns -- though that one
#: is included, being an integer everywhere.
_SCALAR_TYPES = frozenset({
	"u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "bit",
	*(f"u{n}" for n in range(2, 64)), *(f"i{n}" for n in range(2, 64)),
})


def structs_of(resolved: ResolvedSchema) -> list[ResolvedStruct]:
	"""Every struct a driver can acquire over a whole buffer.

	A register is a bus transaction rather than bytes off a wire, and a
	zero-length struct is every buffer at once.
	"""
	order = containment_order(resolved.structs, sorted(resolved.structs))
	return [resolved.structs[name] for name in order
	        if resolved.structs[name].layout.register is None
	        and resolved.structs[name].layout.is_byte_sized
	        and not (resolved.structs[name].layout.is_fixed_size
	                 and resolved.structs[name].layout.size_bytes == 0)]


def generate(schema: ast.Schema, resolved: ResolvedSchema, target: str,
		prefix: str = "situ") -> str:
	"""A driver in `target` that prints what this schema says about a buffer.

	Argv is one hex string. Every acquirable struct gets a section: a header
	line naming it, then one line per probe, or `no-view` where the frame is
	refused. The four drivers print the same text for the same bytes, or one
	of them is wrong.
	"""
	renderer = {
		"c": _c, "cpp": _cpp, "rust": _rust, "python": _python,
	}[target]
	return renderer(resolved, prefix)


# -- C ---------------------------------------------------------------------


def _c(resolved: ResolvedSchema, prefix: str) -> str:
	lines = [
		"/* Generated by situc: what this schema says about a buffer. */",
		"#include <stdio.h>",
		"#include <stdlib.h>",
		"#include <string.h>",
		"",
		'#include "unit.h"',
		"",
		"int main(int argc, char **argv)",
		"{",
		"\tstatic uint8_t raw[4096];",
		"\tuint32_t n = 0;",
		"\tsitu_msg_t msg;",
		"",
		"\tif (argc != 2) { return 2; }",
		"\tfor (n = 0; argv[1][n * 2] != '\\0'; n++) {",
		"\t\tchar pair[3] = { argv[1][n * 2], argv[1][n * 2 + 1], '\\0' };",
		"\t\traw[n] = (uint8_t)strtoul(pair, NULL, 16);",
		"\t}",
		"\tsitu_msg_init(&msg, raw, n);",
		"",
	]

	for struct in structs_of(resolved):
		name   = struct.name
		view   = ident(prefix, name, "view")
		fixed  = struct.layout.is_fixed_size
		lines.extend([
			"\t{",
			"\t\tsitu_view_t view;",
			f'\t\tprintf("-- {name}\\n");',
			f"\t\tif ({view}(&msg, 0{'' if fixed else ', n'}, &view)"
			" != SITU_OK) {",
			'\t\t\tprintf("no-view\\n");',
			"\t\t} else {",
		])
		for ask in asks(struct, set(resolved.structs), resolved.structs):
			lines.extend(_c_ask(prefix, name, ask))
		lines.extend([
			f'\t\t\tprintf("validate %d\\n",'
			f' (int){ident(prefix, name, "validate")}(view));',
			"\t\t}",
			"\t}",
		])

	lines.extend(_c_writes(resolved, prefix))
	lines.extend(["\treturn 0;", "}"])
	return "\n".join(lines) + "\n"


def _c_writes(resolved: ResolvedSchema, prefix: str) -> list[str]:
	"""The write pass, and then the bytes.

	Every struct's writable members, then the buffer, once. What has to agree
	is the buffer: a setter that reverses a byte order or clobbers a
	neighbouring bit field shows there and nowhere else.
	"""
	lines: list[str] = []
	any_write = False

	for struct in structs_of(resolved):
		asked = writes(struct)
		if not asked:
			continue
		any_write = True
		view  = ident(prefix, struct.name, "view")
		fixed = struct.layout.is_fixed_size
		lines.extend([
			"\t{",
			"\t\tsitu_view_t view;",
			f'\t\tprintf("-- write {struct.name}\\n");',
			f"\t\tif ({view}(&msg, 0{'' if fixed else ', n'}, &view)"
			" == SITU_OK) {",
		])
		for ask in asked:
			setter = ident(prefix, struct.name, ask.local, "set")
			getter = ident(prefix, struct.name, ask.local, "get")
			if ask.probe is Probe.COVERED:
				# No read-back: a covered *nested* field has its setter on
				# the parent and its getter on the nested view, which is the
				# right split -- the write has to mark a bit that lives in
				# the message and the read does not. The buffer at the end
				# is what says the bytes landed.
				tag = ask.inside[0]
				lines.extend([
					f"\t\t\t{setter}(&msg, view, {ask.count}u);",
					f'\t\t\tprintf("{ask.local} <- {ask.count} dirty=%d\\n",',
					f"\t\t\t\t{ident(prefix, struct.name, tag, 'is_dirty')}"
					"(&msg) ? 1 : 0);",
				])
				continue
			lines.extend([
				f"\t\t\t{setter}(view, {ask.count}u);",
				f'\t\t\tprintf("{ask.local} <- %llu\\n",'
				f' (unsigned long long){getter}(view));',
			])
		lines.extend(["\t\t}", "\t}"])

	if not any_write:
		return lines

	return lines + [
		"\t{",
		"\t\tuint32_t i;",
		"",
		'\t\tprintf("buffer ");',
		"\t\tfor (i = 0; i < n; i++) {",
		'\t\t\tprintf("%02x", raw[i]);',
		"\t\t}",
		'\t\tprintf("\\n");',
		"\t}",
	]


def _c_ask(prefix: str, struct: str, ask: Ask) -> list[str]:
	call = ident(prefix, struct, ask.local, "{}")
	if ask.probe is Probe.SCALAR:
		return [f'\t\t\tprintf("{ask.local} %lld\\n",'
		        f' (long long){call.format("get")}(view));']
	if ask.probe is Probe.DELIMITED:
		return [f'\t\t\tprintf("{ask.local} len=%u term=%d\\n",'
		        f' {call.format("len")}(view),'
		        f' {call.format("terminated")}(view) ? 1 : 0);']
	if ask.probe is Probe.COUNT:
		return [f'\t\t\tprintf("{ask.local} count=%u\\n",'
		        f' {call.format("count")}(view));']
	if ask.probe is Probe.TAG:
		return ["\t\t\t{",
		        f"\t\t\t\tconst uint8_t *held = {call.format('ptr')}(view);",
		        f'\t\t\t\tprintf("{ask.local} present=%d\\n",'
		        " held == NULL ? 0 : 1);",
		        "\t\t\t}"]
	if ask.probe is Probe.ELEMENT:
		return [f'\t\t\tprintf("{ask.local}[0] %lld\\n",'
		        f' (long long){call.format("get")}(view, 0u));']
	if ask.probe is Probe.NESTED:
		return ["\t\t\t{",
		        "\t\t\t\tsitu_view_t sub;",
		        f"\t\t\t\tconst situ_err_t e = {call.format('view')}"
		        "(view, &sub);",
		        "",
		        f'\t\t\t\tprintf("{ask.local} ok=%d extent=%u\\n",',
		        "\t\t\t\t\te == SITU_OK ? 1 : 0,"
		        " e == SITU_OK ? sub.limit : 0u);",
		        "\t\t\t}"]
	if ask.probe is Probe.MARKER:
		return [f'\t\t\tprintf("{ask.local} little=%d\\n",'
		        f' {call.format("is_little")}(view) ? 1 : 0);']
	if ask.probe is Probe.SEALED:
		gate = ident(prefix, struct, ask.local, "t")
		return ["\t\t\t{",
		        f"\t\t\t\t{gate} held;",
		        f"\t\t\t\tconst situ_err_t refused ="
		        f" {call.format('open')}(view, 0, &held);",
		        f"\t\t\t\tconst situ_err_t opened ="
		        f" {call.format('open')}(view, 1, &held);",
		        "",
		        f'\t\t\t\tprintf("{ask.local} refused=%d opened=%d\\n",',
		        "\t\t\t\t\trefused == SITU_OK ? 0 : 1,",
		        "\t\t\t\t\topened == SITU_OK ? 1 : 0);",
		        "\t\t\t\tif (opened == SITU_OK) {",
		        *[f'\t\t\t\t\tprintf("{one} %lld\\n", (long long)'
		          f"{ident(prefix, struct, ask.local, one, 'get')}(held));"
		          for one in ask.inside],
		        "\t\t\t\t}",
		        "\t\t\t}"]
	if ask.probe is Probe.VARINT:
		return [f'\t\t\tprintf("{ask.local} len=%u value=%llu\\n",'
		        f' {call.format("len")}(view),'
		        f' (unsigned long long){call.format("value")}(view));']
	if ask.probe is Probe.ARM_BYTES:
		return ["\t\t\t{",
		        "\t\t\t\tconst uint8_t *held = NULL;",
		        "\t\t\t\tuint32_t len = 0u;",
		        f"\t\t\t\tconst situ_err_t e = {call.format('ptr')}"
		        "(view, &held, &len);",
		        "",
		        f'\t\t\t\tprintf("{ask.local} ok=%d len=%u\\n",'
		        " e == SITU_OK ? 1 : 0, e == SITU_OK ? len : 0u);",
		        "\t\t\t}"]
	if ask.probe is Probe.ARM_VALUE:
		return ["\t\t\t{",
		        f"\t\t\t\t{'int' if ask.signed else 'uint'}{ask.bits}_t"
		        " held = 0;",
		        f"\t\t\t\tconst situ_err_t e = {call.format('get')}"
		        "(view, &held);",
		        "",
		        f'\t\t\t\tprintf("{ask.local} ok=%d value=%llu\\n",'
		        " e == SITU_OK ? 1 : 0,",
		        "\t\t\t\t\t(unsigned long long)(e == SITU_OK ? held : 0));",
		        "\t\t\t}"]

	length = (f"{macro(prefix, struct, ask.local, 'COUNT')}"
	          if ask.count is not None else f"{call.format('len')}(view)")
	return ["\t\t\t{",
	        f"\t\t\t\tconst uint8_t *held = {call.format('ptr')}(view);",
	        f"\t\t\t\tconst uint32_t len = held == NULL ? 0u : {length};",
	        "",
	        f'\t\t\t\tprintf("{ask.local} len=%u first=%d\\n", len,',
	        "\t\t\t\t\tlen == 0u ? -1 : (int)held[0]);",
	        "\t\t\t}"]


# -- C++ -------------------------------------------------------------------


def _cpp(resolved: ResolvedSchema, prefix: str) -> str:
	lines = [
		"/* Generated by situc: what this schema says about a buffer. */",
		"#include <cstdio>",
		"#include <cstdlib>",
		"",
		'#include "unit.hpp"',
		"",
		"int main(int argc, char **argv)",
		"{",
		"\tstatic std::uint8_t raw[4096];",
		"\tstd::uint32_t n = 0;",
		"",
		"\tif (argc != 2) { return 2; }",
		"\tfor (n = 0; argv[1][n * 2] != '\\0'; n++) {",
		"\t\tchar pair[3] = { argv[1][n * 2], argv[1][n * 2 + 1], '\\0' };",
		"\t\traw[n] = static_cast<std::uint8_t>("
		"std::strtoul(pair, nullptr, 16));",
		"\t}",
		"\t::situ::rt::message msg(raw, n);",
		"",
	]

	for struct in structs_of(resolved):
		name  = struct.name
		fixed = struct.layout.is_fixed_size
		lines.extend([
			"\t{",
			f"\t\t::situ::{c_name(name)} view;",
			f'\t\tstd::printf("-- {name}\\n");',
			f"\t\tif (::situ::{c_name(name)}::at(msg, 0{'' if fixed else ', n'},"
			" view) != ::situ::rt::err::ok) {",
			'\t\t\tstd::printf("no-view\\n");',
			"\t\t} else {",
		])
		for ask in asks(struct, set(resolved.structs), resolved.structs):
			lines.extend(_cpp_ask(ask))
		lines.extend([
			'\t\t\tstd::printf("validate %d\\n",'
			" static_cast<int>(view.validate()));",
			"\t\t}",
			"\t}",
		])

	lines.extend(_cpp_writes(resolved))
	lines.extend(["\treturn 0;", "}"])
	return "\n".join(lines) + "\n"


def _cpp_writes(resolved: ResolvedSchema) -> list[str]:
	"""The write pass, and then the bytes."""
	lines: list[str] = []
	any_write = False

	for struct in structs_of(resolved):
		asked = writes(struct)
		if not asked:
			continue
		any_write = True
		fixed = struct.layout.is_fixed_size
		lines.extend([
			"\t{",
			f"\t\t::situ::{c_name(struct.name)} view;",
			f'\t\tstd::printf("-- write {struct.name}\\n");',
			f"\t\tif (::situ::{c_name(struct.name)}::at(msg,"
			f" 0{'' if fixed else ', n'}, view) == ::situ::rt::err::ok) {{",
		])
		for ask in asked:
			if ask.probe is Probe.COVERED:
				name = c_name(struct.name)
				lines.extend([
					f"\t\t\tview.set_{ask.local}(msg, {ask.count});",
					f'\t\t\tstd::printf("{ask.local} <- {ask.count}'
					' dirty=%d\\n",',
					f"\t\t\t\t::situ::{name}::{ask.inside[0]}_is_dirty(msg)"
					" ? 1 : 0);",
				])
				continue
			lines.extend([
				f"\t\t\tview.set_{ask.local}({ask.count});",
				f'\t\t\tstd::printf("{ask.local} <- %llu\\n",',
				f"\t\t\t\tstatic_cast<unsigned long long>"
				f"(view.{ask.local}()));",
			])
		lines.extend(["\t\t}", "\t}"])

	if not any_write:
		return lines

	return lines + [
		"\t{",
		"\t\tstd::printf(\"buffer \");",
		"\t\tfor (std::uint32_t i = 0; i < n; i++) {",
		'\t\t\tstd::printf("%02x", raw[i]);',
		"\t\t}",
		'\t\tstd::printf("\\n");',
		"\t}",
	]


def _cpp_ask(ask: Ask) -> list[str]:
	if ask.probe is Probe.SCALAR:
		return [f'\t\t\tstd::printf("{ask.local} %lld\\n",'
		        f" static_cast<long long>(view.{ask.local}()));"]
	if ask.probe is Probe.DELIMITED:
		return [f'\t\t\tstd::printf("{ask.local} len=%u term=%d\\n",'
		        f" view.{ask.local}_len(),"
		        f" view.{ask.local}_terminated() ? 1 : 0);"]
	if ask.probe is Probe.COUNT:
		return [f'\t\t\tstd::printf("{ask.local} count=%u\\n",'
		        f" view.{ask.local}_count());"]
	if ask.probe is Probe.TAG:
		return [f'\t\t\tstd::printf("{ask.local} present=%d\\n",'
		        f" view.{ask.local}().empty() ? 0 : 1);"]
	if ask.probe is Probe.ELEMENT:
		return [f'\t\t\tstd::printf("{ask.local}[0] %lld\\n",'
		        f" static_cast<long long>(view.{ask.local}(0)));"]
	if ask.probe is Probe.NESTED:
		return ["\t\t\t{",
		        f"\t\t\t\t::situ::{ask.inner} held;",
		        f"\t\t\t\tconst auto e = view.{ask.local}(held);",
		        "",
		        f'\t\t\t\tstd::printf("{ask.local} ok=%d extent=%u\\n",',
		        "\t\t\t\t\te == ::situ::rt::err::ok ? 1 : 0,",
		        "\t\t\t\t\te == ::situ::rt::err::ok ? held.limit() : 0u);",
		        "\t\t\t}"]
	if ask.probe is Probe.MARKER:
		return [f'\t\t\tstd::printf("{ask.local} little=%d\\n",'
		        f" view.{ask.local}_is_little() ? 1 : 0);"]
	if ask.probe is Probe.SEALED:
		# A callback rather than a returned gate, which is the whole of what
		# C++ adds here: there is no expression that names one outside the
		# verified branch. The answer is the same either way.
		# The interior is captured rather than printed inside the callback:
		# C++ reads it *during* the open, so printing there put the interior
		# ahead of the summary line the other three print first. Same answers,
		# different order, and the diff sees an order.
		return ["\t\t\t{",
		        *[f"\t\t\t\tlong long {one} = 0;" for one in ask.inside],
		        f"\t\t\t\tconst auto refused = view.with_{ask.local}("
		        "false, [](auto) {});",
		        f"\t\t\t\tconst auto opened = view.with_{ask.local}("
		        "true, [&](auto gate) {",
		        *[f"\t\t\t\t\t{one} ="
		          f" static_cast<long long>(gate.{one}());"
		          for one in ask.inside],
		        "\t\t\t\t\t(void)gate;",
		        "\t\t\t\t});",
		        "",
		        f'\t\t\t\tstd::printf("{ask.local} refused=%d opened=%d\\n",',
		        "\t\t\t\t\trefused == ::situ::rt::err::ok ? 0 : 1,",
		        "\t\t\t\t\topened == ::situ::rt::err::ok ? 1 : 0);",
		        "\t\t\t\tif (opened == ::situ::rt::err::ok) {",
		        *[f'\t\t\t\t\tstd::printf("{one} %lld\\n", {one});'
		          for one in ask.inside],
		        "\t\t\t\t}",
		        "\t\t\t}"]
	if ask.probe is Probe.VARINT:
		return [f'\t\t\tstd::printf("{ask.local} len=%u value=%llu\\n",'
		        f" view.{ask.local}_len(),"
		        f" static_cast<unsigned long long>(view.{ask.local}_value()));"]
	if ask.probe is Probe.ARM_BYTES:
		return ["\t\t\t{",
		        "\t\t\t\t::situ::rt::bytes held;",
		        f"\t\t\t\tconst auto e = view.{ask.local}(held);",
		        "",
		        f'\t\t\t\tstd::printf("{ask.local} ok=%d len=%u\\n",',
		        "\t\t\t\t\te == ::situ::rt::err::ok ? 1 : 0,",
		        "\t\t\t\t\te == ::situ::rt::err::ok"
		        " ? static_cast<std::uint32_t>(held.size()) : 0u);",
		        "\t\t\t}"]
	if ask.probe is Probe.ARM_VALUE:
		return ["\t\t\t{",
		        f"\t\t\t\tstd::{'int' if ask.signed else 'uint'}"
		        f"{ask.bits}_t held = 0;",
		        f"\t\t\t\tconst auto e = view.{ask.local}(held);",
		        "",
		        f'\t\t\t\tstd::printf("{ask.local} ok=%d value=%llu\\n",',
		        "\t\t\t\t\te == ::situ::rt::err::ok ? 1 : 0,",
		        "\t\t\t\t\tstatic_cast<unsigned long long>(",
		        "\t\t\t\t\t\te == ::situ::rt::err::ok ? held : 0));",
		        "\t\t\t}"]

	return ["\t\t\t{",
	        f"\t\t\t\tconst auto held = view.{ask.local}();",
	        "",
	        f'\t\t\t\tstd::printf("{ask.local} len=%u first=%d\\n",',
	        "\t\t\t\t\tstatic_cast<std::uint32_t>(held.size()),",
	        "\t\t\t\t\theld.empty() ? -1 : static_cast<int>(held[0]));",
	        "\t\t\t}"]


# -- Rust ------------------------------------------------------------------


def _rust(resolved: ResolvedSchema, prefix: str) -> str:
	lines = [
		"// Generated by situc: what this schema says about a buffer.",
		"mod situ_rt;",
		"mod unit;",
		"",
		"fn main() {",
		"\tlet hex: Vec<String> = std::env::args().collect();",
		"\tlet raw: Vec<u8> = hex[1].as_bytes().chunks(2)",
		"\t\t.map(|pair| u8::from_str_radix("
		"std::str::from_utf8(pair).unwrap(), 16).unwrap())",
		"\t\t.collect();",
		"",
	]

	for struct in structs_of(resolved):
		name = struct.name
		lines.extend([
			"\t{",
			f'\t\tprintln!("-- {name}");',
			f"\t\tmatch unit::{_pascal(name)}::new(&raw) {{",
			"\t\t\tErr(_) => println!(\"no-view\"),",
			"\t\t\tOk(view) => {",
		])
		for ask in asks(struct, set(resolved.structs), resolved.structs):
			lines.extend(_rust_ask(ask))
		lines.extend([
			'\t\t\t\tprintln!("validate {}", match view.validate() {',
			"\t\t\t\t\tOk(())                          => 0,",
			"\t\t\t\t\tErr(situ_rt::Error::Bounds)     => 1,",
			"\t\t\t\t\tErr(situ_rt::Error::Constraint) => 2,",
			"\t\t\t\t\tErr(situ_rt::Error::Version)    => 3,",
			"\t\t\t\t\tErr(_)                          => 9,",
			"\t\t\t\t});",
			"\t\t\t}",
			"\t\t}",
			"\t}",
		])

	lines.extend(_rust_writes(resolved))
	lines.extend(["}"])
	return "\n".join(lines) + "\n"


def _rust_writes(resolved: ResolvedSchema) -> list[str]:
	"""The write pass, and then the bytes.

	A mutable view is its own type here -- `XMut` over `&mut [u8]` -- which is
	how this backend spells section 12.3's invalidation rule: a write while a
	read view is outstanding does not compile. So the buffer is copied once
	and the writes happen against the copy, which is the same bytes the other
	three mutate in place.
	"""
	asked_any = [(struct, writes(struct)) for struct in structs_of(resolved)]
	asked_any = [(struct, asked) for struct, asked in asked_any if asked]
	if not asked_any:
		return []

	lines = ["\tlet mut raw = raw;",
	         "\tlet mut dirty = situ_rt::Dirty::default();",
	         "\tlet _ = &dirty;", ""]

	for struct, asked in asked_any:
		lines.extend([
			"\t{",
			f'\t\tprintln!("-- write {struct.name}");',
			f"\t\tif let Ok(mut view) ="
			f" unit::{_pascal(struct.name)}Mut::new(&mut raw) {{",
		])
		for ask in asked:
			if ask.probe is Probe.COVERED:
				lines.extend([
					f"\t\t\tview.set_{rust_ident(ask.local)}"
					f"(&mut dirty, {ask.count});",
					f'\t\t\tprintln!("{ask.local} <- {ask.count}'
					' dirty={}",',
					f"\t\t\t\tif unit::{_pascal(struct.name)}::"
					f"{rust_ident(ask.inside[0] + '_is_dirty')}(&dirty)"
					" { 1 } else { 0 });",
				])
				continue
			lines.extend([
				f"\t\t\tview.set_{rust_ident(ask.local)}({ask.count});",
				f'\t\t\tprintln!("{ask.local} <- {{}}",'
				f" view.as_ref().{rust_ident(ask.local)}());",
			])
		lines.extend(["\t\t}", "\t}"])

	return lines + [
		"\t{",
		"\t\tlet shown: String = raw.iter()"
		'.map(|b| format!("{:02x}", b)).collect();',
		'\t\tprintln!("buffer {}", shown);',
		"\t}",
	]


def _rust_ask(ask: Ask) -> list[str]:
	call = rust_ident(ask.local)
	if ask.probe is Probe.SCALAR:
		return [f'\t\t\t\tprintln!("{ask.local} {{}}", view.{call}() as i64);']
	if ask.probe is Probe.DELIMITED:
		return [f'\t\t\t\tprintln!("{ask.local} len={{}} term={{}}",'
		        f" view.{rust_ident(ask.local + '_len')}(),"
		        f" if view.{rust_ident(ask.local + '_terminated')}()"
		        " { 1 } else { 0 });"]
	if ask.probe is Probe.COUNT:
		return [f'\t\t\t\tprintln!("{ask.local} count={{}}",'
		        f" view.{rust_ident(ask.local + '_count')}());"]
	if ask.probe is Probe.TAG:
		return [f'\t\t\t\tprintln!("{ask.local} present={{}}",'
		        f" if view.{call}().is_empty() {{ 0 }} else {{ 1 }});"]
	if ask.probe is Probe.NESTED:
		# The extent is asked of the sub-view rather than read off it: the
		# slice behind a generated struct is private, which is the point of
		# the type. `extent()` is emitted for a variable struct and `SIZE` is
		# the constant for a fixed one.
		measure = ("held.extent()" if ask.count is None else str(ask.count))
		return [f"\t\t\t\tmatch view.{call}() {{",
		        f'\t\t\t\t\tOk(held) => println!("{ask.local} ok=1'
		        f' extent={{}}", {measure}),',
		        f'\t\t\t\t\tErr(_) => println!("{ask.local} ok=0'
		        ' extent=0"),',
		        "\t\t\t\t}"]
	if ask.probe is Probe.ELEMENT:
		# `Result`, where C and C++ return the value: the index is the
		# caller's own and the count is the schema's, so the first element is
		# always there and the two shapes agree about it.
		return [f'\t\t\t\tprintln!("{ask.local}[0] {{}}",'
		        f" view.{call}(0).map(|held| held as i64).unwrap_or(0));"]
	if ask.probe is Probe.MARKER:
		return [f'\t\t\t\tprintln!("{ask.local} little={{}}",'
		        f" if view.{rust_ident(ask.local + '_is_little')}()"
		        " { 1 } else { 0 });"]
	if ask.probe is Probe.SEALED:
		opener = rust_ident(f"open_{ask.local}")
		return [f'\t\t\t\tprintln!("{ask.local} refused={{}} opened={{}}",',
		        f"\t\t\t\t\tif view.{opener}(false).is_err()"
		        " { 1 } else { 0 },",
		        f"\t\t\t\t\tif view.{opener}(true).is_ok()"
		        " { 1 } else { 0 });",
		        *([] if not ask.inside else [
			        f"\t\t\t\tif let Ok(gate) = view.{opener}(true) {{",
			        *[f'\t\t\t\t\tprintln!("{one} {{}}",'
			          f" gate.{rust_ident(one)}() as i64);"
			          for one in ask.inside],
			        "\t\t\t\t}",
		        ])]
	if ask.probe is Probe.VARINT:
		return [f'\t\t\t\tprintln!("{ask.local} len={{}} value={{}}",'
		        f" view.{rust_ident(ask.local + '_len')}(),"
		        f" view.{rust_ident(ask.local + '_value')}());"]
	if ask.probe is Probe.ARM_BYTES:
		return [f"\t\t\t\tmatch view.{call}() {{",
		        f'\t\t\t\t\tOk(held) => println!("{ask.local} ok=1'
		        ' len={}", held.len()),',
		        f'\t\t\t\t\tErr(_)   => println!("{ask.local} ok=0 len=0"),',
		        "\t\t\t\t}"]
	if ask.probe is Probe.ARM_VALUE:
		return [f"\t\t\t\tmatch view.{call}() {{",
		        f'\t\t\t\t\tOk(held) => println!("{ask.local} ok=1'
		        ' value={}", held as u64),',
		        f'\t\t\t\t\tErr(_)   =>'
		        f' println!("{ask.local} ok=0 value=0"),',
		        "\t\t\t\t}"]

	return [f"\t\t\t\tlet held = view.{call}();",
	        f'\t\t\t\tprintln!("{ask.local} len={{}} first={{}}", held.len(),',
	        "\t\t\t\t\tif held.is_empty() { -1i32 } else { held[0] as i32 });"]


# -- Python ----------------------------------------------------------------


def _python(resolved: ResolvedSchema, prefix: str) -> str:
	lines = [
		"# Generated by situc: what this schema says about a buffer.",
		"import sys",
		"",
		"import situ_runtime",
		"import unit",
		"",
		"raw = bytearray(bytes.fromhex(sys.argv[1]))",
		"msg = situ_runtime.Message(raw)",
		"",
	]

	for struct in structs_of(resolved):
		name  = struct.name
		fixed = struct.layout.is_fixed_size
		lines.extend([
			f'print("-- {name}")',
			"try:",
			f"\tview = unit.{c_name(name)}.at(msg, 0"
			f"{'' if fixed else ', len(raw)'})",
			"except situ_runtime.BoundsError:",
			'\tprint("no-view")',
			"else:",
		])
		body: list[str] = []
		for ask in asks(struct, set(resolved.structs), resolved.structs):
			body.extend(_python_ask(ask))
		body.extend([
			"try:",
			"\tview.validate()",
			'\tprint("validate 0")',
			"except situ_runtime.BoundsError:",
			'\tprint("validate 1")',
			"except situ_runtime.ConstraintError:",
			'\tprint("validate 2")',
			"except situ_runtime.VersionError:",
			'\tprint("validate 3")',
			"except situ_runtime.SituError:",
			'\tprint("validate 9")',
		])
		lines.extend(f"\t{line}" for line in body)
		lines.append("")

	lines.extend(_python_writes(resolved))
	return "\n".join(lines) + "\n"


def _python_writes(resolved: ResolvedSchema) -> list[str]:
	"""The write pass, and then the bytes."""
	lines: list[str] = []
	any_write = False

	for struct in structs_of(resolved):
		asked = writes(struct)
		if not asked:
			continue
		any_write = True
		fixed = struct.layout.is_fixed_size
		lines.extend([
			f'print("-- write {struct.name}")',
			"try:",
			f"\tview = unit.{c_name(struct.name)}.at(msg, 0"
			f"{'' if fixed else ', len(raw)'})",
			"except situ_runtime.BoundsError:",
			"\tpass",
			"else:",
		])
		for ask in asked:
			if ask.probe is Probe.COVERED:
				lines.extend([
					f"\tview.set_{ask.local}(msg, {ask.count})",
					f'\tprint("{ask.local} <- {ask.count}",',
					f'\t\t"dirty=%d" % (1 if view.{ask.inside[0]}_is_dirty'
					" else 0))",
				])
				continue
			lines.extend([
				f"\tview.{ask.local} = {ask.count}",
				f'\tprint("{ask.local} <-", view.{ask.local})',
			])
		lines.append("")

	if not any_write:
		return lines

	return lines + ['print("buffer", raw.hex())', ""]


def _python_ask(ask: Ask) -> list[str]:
	if ask.probe is Probe.SCALAR:
		return [f'print("{ask.local} %d" % view.{ask.local})']
	if ask.probe is Probe.DELIMITED:
		return [f'print("{ask.local} len=%d term=%d"'
		        f" % (view.{ask.local}_len,"
		        f" 1 if view.{ask.local}_terminated else 0))"]
	if ask.probe is Probe.COUNT:
		return [f'print("{ask.local} count=%d" % view.{ask.local}_count)']
	if ask.probe is Probe.TAG:
		return [f'print("{ask.local} present=%d"'
		        f" % (0 if len(view.{ask.local}) == 0 else 1))"]
	if ask.probe is Probe.ELEMENT:
		return [f'print("{ask.local}[0] %d" % view.{ask.local}(0))']
	if ask.probe is Probe.NESTED:
		return ["try:",
		        f"\theld = view.{ask.local}",
		        "except situ_runtime.SituError:",
		        f'\tprint("{ask.local} ok=0 extent=0")',
		        "else:",
		        f'\tprint("{ask.local} ok=1 extent=%d" % held._len)']
	if ask.probe is Probe.MARKER:
		return [f'print("{ask.local} little=%d"'
		        f" % (1 if view.{ask.local}_is_little else 0))"]
	if ask.probe is Probe.SEALED:
		return ["refused = 0",
		        "try:",
		        f"\tview.open_{ask.local}(False)",
		        "except situ_runtime.SituError:",
		        "\trefused = 1",
		        "opened = 0",
		        "gate = None",
		        "try:",
		        f"\tgate = view.open_{ask.local}(True)",
		        "\topened = 1",
		        "except situ_runtime.SituError:",
		        "\tpass",
		        f'print("{ask.local} refused=%d opened=%d" % (refused, opened))',
		        "if gate is not None:",
		        *[f'\tprint("{one} %d" % gate.{one})' for one in ask.inside],
		        "\tpass"]
	if ask.probe is Probe.VARINT:
		return [f'print("{ask.local} len=%d value=%d"'
		        f" % (view.{ask.local}_len, view.{ask.local}_value))"]
	if ask.probe is Probe.ARM_BYTES:
		return ["try:",
		        f"\theld = view.{ask.local}",
		        "except situ_runtime.SituError:",
		        f'\tprint("{ask.local} ok=0 len=0")',
		        "else:",
		        f'\tprint("{ask.local} ok=1 len=%d" % len(held))']
	if ask.probe is Probe.ARM_VALUE:
		return ["try:",
		        f"\theld = view.{ask.local}",
		        "except situ_runtime.SituError:",
		        f'\tprint("{ask.local} ok=0 value=0")',
		        "else:",
		        f'\tprint("{ask.local} ok=1 value=%d" % held)']

	return [f"held = view.{ask.local}",
	        f'print("{ask.local} len=%d first=%d"'
	        " % (len(held), -1 if len(held) == 0 else held[0]))"]
