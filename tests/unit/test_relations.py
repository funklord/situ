"""The `relation` construct: rung 3 of the layer ladder (26.95).

Decision 0030 defines it -- a pure predicate over two views, holding no state
and allocating nothing -- and 0032 places it. This file covers the front end:
what parses, what is refused, and the three passes that had to learn a new
declaration exists.

Every refusal here is name-level. Whether the two sides of a comparison are
comparable is a resolved-layout question and is deliberately not asked yet.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from every_schema import ROOT
from situc import (ast, capmap, dissector, dump, namespaces, relation,
                   unparse, wellformed)
from situc.codegen.c import fuzz
from situc.codegen.c import generate as generate_c
from situc.codegen.c import relate
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.cpp import relate as relate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.python import relate as relate_py
from situc.codegen.rust import generate as generate_rs
from situc.codegen.rust import relate as relate_rs
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.pack import pack
from situc.resolve import ResolvedSchema, resolve
from walker import report, vm
from walker.image import Image, load
from walker.report import ERR_CONSTRAINT, OK
from walker.walk import View, acquire

RUNTIME  = ROOT / "runtime" / "c"
COMPILER = shutil.which("cc") or shutil.which("gcc")
HOST_CXX = shutil.which("g++") or shutil.which("clang++")
RUSTC    = shutil.which("rustc")

#: What `make test-c` builds generated code with. A predicate is code somebody
#: ships, so it is held to the same bar -- and these flags are the reason
#: every operand is widened rather than compared as it lies.
WARNINGS = ("-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
            "-Wconversion", "-Wsign-conversion")

HEAD = """target buffer;
endian big;

struct head {
	u16 msg;
	u8  index;
	u8  chunks;
	i8  tweak;
	u64 wide;
}

struct frame {
	head hdr;
	u8   payload[4];
}
"""

GOOD = """relation response_to(request: frame, response: frame) {
	must response.hdr.msg   == request.hdr.msg;
	must response.hdr.index <  request.hdr.chunks;
}
"""


def checked(body: str) -> ast.Schema:
	schema = parse_text(HEAD + "\n" + body)
	wellformed.check(schema)
	return schema


def refused(body: str) -> str:
	with pytest.raises(SituError) as caught:
		checked(body)
	return caught.value.diagnostic.render()


# -- what parses -------------------------------------------------------------


def test_a_relation_carries_two_parameters_and_its_constraints() -> None:
	relation = checked(GOOD).relations()[0]

	assert relation.name == "response_to"
	assert [(p.name, p.type_name) for p in relation.params] == [
		("request", "frame"), ("response", "frame")]
	assert len(relation.body) == 2


def test_parameter_order_is_preserved() -> None:
	"""Order is temporal -- the first parameter is the message seen first.

	A dissector says "response to frame N" and a fuzz harness needs to know
	which message to copy bytes from; both read the order, so nothing may
	normalise it.
	"""
	relation = checked(GOOD).relations()[0]

	assert [param.name for param in relation.params] == ["request", "response"]


def test_a_path_may_reach_through_a_struct_typed_member() -> None:
	assert checked(GOOD).relations()[0].body[0].expr is not None


# -- what is refused ---------------------------------------------------------


def test_one_parameter_is_not_a_relation() -> None:
	rendered = refused("relation r(a: frame) {\n\tmust a.hdr.msg == 1;\n}\n")

	assert "over one message" in rendered
	assert "must_eq" in rendered and "require" in rendered


def test_three_parameters_are_refused_with_the_reason() -> None:
	"""Three is nearly always quantification over a set wearing a disguise.

	The refusal says where that belongs -- what `--layer converse` generates
	-- rather than only that it is not allowed here.
	"""
	rendered = refused(
		"relation r(a: frame, b: frame, c: frame) {\n"
		"\tmust b.hdr.msg == a.hdr.msg;\n}\n")

	assert "over 3 messages" in rendered
	assert "converse" in rendered


def test_two_parameters_may_not_share_a_name() -> None:
	rendered = refused(
		"relation r(a: frame, a: frame) {\n\tmust a.hdr.msg == a.hdr.msg;\n}\n")

	assert "already a parameter" in rendered


def test_a_parameter_names_a_declared_struct() -> None:
	rendered = refused(
		"relation r(a: nope, b: frame) {\n\tmust b.hdr.msg == a.hdr.msg;\n}\n")

	assert "unknown struct `nope`" in rendered


def test_an_empty_body_is_refused() -> None:
	"""A relation with no `must` is true of every pair, so it says nothing."""
	rendered = refused("relation r(a: frame, b: frame) {\n}\n")

	assert "states nothing" in rendered


def test_a_path_must_be_rooted_at_a_parameter() -> None:
	rendered = refused(
		"relation r(a: frame, b: frame) {\n\tmust msg == a.hdr.msg;\n}\n")

	assert "not a message this relation was given" in rendered


def test_a_bare_parameter_is_not_a_value() -> None:
	rendered = refused("relation r(a: frame, b: frame) {\n\tmust b == a;\n}\n")

	assert "whole message, not a value" in rendered


def test_a_member_that_does_not_exist_is_refused() -> None:
	"""Resolution walks through struct-typed members, so `hdr.nope` is caught
	against `head` rather than shrugged at because `hdr` existed."""
	rendered = refused(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.nope == a.hdr.msg;\n}\n")

	assert "`head` has no member `nope`" in rendered


def test_a_constraint_over_one_message_is_refused() -> None:
	"""0030's third example was miscounted as a cross-message case.

	Put on the member it is checked whenever that message is validated; put
	in a relation it is checked only when somebody evaluates a pair.
	"""
	rendered = refused(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.index < b.hdr.chunks;\n}\n")

	assert "reads one message" in rendered


def test_a_relation_may_not_take_a_struct_name() -> None:
	rendered = refused(
		"relation frame(a: frame, b: frame) {\n"
		"\tmust b.hdr.msg == a.hdr.msg;\n}\n")

	assert "declared more than once" in rendered


# -- the passes that had to learn the construct ------------------------------


def test_a_namespace_qualifies_the_name_and_the_parameter_types() -> None:
	schema = namespaces.flatten(parse_text(
		"target buffer;\nendian big;\n\n"
		"namespace wire {\n"
		"\tstruct head {\n\t\tu16 msg;\n\t}\n\n"
		"\trelation response_to(request: head, response: head) {\n"
		"\t\tmust response.msg == request.msg;\n"
		"\t}\n}\n"))
	wellformed.check(schema)
	relation = schema.relations()[0]

	assert relation.name == "wire::response_to"
	assert [p.type_name for p in relation.params] == ["wire::head", "wire::head"]


def test_a_namespace_leaves_the_body_alone() -> None:
	"""Parameter names are local to the relation and mean nothing outside it.

	Passing them through the namespace rewriter would turn `request` into
	`wire::request` and leave a body referring to nothing -- the one place
	where not rewriting an expression is the correct rewrite.
	"""
	schema = namespaces.flatten(parse_text(
		"target buffer;\nendian big;\n\n"
		"namespace wire {\n"
		"\tstruct head {\n\t\tu16 msg;\n\t}\n\n"
		"\trelation r(request: head, response: head) {\n"
		"\t\tmust response.msg == request.msg;\n"
		"\t}\n}\n"))

	rendered = unparse.decl_lines(schema.relations()[0])

	assert "must response.msg == request.msg;" in [line.strip() for line in rendered]


def test_dump_ast_renders_a_relation() -> None:
	"""`situc dump-ast` raises on a declaration it does not know, and an
	invariant reached the tree before its case did (26.35). Same trap, so the
	same test arrives with the construct rather than after it.
	"""
	rendered = dump.dump(checked(GOOD))

	# No traceback, and the construct is visible in the output.
	assert "relation response_to(request: frame, response: frame)" in rendered
	assert "must response.hdr.msg == request.hdr.msg" in rendered


def test_unparse_round_trips_a_relation() -> None:
	source = "\n".join(unparse.decl_lines(checked(GOOD).relations()[0]))
	again  = checked(source + "\n").relations()[0]

	assert again.name == "response_to"
	assert [(p.name, p.type_name) for p in again.params] == [
		("request", "frame"), ("response", "frame")]
	assert len(again.body) == 2


# -- the C predicate (26.95) -------------------------------------------------


def analysed(body: str) -> tuple[ast.Schema, ResolvedSchema]:
	schema = parse_text(HEAD + "\n" + body)
	wellformed.check(schema)
	return schema, resolve(schema, solve(schema))


def emitted(body: str, stem: str = "t") -> dict[str, str]:
	schema, resolved = analysed(body)
	return relate.generate(schema, resolved, stem)


def test_the_predicate_takes_two_views_and_returns_an_error() -> None:
	header = emitted(GOOD)["t_relate.h"]

	assert ("situ_err_t situ_rel_response_to(situ_view_t request, "
	        "situ_view_t response);") in header


def test_the_predicate_lands_in_its_own_files() -> None:
	"""A rung adds files and changes none (0032), so the ordinary header is
	byte-identical whether or not this rung was asked for."""
	schema, resolved = analysed(GOOD)

	plain = generate_c(schema, resolved, "t").files()
	extra = relate.generate(schema, resolved, "t")

	assert set(extra) == {"t_relate.h", "t_relate.c"}
	assert not set(plain) & set(extra)


def test_a_rung_adds_files_and_changes_none() -> None:
	"""The additivity invariant, over the pair of rungs that exist."""
	schema, resolved = analysed(GOOD)

	view   = generate_c(schema, resolved, "t").files()
	higher = dict(view)
	higher.update(relate.generate(schema, resolved, "t"))

	assert set(view) < set(higher), "the file set must grow, not merely persist"
	for name, text in view.items():
		assert higher[name] == text, f"{name} changed between rungs"


def test_it_compares_through_the_getters_not_the_bytes() -> None:
	"""Reading through the generated getter is what makes the comparison one
	of values: the getter already byte-swaps, scales and decodes."""
	source = emitted(GOOD)["t_relate.c"]

	assert "situ_head_msg_get(" in source
	assert "situ_frame_hdr_view(" in source


def test_a_failed_constraint_is_a_constraint_error() -> None:
	"""No new failure class: `SITU_ERR_CONSTRAINT` already means this, and an
	eighth would have to arrive in four runtimes."""
	assert "return SITU_ERR_CONSTRAINT;" in emitted(GOOD)["t_relate.c"]


def test_unsigned_operands_widen_to_uint64() -> None:
	source = emitted(GOOD)["t_relate.c"]

	assert "uint64_t situ_v_" in source
	assert "int64_t situ_v_" not in source.replace("uint64_t situ_v_", "")


def test_a_signed_operand_widens_everything_to_int64() -> None:
	"""Signed wins wherever it can: every unsigned width below 64 fits in
	`int64_t` without changing a value."""
	source = emitted(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.tweak == a.hdr.msg;\n}\n")["t_relate.c"]

	assert "int64_t situ_v_" in source


def test_a_u64_against_a_signed_value_is_refused() -> None:
	"""No 64-bit C type holds both ranges, so there is no correct spelling.

	Refused rather than cast, because a cast here changes the answer rather
	than the type.
	"""
	schema, resolved = analysed(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.wide == a.hdr.tweak;\n}\n")

	names = dict(relate.refusals(schema, resolved))

	assert "r" in names
	assert "no 64-bit type holds both ranges" in names["r"]
	assert relate.generate(schema, resolved, "t") == {}


def test_a_refused_relation_does_not_take_the_others_with_it() -> None:
	schema, resolved = analysed(
		"relation bad(a: frame, b: frame) {\n"
		"\tmust b.hdr.wide == a.hdr.tweak;\n}\n\n"
		"relation good(a: frame, b: frame) {\n"
		"\tmust b.hdr.msg == a.hdr.msg;\n}\n")

	header = relate.generate(schema, resolved, "t")["t_relate.h"]

	assert "situ_rel_good" in header
	assert "situ_rel_bad" not in header
	assert [name for name, _ in relate.refusals(schema, resolved)] == ["bad"]


def test_a_call_is_refused_naming_what_a_relation_asks() -> None:
	schema, resolved = analysed(
		"relation r(a: frame, b: frame) {\n"
		"\tmust size(b.hdr) == size(a.hdr);\n}\n")

	names = dict(relate.refusals(schema, resolved))

	assert "asks the layout nothing" in names["r"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_predicate_compiles_under_the_same_warnings(
		tmp_path: Path) -> None:
	"""Generated code needing a relaxed warning set is code nobody can ship.

	This is the check the widening exists for: `-Wsign-conversion` turns an
	`i8` compared against a `u16` into a build failure rather than a warning.
	"""
	schema, resolved = analysed(GOOD)

	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in relate.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")

	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 "-c", str(tmp_path / "t_relate.c"), "-o", str(tmp_path / "o.o")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


# -- the other three backends (26.95) ----------------------------------------
#
# One walk, four spellings. What each test below really checks is that the
# spelling is right; that the relation is *expressible at all* is decided once
# in `situc.relation`, which is why the refusal tests above are not repeated
# per backend.


def test_every_backend_agrees_on_what_it_refuses() -> None:
	"""A relation is refused everywhere or nowhere.

	Python's integers are arbitrary precision and would compare a `u64`
	against an `i8` happily. It is refused there too, because a schema one
	backend accepts and another does not is a schema that means two things --
	the failure the four-way agreement tests exist to catch.
	"""
	schema, resolved = analysed(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.wide == a.hdr.tweak;\n}\n")

	assert relation.refusals(schema, resolved)
	assert relate.generate(schema, resolved, "t") == {}
	assert relate_cpp.generate(schema, resolved, "t") == {}
	assert relate_rs.generate(schema, resolved, "t") == {}
	assert relate_py.generate(schema, resolved, "t") == {}


def test_cpp_takes_the_struct_class_rather_than_a_bare_view() -> None:
	"""C++ can type the parameter, so the predicate cannot be handed the
	wrong message by accident. C cannot: every view there is a
	`situ_view_t`."""
	schema, resolved = analysed(GOOD)

	header = relate_cpp.generate(schema, resolved, "t")["t_relate.hpp"]

	assert ("rel_response_to(const ::situ::frame &request, "
	        "const ::situ::frame &response)") in header
	assert "return ::situ::rt::err::constraint;" in header


def test_cpp_names_the_sub_view_class_it_acquires_not_the_parent() -> None:
	"""`response.hdr` is a `head`, and the local that holds it has to say so.

	C never needed the distinction because every view is one type there, so
	the plan carried only the struct whose accessor reaches the member. C++
	declares the local outright and named the parent class, which does not
	compile.
	"""
	schema, resolved = analysed(GOOD)

	header = relate_cpp.generate(schema, resolved, "t")["t_relate.hpp"]

	assert "::situ::head v_0_held;" in header
	assert "::situ::frame v_0_held;" not in header


def test_rust_carries_failure_with_the_question_mark() -> None:
	"""A sub-view is `Result<Head>` there, so there is no error local to
	thread and `?` does the work C spells out."""
	schema, resolved = analysed(GOOD)

	module = relate_rs.generate(schema, resolved, "t")["t_relate.rs"]

	assert "pub fn rel_response_to(request: &Frame, response: &Frame)" in module
	assert "let v_0 = response.hdr()?;" in module
	assert "return Err(Error::Constraint);" in module


def test_python_raises_rather_than_returning() -> None:
	"""The convention `validate` already follows in this runtime."""
	schema, resolved = analysed(GOOD)

	module = relate_py.generate(schema, resolved, "t")["t_relate.py"]

	assert "def rel_response_to(request: frame, response: frame) -> None:" in module
	assert "raise ConstraintError(" in module


def test_python_reports_the_constraint_in_the_schemas_words() -> None:
	"""The message names paths the author wrote, not the generated locals.

	A reader who sees `v_1 == v_3` has to go and find the generator; one who
	sees the constraint knows what failed.
	"""
	schema, resolved = analysed(GOOD)

	module = relate_py.generate(schema, resolved, "t")["t_relate.py"]

	assert "'(response.hdr.msg == request.hdr.msg)'" in module
	assert "v_1 == v_3'" not in module


def test_python_needs_no_widening_and_says_so_by_not_doing_it() -> None:
	"""Arbitrary precision means the cast the other three need is absent."""
	schema, resolved = analysed(GOOD)

	module = relate_py.generate(schema, resolved, "t")["t_relate.py"]

	assert "uint64" not in module and "int64" not in module


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_cpp_predicate_compiles_under_the_same_warnings(
		tmp_path: Path) -> None:
	schema, resolved = analysed(GOOD)

	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in relate_cpp.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "tu.cpp").write_text(
		'#include "t_relate.hpp"\nint main() { return 0; }\n', encoding="ascii")

	assert HOST_CXX is not None
	built = subprocess.run(
		[HOST_CXX, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Wconversion",
		 "-Wsign-conversion", "-Werror", f"-I{tmp_path}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 "-fsyntax-only", str(tmp_path / "tu.cpp")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_rust_predicate_compiles_under_denied_warnings(
		tmp_path: Path) -> None:
	schema, resolved = analysed(GOOD)

	src = tmp_path / "src"
	src.mkdir()
	# The runtime is `no_std` alone; as a module of a larger crate that
	# attribute belongs to the crate root, which is what the Rust backend's
	# own tests do too.
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding="ascii")
	for name, text in relate_rs.generate(schema, resolved, "t").items():
		(src / name).write_text(text, encoding="ascii")
	(src / "lib.rs").write_text(
		"pub mod situ_rt;\npub mod t;\npub mod t_relate;\n", encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", "--crate-type", "lib",
		 str(src / "lib.rs"), "-o", str(tmp_path / "out.rlib")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_a_rung_adds_files_in_every_backend(tmp_path: Path) -> None:
	"""Additivity is a property of the ladder, not of one backend."""
	schema, resolved = analysed(GOOD)

	pairs = (
		(generate_c(schema, resolved, "t").files(),
		 relate.generate(schema, resolved, "t")),
		(generate_cpp(schema, resolved, "t").files(),
		 relate_cpp.generate(schema, resolved, "t")),
		({"t.rs": generate_rs(schema, resolved, "t").module},
		 relate_rs.generate(schema, resolved, "t")),
		(generate_py(schema, resolved, "t").files(),
		 relate_py.generate(schema, resolved, "t")),
	)

	for view, extra in pairs:
		assert extra, "every backend emits something for this schema"
		assert not set(view) & set(extra), "a rung adds files, it never edits"


# -- the walker, as a fifth column (26.95) -----------------------------------


def packed(body: str) -> tuple[Image, ast.Schema]:
	"""The schema packed to an image, loaded back."""
	schema, resolved = analysed(body)
	blob, _ = pack(schema, resolved, metadata=True)
	return load(blob), schema


def test_the_image_carries_a_relation_and_its_programs() -> None:
	image, _ = packed(GOOD)

	assert len(image.relations) == 1
	assert len(image.relations[0].musts) == 2
	# Both parameters are `frame`, which is the case that made a placement
	# index insufficient on its own and `arg_field` necessary.
	assert image.relations[0].request == image.relations[0].response


def test_the_walk_answers_a_relation_the_way_the_backends_do() -> None:
	"""The fifth column: a table walk and four compiled backends answering
	the same question about the same pair."""
	image, _ = packed(GOOD)
	shape    = image.relations[0].request

	def view(msg: int, index: int, chunks: int) -> View:
		raw = bytes(msg.to_bytes(2, "big") + bytes([index, chunks]) + bytes(13))
		return acquire(image, raw, shape)

	request = view(0x1234, 0, 4)

	assert report.relate(image, 0, request, view(0x1234, 2, 4)) == OK
	assert report.relate(image, 0, request, view(0x9999, 2, 4)) == ERR_CONSTRAINT
	assert report.relate(image, 0, request, view(0x1234, 9, 4)) == ERR_CONSTRAINT


def test_argument_order_is_temporal_in_the_walk_too() -> None:
	image, _ = packed(GOOD)
	shape    = image.relations[0].request

	def view(msg: int, index: int, chunks: int) -> View:
		raw = bytes(msg.to_bytes(2, "big") + bytes([index, chunks]) + bytes(13))
		return acquire(image, raw, shape)

	first, second = view(0x1234, 0, 4), view(0x1234, 2, 0)

	assert report.relate(image, 0, first, second) == OK
	assert report.relate(image, 0, second, first) == ERR_CONSTRAINT


def test_arg_field_is_refused_outside_a_relation() -> None:
	"""A relation program reaching a caller that passes no `load_arg` would
	otherwise read the wrong message, answering the question asked about a
	different pair."""
	code = bytes([vm.ARG_FIELD, 0]) + b"\x00\x00\x00\x00" + bytes([vm.END])

	with pytest.raises(vm.VmError, match="outside a relation"):
		vm.run(code, 0, lambda i: 0, lambda i: 0, lambda i: 0, lambda i: 0, 0)


def test_the_walker_renders_relations_as_a_supported_probe() -> None:
	assert "relation" in report.SUPPORTED


# -- the layer scalars in the capability map (26.95, decision 0032) ----------


def mapped(body: str) -> str:
	schema, resolved = analysed(body)
	return capmap.render(schema, resolved, "t.situ")


def test_a_relation_raises_the_schemas_reach() -> None:
	assert "# layers: floor=view reach=relate" in mapped(GOOD)


def test_a_schema_without_relations_says_nothing_about_layers() -> None:
	"""Silent at the default, which is this map's rule for every axis.

	It is also what keeps a map committed before the ladder existed
	byte-identical to one generated after it -- the three in `std/` are the
	proof, and they are checked by the suite already.
	"""
	assert "layers:" not in mapped("struct only { u8 a; }\n")


def test_deleting_a_relation_is_a_visible_regression() -> None:
	"""The acceptance criterion the scalars exist for.

	A consumer building at `--layer view` emits nothing for a relation, so
	removing one changes none of their generated output. The map is where it
	shows, which is why the fact belongs there rather than in a new artifact
	nothing checks.
	"""
	before = mapped(GOOD)
	after  = mapped("struct only { u8 a; }\n")

	assert before != after
	assert "reach=relate" in before and "reach=relate" not in after


def test_an_unbounded_expansion_codec_raises_the_floor() -> None:
	"""Case E of 0031: the one case no measure-then-allocate pass serves.

	The floor is a property of constructs, so it is the same at every
	invocation -- which is what lets one committed map serve every rung.
	"""
	schema = parse_text(
		"target buffer;\nendian big;\n\n"
		"codec squeeze {\n\texpansion = unbounded;\n\tgranularity = byte;\n"
		"\tseekable;\n\tdeterministic;\n}\n"
		"impl squeeze extern \"squeeze_go\";\n\n"
		"struct packed_up {\n\tu16 len;\n"
		"\tcoded body(squeeze) { u8 raw[4]; }\n}\n")
	resolved = resolve(schema, solve(schema))

	assert "floor=edit" in capmap.render(schema, resolved, "t.situ")


# -- the two artifacts a relation was worth building for (0030) --------------


def test_the_equality_constraints_are_the_conversation_key() -> None:
	"""No second declaration: `must b.msg == a.msg` says what identifies an
	exchange, and both consumers read it back out of the same place."""
	schema = checked(GOOD)

	assert relation.conversation_key(schema.relations()[0]) == [
		("request.hdr.msg", "response.hdr.msg")]


def test_an_inequality_is_a_rule_and_not_a_key() -> None:
	"""`b.index < a.chunks` constrains a pair without identifying one."""
	schema = checked(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.index < a.hdr.chunks;\n}\n")

	assert relation.conversation_key(schema.relations()[0]) == []


def test_the_dissector_tracks_the_conversation() -> None:
	schema, resolved = analysed(GOOD)

	lua = dissector.generate(schema, resolved, "t")

	assert "local situ_conv_response_to = {}" in lua
	assert "situ_conv_response_to_record(tvb, pinfo)" in lua
	assert "frame_f.response_to_request = ProtoField.framenum(" in lua


def test_the_dissector_looks_up_before_it_records() -> None:
	"""Both parameters usually name one struct, so one dissector does both.
	Recording first would make every frame its own request."""
	schema, resolved = analysed(GOOD)

	lua    = dissector.generate(schema, resolved, "t")
	lookup = lua.index("situ_conv_response_to_lookup(tvb")
	record = lua.index("situ_conv_response_to_record(tvb, pinfo)\n",
	                   lua.index("function frame.dissector"))

	assert lookup < record


def test_a_relation_with_no_equality_gets_no_conversation() -> None:
	"""Said in the output rather than left as an absence."""
	schema, resolved = analysed(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.index < a.hdr.chunks;\n}\n")

	lua = dissector.generate(schema, resolved, "t")

	assert "no conversation key" in lua
	assert "situ_conv_r = {}" not in lua


def test_the_fuzz_harness_copies_the_key_across() -> None:
	"""The copy is the whole point.

	A `u16` match is one draw in 65536 and a varint is worse, so a fuzzer
	rediscovering the correlation by luck reaches nothing -- and a target
	that reaches nothing looks exactly like a clean run.
	"""
	schema, resolved = analysed(GOOD)

	harness = fuzz.generate(schema, resolved, "t")

	assert "static void fuzz_rel_response_to(" in harness
	assert "memcpy(b + 0u, a + 0u, 2u);" in harness
	assert "situ_rel_response_to(va, vb)" in harness
	# It needs rung 3's header, and says so rather than failing at the first
	# build with a missing include.
	assert '#include "t_relate.h"' in harness


def test_the_fuzz_entry_point_reaches_the_relation() -> None:
	"""A harness nothing dispatches to is a harness that never runs."""
	schema, resolved = analysed(GOOD)

	harness = fuzz.generate(schema, resolved, "t")

	assert "fuzz_rel_response_to(data + 1, size - 1u);" in harness
