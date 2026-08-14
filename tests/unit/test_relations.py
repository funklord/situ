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
from situc import (ast, capmap, dissector, dump, layers, namespaces,
                   relation, requirements, unparse, wellformed)
from situc.codegen.c import converse, drive, edit, frame, fuzz, owned
from situc.codegen.c import generate as generate_c
from situc.codegen.c import relate
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.cpp import converse as converse_cpp
from situc.codegen.cpp import drive as drive_cpp
from situc.codegen.cpp import edit as edit_cpp
from situc.codegen.cpp import frame as frame_cpp
from situc.codegen.cpp import relate as relate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.python import converse as converse_py
from situc.codegen.python import drive as drive_py
from situc.codegen.python import edit as edit_py
from situc.codegen.python import frame as frame_py
from situc.codegen.python import relate as relate_py
from situc.codegen.rust import generate as generate_rs
from situc.codegen.rust import converse as converse_rs
from situc.codegen.rust import drive as drive_rs
from situc.codegen.rust import edit as edit_rs
from situc.codegen.rust import frame as frame_rs
from situc.codegen.rust import relate as relate_rs
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.pack import pack
from situc.resolve import ResolvedSchema, resolve
from walker import owned as owned_walk
from walker import report, session, vm
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


#: An equality-only relation, for the cases that need two messages known to
#: satisfy one. `GOOD` carries `response.hdr.index < request.hdr.chunks` as
#: well, deliberately -- that inequality is what separates a rule about a pair
#: from the key that identifies one -- and `0 < 0` is false, so a pair of
#: zeroed messages does not satisfy it. Every equality is satisfied by them,
#: which is what makes a positive case free of any layout knowledge.
KEYED = """relation keyed_to(request: frame, response: frame) {
	must response.hdr.msg == request.hdr.msg;
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


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_cpp_predicate_answers_both_ways(tmp_path: Path) -> None:
	"""Compiled is not run, and `-fsyntax-only` is not even linked.

	The check above takes the predicate as far as the C++ front end and stops,
	which invariant 35 names by example as the shape that looks like a test
	and is not. This one builds a program, runs it, and requires the predicate
	to accept a matching pair and refuse a mismatched one -- the same two
	cases the generated cmocka suite holds the C to.
	"""
	schema, resolved = analysed(KEYED)

	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in relate_cpp.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")

	(tmp_path / "main.cpp").write_text("""#include <cstdint>
#include <cstring>

#include "t_relate.hpp"

int main()
{
	std::uint8_t a[::situ::frame::size_bytes];
	std::uint8_t b[::situ::frame::size_bytes];

	std::memset(a, 0x00, sizeof a);
	std::memset(b, 0x00, sizeof b);

	::situ::rt::message owner_a(a, (std::uint32_t)sizeof a);
	::situ::rt::message owner_b(b, (std::uint32_t)sizeof b);
	::situ::frame view_a;
	::situ::frame view_b;

	if (::situ::frame::at(owner_a, 0, view_a) != ::situ::rt::err::ok) return 1;
	if (::situ::frame::at(owner_b, 0, view_b) != ::situ::rt::err::ok) return 2;
	if (::situ::rel_keyed_to(view_a, view_b) != ::situ::rt::err::ok) return 3;

	std::memset(a, 0xff, sizeof a);
	if (::situ::rel_keyed_to(view_a, view_b) == ::situ::rt::err::ok) return 4;

	return 0;
}
""", encoding="ascii")

	assert HOST_CXX is not None
	built = subprocess.run(
		[HOST_CXX, "-std=c++17", "-O1", f"-I{tmp_path}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "situ.c"),
		 "-o", str(tmp_path / "run")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True, text=True)
	assert ran.returncode == 0, f"the C++ predicate answered wrongly at step {ran.returncode}"


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_rust_predicate_answers_both_ways(tmp_path: Path) -> None:
	"""The Rust half of the case above: `--crate-type lib` never runs either."""
	schema, resolved = analysed(KEYED)

	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding="ascii")
	for name, text in relate_rs.generate(schema, resolved, "t").items():
		(src / name).write_text(text, encoding="ascii")

	(src / "main.rs").write_text("""pub mod situ_rt;
pub mod t;
pub mod t_relate;

fn main()
{
	let zeroed = [0u8; 64];
	let filled = [0xffu8; 64];

	let a = t::Frame::new(&zeroed).expect("a view over zeroed bytes");
	let b = t::Frame::new(&zeroed).expect("a view over zeroed bytes");
	assert!(t_relate::rel_keyed_to(&a, &b).is_ok(),
		"a matching pair was refused");

	let c = t::Frame::new(&filled).expect("a view over filled bytes");
	assert!(t_relate::rel_keyed_to(&c, &b).is_err(),
		"a mismatched pair was accepted");
}
""", encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-A", "warnings",
		 str(src / "main.rs"), "-o", str(tmp_path / "run")],
		capture_output=True, text=True, cwd=tmp_path)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True, text=True)
	assert ran.returncode == 0, ran.stderr


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


# -- rung 4: stream framing, and the additivity invariant it owns (26.96) ----


def test_the_reader_holds_bytes_and_allocates_nothing() -> None:
	"""Rung 4's new permission is holding bytes between calls. The buffer is
	the caller's, so the rung above `view` in that respect is still not
	rung 2."""
	schema, resolved = analysed(GOOD)

	header = frame.generate(schema, resolved, "t")["t_frame.h"]

	assert "situ_frame_reader_init(situ_frame_reader_t *reader, uint8_t *buf," in header
	assert "malloc" not in header
	# It stands on the primitive that already existed for exactly this.
	assert "situ_frame_required(reader->buf, reader->have, &need)" in header


def test_next_does_not_consume_and_says_when_the_view_dies() -> None:
	"""A reader that consumed as it returned would hand out a view and
	invalidate it on the next call."""
	schema, resolved = analysed(GOOD)

	header = frame.generate(schema, resolved, "t")["t_frame.h"]

	assert "situ_frame_reader_advance" in header
	assert "IS DEAD FROM HERE ON" in header


def test_a_message_larger_than_the_buffer_is_reported_not_awaited() -> None:
	"""Waiting never makes it fit, so spinning on the stream is the wrong
	answer and BOUNDS is the right one."""
	schema, resolved = analysed(GOOD)

	header = frame.generate(schema, resolved, "t")["t_frame.h"]

	assert "if (need > reader->cap) {" in header


def test_every_adjacent_rung_adds_files_and_changes_none() -> None:
	"""The additivity invariant of 0032, over every rung that exists.

	Two rules it enforces, both of which fail silently otherwise: includes
	point down the ladder and never up, and no rung's file carries
	conditional compilation for a rung above it. The assertion is that the
	set *grows* -- a generator emitting nothing would satisfy a weaker one.
	"""
	schema, resolved = analysed(GOOD)

	view   = generate_c(schema, resolved, "t").files()
	relate_files = dict(view)
	relate_files.update(relate.generate(schema, resolved, "t"))
	frame_files  = dict(relate_files)
	frame_files.update(frame.generate(schema, resolved, "t"))

	for lower, higher in ((view, relate_files), (relate_files, frame_files)):
		assert set(lower) < set(higher), "a rung must add files, not merely keep them"
		for name, text in lower.items():
			assert higher[name] == text, f"{name} changed between rungs"


def test_no_rung_includes_a_rung_above_it() -> None:
	"""Includes point down the ladder. An include pointing up is additivity
	lost while the file list still looks right."""
	schema, resolved = analysed(GOOD)

	lower = generate_c(schema, resolved, "t").files()
	lower.update(relate.generate(schema, resolved, "t"))

	for name, text in lower.items():
		assert "_frame.h" not in text, f"{name} reaches up the ladder"
	for name, text in generate_c(schema, resolved, "t").files().items():
		assert "_relate.h" not in text, f"{name} reaches up the ladder"


def test_no_rung_carries_conditional_compilation_for_a_higher_one() -> None:
	schema, resolved = analysed(GOOD)

	every = generate_c(schema, resolved, "t").files()
	every.update(relate.generate(schema, resolved, "t"))
	every.update(frame.generate(schema, resolved, "t"))

	for name, text in every.items():
		assert "SITU_LAYER" not in text, f"{name} switches on a layer"


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_reader_reassembles_across_every_chunk_boundary(
		tmp_path: Path) -> None:
	"""A framing reader that only works when a message arrives whole is one
	that works on a loopback and nowhere else."""
	schema, resolved = analysed(GOOD)

	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in frame.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "drive.c").write_text(_CHUNK_DRIVER, encoding="ascii")

	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "drive.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "drive")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr


_CHUNK_DRIVER = """#include <assert.h>
#include <string.h>
#include "t_frame.h"

int main(void)
{
	uint8_t stream[3 * 17];
	for (unsigned m = 0; m < 3; m++) {
		uint8_t *f = stream + m * 17;
		memset(f, 0, 17);
		f[0] = 0x12; f[1] = (uint8_t)(0x30 + m);
		f[2] = (uint8_t)m; f[3] = 4;
	}

	for (uint32_t chunk = 1; chunk <= sizeof stream; chunk++) {
		uint8_t back[128];
		situ_frame_reader_t r;
		situ_frame_reader_init(&r, back, sizeof back);

		unsigned seen = 0;
		for (uint32_t at = 0; at < sizeof stream; at += chunk) {
			uint32_t n = chunk;
			if (at + n > sizeof stream) {
				n = (uint32_t)(sizeof stream) - at;
			}
			assert(situ_frame_reader_push(&r, stream + at, n) == SITU_OK);

			situ_msg_t msg;
			situ_view_t v;
			while (situ_frame_reader_next(&r, &msg, &v) == SITU_OK) {
				situ_view_t hdr;
				assert(situ_frame_hdr_view(v, &hdr) == SITU_OK);
				assert(situ_head_index_get(hdr) == seen);
				seen++;
				situ_frame_reader_advance(&r);
			}
		}
		if (seen != 3) {
			return 1;
		}
	}
	return 0;
}
"""


# -- rung 5: matching a reply to its request (26.97) -------------------------


def test_the_table_is_the_callers_and_allocates_nothing() -> None:
	"""The schema states the pairing; the generated code holds the set."""
	schema, resolved = analysed(GOOD)

	header = converse.generate(schema, resolved, "t")["t_converse.h"]

	assert "situ_conv_response_to_slot_t *slots, uint32_t cap)" in header
	assert "malloc" not in header


def test_a_full_table_refuses_rather_than_evicting() -> None:
	"""Evicting would drop an exchange the caller still wanted, silently."""
	schema, resolved = analysed(GOOD)

	header = converse.generate(schema, resolved, "t")["t_converse.h"]

	assert "return SITU_ERR_BOUNDS;" in header


def test_matching_forgets_so_a_duplicate_reply_is_refused() -> None:
	schema, resolved = analysed(GOOD)

	header = converse.generate(schema, resolved, "t")["t_converse.h"]

	assert "table->slots[i].live = 0u;" in header
	assert "return SITU_ERR_CONSTRAINT;" in header


def test_a_key_wider_than_a_packed_word_is_refused() -> None:
	"""The case with a wrong answer and no symptom.

	Two exchanges whose keys collided after truncation would be matched to
	each other, and nothing at run time would say so. Refused with the sum,
	as `relate` refuses a comparison that has no correct spelling.
	"""
	schema = parse_text(
		"target buffer;\nendian big;\n\n"
		"struct wf {\n\tu64 a;\n\tu64 b;\n\tu16 small;\n}\n\n"
		"relation too_wide(p: wf, q: wf) {\n\tmust q.a == p.a;\n"
		"\tmust q.b == p.b;\n}\n\n"
		"relation fits(p: wf, q: wf) {\n\tmust q.small == p.small;\n}\n")
	resolved = resolve(schema, solve(schema))

	names = dict(converse.refusals(schema, resolved))

	assert "128 bits" in names["too_wide"]
	assert "matched to each other" in names["too_wide"]
	# And one bad relation does not take the others with it.
	assert "fits" not in names
	assert "situ_conv_fits_record" in converse.generate(schema, resolved, "t")[
		"t_converse.h"]


def test_a_relation_with_no_equality_gets_no_table() -> None:
	schema, resolved = analysed(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.index < a.hdr.chunks;\n}\n")

	names = dict(converse.refusals(schema, resolved))

	assert "nothing identifies an exchange" in names["r"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_table_matches_refuses_and_fills(tmp_path: Path) -> None:
	"""Every acceptance criterion of 26.97, run rather than read."""
	schema, resolved = analysed(GOOD)

	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in converse.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "drive.c").write_text(_CONVERSE_DRIVER, encoding="ascii")

	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "drive.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "drive")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr


_CONVERSE_DRIVER = """#include <assert.h>
#include <string.h>
#include "t_converse.h"

static situ_view_t frame_of(uint8_t *buf, situ_msg_t *m, uint16_t msg)
{
	situ_view_t v;
	memset(buf, 0, 17);
	buf[0] = (uint8_t)(msg >> 8);
	buf[1] = (uint8_t)(msg & 0xffu);
	buf[3] = 4;
	situ_msg_init(m, buf, 17);
	assert(situ_frame_view(m, 0u, &v) == SITU_OK);
	return v;
}

int main(void)
{
	uint8_t a[17], b[17], c[17];
	situ_msg_t ma, mb, mc;
	situ_conv_response_to_slot_t slots[2];
	situ_conv_response_to_t table;
	uint32_t id = 0;

	situ_conv_response_to_init(&table, slots, 2u);

	situ_view_t req = frame_of(a, &ma, 0x1234u);
	assert(situ_conv_response_to_record(&table, req, 77u) == SITU_OK);

	situ_view_t rep = frame_of(b, &mb, 0x1234u);
	assert(situ_conv_response_to_match(&table, rep, &id) == SITU_OK);
	assert(id == 77u);

	/* A second reply names an exchange that is over. */
	assert(situ_conv_response_to_match(&table, rep, &id) == SITU_ERR_CONSTRAINT);

	situ_view_t other = frame_of(c, &mc, 0x9999u);
	assert(situ_conv_response_to_match(&table, other, &id) == SITU_ERR_CONSTRAINT);

	assert(situ_conv_response_to_record(&table, req, 1u) == SITU_OK);
	assert(situ_conv_response_to_record(&table, other, 2u) == SITU_OK);
	assert(situ_conv_response_to_record(&table, req, 3u) == SITU_ERR_BOUNDS);
	return 0;
}
"""


# -- rung 6's contract, in the schema (26.98) --------------------------------


def _policy(relation: ast.Relation) -> list[tuple[str, int]]:
	"""The stated policy, narrowed: an attribute's value is optional in the
	AST because a bare flag has none, and these never are."""
	found = []
	for attr in relation.attrs:
		assert isinstance(attr.value, ast.IntLiteral)
		found.append((attr.name, attr.value.value))
	return found


POLICY = """relation response_to(request: frame, response: frame)
		[timeout_ms = 800, retries = 3] {
	must response.hdr.msg == request.hdr.msg;
}
"""


def test_an_exchange_states_its_own_timing() -> None:
	"""On the relation, because the relation already identifies the exchange
	-- its equality constraints are the conversation key -- and because both
	endpoints must agree on the policy, which is what makes it schema rather
	than a command-line flag (0032)."""
	relation = checked(POLICY).relations()[0]

	assert _policy(relation) == [("timeout_ms", 800), ("retries", 3)]


def test_stating_no_policy_is_fine() -> None:
	"""A schema that says nothing gets no scheduler, which is the non-goal
	working rather than a gap."""
	assert checked(GOOD).relations()[0].attrs == ()


def test_half_a_policy_is_refused() -> None:
	"""The dangerous shape. `retries = 3` with no timeout says retransmit
	three times and never says when, so a generator would have to invent an
	interval -- and a timeout situ chose rather than the protocol is
	behaviour the schema did not state."""
	rendered = refused(
		"relation r(a: frame, b: frame) [retries = 3] {\n"
		"\tmust b.hdr.msg == a.hdr.msg;\n}\n")

	assert "half a retransmission policy" in rendered
	assert "situ supplies no default here" in rendered


def test_a_zero_in_the_policy_is_refused() -> None:
	rendered = refused(
		"relation r(a: frame, b: frame) [timeout_ms = 0, retries = 3] {\n"
		"\tmust b.hdr.msg == a.hdr.msg;\n}\n")

	assert "expected a positive number" in rendered


def test_the_policy_survives_a_round_trip() -> None:
	"""Both `dump` and `unparse` dropped it at first, so the round-trip
	compared two schemas that had each lost the same thing and passed. A
	check that cannot fail is worse than none -- this asserts the policy is
	*there* afterwards, not merely that the two halves agree.
	"""
	schema = checked(POLICY)

	again = parse_text(unparse.unparse(schema))

	assert _policy(again.relations()[0]) == [("timeout_ms", 800), ("retries", 3)]
	assert "[timeout_ms = 800, retries = 3]" in unparse.unparse(schema)


# -- rung 6: retransmission, and the clock it does not own (26.98) -----------


def driven_pair(body: str) -> tuple[ast.Schema, ResolvedSchema]:
	return analysed(body)


def test_no_policy_generates_no_scheduler() -> None:
	"""Section 2's non-goal, working. A schema that says nothing about timing
	gets nothing, rather than a default situ chose."""
	schema, resolved = analysed(GOOD)

	assert drive.generate(schema, resolved, "t") == {}


def test_a_stated_policy_reaches_the_generated_code() -> None:
	schema, resolved = analysed(POLICY)

	header = drive.generate(schema, resolved, "t")["t_drive.h"]

	assert "#define SITU_RESPONSE_TO_TIMEOUT_MS 800u" in header
	assert "#define SITU_RESPONSE_TO_RETRIES 3u" in header


def test_the_driver_never_reads_a_clock() -> None:
	"""Time is a parameter at every entry point, which is what makes a
	timeout bug reproduce instead of race."""
	schema, resolved = analysed(POLICY)

	header = drive.generate(schema, resolved, "t")["t_drive.h"]

	for banned in ("time(", "clock_gettime", "gettimeofday", "sleep"):
		assert banned not in header, f"the state machine reached for {banned}"
	assert "uint32_t now_ms" in header


def test_step_returns_the_next_deadline() -> None:
	"""0033's core requirement: every multiplexing facility takes a timeout,
	and only the state machine knows when it next needs waking. Without this
	each driver invents a polling interval and the timing contract stops
	being the schema's."""
	schema, resolved = analysed(POLICY)

	header = drive.generate(schema, resolved, "t")["t_drive.h"]

	assert "uint32_t *next_ms," in header
	assert "return SITU_ERR_TRUNCATED;" in header


def test_io_is_a_vtable_the_caller_fills() -> None:
	"""The shipped path and the tested path are the same program: they differ
	in what fills this struct and in nothing else."""
	schema, resolved = analysed(POLICY)

	header = drive.generate(schema, resolved, "t")["t_drive.h"]

	assert "situ_err_t (*submit)(void *ctx, const uint8_t *data, uint32_t len);" \
		in header
	assert "malloc" not in header


def test_the_deployment_overrides_the_value_and_not_the_shape() -> None:
	"""`init` takes both numbers, so a satellite link and a LAN run one
	protocol -- and a relation stating no policy still generates nothing to
	pass them to."""
	schema, resolved = analysed(POLICY)

	header = drive.generate(schema, resolved, "t")["t_drive.h"]

	assert "uint32_t timeout_ms," in header
	assert "uint32_t retries)" in header


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_ten_simulated_minutes_reproduce_exactly(tmp_path: Path) -> None:
	"""26.98's acceptance criterion, run.

	Loss, reorder and duplication over ten simulated minutes, twenty-one
	times, with no sleep anywhere -- and the same answer every time. A wall
	clock inside the state machine would make this a race; a parameter makes
	it a function.
	"""
	schema, resolved = analysed(POLICY)

	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for emitted in (converse.generate(schema, resolved, "t"),
	                drive.generate(schema, resolved, "t")):
		for name, text in emitted.items():
			(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "scen.c").write_text(_SCENARIO, encoding="ascii")

	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "scen.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "scen")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "scen")], capture_output=True,
	                     text=True, timeout=120)
	assert ran.returncode == 0, ran.stdout + ran.stderr


_SCENARIO = """#include <assert.h>
#include <string.h>
#include "t_drive.h"

typedef struct { uint32_t sent; uint64_t digest; } wire_t;

static situ_err_t wire_submit(void *ctx, const uint8_t *data, uint32_t len)
{
	wire_t *w = (wire_t *)ctx;
	w->sent++;
	for (uint32_t i = 0; i < len; i++) {
		w->digest = w->digest * 1099511628211u ^ data[i];
	}
	return SITU_OK;
}

static uint32_t rng(uint32_t *s)
{
	*s ^= *s << 13; *s ^= *s >> 17; *s ^= *s << 5;
	return *s;
}

static uint64_t run_once(void)
{
	enum { N = 4 };
	uint8_t bufs[N][17];
	situ_msg_t msgs[N];
	situ_view_t views[N];
	situ_drive_response_to_slot_t slots[N];
	situ_conv_response_to_slot_t keys[N];
	situ_drive_response_to_t drive;
	wire_t wire = {0, 1469598103934665603u};
	situ_io_t io = { wire_submit, &wire };
	uint32_t seed = 12345u, expired_total = 0u, matched = 0u;

	situ_drive_response_to_init(&drive, slots, keys, N, io, 100u, 2u);

	for (unsigned i = 0; i < N; i++) {
		memset(bufs[i], 0, 17);
		bufs[i][0] = 0x10; bufs[i][1] = (uint8_t)i; bufs[i][3] = 4;
		situ_msg_init(&msgs[i], bufs[i], 17);
		assert(situ_frame_view(&msgs[i], 0u, &views[i]) == SITU_OK);
		assert(situ_drive_response_to_send(&drive, views[i], bufs[i], 17u,
		                                   i, 0u) == SITU_OK);
	}

	for (uint32_t now = 0; now <= 600000u; now += 10u) {
		uint32_t next = 0u, expired = 0u, id = 0u;
		uint32_t roll = rng(&seed) % 100u;

		if (roll < 8u) {
			unsigned which = rng(&seed) % N;
			if (situ_drive_response_to_on_message(&drive, views[which], &id)
					== SITU_OK) {
				matched++;
			}
		}
		if (roll >= 8u && roll < 10u) {
			unsigned which = rng(&seed) % N;
			(void)situ_drive_response_to_on_message(&drive, views[which], &id);
		}

		if (situ_drive_response_to_step(&drive, now, &next, &expired)
				== SITU_ERR_TRUNCATED) {
			expired_total += expired;
			break;
		}
		expired_total += expired;
	}

	return wire.digest ^ ((uint64_t)wire.sent << 32)
	     ^ ((uint64_t)matched << 16) ^ expired_total;
}

int main(void)
{
	const uint64_t first = run_once();
	for (int i = 0; i < 20; i++) {
		if (run_once() != first) {
			return 1;
		}
	}
	return 0;
}
"""


# -- rung 4 in the other three backends (26.96) ------------------------------
#
# Unlike `relate`, these are not one walk in four spellings. The runtimes
# differ in shape: Rust answers a `Framing` enum rather than an error code,
# C++ acquires a view through an `rt::message` the reader has to own, and
# Python imposes no capacity because a `bytearray` grows. Each test below
# pins the difference rather than asserting they are alike.


def test_rust_matches_a_framing_enum_rather_than_an_error() -> None:
	"""`required` answers `Complete(n)` or `Need(n)` there, so there is no
	error to propagate and two variants to match. A reader translated from
	the C one would have invented a failure that does not exist."""
	schema, resolved = analysed(GOOD)

	module = frame_rs.generate(schema, resolved, "t")["t_frame.rs"]

	assert "Framing::Complete(n) => Ok(n)," in module
	assert "Framing::Need(n) if n > self.buf.len() => Err(Error::Bounds)," in module


def test_rust_lets_the_borrow_checker_write_the_lifetime_comment() -> None:
	"""C has to warn in capitals that the view dies at `advance`. Here
	`next` borrows the reader, so doing it early does not compile."""
	schema, resolved = analysed(GOOD)

	module = frame_rs.generate(schema, resolved, "t")["t_frame.rs"]

	assert "pub fn next(&self) -> Result<Frame<'_>>" in module
	assert "pub fn advance(&mut self)" in module


def test_cpp_owns_the_message_it_acquires_views_from() -> None:
	"""`at` takes an `rt::message`, not a raw pointer, so the reader holds
	one over its own buffer."""
	schema, resolved = analysed(GOOD)

	header = frame_cpp.generate(schema, resolved, "t")["t_frame.hpp"]

	assert "::situ::rt::message owner_;" in header
	assert "::situ::frame::at(owner_, 0u, out)" in header


def test_python_imposes_no_capacity() -> None:
	"""A `bytearray` grows, so a caller buffer would be a limit the language
	does not need -- the same reasoning 26.30 gives for `--materialize`
	needing a `max` in three backends and not the fourth."""
	schema, resolved = analysed(GOOD)

	module = frame_py.generate(schema, resolved, "t")["t_frame.py"]

	assert "self._buf   = bytearray()" in module
	assert "cap" not in module


#: A struct a stream cannot be framed into: where one ends is decided by a
#: quoted delimiter and a `remaining` tail, so no prefix of a stream says a
#: whole one has arrived.
UNFRAMEABLE = """struct row {
	u8 field[] until "," [quoted = "\\""];
	u8 rest[remaining];
}
"""


def test_a_struct_that_cannot_be_framed_gets_no_reader() -> None:
	"""`framed_structs` and `frameable` used to disagree, and the reader was
	built out of a `_required` only a frameable struct has -- so the header
	called a function nobody declared. It compiled nowhere and was caught
	nowhere, because until the checks suite included this header nothing in
	the repository ever compiled it. Four structs in `tests/schemas/edges.situ`
	were in that state."""
	schema, resolved = analysed(UNFRAMEABLE)

	framed = [struct.name for struct in frame.framed_structs(resolved)]
	assert "row" not in framed, "a struct no prefix can delimit got a reader"
	assert "frame" in framed, "the frameable structs must still get one"

	header = frame.generate(schema, resolved, "t")["t_frame.h"]
	assert "situ_row_required" not in header


def test_every_backend_frames_the_same_structs() -> None:
	"""The set is a property of the schema, so it cannot differ per language
	even though the readers do."""
	schema, resolved = analysed(GOOD)

	assert (set(frame.generate(schema, resolved, "t")) == {"t_frame.h"}
	        and set(frame_cpp.generate(schema, resolved, "t")) == {"t_frame.hpp"}
	        and set(frame_rs.generate(schema, resolved, "t")) == {"t_frame.rs"}
	        and set(frame_py.generate(schema, resolved, "t")) == {"t_frame.py"})


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_cpp_reader_compiles_under_the_same_warnings(
		tmp_path: Path) -> None:
	schema, resolved = analysed(GOOD)

	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in frame_cpp.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "tu.cpp").write_text(
		'#include "t_frame.hpp"\nint main() { return 0; }\n', encoding="ascii")

	assert HOST_CXX is not None
	built = subprocess.run(
		[HOST_CXX, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Wconversion",
		 "-Wsign-conversion", "-Werror", f"-I{tmp_path}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 "-fsyntax-only", str(tmp_path / "tu.cpp")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_rust_reader_compiles_under_denied_warnings(
		tmp_path: Path) -> None:
	schema, resolved = analysed(GOOD)

	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding="ascii")
	for name, text in frame_rs.generate(schema, resolved, "t").items():
		(src / name).write_text(text, encoding="ascii")
	(src / "lib.rs").write_text(
		"pub mod situ_rt;\npub mod t;\npub mod t_frame;\n", encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", "--crate-type", "lib",
		 str(src / "lib.rs"), "-o", str(tmp_path / "out.rlib")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


def test_the_python_reader_reassembles_across_every_boundary(
		tmp_path: Path) -> None:
	"""Run, not read. A framing reader that only works when a message
	arrives whole is one that works on a loopback and nowhere else."""
	schema, resolved = analysed(GOOD)

	for name, text in generate_py(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in frame_py.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")
	(tmp_path / "run.py").write_text(_PY_CHUNKS, encoding="ascii")

	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output=True, text=True, cwd=tmp_path)
	assert ran.returncode == 0, ran.stdout + ran.stderr


_PY_CHUNKS = """import sys

sys.path.insert(0, ".")

from t_frame import frame_reader
from situ_runtime import TruncatedError


def one(n):
	return bytes([0x12, 0x30 + n, n, 4]) + bytes(13)


stream = b"".join(one(i) for i in range(3))

for chunk in range(1, len(stream) + 1):
	reader = frame_reader()
	seen = []
	for at in range(0, len(stream), chunk):
		reader.push(stream[at:at + chunk])
		while True:
			try:
				view = reader.next()
			except TruncatedError:
				break
			seen.append(view.hdr.index)
			reader.advance()
	assert seen == [0, 1, 2], f"chunk {chunk}: {seen}"
"""


# -- rung 5 in the other three backends (26.97) ------------------------------


def test_rust_calls_the_taking_half_take_because_match_is_a_keyword() -> None:
	"""And it is the better name: `Option::take` already establishes that it
	leaves nothing behind, which is why a duplicate reply is refused."""
	schema, resolved = analysed(GOOD)

	module = converse_rs.generate(schema, resolved, "t")["t_converse.rs"]

	assert "pub fn take(&mut self, response: &Frame) -> Result<u32>" in module
	assert "fn match" not in module


def test_the_rust_table_borrows_nothing_from_a_message() -> None:
	"""A `u64` key and a `u32` handle, so ownership has no opinion. Had it
	kept the request's bytes, a caller-supplied slice would have had to
	outlive every call and the design would have changed rather than been
	translated."""
	schema, resolved = analysed(GOOD)

	module = converse_rs.generate(schema, resolved, "t")["t_converse.rs"]

	assert "\tkey: u64," in module
	assert "\tid: u32," in module
	assert "slots: &'a mut [ResponseToSlot]," in module


def test_cpp_types_both_halves_of_the_exchange() -> None:
	"""So they cannot be passed the wrong way round. C cannot: every view
	there is a `situ_view_t`."""
	schema, resolved = analysed(GOOD)

	header = converse_cpp.generate(schema, resolved, "t")["t_converse.hpp"]

	assert "record(const ::situ::frame &request," in header
	assert "take(const ::situ::frame &response," in header


def test_python_requires_a_capacity_unlike_its_framing_reader() -> None:
	"""The departure worth arguing. A reader's size is about representation
	and a `bytearray` grows; a pending table's bound is about refusing
	somebody who opens exchanges and never answers them, which is the same
	problem in every language.
	"""
	schema, resolved = analysed(GOOD)

	table  = converse_py.generate(schema, resolved, "t")["t_converse.py"]
	reader = frame_py.generate(schema, resolved, "t")["t_frame.py"]

	assert "def __init__(self, cap: int) -> None:" in table
	assert "raise BoundsError(" in table
	assert "cap" not in reader


def test_every_backend_refuses_the_same_relations() -> None:
	"""The key analysis is `situc.relation`'s, so what has no correct
	spelling has none in any of them."""
	schema, resolved = analysed(
		"relation r(a: frame, b: frame) {\n"
		"\tmust b.hdr.wide == a.hdr.wide;\n"
		"\tmust b.hdr.msg == a.hdr.msg;\n}\n")

	assert (dict(converse.refusals(schema, resolved))
	        == dict(converse_cpp.refusals(schema, resolved))
	        == dict(converse_rs.refusals(schema, resolved))
	        == dict(converse_py.refusals(schema, resolved)))


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_cpp_table_compiles_under_the_same_warnings(
		tmp_path: Path) -> None:
	schema, resolved = analysed(GOOD)

	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in converse_cpp.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "tu.cpp").write_text(
		'#include "t_converse.hpp"\nint main() { return 0; }\n',
		encoding="ascii")

	assert HOST_CXX is not None
	built = subprocess.run(
		[HOST_CXX, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Wconversion",
		 "-Wsign-conversion", "-Werror", f"-I{tmp_path}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 "-fsyntax-only", str(tmp_path / "tu.cpp")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_rust_table_compiles_under_denied_warnings(
		tmp_path: Path) -> None:
	schema, resolved = analysed(GOOD)

	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding="ascii")
	for name, text in converse_rs.generate(schema, resolved, "t").items():
		(src / name).write_text(text, encoding="ascii")
	(src / "lib.rs").write_text(
		"pub mod situ_rt;\npub mod t;\npub mod t_converse;\n", encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", "--crate-type", "lib",
		 str(src / "lib.rs"), "-o", str(tmp_path / "out.rlib")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


def test_the_python_table_matches_refuses_and_fills(tmp_path: Path) -> None:
	"""Every acceptance criterion of 26.97, run in the fourth backend."""
	schema, resolved = analysed(GOOD)

	for name, text in generate_py(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in converse_py.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")
	(tmp_path / "run.py").write_text(_PY_TABLE, encoding="ascii")

	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output=True, text=True, cwd=tmp_path)
	assert ran.returncode == 0, ran.stdout + ran.stderr


_PY_TABLE = """import sys

sys.path.insert(0, ".")

from t import frame
from t_converse import response_to_table
from situ_runtime import BoundsError, ConstraintError, Message


def view(msg):
	raw = bytearray(msg.to_bytes(2, "big") + bytes([0, 4]) + bytes(13))
	return frame.at(Message(raw), 0)


table = response_to_table(2)
table.record(view(0x1234), 77)
assert table.take(view(0x1234)) == 77

for bad in (0x1234, 0x9999):
	try:
		table.take(view(bad))
		raise SystemExit("a stale reply was matched")
	except ConstraintError:
		pass

table.record(view(1), 1)
table.record(view(2), 2)
try:
	table.record(view(3), 3)
	raise SystemExit("a full table accepted a third")
except BoundsError:
	pass
"""


# -- rung 6 in the other three backends (26.98) ------------------------------


def test_the_rust_slot_borrows_the_bytes_it_will_resend() -> None:
	"""The design fork this rung forced.

	C keeps a `const uint8_t *` the caller promises not to move. Rust will
	not take a promise, so the slot borrows -- and the driver carries two
	lifetimes, `&'s mut [Slot<'a>]`, because a single one is invariant in
	`'a` and unusable at almost every call site.
	"""
	schema, resolved = analysed(POLICY)

	module = drive_rs.generate(schema, resolved, "t")["t_drive.rs"]

	assert "pub struct ResponseToSlot<'a> {" in module
	assert "\tbytes: &'a [u8]," in module
	assert "slots: &'s mut [ResponseToSlot<'a>]," in module


def test_the_rust_driver_submits_after_walking_its_own_table() -> None:
	"""`self.io` and `self.slots` cannot both be borrowed mutably, which is
	the borrow checker noticing that C reenters its own table through a
	callback and gets away with it."""
	schema, resolved = analysed(POLICY)

	module = drive_rs.generate(schema, resolved, "t")["t_drive.rs"]

	assert "for bytes in due.iter().take(n).flatten() {" in module


def test_io_is_the_shape_each_language_already_has() -> None:
	"""A struct of function pointers is C's way of saying "the caller
	supplies this". Emitting one into the other three would be C leaking."""
	schema, resolved = analysed(POLICY)

	assert "pub trait Io {" in drive_rs.generate(schema, resolved, "t")["t_drive.rs"]
	assert "virtual ::situ::rt::err submit(" in \
		drive_cpp.generate(schema, resolved, "t")["t_drive.hpp"]
	assert "submit: Callable[[bytes], None]" in \
		drive_py.generate(schema, resolved, "t")["t_drive.py"]


def test_no_backend_reads_a_clock() -> None:
	schema, resolved = analysed(POLICY)

	for text in (drive.generate(schema, resolved, "t")["t_drive.h"],
	             drive_cpp.generate(schema, resolved, "t")["t_drive.hpp"],
	             drive_rs.generate(schema, resolved, "t")["t_drive.rs"],
	             drive_py.generate(schema, resolved, "t")["t_drive.py"]):
		for banned in ("time(", "clock_gettime", "Instant::now", "time.time",
		               "sleep"):
			assert banned not in text, f"a driver reached for {banned}"


def test_python_returns_the_expiry_count_even_when_it_empties() -> None:
	"""Found by running it. `step` returned None where nothing was left,
	which lost the count on the very call that gave up on the last exchange
	-- and C writes it through a pointer alongside SITU_ERR_TRUNCATED, so the
	two backends were telling a caller different things.
	"""
	schema, resolved = analysed(POLICY)

	module = drive_py.generate(schema, resolved, "t")["t_drive.py"]

	assert "def step(self, now_ms: int) -> tuple[int | None, int]:" in module
	assert "return (soonest, expired)" in module


def test_no_policy_generates_no_driver_in_any_backend() -> None:
	schema, resolved = analysed(GOOD)

	assert drive.generate(schema, resolved, "t") == {}
	assert drive_cpp.generate(schema, resolved, "t") == {}
	assert drive_rs.generate(schema, resolved, "t") == {}
	assert drive_py.generate(schema, resolved, "t") == {}


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_cpp_driver_compiles_under_the_same_warnings(
		tmp_path: Path) -> None:
	schema, resolved = analysed(POLICY)

	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in drive_cpp.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "tu.cpp").write_text(
		'#include "t_drive.hpp"\nint main() { return 0; }\n', encoding="ascii")

	assert HOST_CXX is not None
	built = subprocess.run(
		[HOST_CXX, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Wconversion",
		 "-Wsign-conversion", "-Werror", f"-I{tmp_path}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 "-fsyntax-only", str(tmp_path / "tu.cpp")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_rust_driver_compiles_with_its_lifetimes(tmp_path: Path) -> None:
	"""The lifetimes are the point, so the check is that they resolve."""
	schema, resolved = analysed(POLICY)

	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding="ascii")
	for name, text in drive_rs.generate(schema, resolved, "t").items():
		(src / name).write_text(text, encoding="ascii")
	(src / "lib.rs").write_text(
		"pub mod situ_rt;\npub mod t;\npub mod t_drive;\n", encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", "--crate-type", "lib",
		 str(src / "lib.rs"), "-o", str(tmp_path / "out.rlib")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


# -- rung 2: the layer boundary made real (26.99) ----------------------------


CASE_E = """target buffer;
endian big;

codec squeeze {
	expansion = unbounded;
	granularity = byte;
	seekable;
	deterministic;
}
impl squeeze extern "squeeze_go";

struct packed_up {
	u16 len;
	coded body(squeeze) { u8 raw[4]; }
}
"""


def test_an_unbounded_expansion_names_the_member_that_needs_storage() -> None:
	"""A path rather than a bare name: two structs may each have a `body`
	and only one of them may need rung 2."""
	schema = parse_text(CASE_E)

	assert layers.allocating(schema) == {"packed_up.body"}
	assert layers.floor(schema) == "edit"


def test_a_schema_of_bounded_constructs_needs_nothing() -> None:
	assert layers.allocating(checked(GOOD)) == set()
	assert layers.floor(checked(GOOD)) == "view"


def test_no_alloc_is_decidable_now() -> None:
	"""Section 16 listed it among four predicates the compiler names and
	cannot decide, because generated code never allocated so it always held
	and the predicate would be a lint. The ladder gave the answer somewhere
	to be no.
	"""
	schema   = parse_text(CASE_E + "\nrequire no_alloc(packed_up.len);\n")
	resolved = resolve(schema, solve(schema))

	outcomes = requirements.discharge(schema, resolved)

	assert outcomes[0].satisfied
	assert outcomes[0].deferred is None, "it is answered, not deferred"


def test_no_alloc_fails_where_storage_is_needed() -> None:
	schema   = parse_text(CASE_E + "\nassert no_alloc(packed_up.body);\n")
	resolved = resolve(schema, solve(schema))

	outcome = requirements.discharge(schema, resolved)[0]

	assert not outcome.satisfied
	assert "expands without a bound" in outcome.detail
	assert outcome.diagnostic is not None, "a failure needs its blame chain"


def test_the_reach_rises_with_a_timing_policy() -> None:
	"""A relation reaches `relate`; one that states a retransmission policy
	reaches `drive`, because that is the rung with something to emit for it."""
	assert layers.reach(checked(GOOD)) == "relate"
	assert layers.reach(checked(POLICY)) == "drive"
	assert layers.reach(parse_text(CASE_E)) == "view"


# -- rung 2: the owned decode against caller backing (26.99, 0031 C/D) -------


VARIABLE = """target buffer;
endian big;

struct label {
	u16 id;
	u8  n;
	u8  name[n];
	u8  tail;
}
"""


def analysed_text(body: str) -> tuple[ast.Schema, ResolvedSchema]:
	schema = parse_text(body)
	return schema, resolve(schema, solve(schema))


def test_a_length_prefixed_run_gets_an_owned_form_at_rung_2() -> None:
	"""`--owned` refuses it -- "a pointer reintroduces exactly the lifetime
	the caller was escaping, and an array of the worst case is a decision
	about memory nobody asked for". Both stop being true once the backing is
	the caller's."""
	schema, resolved = analysed_text(VARIABLE)

	assert [s.name for s in edit.editable(resolved)] == ["label"]
	assert [s.name for s in owned.owned_structs(resolved)] == []


def test_the_owned_form_points_into_the_backing() -> None:
	schema, resolved = analysed_text(VARIABLE)

	header = edit.generate(schema, resolved, "t")["t_edit.h"]

	assert "const uint8_t *name;\t/* into your backing */" in header
	assert "uint32_t name_len;" in header
	assert "uint16_t id;" in header and "uint8_t tail;" in header


def test_it_reads_through_the_accessors_that_already_exist() -> None:
	"""A delimited member's span and a length-prefixed run's extent are
	questions the ordinary header answers; a second answer here would
	eventually disagree with the first."""
	schema, resolved = analysed_text(VARIABLE)

	header = edit.generate(schema, resolved, "t")["t_edit.h"]

	assert "situ_label_name_len(view)" in header
	assert "situ_label_name_ptr(view)" in header


def test_it_allocates_nothing_and_reports_too_little_backing() -> None:
	schema, resolved = analysed_text(VARIABLE)

	header = edit.generate(schema, resolved, "t")["t_edit.h"]

	assert "malloc" not in header
	assert "if (need > cap) {" in header
	assert "return SITU_ERR_BOUNDS;" in header


def test_a_shape_the_data_decides_is_still_refused() -> None:
	"""Backing answers a length. It does not make an honest owned form of a
	variant or a TLV run, which are shapes."""
	schema, resolved = analysed_text(
		"target buffer;\nendian big;\n\n"
		"struct picky {\n\tu8 kind;\n"
		"\tvariant body switch (kind) {\n"
		"\t\tcase 0: u8 a;\n\t\tcase 1: u16 b;\n"
		"\t\tdefault: error;\n\t}\n}\n")

	assert edit.editable(resolved) == []
	assert "shape the data decides" in dict(edit.refusals(resolved))["picky"]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_owned_value_outlives_the_message(tmp_path: Path) -> None:
	"""The whole trade, run: decode, scribble over the buffer, read back."""
	schema, resolved = analysed_text(VARIABLE)

	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in edit.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "drive.c").write_text(_EDIT_DRIVER, encoding="ascii")

	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "drive.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "drive")], capture_output=True,
	                     text=True)
	assert ran.returncode == 0, ran.stdout + ran.stderr


_EDIT_DRIVER = """#include <assert.h>
#include <string.h>
#include "t_edit.h"

int main(void)
{
	uint8_t backing[64];
	situ_label_edit_t owned;
	uint32_t need = 0;

	{
		uint8_t buf[32];
		situ_msg_t msg;
		memset(buf, 0, sizeof buf);
		buf[0] = 0x12;
		buf[1] = 0x34;
		buf[2] = 5;
		memcpy(buf + 3, "hello", 5);
		buf[8] = 0x7f;
		situ_msg_init(&msg, buf, 9);

		assert(situ_label_edit_backing(&msg, 9u, &need) == SITU_OK);
		assert(need == 5u);
		assert(situ_label_edit_decode(&msg, 9u, backing, sizeof backing,
		                              &owned) == SITU_OK);
		memset(buf, 0xAA, sizeof buf);
	}

	assert(owned.id == 0x1234u);
	assert(owned.n == 5u);
	assert(owned.tail == 0x7fu);
	assert(owned.name_len == 5u);
	assert(memcmp(owned.name, "hello", 5) == 0);

	{
		uint8_t buf[32];
		situ_msg_t msg;
		uint8_t tiny[2];
		memset(buf, 0, sizeof buf);
		buf[2] = 5;
		memcpy(buf + 3, "hello", 5);
		situ_msg_init(&msg, buf, 9);
		assert(situ_label_edit_decode(&msg, 9u, tiny, sizeof tiny, &owned)
		       == SITU_ERR_BOUNDS);
	}
	return 0;
}
"""


# -- rung 2 in the other three backends (26.99) ------------------------------


def test_rust_makes_the_backing_a_lifetime_not_a_pointer() -> None:
	"""The whole difference. C hands back a struct holding a pointer and asks
	the caller not to free the storage; Rust holds `&'a [u8]` and the
	compiler will not let them. `Vec` would be the ordinary Rust answer and
	is unavailable -- `situ_rt` is `no_std`, which is why a caller buffer is
	the design rather than a translation.
	"""
	schema, resolved = analysed_text(VARIABLE)

	module = edit_rs.generate(schema, resolved, "t")["t_edit.rs"]

	assert "pub struct LabelOwned<'a> {" in module
	assert "pub name: &'a [u8]," in module
	assert "pub fn decode(data: &[u8], store: &'a mut [u8]) -> Result<Self>" \
		in module


def test_cpp_uses_the_span_the_language_already_has() -> None:
	"""One `rt::const_bytes` rather than a pointer and a length, and the same
	type the ordinary accessor returns."""
	schema, resolved = analysed_text(VARIABLE)

	header = edit_cpp.generate(schema, resolved, "t")["t_edit.hpp"]

	assert "::situ::rt::const_bytes name;" in header
	assert "std::uint16_t id;" in header


def test_python_takes_no_backing_because_bytes_is_the_backing() -> None:
	"""The other three take a buffer because a variable member has to live
	somewhere the message does not. `bytes` is already that somewhere, so a
	backing parameter would be ceremony around what the language did.

	Same reasoning as 26.96's reader imposing no capacity, and the opposite
	of 26.97's table, whose bound is about refusing an attacker rather than
	about representation.
	"""
	schema, resolved = analysed_text(VARIABLE)

	module = edit_py.generate(schema, resolved, "t")["t_edit.py"]

	assert "\tname: bytes" in module
	# The word appears in the docstring explaining its absence, so the check
	# is the signature: `decode` takes the view and nothing else.
	assert "def decode(cls, view: label) -> \"label_owned\":" in module
	assert "store" not in module


def test_every_backend_offers_an_owned_form_for_the_same_structs() -> None:
	"""Which structs qualify is a property of the schema, so it cannot differ
	per language even though the forms do."""
	schema, resolved = analysed_text(VARIABLE)

	assert (set(edit.generate(schema, resolved, "t")) == {"t_edit.h"}
	        and set(edit_cpp.generate(schema, resolved, "t")) == {"t_edit.hpp"}
	        and set(edit_rs.generate(schema, resolved, "t")) == {"t_edit.rs"}
	        and set(edit_py.generate(schema, resolved, "t")) == {"t_edit.py"})


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_cpp_owned_form_compiles_under_the_same_warnings(
		tmp_path: Path) -> None:
	schema, resolved = analysed_text(VARIABLE)

	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in edit_cpp.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "tu.cpp").write_text(
		'#include "t_edit.hpp"\nint main() { return 0; }\n', encoding="ascii")

	assert HOST_CXX is not None
	built = subprocess.run(
		[HOST_CXX, "-std=c++17", "-O1", "-Wall", "-Wextra", "-Wconversion",
		 "-Wsign-conversion", "-Werror", f"-I{tmp_path}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 "-fsyntax-only", str(tmp_path / "tu.cpp")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_rust_owned_form_compiles_with_its_backing_lifetime(
		tmp_path: Path) -> None:
	schema, resolved = analysed_text(VARIABLE)

	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding="ascii")
	for name, text in edit_rs.generate(schema, resolved, "t").items():
		(src / name).write_text(text, encoding="ascii")
	(src / "lib.rs").write_text(
		"pub mod situ_rt;\npub mod t;\npub mod t_edit;\n", encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-D", "warnings", "--crate-type", "lib",
		 str(src / "lib.rs"), "-o", str(tmp_path / "out.rlib")],
		capture_output=True, text=True)

	assert built.returncode == 0, built.stderr


def test_the_python_owned_value_outlives_the_message(tmp_path: Path) -> None:
	"""The trade, run in the backend that needs no backing for it."""
	schema, resolved = analysed_text(VARIABLE)

	for name, text in generate_py(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for name, text in edit_py.generate(schema, resolved, "t").items():
		(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")
	(tmp_path / "run.py").write_text(_PY_OWNED, encoding="ascii")

	ran = subprocess.run(["python3", str(tmp_path / "run.py")],
	                     capture_output=True, text=True, cwd=tmp_path)
	assert ran.returncode == 0, ran.stdout + ran.stderr


_PY_OWNED = """import sys

sys.path.insert(0, ".")

from t import label
from t_edit import label_owned
from situ_runtime import Message

raw = bytearray(b"\\x12\\x34\\x05hello\\x7f")
owned = label_owned.decode(label.at(Message(raw), 0, len(raw)))

raw[:] = b"\\xAA" * len(raw)

assert owned.id == 0x1234
assert owned.name == b"hello"
assert owned.tail == 0x7f
"""


# -- the walker at rungs 4 and 5 (26.96, 26.97) ------------------------------


def test_the_walk_frames_a_stream_without_a_framing_section() -> None:
	"""`struct_extent` already measures one instance from its own bytes --
	that is what `access = Sequential` costs -- so a reader is that function
	plus a buffer held between calls. The image did not have to grow, unlike
	for relations."""
	image, _ = packed(GOOD)
	shape    = image.relations[0].request

	def one(n: int) -> bytes:
		return bytes([0x12, 0x30 + n, n, 4]) + bytes(13)

	stream = b"".join(one(i) for i in range(3))

	for chunk in range(1, len(stream) + 1):
		reader = session.Reader(image, shape)
		seen   = 0
		for at in range(0, len(stream), chunk):
			reader.push(stream[at:at + chunk])
			while reader.next() is not None:
				seen += 1
				reader.advance()
		assert seen == 3, f"chunk {chunk}: {seen}"


def test_next_answers_none_rather_than_refusing_on_a_short_stream() -> None:
	""""Not yet" is the ordinary answer when feeding a stream; reporting it
	as a refusal would make every caller catch the common case."""
	image, _ = packed(GOOD)
	reader   = session.Reader(image, image.relations[0].request)

	assert reader.next() is None
	reader.push(b"\x12\x34")
	assert reader.next() is None


def test_the_walk_matches_by_running_the_relation() -> None:
	"""No key, and no image section for one.

	The compiled backends pack the equality fields into a `u64` because
	comparing every pending request would be a loop in somebody's hot path.
	A walker has no hot path and does have the predicate, so it runs the
	relation -- exactly correct, and it cannot disagree with the compiled
	answer because it is the same program.
	"""
	image, _ = packed(GOOD)
	shape    = image.relations[0].request

	def view(msg: int) -> View:
		return acquire(image, bytes([msg >> 8, msg & 0xff, 0, 4]) + bytes(13),
		               shape)

	talk = session.Conversation(image, 0, cap=2)

	assert talk.record(view(0x1234), 77)
	assert talk.take(view(0x1234)) == 77
	assert talk.take(view(0x1234)) is None, "a duplicate names an exchange over"
	assert talk.take(view(0x9999)) is None, "nobody opened this one"


def test_the_walkers_table_is_bounded_like_the_compiled_ones() -> None:
	"""The bound is not about representation -- it is about refusing somebody
	who opens exchanges and never answers, which is the same problem whoever
	is walking."""
	image, _ = packed(GOOD)
	shape    = image.relations[0].request

	def view(msg: int) -> View:
		return acquire(image, bytes([msg >> 8, msg & 0xff, 0, 4]) + bytes(13),
		               shape)

	talk = session.Conversation(image, 0, cap=2)

	assert talk.record(view(1), 1)
	assert talk.record(view(2), 2)
	assert not talk.record(view(3), 3)


# -- the walker at rung 2 (26.99) --------------------------------------------


def test_the_walk_decodes_an_owned_value_from_an_image() -> None:
	"""The shape 0034's editor wants: names against values you can hold.

	No backing parameter, for the reason the Python backend takes none --
	`bytes` is already storage that outlives the message, so a parameter
	would be ceremony around what the language did.
	"""
	schema, resolved = analysed_text(VARIABLE)
	image = load(pack(schema, resolved, metadata=True)[0])

	raw  = bytearray(b"\x12\x34\x05hello\x7f")
	held = owned_walk.decode(acquire(image, raw, 0))

	assert held == {"id": 0x1234, "n": 5, "name": b"hello", "tail": 0x7f}


def test_the_owned_value_does_not_read_the_message_afterwards() -> None:
	"""Run against a live buffer, not a copy of one.

	The first version of this test handed `acquire` a `bytes(raw)` and then
	mutated `raw`, which proved nothing: the copy was made before the
	overwrite. Passing the bytearray itself is what makes the claim real.
	"""
	schema, resolved = analysed_text(VARIABLE)
	image = load(pack(schema, resolved, metadata=True)[0])

	raw  = bytearray(b"\x12\x34\x05hello\x7f")
	held = owned_walk.decode(acquire(image, raw, 0))
	raw[:] = b"\xAA" * len(raw)

	assert held["name"] == b"hello"
	assert held["id"] == 0x1234


def test_an_owned_run_is_bytes_rather_than_whatever_slicing_gave() -> None:
	"""A bytearray slice of a bytearray message is a copy and so survives,
	but it is mutable -- and an owned value a caller can edit in place is a
	different promise from the one this makes."""
	schema, resolved = analysed_text(VARIABLE)
	image = load(pack(schema, resolved, metadata=True)[0])

	held = owned_walk.decode(
		acquire(image, bytearray(b"\x12\x34\x05hello\x7f"), 0))

	assert type(held["name"]) is bytes


def test_it_keys_by_name_where_the_image_carries_them() -> None:
	"""`--metadata` is the tail 26.33 split off for a reader rather than a
	device. This is a reader."""
	schema, resolved = analysed_text(VARIABLE)
	bare = load(pack(schema, resolved, metadata=False)[0])

	held = owned_walk.decode(acquire(bare, bytearray(b"\x12\x34\x02hi\x7f"), 0))

	assert all(key.startswith("placement[") for key in held), held


# -- rungs 4 and 5, executed rather than compiled -----------------------------
#
# The checks suite runs the C reader and the C table on every `make test`. The
# other two backends were taken as far as their compilers and no further, which
# is the same gap the predicate had: `-fsyntax-only` and `--crate-type lib`
# both report success over code nobody ran.


@pytest.mark.skipif(HOST_CXX is None, reason="no C++ compiler")
def test_the_cpp_reader_and_table_answer_at_runtime(tmp_path: Path) -> None:
	"""Two whole messages a byte at a time, then a reply matched to its
	request and refused the second time. Distinct exit codes so a failure
	names which step went wrong rather than only that one did."""
	schema, resolved = analysed(KEYED)

	for name, text in generate_cpp(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for module in (relate_cpp, frame_cpp, converse_cpp):
		for name, text in module.generate(schema, resolved, "t").items():
			(tmp_path / name).write_text(text, encoding="ascii")

	(tmp_path / "main.cpp").write_text("""#include <cstdint>
#include <cstring>

#include "t_converse.hpp"
#include "t_frame.hpp"

int main()
{
	const std::uint32_t n = ::situ::frame::size_bytes;
	std::uint8_t stream[::situ::frame::size_bytes * 2u];
	std::uint8_t back[::situ::frame::size_bytes * 2u];
	std::memset(stream, 0, sizeof stream);

	::situ::frame_reader reader(back, (std::uint32_t)sizeof back);
	::situ::frame got;
	unsigned seen = 0u;

	for (std::uint32_t i = 0u; i < (std::uint32_t)sizeof stream; i++) {
		if (reader.push(&stream[i], 1u) != ::situ::rt::err::ok) return 1;
		while (seen <= 2u && reader.next(got) == ::situ::rt::err::ok) {
			seen++;
			reader.advance();
		}
	}
	if (seen != 2u) return 2;

	std::uint8_t q[::situ::frame::size_bytes];
	std::uint8_t r[::situ::frame::size_bytes];
	std::memset(q, 0, sizeof q);
	std::memset(r, 0, sizeof r);

	::situ::rt::message owner_q(q, n);
	::situ::rt::message owner_r(r, n);
	::situ::frame view_q;
	::situ::frame view_r;
	if (::situ::frame::at(owner_q, 0, view_q) != ::situ::rt::err::ok) return 3;
	if (::situ::frame::at(owner_r, 0, view_r) != ::situ::rt::err::ok) return 4;

	::situ::keyed_to_table::slot slots[2];
	::situ::keyed_to_table table(slots, 2u);
	std::uint32_t id = 0u;

	if (table.record(view_q, 7u) != ::situ::rt::err::ok) return 5;
	if (table.take(view_r, id)  != ::situ::rt::err::ok) return 6;
	if (id != 7u) return 7;
	if (table.take(view_r, id)  == ::situ::rt::err::ok) return 8;

	return 0;
}
""", encoding="ascii")

	assert HOST_CXX is not None
	built = subprocess.run(
		[HOST_CXX, "-std=c++17", "-O1", f"-I{tmp_path}",
		 f"-I{ROOT / 'runtime' / 'cpp'}", f"-I{RUNTIME}",
		 str(tmp_path / "main.cpp"), str(RUNTIME / "situ.c"),
		 "-o", str(tmp_path / "run")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True, text=True)
	assert ran.returncode == 0, f"the C++ rungs answered wrongly at step {ran.returncode}"


@pytest.mark.skipif(RUSTC is None, reason="no rustc")
def test_the_rust_reader_and_table_answer_at_runtime(tmp_path: Path) -> None:
	"""The Rust half of the case above."""
	schema, resolved = analysed(KEYED)

	src = tmp_path / "src"
	src.mkdir()
	(src / "situ_rt.rs").write_text(
		(ROOT / "runtime" / "rust" / "situ_rt.rs")
		.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(src / "t.rs").write_text(
		generate_rs(schema, resolved, "t").module, encoding="ascii")
	for module in (relate_rs, frame_rs, converse_rs):
		for name, text in module.generate(schema, resolved, "t").items():
			(src / name).write_text(text, encoding="ascii")

	(src / "main.rs").write_text("""pub mod situ_rt;
pub mod t;
pub mod t_converse;
pub mod t_frame;
pub mod t_relate;

fn main()
{
	const N: usize = t::Frame::SIZE;

	let stream = [0u8; N * 2];
	let mut back = [0u8; N * 2];
	let mut reader = t_frame::FrameReader::new(&mut back);
	let mut seen = 0u32;

	for i in 0..stream.len() {
		reader.push(&stream[i..i + 1]).expect("a byte the buffer has room for");
		while seen <= 2 && reader.next().is_ok() {
			seen += 1;
			reader.advance().expect("a message the reader just handed out");
		}
	}
	assert_eq!(seen, 2, "a two-message stream did not yield two messages");

	let zeroed = [0u8; N];
	let query = t::Frame::new(&zeroed).expect("a view over zeroed bytes");
	let reply = t::Frame::new(&zeroed).expect("a view over zeroed bytes");

	let mut slots = [t_converse::KeyedToSlot::default(); 2];
	let mut table = t_converse::KeyedToTable::new(&mut slots);

	table.record(&query, 7).expect("a slot to record into");
	assert_eq!(table.take(&reply).expect("the request just recorded"), 7);
	assert!(table.take(&reply).is_err(), "matching did not forget");
}
""", encoding="ascii")

	assert RUSTC is not None
	built = subprocess.run(
		[RUSTC, "--edition", "2021", "-A", "warnings",
		 str(src / "main.rs"), "-o", str(tmp_path / "run")],
		capture_output=True, text=True, cwd=tmp_path)
	assert built.returncode == 0, built.stderr

	ran = subprocess.run([str(tmp_path / "run")], capture_output=True, text=True)
	assert ran.returncode == 0, ran.stderr
