"""Cross-message relations in C: rung 3 of the layer ladder (26.95).

A relation is a pure predicate over two views. It holds no state, allocates
nothing, and does not know which messages exist -- the caller owns the
pairing, and this answers only whether a pairing is well formed. Decision 0030
is the construct, 0032 places it on the ladder.

WHAT THIS EMITS, AND WHY IT IS A SEPARATE FILE. A `<name>_relate.h` and
`<name>_relate.c` beside the ordinary output, for the reason `owned.py` gives
for the same choice: a rung is additive, and 0032 requires that `--layer
relate` emit every file `--layer view` emits, byte-identical, plus new ones. A
predicate folded into the ordinary header would change that header and break
the invariant while still appearing to hold.

HOW IT COMPARES. Through the generated getters, never against the bytes. That
is what makes the comparison one of *values*: the getter already byte-swaps,
scales a fixed-point field and decodes a BCD one, so a big-endian `u16`
against a little-endian `u32` compares correctly with nobody thinking about
it. Reconstructing offsets here would be a second answer to a question
`traverse` already answers, and the two would eventually disagree.

WHY EVERY OPERAND IS WIDENED. Generated C is compiled with `-Wconversion
-Wsign-conversion -Werror`, so an `i8` field compared against a `u16` one is
a build failure rather than a warning. Each operand is therefore cast to one
64-bit type chosen per constraint, which makes every comparison same-typed by
construction. The one case that cannot be made safe -- a 64-bit unsigned
against anything signed, where no 64-bit type holds both ranges -- is refused
rather than papered over with a cast that changes the answer.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import ident
from situc.layout import Placement
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import is_own_member, local_name
from situc.types import ScalarKind

#: Operators a relation body may use, spelled the same in C as in situ.
#:
#: Comparison, arithmetic, bitwise and logical. Deliberately a closed set: an
#: operator that reached the output unchecked because both languages happen to
#: spell it alike is a silent difference waiting for the first one that does
#: not.
OPERATORS = frozenset({
	"==", "!=", "<", "<=", ">", ">=",
	"+", "-", "*", "/", "%",
	"&", "|", "^", "<<", ">>",
	"&&", "||",
})

UNARY = frozenset({"-", "~", "!"})


class Refused(Exception):
	"""This relation cannot be emitted, and the message says why.

	Raised rather than returned because a refusal can surface from anywhere in
	the walk, and every caller above wants the same thing: drop this one
	relation, keep the rest, and tell the operator.
	"""


def _paths_in(expr: ast.Expr) -> list[str]:
	"""Every dotted path the expression names, in order, with duplicates.

	`situc.invariant.paths_in` answers the same question and is not reused: it
	folds a `Call`'s arguments into the caller's list, which is right for an
	invariant -- where a call is `size(x)` and `x` is the thing named -- and
	wrong here, where a call is refused outright and its arguments must not be
	silently promoted into paths that look reachable.
	"""
	if isinstance(expr, ast.Access):
		base = _paths_in(expr.base)
		return [f"{base[0]}.{expr.name}"] if base else [expr.name]
	if isinstance(expr, ast.NameRef):
		return [expr.name]
	if isinstance(expr, ast.Binary):
		return _paths_in(expr.left) + _paths_in(expr.right)
	if isinstance(expr, ast.Unary):
		return _paths_in(expr.operand)
	return []


def _member(struct: ResolvedStruct, name: str) -> Placement | None:
	"""One of the struct's own members, by its local name."""
	for entry in struct.entries:
		placement = entry.placement
		if (is_own_member(struct, placement)
				and local_name(struct, placement) == name):
			return placement
	return None


class _Chain:
	"""One path resolved to the accessors that reach it.

	`sub` is the sub-view acquisitions to perform in order, each a (struct,
	member) pair; `leaf` is the scalar the getter finally reads.
	"""

	def __init__(self, sub: list[tuple[str, str]], leaf: tuple[str, str],
			scalar_signed: bool, scalar_bits: int) -> None:
		self.sub    = sub
		self.leaf   = leaf
		self.signed = scalar_signed
		self.bits   = scalar_bits


def _resolve(path: str, param: ast.RelationParam,
		resolved: ResolvedSchema) -> _Chain:
	"""Walk `request.hdr.msg` down to the getter that reads it."""
	components = path.split(".")[1:]
	struct     = resolved.structs.get(param.type_name)
	if struct is None:
		raise Refused(f"`{param.type_name}` has no resolved layout")

	sub: list[tuple[str, str]] = []

	for index, component in enumerate(components):
		placement = _member(struct, component)
		if placement is None:
			# wellformed proved the name exists on the *declaration*; a
			# placement absent here means the solver dropped it, which is a
			# different fact and worth a different sentence.
			raise Refused(f"`{struct.name}.{component}` has no placement")

		last = index == len(components) - 1

		if not last:
			nested = resolved.structs.get(placement.type_name or "")
			if nested is None:
				raise Refused(f"`{path}` reaches through `{component}`, which "
				              f"is not a struct")
			sub.append((struct.name, component))
			struct = nested
			continue

		scalar = placement.scalar
		if scalar is None:
			raise Refused(f"`{path}` names `{placement.kind}`, which has no "
			              f"single value to compare")
		if placement.array_count is not None:
			raise Refused(f"`{path}` is an array, and a relation compares "
			              f"one value against another")
		if scalar.kind is ScalarKind.FLOAT:
			raise Refused(f"`{path}` is floating point, and an exact "
			              f"comparison of one is rarely what a wire contract "
			              f"means")

		return _Chain(sub, (struct.name, component), scalar.signed, scalar.bits)

	raise Refused(f"`{path}` names no member")


def _common_type(chains: list[_Chain], where: str) -> str:
	"""One 64-bit type every operand of a constraint is cast to.

	Signed wins wherever it can, because every unsigned width below 64 fits in
	`int64_t` without changing a value. What it cannot cover is a `u64`
	alongside anything signed: no 64-bit type holds both ranges, so the
	comparison has no correct C spelling and is refused instead of being
	given a wrong one.
	"""
	signed   = [chain for chain in chains if chain.signed]
	unsigned = [chain for chain in chains if not chain.signed]

	if not signed:
		return "uint64_t"
	if any(chain.bits >= 64 for chain in unsigned):
		raise Refused(f"{where} compares a 64-bit unsigned value against a "
		              f"signed one, and no 64-bit C type holds both ranges")
	return "int64_t"


def _expression(expr: ast.Expr, locals_for: dict[str, str], where: str) -> str:
	"""The constraint in C, with each path replaced by the local holding it."""
	if isinstance(expr, ast.IntLiteral):
		return str(expr.value)

	if isinstance(expr, (ast.Access, ast.NameRef)):
		path = _paths_in(expr)[0]
		return locals_for[path]

	if isinstance(expr, ast.Binary):
		if expr.op not in OPERATORS:
			raise Refused(f"{where} uses `{expr.op}`, which a relation may not")
		left  = _expression(expr.left, locals_for, where)
		right = _expression(expr.right, locals_for, where)
		return f"({left} {expr.op} {right})"

	if isinstance(expr, ast.Unary):
		if expr.op not in UNARY:
			raise Refused(f"{where} uses unary `{expr.op}`, which a relation "
			              f"may not")
		return f"({expr.op}{_expression(expr.operand, locals_for, where)})"

	if isinstance(expr, ast.Call):
		raise Refused(f"{where} calls `{expr.name}`; a relation compares "
		              f"values a getter returns, and asks the layout nothing")

	raise Refused(f"{where} holds an expression a relation cannot emit")


def _body(relation: ast.Relation, resolved: ResolvedSchema,
		prefix: str) -> list[str]:
	"""The function body: bind what each constraint reads, then check it."""
	params = {param.name: param for param in relation.params}
	lines: list[str] = []
	index = 0

	for number, must in enumerate(relation.body, start=1):
		where  = f"constraint {number} of `{relation.name}`"
		paths  = list(dict.fromkeys(_paths_in(must.expr)))
		chains = {path: _resolve(path, params[path.split(".")[0]], resolved)
		          for path in paths}
		cast   = _common_type(list(chains.values()), where)

		locals_for: dict[str, str] = {}
		block: list[str] = []

		for path in paths:
			chain = chains[path]
			view  = path.split(".")[0]

			for struct_name, member in chain.sub:
				sub_local = f"situ_sub{index}"
				index += 1
				accessor = ident(prefix, struct_name, member, "view")
				block += [
					f"\tsitu_view_t {sub_local};\t/* {view}.{member} */",
					f"\terr = {accessor}({view}, &{sub_local});",
					"\tif (err != SITU_OK) {",
					"\t\treturn err;",
					"\t}",
				]
				view = sub_local

			value  = f"situ_val{index}"
			index += 1
			getter = ident(prefix, chain.leaf[0], chain.leaf[1], "get")
			block.append(f"\t{cast} {value} = ({cast}){getter}({view});"
			             f"\t/* {path} */")
			locals_for[path] = value

		check = _expression(must.expr, locals_for, where)
		block += [
			f"\tif (!{check}) {{",
			"\t\treturn SITU_ERR_CONSTRAINT;",
			"\t}",
		]

		lines += ["\t{", *[f"\t{line}" for line in block], "\t}", ""]

	return lines


def signature(relation: ast.Relation, prefix: str) -> str:
	params = ", ".join(f"situ_view_t {param.name}" for param in relation.params)
	return f"situ_err_t {ident(prefix, 'rel', relation.name)}({params})"


def _comment(relation: ast.Relation) -> list[str]:
	first, second = relation.params
	return [
		f"/** Whether `{second.name}` is a well-formed counterpart to "
		f"`{first.name}`.",
		" *",
		" * Pure: reads two views, holds nothing, allocates nothing, and does",
		" * not know which messages exist. The caller owns the pairing.",
		" *",
		f" * Parameter order is temporal -- `{first.name}` is the message seen",
		" * first, which is what lets a dissector say \"response to frame N\".",
		" *",
		" * SITU_OK              every constraint holds",
		" * SITU_ERR_CONSTRAINT  one did not",
		" * SITU_ERR_BOUNDS      a sub-view did not fit the view it was in",
		" */",
	]


def refusals(schema: ast.Schema, resolved: ResolvedSchema,
		prefix: str = "situ") -> list[tuple[str, str]]:
	"""Every relation that gets no predicate, and why.

	Reported rather than silently skipped, for the reason `owned.refusals`
	gives: a caller who asked for this and found their relation missing would
	conclude the generator was broken, which is worse than being told the
	comparison has no correct spelling.
	"""
	found = []
	for relation in schema.relations():
		try:
			_body(relation, resolved, prefix)
		except Refused as why:
			found.append((relation.name, str(why)))
	return found


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The relation header and source, or nothing if no relation qualifies."""
	emitted: list[tuple[ast.Relation, list[str]]] = []
	for relation in schema.relations():
		try:
			emitted.append((relation, _body(relation, resolved, prefix)))
		except Refused:
			continue

	if not emitted:
		return {}

	guard  = ident(prefix, basename, "RELATE_H").upper()
	header = [
		f"/* Generated by situc from {basename}.situ -- do not edit.",
		" *",
		" * Cross-message relations: a pure predicate per relation, over two",
		" * views the caller already holds. No state, no allocation, and no",
		" * knowledge of which messages exist.",
		" *",
		" * A separate file on purpose. This is rung 3 of the layer ladder and",
		" * a rung adds files rather than changing them, so the ordinary",
		" * header is byte-identical whether or not this one was asked for.",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include \"situ.h\"",
		f"#include \"{basename}.h\"",
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
	]

	for relation, _ in emitted:
		header += [*_comment(relation), f"{signature(relation, prefix)};", ""]

	header += [
		"#ifdef __cplusplus",
		"}",
		"#endif",
		"",
		f"#endif /* {guard} */",
	]

	source = [
		f"/* Generated by situc from {basename}.situ -- do not edit. */",
		"",
		f"#include \"{basename}_relate.h\"",
		"",
	]

	for relation, body in emitted:
		source += [
			f"{signature(relation, prefix)}",
			"{",
			"\tsitu_err_t err = SITU_OK;",
			"\t(void)err;",
			"",
			*body,
			"\treturn SITU_OK;",
			"}",
			"",
		]

	return {
		f"{basename}_relate.h": "\n".join(header) + "\n",
		f"{basename}_relate.c": "\n".join(source).rstrip("\n") + "\n",
	}
