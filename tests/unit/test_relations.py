"""The `relation` construct: rung 3 of the layer ladder (26.95).

Decision 0030 defines it -- a pure predicate over two views, holding no state
and allocating nothing -- and 0032 places it. This file covers the front end:
what parses, what is refused, and the three passes that had to learn a new
declaration exists.

Every refusal here is name-level. Whether the two sides of a comparison are
comparable is a resolved-layout question and is deliberately not asked yet.
"""

from __future__ import annotations

import pytest

from situc import ast, dump, namespaces, unparse, wellformed
from situc.diagnostics import SituError
from situc.parser import parse_text

HEAD = """target buffer;
endian big;

struct head {
	u16 msg;
	u8  index;
	u8  chunks;
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
