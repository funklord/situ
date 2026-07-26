"""Recursive-descent parser producing a well-formed AST.

Scope is the phase 1 static subset (project.md section 26.1). Every construct
belonging to a later phase is recognised by keyword and rejected with a
diagnostic naming the phase that will accept it, so a user meeting one knows
whether to wait or to rewrite the schema.

Hand-written rather than generated: no dependency (decision 0001), and the
diagnostics are the product.
"""

from __future__ import annotations

from situc import ast, wellformed
from situc.diagnostics import Source, Span, error, not_yet_implemented
from situc.lexer import Token, TokenKind, tokenize
from situc.types import WidthError, lookup

# Constructs recognised but not yet accepted, mapped to the phase that adds
# them (project.md section 26). Keyed by the keyword that introduces one.
FUTURE_CONSTRUCTS = {
	"variant":		(6,  "`variant`"),
	"opaque":		(6,  "`opaque`"),
	"tlv":			(6,  "`tlv`"),
	"indexed":		(6,  "`indexed`"),
	"varint_type":		(6,  "`varint_type`"),
	"codec":		(7,  "`codec`"),
	"impl":			(7,  "`impl`"),
	"authenticated":	(8,  "`authenticated`"),
	"sealed":		(8,  "`sealed`"),
	"tag":			(8,  "`tag`"),
	"checksum":		(8,  "`checksum`"),
	"register":		(10, "`register`"),
	"register_block":	(10, "`register_block`"),
}

# The attribute vocabulary is closed and fixed by the language, which is what
# makes `[` decidable: see disambiguate_bracket and
# docs/decisions/0006-bracket-disambiguation.md. Names for later phases are
# listed now so the disambiguation does not change meaning as phases land.
ATTRIBUTE_NAMES = frozenset({
	# section 8.2 - 8.6: layout and representation
	"allow_straddle", "require_aligned", "encoding", "nul_terminated",
	"endian", "bit_order", "size",
	# section 8.3: host-dependent byte order
	"allow_host_dependent",
	# section 8.8: reserved behaviour
	"must_be_zero", "must_be_one", "preserve", "unknown",
	# section 5.1 and 18.2: constraints
	"must_eq", "max", "min",
	# section 17.0: variant arm padding
	"equalize",
	# section 14: cryptographic model
	"secret", "nonce", "covers", "allow_unverified_read", "trusted",
	# section 15.2: register access modes and side effects (phase 10)
	"rw", "ro", "wo", "w1c", "w0c", "w1s", "w0s", "rc", "rs", "wo_once", "rsvd",
	"on_read", "on_write", "volatile", "no_rmw",
})

TARGET_KINDS	= {kind.value: kind for kind in ast.TargetKind}
ENDIANS		= {endian.value: endian for endian in ast.Endian}
BIT_ORDERS	= {order.value: order for order in ast.BitOrder}
ENUM_DEFAULTS	= {default.value: default for default in ast.EnumDefault}

# Binary operator precedence, loosest first. Section 10 permits arithmetic and
# bitwise operators everywhere, comparison and boolean operators in constraints
# only; that restriction is a later pass's to enforce, not the parser's.
PRECEDENCE: tuple[tuple[str, ...], ...] = (
	("||",),
	("&&",),
	("|",),
	("^",),
	("&",),
	("==", "!="),
	("<", ">", "<=", ">="),
	("<<", ">>"),
	("+", "-"),
	("*", "/", "%"),
)

UNARY_OPS = ("-", "~", "!")


class Parser:
	def __init__(self, source: Source) -> None:
		self.source = source
		self.tokens = tokenize(source)
		self.pos    = 0

	# -- token access ---------------------------------------------------

	@property
	def current(self) -> Token:
		return self.tokens[self.pos]

	def peek(self, ahead: int = 1) -> Token:
		return self.tokens[min(self.pos + ahead, len(self.tokens) - 1)]

	def advance(self) -> Token:
		token = self.tokens[self.pos]
		if token.kind is not TokenKind.EOF:
			self.pos += 1
		return token

	def accept_symbol(self, *symbols: str) -> Token | None:
		if self.current.is_symbol(*symbols):
			return self.advance()
		return None

	def accept_ident(self, *names: str) -> Token | None:
		if self.current.is_ident(*names):
			return self.advance()
		return None

	def expect_symbol(self, symbol: str, context: str) -> Token:
		token = self.accept_symbol(symbol)
		if token is None:
			raise error(
				f"expected `{symbol}` {context}, found {self.current.describe()}",
				self.current.span,
				label = f"expected `{symbol}` here",
			)
		return token

	def expect_ident(self, context: str) -> Token:
		if self.current.kind is not TokenKind.IDENT:
			raise error(
				f"expected {context}, found {self.current.describe()}",
				self.current.span,
				label = f"expected {context} here",
			)
		return self.advance()

	def expect_keyword(self, name: str, context: str) -> Token:
		token = self.accept_ident(name)
		if token is None:
			raise error(
				f"expected `{name}` {context}, found {self.current.describe()}",
				self.current.span,
				label = f"expected `{name}` here",
			)
		return token

	def span_from(self, start: Token) -> Span:
		return Span(self.source, start.span.start, self.tokens[self.pos - 1].span.end)

	# -- schema ---------------------------------------------------------

	def parse(self) -> ast.Schema:
		schema = ast.Schema(Span(self.source, 0, len(self.source.text)))

		while self.current.kind is not TokenKind.EOF:
			schema.decls.append(self.parse_decl())

		wellformed.check(schema)
		return schema

	def parse_decl(self) -> ast.Decl:
		token = self.current

		if token.kind is not TokenKind.IDENT:
			raise error(
				f"expected a declaration, found {token.describe()}",
				token.span,
				label = "expected `struct`, `enum`, `const`, a directive or a requirement",
			)

		future = FUTURE_CONSTRUCTS.get(token.text)
		if future is not None:
			phase, described = future
			raise not_yet_implemented(described, token.span, phase)

		handlers = {
			"target":	self.parse_target,
			"endian":	self.parse_endian,
			"bit_order":	self.parse_bit_order,
			"import":	self.parse_import,
			"const":	self.parse_const,
			"enum":		self.parse_enum,
			"struct":	self.parse_struct,
			"endian_marker": self.parse_endian_marker,
			"require":	self.parse_requirement,
			"assert":	self.parse_requirement,
		}

		handler = handlers.get(token.text)
		if handler is None:
			raise error(
				f"unknown declaration `{token.text}`",
				token.span,
				label = "not a declaration keyword",
				notes = ["expected `target`, `endian`, `bit_order`, `import`, `const`, "
				         "`enum`, `struct`, `require` or `assert`"],
			)

		return handler()

	# -- directives -----------------------------------------------------

	def parse_target(self) -> ast.TargetDirective:
		start = self.advance()
		token = self.expect_ident("a target kind")
		kind  = TARGET_KINDS.get(token.text)
		if kind is None:
			raise error(
				f"unknown target `{token.text}`",
				token.span,
				label = "expected `buffer` or `mmio`",
			)
		self.expect_symbol(";", "after the target directive")
		return ast.TargetDirective(self.span_from(start), kind)

	def parse_endian(self) -> ast.EndianDirective:
		start  = self.advance()
		token  = self.expect_ident("an endianness")
		endian = ENDIANS.get(token.text)
		if endian is None:
			raise error(
				f"unknown endianness `{token.text}`",
				token.span,
				label = "expected `big`, `little` or `native`",
			)
		self.expect_symbol(";", "after the endian directive")
		return ast.EndianDirective(self.span_from(start), endian)

	def parse_bit_order(self) -> ast.BitOrderDirective:
		start = self.advance()
		token = self.expect_ident("a bit order")
		order = BIT_ORDERS.get(token.text)
		if order is None:
			raise error(
				f"unknown bit order `{token.text}`",
				token.span,
				label = "expected `msb_first` or `lsb_first`",
			)
		self.expect_symbol(";", "after the bit_order directive")
		return ast.BitOrderDirective(self.span_from(start), order)

	def parse_import(self) -> ast.ImportDirective:
		start = self.advance()
		if self.current.kind is not TokenKind.STRING:
			raise error(
				f"expected a quoted path, found {self.current.describe()}",
				self.current.span,
				label = "expected a string literal here",
			)
		path = self.advance()
		self.expect_symbol(";", "after the import directive")
		return ast.ImportDirective(self.span_from(start), path.text)

	# -- declarations ---------------------------------------------------

	def parse_const(self) -> ast.ConstDecl:
		start = self.advance()
		name  = self.expect_ident("a constant name")
		self.expect_symbol("=", "after the constant name")
		value = self.parse_expr()
		self.expect_symbol(";", "after the constant value")
		return ast.ConstDecl(self.span_from(start), name.text, value)

	def parse_enum(self) -> ast.EnumDecl:
		start = self.advance()
		name  = self.expect_ident("an enum name")
		self.expect_symbol(":", "after the enum name")

		backing = self.parse_type_ref()
		if not backing.is_scalar:
			raise error(
				f"enum backing type must be a scalar, found `{backing.name}`",
				backing.span,
				label = "not a scalar type",
				notes = ["the backing type is mandatory and fixes the enum's width "
				         "(project.md section 8.7)"],
			)

		self.expect_symbol("{", "to open the enum body")

		members: list[ast.EnumMember] = []
		default: ast.EnumDefault | None = None

		while not self.current.is_symbol("}"):
			if self.current.is_ident("default") and self.peek().is_symbol("="):
				default = self.parse_enum_default()
			else:
				members.append(self.parse_enum_member())

			if self.accept_symbol(",") is None:
				break

		self.expect_symbol("}", "to close the enum body")
		return ast.EnumDecl(self.span_from(start), name.text, backing,
		                    tuple(members), default)

	def parse_enum_member(self) -> ast.EnumMember:
		name = self.expect_ident("an enum member name")
		self.expect_symbol("=", "after the enum member name")
		value = self.parse_expr()
		return ast.EnumMember(self.span_from(name), name.text, value)

	def parse_enum_default(self) -> ast.EnumDefault:
		self.advance()
		self.advance()
		token   = self.expect_ident("`error` or `pass`")
		default = ENUM_DEFAULTS.get(token.text)
		if default is None:
			raise error(
				f"unknown enum default `{token.text}`",
				token.span,
				label = "expected `error` or `pass`",
				notes = ["`default = pass` accepts unknown values and sets "
				         "canonical = NonCanonical (project.md section 8.7)"],
			)
		return default

	def parse_endian_marker(self) -> ast.EndianMarkerDecl:
		"""`endian_marker byte_order : u16 { little = 0x4949, big = 0x4D4D, }`

		Exactly two members, named for the orders they select. Naming them
		anything else would leave the compiler guessing which is which, and a
		wrong guess here is undetectable at runtime.
		"""
		start = self.advance()
		name  = self.expect_ident("a marker name")
		self.expect_symbol(":", "after the marker name")

		backing = self.parse_type_ref()
		if not backing.is_scalar or backing.scalar is None:
			raise error(
				f"marker backing type must be a scalar, found `{backing.name}`",
				backing.span,
				label = "not a scalar type",
			)
		if backing.scalar.is_bit_packed:
			raise error(
				f"marker backing type `{backing.name}` is not a whole number of bytes",
				backing.span,
				label = "must be byte-sized",
				notes = ["the marker is read before its own byte order is known, "
				         "so it has to be a plain byte sequence"],
			)

		self.expect_symbol("{", "to open the marker body")

		seen: dict[str, ast.Expr] = {}
		while not self.current.is_symbol("}"):
			member = self.expect_ident("`little` or `big`")
			if member.text not in ("little", "big"):
				raise error(
					f"unknown marker member `{member.text}`",
					member.span,
					label = "expected `little` or `big`",
				)
			if member.text in seen:
				raise error(f"`{member.text}` is declared twice", member.span)

			self.expect_symbol("=", "after the marker member name")
			seen[member.text] = self.parse_expr()

			if self.accept_symbol(",") is None:
				break

		self.expect_symbol("}", "to close the marker body")

		missing = sorted({"little", "big"} - set(seen))
		if missing:
			raise error(
				f"marker `{name.text}` does not declare {' or '.join(missing)}",
				self.span_from(start),
				label = "both orders must be given",
				notes = ["a marker that names only one order cannot select the other"],
			)

		return ast.EndianMarkerDecl(self.span_from(start), name.text, backing,
		                            seen["little"], seen["big"])

	def parse_marker_field(self) -> ast.MarkerField:
		start = self.advance()
		name  = self.expect_ident("the marker's name")
		attrs = self.parse_attrs()
		self.expect_symbol(";", "after the marker field")
		return ast.MarkerField(self.span_from(start), name.text, attrs)

	def parse_struct(self) -> ast.StructDecl:
		start = self.advance()
		name  = self.expect_ident("a struct name")
		attrs = self.parse_attrs()
		self.expect_symbol("{", "to open the struct body")

		members = self.parse_members()

		self.expect_symbol("}", "to close the struct body")
		return ast.StructDecl(self.span_from(start), name.text, members, attrs)

	def parse_members(self) -> tuple[ast.Member, ...]:
		members: list[ast.Member] = []
		while not self.current.is_symbol("}"):
			if self.current.kind is TokenKind.EOF:
				raise error(
					"unexpected end of file inside a struct body",
					self.current.span,
					label = "expected `}`",
				)
			members.append(self.parse_member())
		return tuple(members)

	def parse_member(self) -> ast.Member:
		token = self.current

		if token.kind is TokenKind.IDENT:
			future = FUTURE_CONSTRUCTS.get(token.text)
			if future is not None:
				phase, described = future
				raise not_yet_implemented(described, token.span, phase)

			if token.text == "endian_marker":
				return self.parse_marker_field()
			if token.text == "reserved":
				return self.parse_reserved()
			if token.text == "positional":
				return self.parse_positional()

		return self.parse_field()

	def parse_positional(self) -> ast.PositionalBlock:
		start = self.advance()
		self.expect_symbol("{", "to open the positional block")
		members = self.parse_members()
		self.expect_symbol("}", "to close the positional block")
		return ast.PositionalBlock(self.span_from(start), members)

	def parse_reserved(self) -> ast.Reserved:
		start    = self.advance()
		type_ref = self.parse_type_ref()
		if not type_ref.is_scalar:
			raise error(
				f"`reserved` needs a scalar type, found `{type_ref.name}`",
				type_ref.span,
				label = "not a scalar type",
			)

		array = self.parse_array_spec()
		attrs = self.parse_attrs()
		self.expect_symbol(";", "after the reserved declaration")
		return ast.Reserved(self.span_from(start), type_ref, array, attrs)

	def parse_field(self) -> ast.Field:
		start    = self.current
		type_ref = self.parse_type_ref()
		name     = self.expect_ident("a field name")
		array    = self.parse_array_spec()
		pin      = self.parse_pin()
		attrs    = self.parse_attrs()
		self.expect_symbol(";", "after the field declaration")
		return ast.Field(self.span_from(start), name.text, type_ref, array, pin, attrs)

	def parse_type_ref(self) -> ast.TypeRef:
		token = self.expect_ident("a type name")
		try:
			scalar = lookup(token.text)
		except WidthError as exc:
			raise error(
				str(exc),
				token.span,
				label = "invalid scalar width",
				notes = ["integer widths run from 1 to 64 "
				         "(docs/decisions/0005-integer-widths.md)"],
			) from exc

		return ast.TypeRef(token.span, token.text, scalar)

	def bracket_is_attrs(self) -> bool:
		"""Decide whether the `[` at the cursor opens attributes or an array.

		Both constructs open with `[` (project.md section 7), so the choice has
		to be made by looking inside. See
		docs/decisions/0006-bracket-disambiguation.md.
		"""
		if not self.current.is_symbol("["):
			return False

		depth  = 0
		cursor = self.pos
		inner: list[Token] = []

		while cursor < len(self.tokens):
			token = self.tokens[cursor]
			if token.is_symbol("[", "("):
				depth += 1
			elif token.is_symbol("]", ")"):
				depth -= 1
				if depth == 0:
					break
			elif depth == 1:
				# `=` and `,` cannot occur at the top level of a size
				# expression: `==` is a separate token and an array has
				# exactly one size.
				if token.is_symbol("=", ","):
					return True
				inner.append(token)
			cursor += 1

		return len(inner) == 1 and inner[0].is_ident(*ATTRIBUTE_NAMES)

	def parse_array_spec(self) -> ast.ArraySpec | None:
		if self.bracket_is_attrs():
			return None

		start = self.accept_symbol("[")
		if start is None:
			return None

		size: ast.Expr | None = None
		if not self.current.is_symbol("]"):
			size = self.parse_expr()

		self.expect_symbol("]", "to close the array size")
		return ast.ArraySpec(self.span_from(start), size)

	def parse_pin(self) -> ast.Expr | None:
		if self.accept_symbol("@") is None:
			return None
		return self.parse_expr()

	def parse_attrs(self) -> tuple[ast.Attr, ...]:
		if self.accept_symbol("[") is None:
			return ()

		attrs: list[ast.Attr] = []
		while not self.current.is_symbol("]"):
			attrs.append(self.parse_attr())
			if self.accept_symbol(",") is None:
				break

		self.expect_symbol("]", "to close the attribute list")
		return tuple(attrs)

	def parse_attr(self) -> ast.Attr:
		name  = self.expect_ident("an attribute name")
		value = self.parse_expr() if self.accept_symbol("=") else None
		return ast.Attr(self.span_from(name), name.text, value)

	def parse_requirement(self) -> ast.Requirement:
		start = self.advance()
		kind  = (ast.RequirementKind.REQUIRE if start.text == "require"
		         else ast.RequirementKind.ASSERT)
		expr = self.parse_expr()
		self.expect_symbol(";", "after the requirement")
		return ast.Requirement(self.span_from(start), kind, expr)

	# -- expressions ----------------------------------------------------

	def parse_expr(self, level: int = 0) -> ast.Expr:
		if level >= len(PRECEDENCE):
			return self.parse_unary()

		left = self.parse_expr(level + 1)
		while self.current.is_symbol(*PRECEDENCE[level]):
			op    = self.advance()
			right = self.parse_expr(level + 1)
			left  = ast.Binary(left.span.to(right.span), op.text, left, right)

		return left

	def parse_unary(self) -> ast.Expr:
		op = self.accept_symbol(*UNARY_OPS)
		if op is None:
			return self.parse_postfix()

		operand = self.parse_unary()
		return ast.Unary(Span(self.source, op.span.start, operand.span.end),
		                 op.text, operand)

	def parse_postfix(self) -> ast.Expr:
		expr = self.parse_primary()

		while True:
			if self.accept_symbol("."):
				name = self.expect_ident("a member name after `.`")
				expr = ast.Access(Span(self.source, expr.span.start, name.span.end),
				                  expr, name.text)
			elif self.current.is_symbol("["):
				expr = self.parse_index(expr)
			else:
				return expr

	def parse_index(self, base: ast.Expr) -> ast.Expr:
		self.advance()
		index = None if self.current.is_symbol("]") else self.parse_expr()
		close = self.expect_symbol("]", "to close the index")
		return ast.Index(Span(self.source, base.span.start, close.span.end), base, index)

	def parse_primary(self) -> ast.Expr:
		token = self.current

		if token.kind is TokenKind.INT:
			self.advance()
			return ast.IntLiteral(token.span, token.value, token.text)

		if token.kind is TokenKind.STRING:
			self.advance()
			return ast.StringLiteral(token.span, token.text)

		if self.accept_symbol("(") is not None:
			inner = self.parse_expr()
			self.expect_symbol(")", "to close the parenthesised expression")
			return inner

		if token.kind is TokenKind.IDENT:
			self.advance()
			if token.text == "remaining":
				return ast.Remaining(token.span)
			if self.current.is_symbol("("):
				return self.parse_call(token)
			return ast.NameRef(token.span, token.text)

		raise error(
			f"expected an expression, found {token.describe()}",
			token.span,
			label = "expected an expression here",
		)

	def parse_call(self, name: Token) -> ast.Expr:
		self.expect_symbol("(", "to open the argument list")

		args: list[ast.Expr] = []
		while not self.current.is_symbol(")"):
			args.append(self.parse_expr())
			if self.accept_symbol(",") is None:
				break

		close = self.expect_symbol(")", "to close the argument list")
		return ast.Call(Span(self.source, name.span.start, close.span.end),
		                name.text, tuple(args))


def parse(source: Source) -> ast.Schema:
	return Parser(source).parse()


def parse_text(text: str, path: str = "<input>") -> ast.Schema:
	return parse(Source(path, text))
