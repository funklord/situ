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
variant's arms, and `validate`. Not yet: coded regions, nested structs, sealed
interiors and versioned members -- each answers with an error in three
languages and an exception in the fourth, and a probe spelled wrong in one
language reports a disagreement that is not there.

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
from situc.layout import BITS_PER_BYTE, Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import (
	Member, arm_members, classify, containment_order, local_name,
	own_entries,
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


def asks(struct: ResolvedStruct, structs: set[str]) -> list[Ask]:
	"""Which members this struct can be asked about, in declaration order.

	`traverse.classify` decides what kind a member is, so the four drivers ask
	about exactly the same members -- the alternative is four lists that agree
	today.
	"""
	found: list[Ask] = []

	for entry in own_entries(struct):
		placement = entry.placement
		kind      = classify(struct, placement, structs)
		local     = c_name(local_name(struct, placement))

		# Skipped, each for a reason the module docstring gives: a gated
		# member is reached through the gate, a versioned one answers
		# differently in each language, and a coded member's bytes are the
		# transform's rather than the field's.
		if placement.sealed_by or placement.since is not None \
				or placement.codec is not None:
			continue

		scalar = placement.scalar

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
				and scalar.bits == BITS_PER_BYTE \
				and placement.array_count is not None:
			found.append(Ask(Probe.BYTES, local, placement.array_count))
		elif kind is Member.VARIABLE and scalar is not None \
				and scalar.bits == BITS_PER_BYTE:
			# Only where a *field* gives the length. A member sized by
			# arithmetic over other fields -- `u8 data[(len + 1) * 8 - 2]` --
			# gets no `_len` accessor in C, the count being an expression the
			# caller can evaluate, so there is no fourth spelling to compare.
			if placement.sized_by is None:
				continue
			found.append(Ask(Probe.BYTES, local))
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
		for ask in asks(struct, set(resolved.structs)):
			lines.extend(_c_ask(prefix, name, ask))
		lines.extend([
			f'\t\t\tprintf("validate %d\\n",'
			f' (int){ident(prefix, name, "validate")}(view));',
			"\t\t}",
			"\t}",
		])

	lines.extend(["\treturn 0;", "}"])
	return "\n".join(lines) + "\n"


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
	if ask.probe is Probe.MARKER:
		return [f'\t\t\tprintf("{ask.local} little=%d\\n",'
		        f' {call.format("is_little")}(view) ? 1 : 0);']
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
		for ask in asks(struct, set(resolved.structs)):
			lines.extend(_cpp_ask(ask))
		lines.extend([
			'\t\t\tstd::printf("validate %d\\n",'
			" static_cast<int>(view.validate()));",
			"\t\t}",
			"\t}",
		])

	lines.extend(["\treturn 0;", "}"])
	return "\n".join(lines) + "\n"


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
	if ask.probe is Probe.MARKER:
		return [f'\t\t\tstd::printf("{ask.local} little=%d\\n",'
		        f" view.{ask.local}_is_little() ? 1 : 0);"]
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
		for ask in asks(struct, set(resolved.structs)):
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

	lines.extend(["}"])
	return "\n".join(lines) + "\n"


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
	if ask.probe is Probe.MARKER:
		return [f'\t\t\t\tprintln!("{ask.local} little={{}}",'
		        f" if view.{rust_ident(ask.local + '_is_little')}()"
		        " { 1 } else { 0 });"]
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
		for ask in asks(struct, set(resolved.structs)):
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

	return "\n".join(lines) + "\n"


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
	if ask.probe is Probe.MARKER:
		return [f'print("{ask.local} little=%d"'
		        f" % (1 if view.{ask.local}_is_little else 0))"]
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
