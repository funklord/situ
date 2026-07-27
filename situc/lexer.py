"""Tokenisation of situ source text (project.md section 6).

Keywords are not distinguished here. Situ has many contextual keywords --
`default`, `error`, `pass`, `remaining`, `covers` -- and reserving them in the
lexer would forbid them as field names for no benefit. The parser matches on
identifier text instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from situc.diagnostics import Source, Span, error


class TokenKind(Enum):
	IDENT	= "identifier"
	INT	= "integer"
	STRING	= "string"
	SYMBOL	= "symbol"
	EOF	= "end of file"


@dataclass(frozen=True)
class Token:
	kind: TokenKind
	text: str
	span: Span
	value: int = 0		# INT only

	def is_ident(self, *names: str) -> bool:
		return self.kind is TokenKind.IDENT and self.text in names

	def is_symbol(self, *symbols: str) -> bool:
		return self.kind is TokenKind.SYMBOL and self.text in symbols

	def describe(self) -> str:
		if self.kind is TokenKind.EOF:
			return "end of file"
		return f"`{self.text}`"


# Longest first: the scanner takes the first match, so `<<` must precede `<`.
SYMBOLS = (
	"<<", ">>", "==", "!=", "<=", ">=", "&&", "||", "|>", "::",
	";", ",", ":", ".", "@", "=", "+", "-", "*", "/", "%",
	"&", "|", "^", "~", "<", ">", "!", "(", ")", "[", "]", "{", "}",
)

IDENT_START	= frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
IDENT_BODY	= IDENT_START | frozenset("0123456789")
DIGITS		= frozenset("0123456789")

RADIX_DIGITS = {
	"x": frozenset("0123456789abcdefABCDEF"),
	"b": frozenset("01"),
}


class Lexer:
	"""Scans one source file into a token list."""

	def __init__(self, source: Source) -> None:
		self.source = source
		self.text   = source.text
		self.pos    = 0

	def span(self, start: int, end: int | None = None) -> Span:
		return Span(self.source, start, self.pos if end is None else end)

	def tokenize(self) -> list[Token]:
		tokens: list[Token] = []
		while True:
			self.skip_trivia()
			if self.pos >= len(self.text):
				break
			tokens.append(self.scan_token())

		tokens.append(Token(TokenKind.EOF, "", self.span(len(self.text), len(self.text))))
		return tokens

	def skip_trivia(self) -> None:
		while self.pos < len(self.text):
			char = self.text[self.pos]
			if char in " \t\r\n":
				self.pos += 1
			elif self.text.startswith("//", self.pos):
				end = self.text.find("\n", self.pos)
				self.pos = len(self.text) if end < 0 else end
			elif self.text.startswith("/*", self.pos):
				self.skip_block_comment()
			else:
				return

	def skip_block_comment(self) -> None:
		"""Block comments nest (project.md section 6)."""
		start = self.pos
		depth = 0
		while self.pos < len(self.text):
			if self.text.startswith("/*", self.pos):
				depth   += 1
				self.pos += 2
			elif self.text.startswith("*/", self.pos):
				depth   -= 1
				self.pos += 2
				if depth == 0:
					return
			else:
				self.pos += 1

		raise error(
			"unterminated block comment",
			self.span(start, start + 2),
			label = "opened here",
			notes = ["block comments nest, so every `/*` needs its own `*/`"],
		)

	def scan_token(self) -> Token:
		start = self.pos
		char  = self.text[start]

		if char in IDENT_START:
			return self.scan_ident()
		if char in DIGITS:
			return self.scan_int()
		if char == '"':
			return self.scan_string()

		for symbol in SYMBOLS:
			if self.text.startswith(symbol, start):
				self.pos += len(symbol)
				return Token(TokenKind.SYMBOL, symbol, self.span(start))

		self.pos += 1
		if not char.isascii():
			raise error(
				f"non-ASCII byte 0x{ord(char):02x} outside a string literal",
				self.span(start),
				label = "not permitted here",
				notes = ["situ source is ASCII; only string literals may hold other bytes"],
			)

		raise error(f"unexpected character `{char}`", self.span(start))

	def scan_ident(self) -> Token:
		start = self.pos
		while self.pos < len(self.text) and self.text[self.pos] in IDENT_BODY:
			self.pos += 1
		return Token(TokenKind.IDENT, self.text[start : self.pos], self.span(start))

	def scan_int(self) -> Token:
		start = self.pos
		radix = 10
		digits = DIGITS

		if self.text.startswith("0", self.pos) and self.pos + 1 < len(self.text):
			marker = self.text[self.pos + 1]
			if marker in RADIX_DIGITS:
				radix    = 16 if marker == "x" else 2
				digits   = RADIX_DIGITS[marker]
				self.pos += 2
				if self.pos >= len(self.text) or self.text[self.pos] not in digits:
					raise error(
						f"`0{marker}` with no digits",
						self.span(start),
						label = "expected at least one digit here",
					)

		body_start = self.pos
		while self.pos < len(self.text) and (self.text[self.pos] in digits
		                                     or self.text[self.pos] == "_"):
			self.pos += 1

		body = self.text[body_start : self.pos].replace("_", "")
		if not body:
			raise error("malformed integer literal", self.span(start))

		# `0x1g` would otherwise scan as `0x1` followed by the identifier `g`,
		# which is never what the author meant.
		if self.pos < len(self.text) and self.text[self.pos] in IDENT_BODY:
			raise error(
				"invalid digit in integer literal",
				self.span(self.pos, self.pos + 1),
				label = f"not a base-{radix} digit",
			)

		return Token(TokenKind.INT, self.text[start : self.pos], self.span(start),
		             value = int(body, radix))

	def scan_string(self) -> Token:
		start = self.pos
		self.pos += 1
		chunks: list[str] = []

		while self.pos < len(self.text):
			char = self.text[self.pos]
			if char == '"':
				self.pos += 1
				return Token(TokenKind.STRING, "".join(chunks), self.span(start))
			if char == "\n":
				break
			if char == "\\":
				chunks.append(self.scan_escape())
				continue
			chunks.append(char)
			self.pos += 1

		raise error("unterminated string literal", self.span(start, start + 1),
		            label = "opened here")

	def scan_escape(self) -> str:
		start = self.pos
		self.pos += 1
		if self.pos >= len(self.text):
			raise error("unterminated escape sequence", self.span(start))

		char = self.text[self.pos]
		self.pos += 1
		simple = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "\\": "\\", '"': '"'}
		if char in simple:
			return simple[char]

		raise error(f"unknown escape sequence `\\{char}`", self.span(start, self.pos))


def tokenize(source: Source) -> list[Token]:
	return Lexer(source).tokenize()
