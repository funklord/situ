"""The expression bytecode, evaluated.

Section 10's language is total -- no calls, no recursion, no iteration, no
floating point -- so this is a loop over a switch with no jumps and no way to
not terminate: the program counter only ever advances, so a program's length
is its own bound. That property is the whole argument for shipping an
evaluator to a device, and it is a property of the language rather than of
this implementation.

It is also why nothing here needs a step limit. A guard that cannot fire is
worse than no guard, because it suggests the danger it does not address.
"""

from __future__ import annotations

import struct as _struct
from collections.abc import Callable

#: Opcodes, matching `situc/pack.py`. Two copies of one table is exactly what
#: the image exists to avoid -- but the compiler must not import the walker
#: (0026), and the walker must not import the compiler, or the fifth column
#: would be comparing a backend against itself. So the numbers are repeated
#: here on purpose and `test_the_opcodes_match_the_packer` reads both files
#: and fails when they drift.
END, PUSH, FIELD, REMAINING, SIZE, OFFSET, COUNT = range(7)
#: Only relation programs carry this (26.95). A relation's two
#: parameters are usually the same struct, so a placement index alone
#: does not say which message to read it out of; the operand is a
#: parameter byte and then the index.
ARG_FIELD = 7
#: `FIELD` plus the byte offset the referenced field's struct sits at in the
#: frame being walked. A placement's own offset is within its own struct, so
#: a field of a nested struct needs the nesting offset as well -- without it
#: the read lands at the right offset of the wrong struct, which is right
#: only where the nested struct is at offset 0 (26.184).
FIELD_IN = 8
ADD, SUB, MUL, DIV, MOD, AND, OR, XOR, SHL, SHR, NEG, NOT = range(0x10, 0x1C)
EQ, NE, LT, LE, GT, GE, LAND, LOR = range(0x20, 0x28)
MIN, MAX, ALIGN_UP = range(0x30, 0x33)


class VmError(Exception):
	"""The program is not one this walker can run. Raised rather than
	returning a number: a wrong length is indistinguishable from a right one
	once it leaves here."""


def _align_up(value: int, to: int) -> int:
	if to <= 0:
		raise VmError("align_up to a non-positive boundary")
	return ((value + to - 1) // to) * to


#: Every binary operator, by opcode. Division and modulo by zero raise rather
#: than returning zero: a length computed from a division nobody checked is a
#: buffer overrun in whatever reads it next.
def _div(a: int, b: int) -> int:
	if b == 0:
		raise VmError("division by zero")
	return abs(a) // abs(b) * (1 if (a < 0) == (b < 0) else -1)


def _mod(a: int, b: int) -> int:
	if b == 0:
		raise VmError("modulo by zero")
	return a - _div(a, b) * b


BINARY: dict[int, Callable[[int, int], int]] = {
	ADD: lambda a, b: a + b,
	SUB: lambda a, b: a - b,
	MUL: lambda a, b: a * b,
	DIV: _div,
	MOD: _mod,
	AND: lambda a, b: a & b,
	OR:  lambda a, b: a | b,
	XOR: lambda a, b: a ^ b,
	SHL: lambda a, b: a << b if 0 <= b < 64 else 0,
	SHR: lambda a, b: a >> b if 0 <= b < 64 else 0,
	EQ:  lambda a, b: int(a == b),
	NE:  lambda a, b: int(a != b),
	LT:  lambda a, b: int(a < b),
	LE:  lambda a, b: int(a <= b),
	GT:  lambda a, b: int(a > b),
	GE:  lambda a, b: int(a >= b),
	LAND: lambda a, b: int(bool(a) and bool(b)),
	LOR: lambda a, b: int(bool(a) or bool(b)),
	MIN: min,
	MAX: max,
	ALIGN_UP: _align_up,
}


def run(code: bytes, at: int, load_field: Callable[[int], int],
        size_of: Callable[[int], int], offset_of: Callable[[int], int],
        count_of: Callable[[int], int], remaining: int,
        load_arg: Callable[[int, int], int] | None = None,
        load_field_in: Callable[[int, int], int] | None = None) -> int:
	"""Evaluate the program starting at `at` and return its one value.

	The callbacks are what ties an expression to a message: `load_field`
	reads a placement's value out of the buffer, and the other three answer
	the section 10 builtins. They are passed rather than reached for so that
	this module knows nothing about buffers, which is what makes it testable
	against hand-written programs.
	"""
	stack: list[int] = []
	pc = at
	while True:
		if pc >= len(code):
			raise VmError("program ran off the end without END")
		op = code[pc]
		pc += 1

		if op == END:
			if len(stack) != 1:
				raise VmError(f"END with {len(stack)} values on the stack")
			return stack[0]
		if op == PUSH:
			stack.append(_struct.unpack_from("<q", code, pc)[0])
			pc += 8
			continue
		if op in (FIELD, SIZE, OFFSET, COUNT):
			index = _struct.unpack_from("<I", code, pc)[0]
			pc += 4
			stack.append({FIELD: load_field, SIZE: size_of,
			              OFFSET: offset_of, COUNT: count_of}[op](index))
			continue
		if op == FIELD_IN:
			index = _struct.unpack_from("<I", code, pc)[0]
			base  = _struct.unpack_from("<i", code, pc + 4)[0]
			pc += 8
			if load_field_in is None:
				raise VmError("field_in needs a base-aware loader")
			stack.append(load_field_in(index, base))
			continue
		if op == ARG_FIELD:
			if load_arg is None:
				raise VmError("arg_field outside a relation")
			arg   = code[pc]
			index = _struct.unpack_from("<I", code, pc + 1)[0]
			pc += 5
			stack.append(load_arg(arg, index))
			continue
		if op == REMAINING:
			stack.append(remaining)
			continue
		if op in (NEG, NOT):
			if not stack:
				raise VmError("unary operator on an empty stack")
			value = stack.pop()
			stack.append(-value if op == NEG else ~value)
			continue

		operator = BINARY.get(op)
		if operator is None:
			raise VmError(f"opcode {op:#04x} is not one this walker knows")
		if len(stack) < 2:
			raise VmError("binary operator without two values")
		right = stack.pop()
		left  = stack.pop()
		stack.append(operator(left, right))
