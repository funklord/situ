"""Recursive-descent parser producing a well-formed AST.

Scope is the phase 1 static subset (project.md section 26.1). Every construct
belonging to a later phase is recognised by keyword and rejected with a
diagnostic naming the phase that will accept it, so a user meeting one knows
whether to wait or to rewrite the schema.

Hand-written rather than generated: no dependency (decision 0001), and the
diagnostics are the product.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from collections.abc import Callable
from typing import TypeVar

from situc import ast, kernels, namespaces, wellformed
from situc.diagnostics import Source, Span, error, not_yet_implemented
from situc.lexer import Token, TokenKind, tokenize
from situc.types import WidthError, lookup


EnumT = TypeVar("EnumT", bound=Enum)


@dataclass
class _CodecProperties:
	"""Accumulator for a codec body.

	Every default is the conservative one: a signature that says nothing claims
	nothing, which is the only safe reading of silence in a declaration the
	compiler cannot verify.
	"""

	expansion: ast.Expansion	= ast.Expansion.PRESERVING
	expansion_add: int		= 0
	ratio: tuple[int, int] | None	= None
	seekable: ast.Seekable		= ast.Seekable.NONE
	granularity: ast.Granularity	= ast.Granularity.STREAM
	granularity_size: int | None	= None
	systematic: bool		= False
	authenticated: bool		= False
	invertible: bool		= False
	deterministic: bool		= False
	error_propagating: bool		= False
	has_kernel: bool		= False
	kernel: ast.Kernel | None	= None
	pipeline: tuple[str, ...]	= ()

	def build(self, span: Span, name: str) -> ast.CodecDecl:
		return ast.CodecDecl(
			span              = span,
			name              = name,
			expansion         = self.expansion,
			expansion_add     = self.expansion_add,
			ratio             = self.ratio,
			seekable          = self.seekable,
			granularity       = self.granularity,
			granularity_size  = self.granularity_size,
			systematic        = self.systematic,
			authenticated     = self.authenticated,
			invertible        = self.invertible,
			deterministic     = self.deterministic,
			error_propagating = self.error_propagating,
			has_kernel        = self.has_kernel,
			kernel            = self.kernel,
			pipeline          = self.pipeline,
		)


def evaluate_literal(expr: ast.Expr) -> int | None:
	"""An integer literal, or None. Used where a property must be a constant
	the parser can see, rather than an expression a later pass folds."""
	return expr.value if isinstance(expr, ast.IntLiteral) else None

# Constructs recognised but not yet accepted, mapped to the phase that adds
# them (project.md section 26). Keyed by the keyword that introduces one.
FUTURE_CONSTRUCTS: dict[str, tuple[int, str]] = {}

# The settings a register or a register block may declare, from section 15.2.
REGISTER_SETTINGS = frozenset({"width", "access_width", "volatile", "no_rmw"})


@dataclass
class _RegisterDefaults:
	"""Scoped defaults, which a `register_block` sets once for what it holds."""

	width: int | None        = None
	access_width: int | None = None
	volatile: bool           = True		# implicit under `target mmio` (15.1)
	no_rmw: bool             = False

	def copy(self) -> _RegisterDefaults:
		return _RegisterDefaults(self.width, self.access_width,
		                        self.volatile, self.no_rmw)

# The attribute vocabulary is closed and fixed by the language, which is what
# makes `[` decidable: see disambiguate_bracket and
# docs/decisions/0006-bracket-disambiguation.md. Names for later phases are
# listed now so the disambiguation does not change meaning as phases land.
ATTRIBUTE_NAMES = frozenset({
	# section 8.2 - 8.6: layout and representation
	"allow_straddle", "require_aligned", "encoding", "nul_terminated",
	"endian", "bit_order", "size",
	# section 8.6.1: how a delimiter is made inert inside the content
	"quoted", "escape",
	# section 8.6.2: one spelling per value for a text number
	"minimal",
	# section 8.6.4: what a delimited value ignores
	"trim", "case_insensitive",
	# section 19.4: one file, more than one version of the protocol
	"since", "version",
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

#: Section 8.6.2. The base a text-encoded number is written in. Two, because
#: these are the two that appear in the protocols this targets; a third would
#: need a reason beyond being arithmetically possible.
RADIX_KEYWORDS = {"decimal": 10, "hex": 16}

KERNEL_FAMILIES	= {family.value: family for family in ast.KernelFamily}
TARGET_KINDS	= {kind.value: kind for kind in ast.TargetKind}
STRICTNESS	= {level.value: level for level in ast.Strictness}
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
		# Set by `codec X extern { ... }`, which implies its own binding. The
		# implied impl is appended after the declaration so both spellings of a
		# binding reach the same place.
		self._pending_extern: tuple[str, Span] | None = None
		self._dispatch_cases: tuple[int, ...] = ()
		# A `register_block` lowers to several declarations; the rest are
		# handed back one at a time here.
		self._pending_block: list[ast.Decl] = []

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

		while self.current.kind is not TokenKind.EOF or self._pending_block:
			if self._pending_block:
				schema.decls.append(self._pending_block.pop(0))
				continue
			schema.decls.append(self.parse_decl())
			if self._pending_extern is not None:
				name, span = self._pending_extern
				schema.decls.append(
					ast.ImplDecl(span, name, ast.ImplKind.EXTERN, None))
				self._pending_extern = None

		namespaces.flatten(schema)
		kernels.resolve_signatures(schema)
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

		if token.text == "register_block":
			# The one declaration that lowers to several, so it cannot go in
			# the table below.
			self._pending_block = self.parse_register_block()
			return self._pending_block.pop(0)

		handlers: dict[str, Callable[[], ast.Decl]] = {
			"namespace":	self.parse_namespace,
			"register":	self.parse_register,
			"target":	self.parse_target,
			"endian":	self.parse_endian,
			"bit_order":	self.parse_bit_order,
			"strictness":	self.parse_strictness,
			"import":	self.parse_import,
			"const":	self.parse_const,
			"enum":		self.parse_enum,
			"struct":	self.parse_struct,
			"endian_marker": self.parse_endian_marker,
			"varint_type":	self.parse_varint,
			"codec":	self.parse_codec,
			"impl":		self.parse_impl,
			"require":	self.parse_requirement,
			"assert":	self.parse_requirement,
			"invariant":	self.parse_invariant,
		}

		handler = handlers.get(token.text)
		if handler is None:
			raise error(
				f"unknown declaration `{token.text}`",
				token.span,
				label = "not a declaration keyword",
				notes = ["expected `target`, `endian`, `bit_order`, `import`, `const`, "
				         "`enum`, `struct`, `require`, `assert` or `invariant`"],
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

	def parse_register(self, defaults: _RegisterDefaults | None = None
			) -> ast.StructDecl:
		"""`register ctrl @ 0x00 { width = 32; ... bit enable [rw]; }`

		Lowered straight to a `StructDecl` carrying a `RegisterInfo`. A register
		*is* a struct -- a fixed-width container of fields at an offset -- so
		the layout solver, the lattice and the capability map all work on one
		unchanged, and only the backend needs to know it is emitting bus
		transactions (section 15.1).
		"""
		start   = self.advance()
		name    = self.expect_ident("a register name")
		address = None

		if self.accept_symbol("@") is not None:
			token   = self.current
			address = evaluate_literal(self.parse_expr())
			if address is None or address < 0:
				raise error("a register address must be a literal", token.span,
				            label="expected a number such as `0x00`")

		self.expect_symbol("{", "to open the register body")

		settings = _RegisterDefaults() if defaults is None else defaults.copy()
		members: list[ast.Member] = []

		while not self.current.is_symbol("}"):
			if self.current.kind is TokenKind.EOF:
				raise error("unexpected end of file inside a register",
				            self.current.span, label="expected `}`")
			if self._is_register_setting():
				self.parse_register_setting(settings)
				continue
			members.append(self.parse_member())

		self.expect_symbol("}", "to close the register body")
		span = self.span_from(start)

		if settings.width is None:
			raise error(f"register `{name.text}` declares no `width`", span,
			            label="expected `width = N;` in the body",
			            notes=["a register's width is the size of the object on "
			                   "the bus, and nothing else implies it"])
		if settings.access_width is None:
			raise error(f"register `{name.text}` declares no `access_width`", span,
			            label="expected `access_width = N;` in the body",
			            notes=["`target mmio` makes it mandatory (section 15.1): "
			                   "it decides whether a narrow field can be written "
			                   "without touching its neighbours",
			                   f"`access_width = {settings.width};` forbids partial "
			                   "access, which is the common case"])

		return ast.StructDecl(
			span     = span,
			name     = name.text,
			members  = tuple(members),
			register = ast.RegisterInfo(
				address      = address,
				width        = settings.width,
				access_width = settings.access_width,
				volatile     = settings.volatile,
				no_rmw       = settings.no_rmw,
			),
		)

	def parse_register_block(self) -> list[ast.Decl]:
		"""`register_block dma { width = 32; register a @ 0 { ... } }`

		Scoped defaults and nothing else (section 15.2): the block declares
		`width`, `access_width` and the flags once, and the registers inside
		inherit them. It is not a namespace and not a layout -- it disappears
		here, leaving the registers it contained.
		"""
		start = self.advance()
		self.expect_ident("a register block name")
		self.expect_symbol("{", "to open the register block")

		settings = _RegisterDefaults()
		found: list[ast.Decl] = []

		while not self.current.is_symbol("}"):
			if self.current.kind is TokenKind.EOF:
				raise error("unexpected end of file inside a register block",
				            self.current.span, label="expected `}`")
			if self._is_register_setting():
				self.parse_register_setting(settings)
			elif self.current.is_ident("register"):
				found.append(self.parse_register(settings))
			else:
				raise error(
					f"expected a register or a default, found "
					f"{self.current.describe()}",
					self.current.span,
					label = "a register block holds registers and scoped defaults",
					notes = ["`width`, `access_width`, `volatile` and `no_rmw` may "
					         "be declared once for every register in the block "
					         "(project.md section 15.2)"],
				)

		self.expect_symbol("}", "to close the register block")

		if not found:
			raise error("a register block declares no registers",
			            self.span_from(start), label="nothing to scope")
		return found

	def _is_register_setting(self) -> bool:
		return (self.current.kind is TokenKind.IDENT
		        and self.current.text in REGISTER_SETTINGS)

	def parse_register_setting(self, settings: _RegisterDefaults) -> None:
		token = self.advance()

		if token.text in ("volatile", "no_rmw"):
			setattr(settings, token.text, True)
		else:
			self.expect_symbol("=", f"after `{token.text}`")
			where = self.current
			value = evaluate_literal(self.parse_expr())
			if value is None or value <= 0:
				raise error(f"`{token.text}` needs a positive literal", where.span)
			setattr(settings, token.text, value)

		self.expect_symbol(";", f"after `{token.text}`")

	def parse_namespace(self) -> ast.NamespaceDecl:
		"""`namespace outer { struct Header { ... } }`

		A block rather than a positional directive, deliberately. A repeated
		positional form would have to answer what a second one does to the
		declarations before it, and `endian` already answers that question the
		other way -- the last one wins, file-wide. Braces make the scope visible
		instead of arguable, and they are what the word means to anyone who has
		met C++. See docs/decisions/0012-namespaces.md.
		"""
		start = self.advance()
		name  = self.expect_ident("a namespace name")
		self.expect_symbol("{", "to open the namespace")

		decls: list[ast.Decl] = []
		while not self.current.is_symbol("}"):
			if self.current.kind is TokenKind.EOF:
				raise error(
					"unexpected end of file inside a namespace",
					self.current.span,
					label = "expected `}`",
				)
			if self.current.is_ident("namespace"):
				raise not_yet_implemented(
					"a nested `namespace`", self.current.span, 12,
					notes = ["one level is supported; nesting needs the path "
					         "resolution rules of a later phase",
					         "a qualified name is written `outer::Header`"])
			decls.append(self.parse_decl())

		self.expect_symbol("}", "to close the namespace")
		return ast.NamespaceDecl(self.span_from(start), name.text, decls)

	def parse_strictness(self) -> ast.StrictnessDirective:
		"""`strictness = lenient;` (section 14.5).

		Spelled with `=` because it is a policy rather than a property of the
		data: `endian big` describes the bytes, `strictness = lenient` describes
		what the parser will put up with.
		"""
		start = self.advance()
		self.expect_symbol("=", "after `strictness`")
		token = self.expect_ident("a strictness")
		level = STRICTNESS.get(token.text)
		if level is None:
			raise error(
				f"unknown strictness `{token.text}`",
				token.span,
				label = "expected `strict` or `lenient`",
				notes = ["`strict` is the default and needs no directive; "
				         "`lenient` sets `canonical = NonCanonical` for the schema "
				         "(project.md section 14.5)"],
			)
		self.expect_symbol(";", "after the strictness directive")
		return ast.StrictnessDirective(self.span_from(start), level)

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

	def parse_codec(self) -> ast.CodecDecl:
		"""`codec aes_ctr_128 { length_preserving; seekable = linear; ... }`

		Every property is optional and every default is the conservative one:
		not seekable, stream granularity, not systematic, not invertible. A
		signature that says nothing therefore claims nothing, which is the only
		safe reading of silence in a declaration the compiler cannot verify.
		"""
		start = self.advance()
		name  = self.expect_ident("a codec name")

		# `codec framed = rs |> interleave |> manchester;` -- a pipeline, whose
		# properties compose from its stages rather than being declared
		# (section 13.4).
		if self.accept_symbol("=") is not None:
			return self.parse_pipeline(start, name)

		# `codec X extern { ... }` is the section 13.2 spelling; the binding it
		# implies is recorded as a separate impl so both spellings converge.
		external = self.accept_ident("extern") is not None

		self.expect_symbol("{", "to open the codec body")

		properties     = _CodecProperties()
		seen: set[str] = set()

		while not self.current.is_symbol("}"):
			self.parse_codec_property(properties, seen)
			self.expect_symbol(";", "after the codec property")

		self.expect_symbol("}", "to close the codec body")
		span = self.span_from(start)

		self._pending_extern = (name.text, span) if external else None
		return properties.build(span, name.text)

	def parse_pipeline(self, start: Token, name: Token) -> ast.CodecDecl:
		"""`codec framed = rs_255_223 |> interleave(16) |> manchester;`

		The stages are named here and resolved later: a pipeline may name a
		codec declared below it, and requiring otherwise would make the order
		of a file carry meaning it does not have.
		"""
		stages = [self.expect_ident("a codec name").text]
		self._skip_stage_arguments()

		while self.accept_symbol("|>") is not None:
			stages.append(self.expect_ident("a codec name").text)
			self._skip_stage_arguments()

		self.expect_symbol(";", "after the pipeline")

		if len(stages) < 2:
			raise error(
				f"`{name.text}` is a pipeline of one stage",
				self.span_from(start),
				label = "expected `a |> b`",
				notes = ["a pipeline of one is the codec it names; say that "
				         "instead"],
			)

		return ast.CodecDecl(self.span_from(start), name.text,
		                     pipeline=tuple(stages))

	def _skip_stage_arguments(self) -> None:
		"""`interleave(16)`: a stage may be parameterised.

		The arguments belong to the stage's own kernel, which already has them;
		what a pipeline needs is the order of the stages.
		"""
		if self.current.is_symbol("("):
			self._skip_balanced("(", ")")

	def parse_codec_property(self, properties: _CodecProperties,
			seen: set[str]) -> None:
		negated = self.accept_ident("not") is not None
		token   = self.expect_ident("a codec property")

		if token.text in seen:
			raise error(f"`{token.text}` is given twice", token.span)
		seen.add(token.text)

		if token.text == "length_preserving":
			properties.expansion = ast.Expansion.PRESERVING
		elif token.text == "expansion":
			self.expect_symbol("=", "after `expansion`")
			self.parse_expansion(properties)
		elif token.text == "seekable":
			if negated:
				properties.seekable = ast.Seekable.NONE
			elif self.accept_symbol("=") is not None:
				properties.seekable = self._named(
					ast.Seekable, "seekability", properties, size=False)
			else:
				# Bare `seekable;` is the linear case, as in example 5.3.
				properties.seekable = ast.Seekable.LINEAR
		elif token.text == "granularity":
			self.expect_symbol("=", "after `granularity`")
			properties.granularity = self._named(
				ast.Granularity, "granularity", properties, size=True)
		elif token.text == "kernel":
			self.expect_symbol("=", "after `kernel`")
			properties.kernel = self.parse_kernel()
			properties.has_kernel = True
		elif token.text in ("systematic", "authenticated", "invertible",
		                    "deterministic", "error_propagating"):
			setattr(properties, token.text, not negated)
		else:
			raise error(
				f"unknown codec property `{token.text}`",
				token.span,
				label = "not a property of a transform",
				notes = ["the property set is fixed by section 13.2; a codec "
				         "cannot declare a property the lattice does not read"],
			)

	def parse_expansion(self, properties: _CodecProperties) -> None:
		if self.accept_symbol("+") is not None:
			token = self.current
			value = evaluate_literal(self.parse_expr())
			if value is None or value < 0:
				raise error("`expansion = +N` needs a literal byte count", token.span)
			properties.expansion     = ast.Expansion.FIXED_ADD
			properties.expansion_add = value
			return

		token = self.expect_ident("an expansion form")

		if token.text == "unbounded":
			properties.expansion = ast.Expansion.UNBOUNDED
			return

		if token.text in ("ratio_exact", "ratio_padded", "ratio_bounded"):
			self.expect_symbol("(", "before the ratio")
			first = evaluate_literal(self.parse_expr())
			self.expect_symbol(",", "between the ratio terms")
			second = evaluate_literal(self.parse_expr())
			self.expect_symbol(")", "after the ratio")

			if first is None or second is None or first <= 0 or second <= 0:
				raise error("a ratio needs two positive literals", token.span)

			properties.expansion = {
				"ratio_exact":   ast.Expansion.RATIO_EXACT,
				"ratio_padded":  ast.Expansion.RATIO_PADDED,
				"ratio_bounded": ast.Expansion.RATIO_BOUNDED,
			}[token.text]
			properties.ratio = (first, second)
			return

		raise error(
			f"unknown expansion form `{token.text}`",
			token.span,
			label = "expected `+N`, `unbounded`, `ratio_exact(a, b)`, "
			        "`ratio_padded(a, b)` or `ratio_bounded(a, b)`",
		)

	def _named(self, enum: type[EnumT], described: str,
			properties: _CodecProperties, size: bool) -> EnumT:
		"""A property value that may carry a size: `block(16)`, `symbol(5)`."""
		token = self.expect_ident(f"a {described}")

		for candidate in enum:
			if candidate.value != token.text:
				continue
			if self.current.is_symbol("("):
				self.advance()
				inner = self.current
				value = evaluate_literal(self.parse_expr())
				self.expect_symbol(")", "after the size")
				if value is None and not inner.is_ident("any"):
					raise error(f"`{token.text}` needs a literal size or `any`",
					            inner.span)
				if size:
					properties.granularity_size = value
			return candidate

		options = ", ".join(f"`{item.value}`" for item in enum)
		raise error(f"unknown {described} `{token.text}`", token.span,
		            label = f"expected one of {options}")

	def parse_kernel(self) -> ast.Kernel:
		"""`kernel = polynomial(width = 32, poly = 0x04C11DB7, reflect);`

		The family decides which arguments mean what; this only reads them.
		What a kernel implies about capabilities is derived in
		`situc/kernels.py`, because a description the compiler can generate an
		implementation from is one whose properties it can compute rather than
		take on trust (section 13.1).
		"""
		start  = self.current
		token  = self.expect_ident("a kernel family")
		family = KERNEL_FAMILIES.get(token.text)

		if family is None:
			options = ", ".join(f"`{name}`" for name in sorted(KERNEL_FAMILIES))
			raise error(
				f"unknown kernel family `{token.text}`",
				token.span,
				label = "not one of the section 13.4 families",
				notes = [f"expected one of {options}",
				         "essentially every line code, FEC, scrambler and "
				         "framing code in practical use is one of these or a "
				         "pipeline of them"],
			)

		args: list[ast.Attr] = []
		if self.accept_symbol("(") is not None:
			while not self.current.is_symbol(")"):
				args.append(self.parse_attr())
				if self.accept_symbol(",") is None:
					break
			self.expect_symbol(")", "after the kernel arguments")

		return ast.Kernel(self.span_from(start), family, tuple(args))

	def parse_impl(self) -> ast.ImplDecl:
		"""`impl crc32 derived;` or `impl crc32 extern "my_fast_crc32";`"""
		start = self.advance()
		codec = self.expect_ident("a codec name")
		kind  = self.expect_ident("`derived` or `extern`")

		if kind.text == "derived":
			self.expect_symbol(";", "after the impl binding")
			return ast.ImplDecl(self.span_from(start), codec.text,
			                    ast.ImplKind.DERIVED)

		if kind.text != "extern":
			raise error(f"unknown impl kind `{kind.text}`", kind.span,
			            label = "expected `derived` or `extern`")

		if self.current.kind is not TokenKind.STRING:
			raise error(
				"`extern` needs a quoted symbol name",
				self.current.span,
				label = "expected a string literal",
				notes = ['for example: impl crc32 extern "my_fast_crc32";'],
			)

		symbol = self.advance()
		self.expect_symbol(";", "after the impl binding")
		return ast.ImplDecl(self.span_from(start), codec.text,
		                    ast.ImplKind.EXTERN, symbol.text)

	def parse_varint(self) -> ast.VarintDecl:
		"""`varint_type leb128 { encoding = leb128; max_bits = 64; minimal; }`

		`minimal` is present or absent, never defaulted: section 17.0 lists
		non-minimal varint acceptance as an ambiguity that has to be resolved
		explicitly, because it decides whether the format can be canonical and
		the wrong answer is undetectable at runtime.
		"""
		start = self.advance()
		name  = self.expect_ident("a varint type name")
		self.expect_symbol("{", "to open the varint body")

		encoding: ast.VarintEncoding | None  = None
		transform: ast.VarintTransform | None = None
		max_bits: int | None                  = None
		minimal                               = False
		seen: set[str] = set()

		while not self.current.is_symbol("}"):
			prop = self.expect_ident("a varint property")
			if prop.text in seen:
				raise error(f"`{prop.text}` is given twice", prop.span)
			seen.add(prop.text)

			if prop.text == "minimal":
				minimal = True
			elif prop.text == "encoding":
				self.expect_symbol("=", "after `encoding`")
				encoding = self._varint_enum(ast.VarintEncoding, "encoding")
			elif prop.text == "transform":
				self.expect_symbol("=", "after `transform`")
				transform = self._varint_enum(ast.VarintTransform, "transform")
			elif prop.text == "max_bits":
				self.expect_symbol("=", "after `max_bits`")
				token = self.current
				max_bits = evaluate_literal(self.parse_expr())
				if max_bits is None or not 1 <= max_bits <= 64:
					raise error(
						"`max_bits` must be a literal from 1 to 64",
						token.span,
						label = "out of range",
					)
			else:
				raise error(
					f"unknown varint property `{prop.text}`",
					prop.span,
					label = "expected `encoding`, `transform`, `max_bits` or `minimal`",
				)

			self.expect_symbol(";", "after the varint property")

		self.expect_symbol("}", "to close the varint body")

		if encoding is None:
			raise error(
				f"varint type `{name.text}` does not declare an encoding",
				self.span_from(start),
				label = "expected `encoding = leb128;`",
			)
		if max_bits is None:
			raise error(
				f"varint type `{name.text}` does not declare `max_bits`",
				self.span_from(start),
				label = "expected `max_bits = N;`",
				notes = ["without it the worst-case encoded length is unknown, so "
				         "nothing downstream can be bounded"],
			)

		return ast.VarintDecl(self.span_from(start), name.text, encoding,
		                      max_bits, minimal, transform)

	def _varint_enum(self, enum: type[EnumT], described: str) -> EnumT:
		token = self.expect_ident(f"a varint {described}")
		for candidate in enum:
			if candidate.value == token.text:
				return candidate

		options = ", ".join(f"`{item.value}`" for item in enum)
		raise error(f"unknown {described} `{token.text}`", token.span,
		            label = f"expected one of {options}")

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

			if token.text in RADIX_KEYWORDS:
				return self.parse_text_field()
			if token.text == "endian_marker":
				return self.parse_marker_field()
			if token.text == "reserved":
				return self.parse_reserved()
			if token.text == "positional":
				return self.parse_positional()
			if token.text == "variant":
				return self.parse_variant()
			if token.text == "opaque":
				return self.parse_opaque()
			if token.text == "indexed":
				return self.parse_indexed()
			if token.text == "tlv":
				return self.parse_tlv()
			if token.text == "coded":
				return self.parse_coded()
			if token.text == "authenticated":
				return self.parse_authenticated()
			if token.text == "sealed":
				return self.parse_sealed()
			if token.text in ("tag", "checksum"):
				return self.parse_tag_field()

		return self.parse_field()

	def parse_positional(self) -> ast.PositionalBlock:
		start = self.advance()
		self.expect_symbol("{", "to open the positional block")
		members = self.parse_members()
		self.expect_symbol("}", "to close the positional block")
		return ast.PositionalBlock(self.span_from(start), members)

	def parse_opaque(self) -> ast.Opaque:
		"""`opaque ciphertext [hdr.length];`"""
		start = self.advance()
		name  = self.expect_ident("a region name")
		self.expect_symbol("[", "before the region size")
		size = self.parse_expr()
		self.expect_symbol("]", "after the region size")
		attrs = self.parse_attrs()
		self.expect_symbol(";", "after the opaque region")
		return ast.Opaque(self.span_from(start), name.text, size, attrs)

	def parse_indexed(self) -> ast.Indexed:
		"""`indexed(offset_type = u16, count = hdr.n) { Record entries[]; }`"""
		start = self.advance()
		self.expect_symbol("(", "before the index arguments")

		args: list[ast.Attr] = []
		while not self.current.is_symbol(")"):
			args.append(self.parse_attr())
			if self.accept_symbol(",") is None:
				break

		self.expect_symbol(")", "after the index arguments")
		self.expect_symbol("{", "to open the indexed block")
		members = self.parse_members()
		self.expect_symbol("}", "to close the indexed block")

		if len(members) != 1:
			raise error(
				"an `indexed` region holds exactly one element declaration",
				self.span_from(start),
				label = f"found {len(members)}",
				notes = ["the offset table indexes one array; several would need "
				         "several tables"],
			)

		name = members[0].name if isinstance(members[0], ast.Field) else "entries"
		return ast.Indexed(self.span_from(start), name, tuple(args), members)

	def parse_coded(self) -> ast.Coded:
		"""`coded body(aes_ctr_128) { u16 kind; u8 rest[remaining]; }`

		The codec's properties decide what the interior keeps: a
		length-preserving, byte-granular, linearly seekable transform leaves
		fixed offsets and single-field mutation intact, while a stream cipher
		with no seekability does not. None of that is decided here.
		"""
		start = self.advance()
		name  = self.expect_ident("a region name")
		self.expect_symbol("(", "before the codec name")
		codec = self.expect_ident("a codec name")

		args: list[ast.Attr] = []
		while self.accept_symbol(",") is not None:
			args.append(self.parse_attr())

		self.expect_symbol(")", "after the codec arguments")
		until = self.parse_until()
		attrs = self.parse_attrs()
		self.expect_symbol("{", "to open the coded region")
		members = self.parse_members()
		self.expect_symbol("}", "to close the coded region")

		return ast.Coded(self.span_from(start), name.text, codec.text,
		                 tuple(args), members, attrs, until)

	def parse_authenticated(self) -> ast.Authenticated:
		"""`authenticated { ... }`, or `authenticated header { ... }`.

		The name is optional and defaults to the keyword, which is what makes
		`covers(authenticated)` and the map entry addressable in the common case
		of one such region per struct. See
		docs/decisions/0010-region-and-tag-names.md.
		"""
		start = self.advance()
		name  = self.optional_region_name("authenticated")
		attrs = self.parse_attrs()
		self.expect_symbol("{", "to open the authenticated block")
		members = self.parse_members()
		self.expect_symbol("}", "to close the authenticated block")

		return ast.Authenticated(self.span_from(start), name, members, attrs)

	def parse_sealed(self) -> ast.Sealed:
		"""`sealed(aes_gcm_128, nonce = nonce) { u16 inner_kind; }`

		The same shape as `coded`, because it is `coded` plus authentication
		(decision 0009). The region name is optional here as it is not for
		`coded`: 5.3 writes `sealed(...)` with no name and then addresses the
		region as `Packet.sealed`.
		"""
		start = self.advance()
		name  = self.optional_region_name("sealed")
		self.expect_symbol("(", "before the codec name")
		codec = self.expect_ident("a codec name")

		args: list[ast.Attr] = []
		while self.accept_symbol(",") is not None:
			args.append(self.parse_attr())

		self.expect_symbol(")", "after the codec arguments")
		attrs = self.parse_attrs()
		self.expect_symbol("{", "to open the sealed region")
		members = self.parse_members()
		self.expect_symbol("}", "to close the sealed region")

		return ast.Sealed(self.span_from(start), name, codec.text,
		                  tuple(args), members, attrs)

	def parse_tag_field(self) -> ast.TagField:
		"""`tag u8[16] covers(hdr, body);` and the same for `checksum`.

		`covers` is a bare clause rather than an attribute because the grammar
		of section 7 puts it there, and because a coverage list is not a flag on
		the field -- it is the thing the field is for.
		"""
		start = self.advance()
		kind  = (ast.TagKind.TAG if start.text == "tag" else ast.TagKind.CHECKSUM)

		type_ref = self.parse_type_ref()
		name     = self.optional_region_name(start.text)
		array    = self.parse_array_spec()

		if array is None:
			raise error(
				f"`{start.text}` needs a length",
				self.current.span,
				label = f"expected `[N]` after the type",
				notes = [f"a {start.text} is a byte string, so its width is part of "
				         "the declaration: `tag u8[16];`"],
			)

		covers = self.parse_covers()
		attrs  = self.parse_attrs()
		self.expect_symbol(";", f"after the {start.text} declaration")

		return ast.TagField(self.span_from(start), name, type_ref, array,
		                    covers, kind, attrs)

	def parse_covers(self) -> tuple[str, ...]:
		"""`covers(a, b)`, or nothing at all, which means inference."""
		if not (self.current.kind is TokenKind.IDENT
				and self.current.text == "covers"):
			return ()

		self.advance()
		self.expect_symbol("(", "before the covered regions")

		names: list[str] = []
		while not self.current.is_symbol(")"):
			names.append(self.expect_ident("a region name").text)
			if self.accept_symbol(",") is None:
				break

		self.expect_symbol(")", "after the covered regions")

		if not names:
			raise error(
				"`covers()` covers nothing",
				self.span_from(self.tokens[self.pos - 1]),
				label = "expected at least one region name",
				notes = ["omit the clause entirely to cover every authenticated "
				         "and sealed region in the struct (project.md section 14.1)"],
			)

		return tuple(names)

	def optional_region_name(self, default: str) -> str:
		"""A name where the grammar allows one to be left out.

		Unambiguous by construction: what follows an unnamed region is `(` or
		`{` or `[`, never an identifier.
		"""
		if self.current.kind is TokenKind.IDENT and self.current.text != "covers":
			return self.advance().text
		return default

	def parse_tlv(self) -> ast.Tlv:
		"""`tlv options (tag_type = u8, known = { ... }, unknown = error);`

		Both the simple and the general form of section 9.5 parse here: the
		general form is the same argument list with `tag_decode`, `value_size`
		and `duplicate_tags` present. Nothing about the shape differs, so
		nothing about the parse does either.
		"""
		start = self.advance()
		name  = self.expect_ident("a region name")
		self.expect_symbol("(", "before the tlv arguments")

		self._dispatch_cases = ()
		args: list[ast.Attr] = []
		while not self.current.is_symbol(")"):
			args.append(self.parse_tlv_argument())
			if self.accept_symbol(",") is None:
				break

		self.expect_symbol(")", "after the tlv arguments")
		attrs = self.parse_attrs()
		self.expect_symbol(";", "after the tlv region")

		span = self.span_from(start)
		return ast.Tlv(
			span       = span,
			name       = name.text,
			args       = tuple(args),
			unknown    = self._tlv_policy(args, "unknown", ast.UnknownPolicy, span),
			duplicates = self._tlv_policy(args, "duplicate_tags",
			                              ast.DuplicatePolicy, span),
			ordered    = any(arg.name == "ordering" for arg in args),
			attrs      = attrs,
			wire_types = self._dispatch_cases,
		)

	def parse_tlv_argument(self) -> ast.Attr:
		"""One `key = value` of a tlv argument list.

		The values are structured -- a `{ ... }` map of known tags, a
		`switch (...)` over wire types -- so they are captured as written and
		interpreted by the pass that needs them, rather than being flattened
		into an expression here.
		"""
		name = self.expect_ident("a tlv argument name")
		if self.accept_symbol("=") is None:
			return ast.Attr(self.span_from(name), name.text, None)

		if self.current.is_symbol("{"):
			start = self.current
			self._skip_balanced("{", "}")
			return ast.Attr(self.span_from(name), name.text, None,
			                raw=self._verbatim(start))

		if self.current.is_ident("switch"):
			start = self.current
			self.advance()
			if self.current.is_symbol("("):
				self._skip_balanced("(", ")")
			self._dispatch_cases = self._collect_cases()
			return ast.Attr(self.span_from(name), name.text, None,
			                raw=self._verbatim(start))

		return ast.Attr(self.span_from(name), name.text, self.parse_expr())

	def _verbatim(self, start: Token) -> str:
		"""The source text from `start` to the cursor, as written."""
		return self.source.text[start.span.start : self.tokens[self.pos - 1].span.end]

	def _collect_cases(self) -> tuple[int, ...]:
		"""Record the `case N:` labels of a dispatch while skipping its bodies.

		The bodies are `self_delimiting`, `prefixed(...)` and the like, which
		belong to a later pass. The labels are wire types, and which ones a
		region accepts is a capability question: see the packed-versus-unpacked
		rule in propagate.py.
		"""
		labels: list[int] = []

		self.expect_symbol("{", "to open the dispatch")
		depth = 1
		while depth > 0:
			if self.current.kind is TokenKind.EOF:
				raise error("unterminated dispatch", self.current.span)

			if self.current.is_symbol("{"):
				depth += 1
			elif self.current.is_symbol("}"):
				depth -= 1
			elif (depth == 1 and self.current.is_ident("case")
					and self.peek().kind is TokenKind.INT):
				labels.append(self.peek().value)

			self.advance()

		return tuple(labels)

	def _skip_balanced(self, opener: str, closer: str) -> None:
		"""Consume a balanced group, keeping its span but not its structure."""
		self.expect_symbol(opener, "to open the group")
		depth = 1
		while depth > 0:
			if self.current.kind is TokenKind.EOF:
				raise error(f"unterminated `{opener}` group", self.current.span)
			if self.current.is_symbol(opener):
				depth += 1
			elif self.current.is_symbol(closer):
				depth -= 1
			self.advance()

	def _tlv_policy(self, args: list[ast.Attr], name: str, enum: type[EnumT],
			span: Span) -> EnumT:
		"""Read a policy argument, defaulting to the safe option.

		Section 14.5: unknown tags are rejected by default and duplicates are
		too. A schema that wants the permissive behaviour has to say so, and
		saying so appears in the capability map.
		"""
		for arg in args:
			if arg.name != name:
				continue
			if not isinstance(arg.value, ast.NameRef):
				raise error(f"`{name}` needs a policy name", arg.span)
			for candidate in enum:
				if candidate.value == arg.value.name:
					return candidate
			options = ", ".join(f"`{item.value}`" for item in enum)
			raise error(f"unknown `{name}` policy `{arg.value.name}`",
			            arg.value.span, label=f"expected one of {options}")

		return next(iter(enum))		# ERROR is first in both policies

	def parse_variant(self) -> ast.Variant:
		"""`variant body switch (hdr.type) { case A: X a; default: error; }`"""
		start = self.advance()
		name  = self.expect_ident("a variant name")
		self.expect_keyword("switch", "after the variant name")
		self.expect_symbol("(", "before the discriminant")
		discriminant = self.parse_expr()
		self.expect_symbol(")", "after the discriminant")
		attrs = self.parse_attrs()
		self.expect_symbol("{", "to open the variant body")

		arms: list[ast.VariantArm] = []
		while not self.current.is_symbol("}"):
			arms.append(self.parse_variant_arm())

		self.expect_symbol("}", "to close the variant body")

		if not arms:
			raise error(
				f"variant `{name.text}` has no arms",
				self.span_from(start),
				label = "expected at least one `case`",
			)

		defaults = [arm for arm in arms if arm.is_default]
		if len(defaults) > 1:
			raise error("a variant has at most one `default` arm", defaults[1].span)

		return ast.Variant(self.span_from(start), name.text, discriminant,
		                   tuple(arms), attrs)

	def parse_variant_arm(self) -> ast.VariantArm:
		start = self.current

		if self.accept_ident("default") is not None:
			self.expect_symbol(":", "after `default`")
			return self._arm_body(start, value=None)

		self.expect_keyword("case", "to open a variant arm")
		value = self.parse_expr()
		self.expect_symbol(":", "after the case value")
		return self._arm_body(start, value)

	def _arm_body(self, start: Token, value: ast.Expr | None) -> ast.VariantArm:
		"""An arm is a member, or one of the two policies for an unknown value."""
		if self.accept_ident("error") is not None:
			self.expect_symbol(";", "after `error`")
			return ast.VariantArm(self.span_from(start), value, None, is_error=True)

		if self.current.is_ident("opaque") and self.peek().is_symbol(";"):
			self.advance()
			self.expect_symbol(";", "after `opaque`")
			return ast.VariantArm(self.span_from(start), value, None, is_opaque=True)

		member = self.parse_member()
		return ast.VariantArm(self.span_from(start), value, member)

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

	def parse_text_field(self) -> ast.Field:
		"""`decimal u32 n until ":"` -- a number written as digits.

		The scalar names the value's *domain*, not its width in the buffer:
		"7" and "1234567" are the same kind of field at different widths, so a
		text number has no width to declare. What `u32` says is which values
		are representable, which is what a caller needs and what the range
		check is written against.
		"""
		start = self.advance()
		radix = RADIX_KEYWORDS[start.text]
		field = self.parse_field()

		# Two ways to say where the digits stop, and SMTP needs the second.
		# A reply code is exactly three digits with nothing after them, and
		# requiring `until` made that unwriteable -- "a text number is as wide
		# as the number" is true of a delimited one and false of a padded
		# field, which is a common shape in every fixed-record format.
		framed = field.until is not None
		fixed  = field.array is not None and field.array.size is not None

		if not framed and not fixed:
			raise error(
				f"`{start.text} {field.name}` has no end",
				field.span,
				label = "neither a width nor a delimiter",
				notes = ["a text number is as wide as the number unless "
				         "something says otherwise, so one of two things has "
				         "to",
				         f'`{start.text} {field.type_ref.name} {field.name} '
				         f'until ":"` stops it at a delimiter',
				         f"`{start.text} {field.type_ref.name} {field.name}[3]` "
				         "gives it a fixed width, padded"],
			)

		if framed and fixed:
			raise error(
				f"`{field.name}` says twice how wide it is",
				field.span,
				label = "a width and a delimiter",
				notes = ["a fixed-width number does not need a delimiter, and "
				         "a delimited one has no width to declare"],
			)

		return ast.Field(self.span_from(start), field.name, field.type_ref,
		                 field.array, field.pin, field.attrs, field.until,
		                 radix)

	def parse_field(self) -> ast.Field:
		start    = self.current
		type_ref = self.parse_type_ref()
		name     = self.expect_ident("a field name")
		array    = self.parse_array_spec()
		until    = self.parse_until()
		pin      = self.parse_pin()
		attrs    = self.parse_attrs()
		self.expect_symbol(";", "after the field declaration")
		return ast.Field(self.span_from(start), name.text, type_ref, array, pin,
		                 attrs, until)

	def parse_until(self) -> ast.Until | None:
		"""`until "\\r\\n"`, optionally bounded and optionally relaxed.

		Between the array spec and the pin, because it says where the member
		*ends* and belongs beside the thing that says how many there are. The
		relaxations are attributes rather than more keywords: `[quoted = '"']`
		reads as a property of the field, which it is.
		"""
		if not self.current.is_ident("until"):
			return None

		start = self.advance()
		token = self.current

		if token.kind is not TokenKind.STRING:
			raise error(
				"a delimiter must be a string literal",
				token.span,
				label = "expected a string",
				notes = ['`until "\\r\\n"` or `until "\\0"`: the bytes a member '
				         "ends at",
				         "an expression would have to be evaluated against the "
				         "data the delimiter is being looked for in"],
			)
		self.advance()

		if not token.text:
			raise error(
				"an empty delimiter matches everywhere",
				token.span,
				label = "no bytes to look for",
				notes = ["a zero-length delimiter is found at offset 0 of any "
				         "buffer, so the member would always be empty"],
			)

		cap: ast.Expr | None = None
		if self.current.is_ident("max"):
			self.advance()
			cap = self.parse_expr()

		return ast.Until(self.span_from(start), token.text.encode("latin-1"),
		                 cap = cap)

	def parse_qualification(self, head: Token) -> str:
		"""`outer::Header`, from the `::` at the cursor.

		One level, matching what `namespace` accepts. A second `::` gets the
		same diagnostic the nested declaration does, so the two halves of the
		restriction say the same thing.
		"""
		self.expect_symbol("::", "in a qualified name")
		tail = self.expect_ident("a name after `::`")

		if self.current.is_symbol("::"):
			raise not_yet_implemented(
				"a nested qualified name", self.current.span, 12,
				notes = ["one level is supported: `outer::Header`"])

		return f"{head.text}::{tail.text}"

	def parse_type_ref(self) -> ast.TypeRef:
		token = self.expect_ident("a type name")

		if self.current.is_symbol("::"):
			qualified = self.parse_qualification(token)
			return ast.TypeRef(self.span_from(token), qualified)

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

	def parse_invariant(self) -> ast.Invariant:
		"""`invariant s.total == size(s.hdr) + size(s.body);`

		The left side is one field path and nothing else. An invariant whose
		left side were an expression would say what must be true without saying
		which field situ is to maintain, and maintaining it is the whole point
		-- a checked-but-unmaintained equality is what `require` already is.
		"""
		start   = self.advance()
		derived = self.parse_path("the field the invariant maintains")
		self.expect_symbol("==", "after the field an invariant maintains")
		expr = self.parse_expr()
		self.expect_symbol(";", "after the invariant")
		return ast.Invariant(self.span_from(start), derived, expr)

	def parse_path(self, context: str) -> str:
		"""A dotted field path, as text."""
		parts = [self.expect_ident(context).text]
		while self.accept_symbol("."):
			parts.append(self.expect_ident("a field name").text)
		return ".".join(parts)

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
			elif self.current.is_symbol("[") and not self.bracket_is_attrs():
				# Decision 0006's rule, which the expression parser did not
				# know. `until "," max 16 [encoding = ascii]` reaches here
				# with the cursor on the attribute list, and indexing `16` by
				# it is the same ambiguity that rule exists to settle -- one
				# level down, where nobody had looked for it.
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
			if self.current.is_symbol("::"):
				qualified = self.parse_qualification(token)
				return ast.NameRef(self.span_from(token), qualified)
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
