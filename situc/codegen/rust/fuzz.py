"""`gen-fuzz --target rust`: a harness driving the Rust accessors.

The fourth language for the second half of section 1's "test scaffolding",
and the same shape as `--target python`'s for the same reason (26.847):
`one_input` is the entry point, a stdin `main` drives it, and no
dependency is taken -- so this repository's own gate RUNS the harness
rather than skipping where `cargo-fuzz` is absent. `cargo-fuzz` is three
lines on top -- and they are in the harness, behind `cfg(fuzzing)`, the
flag `cargo fuzz` sets, so the file is a fuzz target as it stands and
`make fuzz-rust` builds it with nothing written around it:

    #![cfg_attr(fuzzing, no_main)]
    #[cfg(fuzzing)]
    libfuzzer_sys::fuzz_target!(|data: &[u8]| { one_input(data); });

That split is `--target c`'s, whose `SITU_FUZZ_STANDALONE` main exists "so
the harness can be built and run without a fuzzing engine. This is what
keeps it from rotting between runs."
"""

from __future__ import annotations

from situc import __version__, ast
from situc.codegen import refuse_only_arguments, takes_arguments
from situc.codegen.c.names import c_name
from situc.codegen.rust.emit import _ident, _pascal
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.types import ScalarKind
from situc.traverse import data_sized, own_entries


def _one(basename: str, enums: set[str],
		struct: ResolvedStruct) -> list[str]:
	"""One struct's harness: acquire, validate, read everything."""
	name   = c_name(struct.name)
	kind   = _pascal(struct.name)
	extent = struct.layout.size_bytes

	# **The filter is `gen-checks`' decode filter, which three backends
	# have already run over this corpus**, rather than an enumeration of
	# getter shapes grown one rustc error at a time. That enumeration was
	# tried: `&[u8]` for a byte run, `Option<T>` for an enum,
	# `Result<&[u8], Error>` for a sealed run, and a getter taking an index
	# for an array -- four shapes, each found by a compile failure, and no
	# reason to think the next schema has no fifth.
	#
	# A plain fixed integer field is the one shape whose getter signature
	# is certain, so that is what is read, and the count of what is not
	# read goes in the harness. Coverage traded for a harness that
	# compiles for every schema, which is the trade `--target c` makes
	# differently because C has one getter shape.
	reads  = []
	passed = 0
	for entry in own_entries(struct):
		placement = entry.placement
		if placement.kind != "field" or placement.scalar is None:
			continue
		# A variable struct is here for its `validate`, which walks what
		# varies; its getters may answer a `Result` even at a fixed offset
		# (`edges`' `magic`), so none is read.
		if not struct.layout.is_fixed_size:
			passed += 1
			continue
		if (placement.array_count is not None
				or data_sized(placement)
				or placement.radix is not None
				or placement.type_name in enums
				or placement.size_max_bits != placement.size_bits
				or placement.sealed_by is not None
				or placement.scalar.kind not in (ScalarKind.UINT,
				                                 ScalarKind.SINT)
				or placement.scalar.frac_bits
				or placement.scalar.is_bit_packed):
			passed += 1
			continue
		reads.append(f"\t\tsink(i128::from(view.{_ident(placement.name)}()));")

	return [
		f"/// `{struct.name}` over arbitrary bytes.",
		f"pub fn fuzz_{name}(data: &[u8]) {{",
		f"\tif data.len() < {extent} {{",
		"\t\treturn;",
		"\t}",
		"",
		f"\tlet view = match crate::{basename}::{kind}::new(data) {{",
		"\t\tOk(view) => view,",
		"\t\tErr(_) => return,",
		"\t};",
		"",
		"\t// Validation must be safe on arbitrary bytes: it is the first",
		"\t// thing a parser runs and the last thing an attacker controls.",
		"\tlet _ = view.validate();",
		"",
		"\t// Every field READ, not merely the view taken. A getter that",
		"\t// indexes outside its own claim is reached here and nowhere",
		"\t// else in this harness.",
		*(reads or ["\t// No plain fixed integer field to read."]),
		*([f"\t// {passed} further field(s) are not read: their getters'",
		   "\t// signatures vary by shape and only a plain fixed integer's",
		   "\t// is certain from the map alone."] if passed else []),
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema,
		basename: str, prefix: str = "situ") -> str:
	"""A dependency-free fuzz harness over the Rust accessors."""
	refuse_only_arguments(schema, resolved)

	enums = {decl.name for decl in schema.enums()}
	# A register is a bus transaction rather than bytes off a wire, and a
	# zero-byte struct would frame an unbounded number of empty messages.
	# Both are `--target c`'s exclusions and for its reasons.
	# Variable structs too: their `validate` walks runs, variants and
	# nesting, which is where the C and C++ readers read past a buffer and
	# all four took time exponential in nesting (87bba626) -- and a Rust
	# reader that indexes wrongly panics, which is the same denial of
	# service. Fixed-size only, the harness never reached any of it.
	fuzzable = [struct for struct in resolved.structs.values()
	            if struct.layout.is_byte_sized
	            and not takes_arguments(struct)
	            and struct.layout.register is None
	            and not (struct.layout.is_fixed_size
	                     and struct.layout.size_bytes == 0)]

	bodies: list[str] = []
	for struct in fuzzable:
		bodies.extend(_one(basename, enums, struct))

	names = [c_name(struct.name) for struct in fuzzable]
	lines = [
		f"// Generated by situ {__version__} from {basename}.situ"
		" -- do not edit.",
		"//",
		"// A fuzz harness over the Rust accessors: acquire, validate on",
		"// arbitrary bytes, then read every field.",
		"//",
		f"//     rustc --edition 2021 -O {basename}_fuzz.rs"
		" && ./" f"{basename}_fuzz < some-input",
		"//",
		"// `one_input` takes no dependency, which is what lets this",
		"// repository's own gate RUN it rather than skip where cargo-fuzz",
		"// is absent. Built by `cargo fuzz`, which sets `cfg(fuzzing)`,",
		"// the file is a fuzz target as it stands: libFuzzer's entry point",
		"// below takes the place of `main`, and `libfuzzer-sys` is then",
		"// the one dependency.",
		"//",
		"// The split is `--target c`'s, whose `SITU_FUZZ_STANDALONE` main",
		'// exists so the harness "can be built and run without a fuzzing',
		'// engine. This is what keeps it from rotting between runs."',
		"",
		"#![cfg_attr(fuzzing, no_main)]",
		"",
		"mod situ_rt;",
		f"mod {basename};",
		"",
		"/// Where a read goes, so nothing is optimised away.",
		"///",
		"/// `black_box` is unstable outside `core::hint` on some",
		"/// toolchains, so this is a `#[inline(never)]` function reading",
		"/// its argument -- enough to keep a release build from dropping",
		"/// the call, and no feature gate to carry.",
		"#[inline(never)]",
		"pub fn sink(value: i128) {",
		"\tif value == i128::MIN {",
		'\t\tstd::process::abort();',
		"\t}",
		"}",
		"",
		*bodies,
		"/// One input, dispatched by its first byte.",
		"///",
		"/// The first byte selects a struct, so one corpus drives every",
		"/// parser in the schema rather than needing a harness each.",
		"pub fn one_input(data: &[u8]) {",
	]
	if not names:
		lines.extend([
			"\t// Nothing in this schema takes a view: every struct is a",
			"\t// register, or has no fixed bytes to read.",
			"\tlet _ = data;",
			"}",
		])
	else:
		lines.extend([
			"\tif data.is_empty() {",
			"\t\treturn;",
			"\t}",
			"",
			f"\tmatch data[0] as usize % {len(names)} {{",
			*(f"\t\t{index} => fuzz_{one}(&data[1..]),"
			  for index, one in enumerate(names)),
			"\t\t_ => (),",
			"\t}",
			"}",
		])

	lines.extend([
		"",
		"// libFuzzer's entry point, where `cargo fuzz` builds this file.",
		"#[cfg(fuzzing)]",
		"libfuzzer_sys::fuzz_target!(|data: &[u8]| {",
		"\tone_input(data);",
		"});",
		"",
		"/// One input from stdin, so the harness runs with no engine at",
		"/// all. This is what keeps it from rotting between runs.",
		"#[cfg(not(fuzzing))]",
		"fn main() {",
		"\tuse std::io::Read;",
		"",
		"\tlet mut data = Vec::new();",
		"\tif std::io::stdin().read_to_end(&mut data).is_err() {",
		"\t\treturn;",
		"\t}",
		"\tone_input(&data);",
		"}",
	])
	return "\n".join(lines) + "\n"
