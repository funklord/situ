"""situ's accessors against somebody else's implementation of the same format.

Every other test of the generated code compares situ against situ: four
backends against each other (which finds disagreement but not shared error),
the accessors against the capability map, the accessors against arbitrary
bytes. All of them are downstream of one schema written by one person reading
one specification.

This is the one that is not. `oracles.py` has the argument in full; the short
version is that a hand-authored vector and a misread specification fail in the
same direction and agree forever, and an independent implementation does not.

Skips loudly when a tool is absent -- a differential test that quietly becomes
a no-op is worth less than no test, because the suite still reports green.
`test_the_report_names_what_did_not_run` is what makes the skip visible.
"""

from __future__ import annotations

import binascii
import ctypes
import importlib
import itertools
import math
import random
import shutil
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from every_schema import ROOT
from oracles import DRIVERS, LIES, ORACLES, Oracle, have

from situc import ast
from situc.codegen import c as generate_c
from situc.codegen import python as generate_py
from situc.codegen.c import derived as generate_derived
from situc.diagnostics import Source
from situc.kernels import STUFFING_BOUNDS
from situc.layout import solve
from situc.parser import parse
from situc.resolve import resolve


#: Each built module gets a directory of its own, counted rather than named
#: after the schema.
_BUILDS = itertools.count()


def build_module(schema: Path, tmp: Path) -> object:
	"""The Python accessors for one schema, importable.

	Every build lands in a fresh directory and is imported under a fresh
	name. Two versions of one schema in one process is exactly what the
	honest/lying pair below is, and sharing a directory means the second
	import can be served from the first's cached bytecode -- the module
	stays stale, the comparison agrees, and the test that exists to prove a
	mutation is noticed reports that it was not. That cost an hour of
	believing a false alarm, which is cheaper than believing a false green.
	"""
	source   = Source(str(schema), schema.read_text(encoding="ascii"))
	parsed   = parse(source)
	resolved = resolve(parsed, solve(parsed))
	emitted  = generate_py.generate(parsed, resolved, schema.stem)

	where = tmp / f"build{next(_BUILDS)}"
	where.mkdir()
	(where / f"{schema.stem}.py").write_text(emitted.module, encoding="ascii")
	runtime = ROOT / "runtime" / "python" / "situ_runtime.py"
	(where / "situ_runtime.py").write_text(runtime.read_text(encoding="ascii"),
	                                       encoding="ascii")

	sys.path.insert(0, str(where))
	try:
		importlib.invalidate_caches()
		for stale in (schema.stem, "situ_runtime"):
			sys.modules.pop(stale, None)
		return importlib.import_module(schema.stem)
	finally:
		sys.path.remove(str(where))


@pytest.mark.parametrize("oracle", ORACLES, ids=[o.name for o in ORACLES])
def test_situ_agrees_with_an_independent_implementation(
		oracle: Oracle, tmp_path: Path) -> None:
	if not have(oracle.tool):
		pytest.skip(f"no `{oracle.tool}` on PATH; "
		            f"the {oracle.name} oracle did not run")

	corpus, independently, through_situ = DRIVERS[oracle.name]

	bytes_ = corpus(tmp_path)
	assert bytes_, f"{oracle.name}: the oracle produced an empty corpus"

	theirs = independently(bytes_, tmp_path)
	ours   = through_situ(build_module(oracle.schema, tmp_path), bytes_)

	# An empty comparison passes and means nothing. It is reachable here: the
	# network oracles drop frames tshark could not fully dissect, and randpkt
	# truncates, so a bad `-b` could filter every frame away.
	assert theirs, f"{oracle.name}: `{oracle.tool}` reported nothing to compare"
	assert ours, f"{oracle.name}: situ read nothing to compare"

	assert ours == theirs, (
		f"{oracle.name}: situ and `{oracle.tool}` disagree about bytes "
		f"`{oracle.tool}` wrote.\n"
		f"  situ:            {ours}\n"
		f"  {oracle.tool}:   {theirs}\n"
		f"\n{oracle.why}")


@pytest.mark.parametrize(
	"oracle", [o for o in ORACLES if o.name in LIES],
	ids=[o.name for o in ORACLES if o.name in LIES])
def test_each_oracle_notices_a_schema_that_lies(
		oracle: Oracle, tmp_path: Path) -> None:
	"""Every oracle, not just one: break its schema and require a red result.

	Two adjacent members are swapped, so the fields all still exist and only
	their offsets move. Anything genuinely reading bytes notices; anything
	that had quietly stopped comparing does not.
	"""
	if not have(oracle.tool):
		pytest.skip(f"no `{oracle.tool}` on PATH")

	corpus, independently, through_situ = DRIVERS[oracle.name]
	honest_text, lying_text = LIES[oracle.name]

	honest = oracle.schema.read_text(encoding="ascii")
	assert honest_text in honest, (
		f"{oracle.name}: the schema no longer contains the members this "
		f"test swaps; the mutation in LIES needs updating")

	broken = tmp_path / oracle.schema.name
	broken.write_text(honest.replace(honest_text, lying_text), encoding="ascii")

	bytes_ = corpus(tmp_path)
	theirs = independently(bytes_, tmp_path)

	# Refusing to read at all counts as noticing: a swap can move a member
	# past the frame, and an accessor that reports that is doing its job.
	# What must not happen is agreement.
	try:
		ours = through_situ(build_module(broken, tmp_path), bytes_)
	except Exception:                                  # noqa: BLE001
		return

	assert ours != theirs, (
		f"{oracle.name}: the oracle agreed with a schema whose members are "
		f"swapped, so it is not comparing what it claims to compare")


# -- generated computation, not generated layout ------------------------------
#
# Every oracle above checks where the bytes are. This one checks what situ
# *computes* from them: `gen-derived` emits CRC implementations from a kernel
# description -- a 256-entry table it calculates rather than copies -- and
# situ's own property tests read that same description, so a table built from
# a mistranscribed polynomial would agree with them forever.
#
# This existed and was deleted on 2026-08-04 by 17724a0, a commit about network
# oracles whose message never mentions it. The imports it used stayed behind,
# so nothing went red: an unused import is not a failure, and the only outside
# check on any derived codec vanished under a green suite. That is what
# `test_every_polynomial_codec_is_checked_or_excused` exists to prevent a
# second time.
#
# Two kinds of evidence here, and they are not equally strong.
#
# `zlib.crc32` and `binascii.crc_hqx` are independent *implementations* that
# ship with Python, both old enough and used enough that disagreement means
# situ is wrong. That is the real oracle, and it reaches two codecs.
#
# A published check value -- what a CRC produces for "123456789" -- is weaker,
# and saying so matters: it comes from the same catalogue the kernel
# parameters were transcribed from, so it is not a second implementation and
# must not be counted as one. What it does catch is the transcription, which
# is the failure that actually happens: a wrong poly, init, xorout or reflect
# gives a different check value. It is the only outside evidence available for
# the five CRCs the standard library does not implement.
#
# Measured, so that the strength of both is known rather than assumed:
# flipping one bit of crc32's polynomial disagrees with zlib at 8 of the 9
# lengths below. The exception is the empty input, where the result is
# init ^ xorout and the table never runs.

CRC_CASES = (
	("crc32", "situ_crc32", ctypes.c_uint32,
	 lambda data: zlib.crc32(data)),
	("crc16_ccitt", "situ_crc16_ccitt", ctypes.c_uint16,
	 lambda data: binascii.crc_hqx(data, 0xFFFF)),
)

#: Each CRC's published check value: its output over the nine bytes
#: "123456789", which is how the catalogue identifies a parameter set.
CRC_CHECK_VALUES = {
	"crc8_smbus":   (ctypes.c_uint8,  0xF4),
	"crc16_ccitt":  (ctypes.c_uint16, 0x29B1),
	"crc16_modbus": (ctypes.c_uint16, 0x4B37),
	"crc24_ble":    (ctypes.c_uint32, 0xC25A56),
	"crc32":        (ctypes.c_uint32, 0xCBF43926),
	"crc32c":       (ctypes.c_uint32, 0xE3069283),
	"crc40_gsm":    (ctypes.c_uint64, 0xD4164FC646),
	"crc8_maxim":   (ctypes.c_uint8,  0xA1),
	"crc16_xmodem": (ctypes.c_uint16, 0x31C3),
	"crc16_kermit": (ctypes.c_uint16, 0x2189),
	"crc16_usb":    (ctypes.c_uint16, 0xB4C8),
	"crc32_bzip2":  (ctypes.c_uint32, 0xFC891918),
	"crc64_xz":     (ctypes.c_uint64, 0x995DC9BBDF1939FA),
}

#: A polynomial codec neither oracle reaches, and why. Being named here is a
#: decision a reader can see and argue with; being in none of the three is the
#: silence the guard below refuses.
CRC_UNCHECKED = {
	"reed_solomon_255_223": "a block code rather than a CRC -- it has no "
	                        "check value and its own encode/decode shape",
	"reed_solomon_64_56":   "as reed_solomon_255_223",
}


@pytest.fixture(scope="module")
def kernel_library(tmp_path_factory: pytest.TempPathFactory) -> ctypes.CDLL:
	"""Build the derived codecs of `std/kernels.situ` into a shared object.

	Built once for the module rather than once per case: the nine checks
	below all read the same standard kernels, and a compile each was most
	of what they cost.

	Skips loudly without a compiler rather than passing: a differential
	test that quietly becomes a no-op is worth less than no test at all.
	"""
	compiler = shutil.which("cc") or shutil.which("gcc")
	if compiler is None:
		pytest.skip("no C compiler; the CRC oracle did not run")

	tmp = tmp_path_factory.mktemp("kernels")

	kernels = ROOT / "std" / "kernels.situ"
	source  = Source(str(kernels), kernels.read_text(encoding="ascii"))
	parsed  = parse(source)
	solved  = resolve(parsed, solve(parsed))

	(tmp / "kernels.h").write_text(
		generate_c.generate(parsed, solved, "kernels").header, encoding="ascii")
	(tmp / "derived.c").write_text(
		generate_derived.generate(parsed, "kernels"), encoding="ascii")

	shared = tmp / "kernels.so"
	built  = subprocess.run(
		[compiler, "-O2", "-shared", "-fPIC",
		 "-I", str(ROOT / "runtime" / "c"), "-I", str(tmp),
		 str(tmp / "derived.c"), "-o", str(shared)],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr

	return ctypes.CDLL(str(shared))


def _crc(lib: ctypes.CDLL, name: str, ctype: type) -> object:
	"""One generated CRC, bound with its real signature."""
	fn = getattr(lib, f"situ_{name}")
	fn.restype  = ctype
	fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32]
	return fn


@pytest.mark.parametrize(
	"name,symbol,ctype,independently", CRC_CASES,
	ids=[case[0] for case in CRC_CASES])
def test_a_generated_crc_matches_an_independent_implementation(
		name: str, symbol: str, ctype: type, independently: object,
		kernel_library: ctypes.CDLL) -> None:
	"""What situ computes, against what the standard library computes.

	The kernel description says width, polynomial, initial value and whether
	the input is reflected; situ turns that into a 256-entry table and a loop.
	A table generated from a wrong polynomial is wrong consistently, so situ's
	own tests -- which read the same description -- would agree with it.
	"""
	fn = _crc(kernel_library, name, ctype)

	# Lengths around the boundaries a table-driven CRC gets wrong: empty, a
	# single byte, and either side of a 256-byte table wrap.
	random.seed(20260804)
	for length in (0, 1, 2, 15, 64, 255, 256, 257, 1024):
		data = bytes(random.randrange(256) for _ in range(length))
		buf  = (ctypes.c_uint8 * max(1, length))(*data)

		assert fn(buf, length) == independently(data), (  # type: ignore[operator]
			f"{name}: situ and the standard library disagree at {length} bytes")


@pytest.mark.parametrize("name", sorted(CRC_CHECK_VALUES))
def test_a_generated_crc_produces_its_published_check_value(
		name: str, kernel_library: ctypes.CDLL) -> None:
	"""Every CRC here against the value its catalogue entry publishes.

	This is the transcription check, and it is the only outside evidence for
	the five the standard library does not implement. A parameter copied
	wrongly out of the catalogue -- the polynomial, the initial value, the
	final xor, the reflection -- lands on a different check value.
	"""
	ctype, expected = CRC_CHECK_VALUES[name]
	fn = _crc(kernel_library, name, ctype)

	data = b"123456789"
	buf  = (ctypes.c_uint8 * len(data))(*data)
	got  = fn(buf, len(data))                             # type: ignore[operator]

	assert got == expected, (
		f"{name}: situ computes {got:#x} over \"123456789\" where the "
		f"catalogue publishes {expected:#x}, so a kernel parameter in "
		f"std/kernels.situ does not say what it was meant to say")


#: An additive scrambler whose polynomial is not primitive, and why that is
#: not a defect. A multiplicative register is fed from the ciphertext rather
#: than running free, so its cycle length is not a property of the taps alone
#: and the check below does not apply to it.
NON_MAXIMAL = {
	"scrambler_multiplicative": "multiplicative: the register is fed from the "
	                            "output, so it does not run free",
}

#: Registers too wide to walk two periods of inside a unit test. Named rather
#: than skipped by a width comparison alone, so that adding a wide scrambler
#: is a decision somebody sees: a cap that drops work silently reads exactly
#: like coverage.
TOO_WIDE = {"prbs23"}


def test_every_additive_scrambler_has_maximal_period(
		kernel_library: ctypes.CDLL) -> None:
	"""A primitive polynomial visits every non-zero state, and a typo does not.

	This is the scrambler equivalent of a CRC's published check value, and it
	is better evidence: the period is a mathematical property of the taps
	rather than a number somebody wrote down, so nothing here was copied from
	the same place the parameters were.

	It is measured from the generated C -- stepping the emitted encoder over a
	run of zero bytes recovers the keystream, and the keystream of an additive
	scrambler is the register's own sequence. A period short of 2^n - 1 means
	the polynomial is reducible, which for a mistyped tap is the usual
	outcome: of the four protocol polynomials added with this test, every one
	was confirmed maximal before it was written down, and the encoding rule
	itself was checked against `scrambler_additive`, whose 0xB400 was already
	here.

	`scrambler_multiplicative` is excluded with its reason. Its taps are the
	Galois form of x^16 + x^12 + x^5 + 1 -- the CRC-CCITT polynomial, which is
	not primitive and gives 32767 rather than 65535. That is correct for a
	self-synchronising scrambler and would be a defect in a free-running one,
	which is the whole distinction this test is keyed on.
	"""
	kernels = ROOT / "std" / "kernels.situ"
	parsed  = parse(Source(str(kernels), kernels.read_text(encoding="ascii")))

	additive: dict[str, int] = {}
	for codec in parsed.codecs():
		if codec.kernel is None:
			continue
		if codec.kernel.family is not ast.KernelFamily.SHIFT:
			continue
		source = codec.kernel.argument("feedback")
		if not isinstance(source, ast.NameRef) or source.name != "input":
			continue
		# 16 is the family's own default, and a `width` that is not a literal
		# would be a schema this test cannot size rather than one it may
		# guess at.
		declared = codec.kernel.argument("width")
		if declared is None:
			additive[codec.name] = 16
			continue
		assert isinstance(declared, ast.IntLiteral), (
			f"{codec.name}: `width` is not a literal, so the period this "
			f"test would check is not one it can compute")
		additive[codec.name] = declared.value

	assert additive, (
		"no additive shift-register codec found in std/kernels.situ -- this "
		"is reading the wrong thing, and an empty set passes as loudly as a "
		"real one")

	for name, width in sorted(additive.items()):
		if name in NON_MAXIMAL:
			continue

		fn = getattr(kernel_library, f"situ_{name}_encode")
		fn.restype  = ctypes.c_uint32
		fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
		               ctypes.POINTER(ctypes.c_uint8)]

		# A run of zeroes through an additive scrambler is the keystream, and
		# the keystream repeats exactly when the register does. Two periods'
		# worth of bits, capped so a wide register does not run the suite.
		full  = (1 << width) - 1
		if name in TOO_WIDE:
			continue
		assert full <= (1 << 17), (
			f"{name} is {width} bits, so walking two periods here would run "
			f"the suite for {full * 2} bits. Add it to TOO_WIDE, which is a "
			f"visible gap rather than a silent one")
		bytes_needed = (full * 2 + 7) // 8 + 1
		zeroes = (ctypes.c_uint8 * bytes_needed)()
		out    = (ctypes.c_uint8 * bytes_needed)()
		fn(zeroes, bytes_needed, out)

		stream = "".join(f"{out[i]:08b}"[::-1] for i in range(bytes_needed))
		head   = stream[:full]
		assert stream[full:full * 2] == head, (
			f"{name}: the keystream does not repeat with period {full}, so "
			f"the taps are not the primitive polynomial they were meant to be")
		assert head != "0" * full, f"{name}: the keystream is all zeroes"
		assert len(set(head)) == 2, f"{name}: the keystream is constant"

	unchecked = sorted(TOO_WIDE - set(additive))
	assert not unchecked, (
		f"{unchecked}: named too wide to walk, and not an additive scrambler "
		f"in std/kernels.situ at all -- the exclusion outlived what it was for")


def test_nrzi_transitions_on_a_one_and_holds_on_a_zero(
		kernel_library: ctypes.CDLL) -> None:
	"""NRZI's defining behaviour, asked of it without restating its algorithm.

	`nrzi_transition_on_one` is a one-bit multiplicative register: the output
	bit is the input bit exclusive-ored with the previous output bit. Nothing
	implements it specially, which is the interesting part -- it is the
	scrambler family with the width turned down as far as it goes, and the
	documentation had it filed under table codes, where a stateless symbol
	map cannot express a differential rule at all.

	The assertions are the two facts a standard states rather than the rule a
	generator would be written from: all ones transitions on every bit, and
	all zeroes holds. A generator that had accidentally emitted the identity,
	or the inversion, or a stateless table, fails both.
	"""
	fn = getattr(kernel_library, "situ_nrzi_transition_on_one_encode")
	fn.restype  = ctypes.c_uint32
	fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
	               ctypes.POINTER(ctypes.c_uint8)]

	def encode(data: bytes) -> bytes:
		buf = (ctypes.c_uint8 * len(data))(*data)
		out = (ctypes.c_uint8 * len(data))()
		fn(buf, len(data), out)
		return bytes(out)

	# Every input bit a one, so the level flips on every bit: 0101...
	# from a zero seed, which is 0x55 per byte with bit 0 sent first.
	assert encode(b"\xff" * 4) == b"\x55" * 4, encode(b"\xff" * 4).hex()

	# Every input bit a zero, so the level never moves and stays at the seed.
	assert encode(b"\x00" * 4) == b"\x00" * 4, encode(b"\x00" * 4).hex()

	# And the round trip, which is the weakest of the three and included
	# because a decoder shifting in what it made rather than what it received
	# passes the two above and fails this.
	back = getattr(kernel_library, "situ_nrzi_transition_on_one_decode")
	back.restype  = ctypes.c_uint32
	back.argtypes = fn.argtypes
	data = bytes(range(64))
	coded = encode(data)
	buf = (ctypes.c_uint8 * len(coded))(*coded)
	out = (ctypes.c_uint8 * len(coded))()
	back(buf, len(coded), out)
	assert bytes(out) == data


def test_usb_nrzi_transitions_on_a_zero_and_holds_on_a_one(
		kernel_library: ctypes.CDLL) -> None:
	"""The other convention, which is the one on every USB cable.

	USB 2.0 section 7.1.8: a one is sent as no change in the level and a zero
	as a change. That is the exact opposite of the codec above, and the
	assertions here are the same two facts with the inputs swapped -- all
	zeroes now flips on every bit, all ones now holds.

	The pair is the point. Each codec passes its own two assertions and fails
	the other's, which is what a name has to distinguish: a receiver built on
	the wrong convention returns the complement of what was sent, with
	nothing at run time to notice. Checking either alone would not show that
	`complement_feedback` reaches the generated code at all -- an emitter
	that dropped the flag produces the transition-on-one codec twice, and
	only the cross-check below can see it.
	"""
	def run(codec: str, direction: str, data: bytes) -> bytes:
		fn = getattr(kernel_library, f"situ_{codec}_{direction}")
		fn.restype  = ctypes.c_uint32
		fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
		               ctypes.POINTER(ctypes.c_uint8)]

		buf = (ctypes.c_uint8 * len(data))(*data)
		out = (ctypes.c_uint8 * len(data))()
		fn(buf, len(data), out)
		return bytes(out)

	usb = "nrzi_transition_on_zero"

	# Every input bit a zero, so the level flips on every bit: 0101... from a
	# zero seed, which is 0x55 per byte with bit 0 sent first.
	assert run(usb, "encode", b"\x00" * 4) == b"\x55" * 4

	# Every input bit a one, so the level never moves and stays at the seed.
	assert run(usb, "encode", b"\xff" * 4) == b"\x00" * 4

	# The two codecs are opposites rather than the same generator emitted
	# twice, and the relation between them is not the complement it looks
	# like. Writing o for transition-on-one and u for transition-on-zero:
	# u(0) = o(0) ^ 1, and thereafter each extra inversion cancels the one
	# before it, so u(n) = o(n) ^ 1 for even n and u(n) = o(n) for odd. The
	# state carries across bytes and a byte starts on an even bit, so every
	# byte differs by 0x55 with bit 0 sent first -- which is also what the
	# two assertions above say, read against each other.
	hdlc = "nrzi_transition_on_one"
	data = bytes(range(64))
	ours   = run(usb, "encode", data)
	theirs = run(hdlc, "encode", data)
	assert ours == bytes(byte ^ 0x55 for byte in theirs), ours.hex()

	# And the round trip, which a decoder that dropped the complement passes
	# nothing else here would catch: it would decode its own encoder happily.
	assert run(usb, "decode", ours) == data


#: What each generated stuffing encoder does, measured rather than declared.
#:
#: Per code: an input that makes it expand as far as it can and the count of
#: units that is -- bits for HDLC, whose lengths are in bits, and bytes for
#: the rest -- then the ratio it follows and the constant it adds on top.
#: Each input is the case its own comment in `std/kernels.situ` names: a
#: payload of nothing but the delimiter, a run with no zero to point past, a
#: body of nothing but dot lines.
STUFFING_MEASURED: dict[str, tuple[bytes, int, tuple[int, int], int]] = {
	"cobs":      (b"\x01" * 254, 254, (255, 254), 1),
	"hdlc":      (b"\xff" * 5,    40, (6, 5),     0),
	"ppp_async": (b"\x7e" * 16,   16, (2, 1),     1),
	"slip":      (b"\xc0" * 16,   16, (2, 1),     1),
	"smtp_dot":  (b".\r\n" * 16,  48, (4, 3),     0),
	"usb":       (b"\xff" * 3,    24, (7, 6),     0),
}

#: The codes whose generated encoder appends a frame delimiter, so that its
#: output is one byte longer than the `ratio_bounded` its signature declares.
#:
#: Named here rather than repaired, because the repair is not one thing. The
#: signature could gain the constant -- the layout arithmetic already carries
#: a ratio with an addend, `_expand` in `layout.py`, for a pipeline that
#: appends parity before expanding -- or the delimiter could be held to be
#: framing rather than the codec's, since section 20.3 makes framing its own
#: concept and a COBS block in the literature does not carry it. The two lead
#: to different code and the choice is not this guard's to make; what is not
#: acceptable is that neither is chosen and the map keeps saying the shorter
#: number. See project.md 26.147.
DELIMITER_NOT_IN_THE_SIGNATURE = frozenset({"cobs", "ppp_async", "slip"})


def test_every_stuffing_code_expands_by_the_amount_it_is_measured_at(
		kernel_library: ctypes.CDLL) -> None:
	"""`ratio_bounded(w, p)` is a promise, so it is measured, not trusted.

	The declared pair is the codec's signature and is what a consumer sizes a
	receive buffer from; the generated implementation has its own constants.
	Nothing held the two together until `kernels.STUFFING_BOUNDS` did, and
	that table is itself a written-down claim -- so it is checked here against
	what the emitted encoder does, by feeding each code the input its own
	schema comment calls the worst case and measuring what comes back.

	It found the delimiter immediately. Three of the five write one byte more
	than their ratio predicts, which is a consumer's buffer overrunning by one
	on every encode, and every committed schema declares the ratio the table
	does -- so nothing in the suite could have caught it from the declaration
	side. That is why this measures the object file instead.
	"""
	kernels = ROOT / "std" / "kernels.situ"
	parsed  = parse(Source(str(kernels), kernels.read_text(encoding="ascii")))

	# code name -> codec name, from the schema, so a sixth stuffing code is
	# covered the day it is declared rather than the day somebody adds a row.
	codecs: dict[str, str] = {}
	for codec in parsed.codecs():
		if codec.kernel is None:
			continue
		if codec.kernel.family is not ast.KernelFamily.STUFFING:
			continue
		named = codec.kernel.argument("code")
		code  = getattr(named, "name", None)
		# A stuffing kernel naming no code would drop out of the population
		# silently, and a guard over a short population passes as loudly as
		# one over a whole one.
		assert isinstance(code, str), f"{codec.name} names no stuffing code"
		codecs[code] = codec.name

	assert set(codecs) == set(STUFFING_MEASURED) == set(STUFFING_BOUNDS), (
		f"std/kernels.situ declares {sorted(codecs)}, the bounds table holds "
		f"{sorted(STUFFING_BOUNDS)} and this file measures "
		f"{sorted(STUFFING_MEASURED)}; one of the three has moved")

	for code, name in codecs.items():
		data, units, ratio, adds = STUFFING_MEASURED[code]
		worst, per = ratio

		assert STUFFING_BOUNDS[code] == ratio, (
			f"{code}: the bounds table says {STUFFING_BOUNDS[code]} and the "
			f"generated encoder follows {ratio}")

		fn = getattr(kernel_library, f"situ_{name}_encode")
		fn.restype  = ctypes.c_uint32
		fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
		               ctypes.POINTER(ctypes.c_uint8)]

		buf = (ctypes.c_uint8 * len(data))(*data)
		out = (ctypes.c_uint8 * (len(data) * worst // per + 8))()
		written = fn(buf, units, out)

		assert written == units * worst // per + adds, (
			f"{name}: measured {written} out for {units} in, and "
			f"{worst} for {per} plus {adds} predicts "
			f"{units * worst // per + adds}")

		# The constant is what the signature does not carry, so which codes
		# have one is asserted rather than merely recorded: a sixth code that
		# appends a delimiter joins the defect and must join the list.
		assert (adds > 0) == (code in DELIMITER_NOT_IN_THE_SIGNATURE), (
			f"{code}: adds {adds} bytes beyond its ratio and is "
			f"{'not ' if code not in DELIMITER_NOT_IN_THE_SIGNATURE else ''}"
			f"listed as one that does")


def test_the_two_bit_stuffings_differ_at_the_run_that_names_them(
		kernel_library: ctypes.CDLL) -> None:
	"""Five ones is HDLC's trigger and not USB's, which is the whole of it.

	One algorithm and one constant, so a generator that ignored the constant
	emits the same function twice and each copy passes its own round trip.
	The discriminating input is a run of exactly five: HDLC must insert a
	zero after it and USB must not, and the outputs differ in length as well
	as content, so neither a length check nor a content check alone is being
	relied on.

	Bits are read back MSB first, which is the order `situ_bits_set_msb`
	writes them and the order both codes count runs in.
	"""
	def stuff(codec: str, bits: str) -> str:
		data = bits.ljust((len(bits) + 7) // 8 * 8, "0")
		buf  = (ctypes.c_uint8 * (len(data) // 8))(
			*(int(data[at:at + 8], 2) for at in range(0, len(data), 8)))
		out  = (ctypes.c_uint8 * (len(data) // 8 + 4))()

		fn = getattr(kernel_library, f"situ_{codec}_encode")
		fn.restype  = ctypes.c_uint32
		fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
		               ctypes.POINTER(ctypes.c_uint8)]
		written = fn(buf, len(bits), out)

		whole = "".join(f"{byte:08b}" for byte in out)
		return whole[:written]

	hdlc = "hdlc_bit_stuffing"
	usb  = "usb_bit_stuffing"

	# Five ones and then a zero. HDLC has already stuffed by the time the
	# zero arrives; USB has not, and never will on this input.
	assert stuff(hdlc, "11111000") == "111110000"
	assert stuff(usb,  "11111000") == "11111000"

	# Six ones, which is where USB acts and where HDLC has acted once
	# already. Both grow by one bit and they put the bit in different places.
	assert stuff(hdlc, "11111111") == "111110111"
	assert stuff(usb,  "11111111") == "111111011"

	# And the round trip, per code, over a run long enough to stuff twice.
	for codec in (hdlc, usb):
		bits = "1" * 24
		coded = stuff(codec, bits)
		buf = (ctypes.c_uint8 * ((len(coded) + 7) // 8))(
			*(int(coded.ljust((len(coded) + 7) // 8 * 8, "0")[at:at + 8], 2)
			  for at in range(0, (len(coded) + 7) // 8 * 8, 8)))
		out = (ctypes.c_uint8 * 8)()

		back = getattr(kernel_library, f"situ_{codec}_decode")
		back.restype  = ctypes.c_uint32
		back.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
		                 ctypes.POINTER(ctypes.c_uint8)]
		written = back(buf, len(coded), out)

		whole = "".join(f"{byte:08b}" for byte in out)
		assert whole[:written] == bits, f"{codec}: {whole[:written]}"


def test_every_table_codec_encodes_by_the_ratio_it_declares(
		kernel_library: ctypes.CDLL) -> None:
	"""The declared ratio measured against the generated encoder, and the
	partial symbol nobody was refusing.

	A `table` kernel comes in two shapes and they count in different units:
	an exact-ratio code walks bits, and a padded one walks whole input bytes
	into whole output groups. `traverse.decode_counts_bits` exists because
	getting that wrong was a buffer overrun (26.35); what nothing measured is
	whether the ratio itself is what comes out.

	It is, in every case, for a length that is a whole number of units. What
	was not is a length that is not. `base16` handed seven bits encoded four
	of them and returned 8 -- a successful-looking answer, three bits gone,
	and a generated comment promising `bits * 8 / 4` which for seven bits is
	14. Manchester's decoder dropped an odd trailing bit the same way. Both
	refuse now, as `interleave_16` already did for a partial block, and the
	generated comment says so.
	"""
	kernels = ROOT / "std" / "kernels.situ"
	parsed  = parse(Source(str(kernels), kernels.read_text(encoding="ascii")))

	def call(codec: str, direction: str, data: bytes, units: int) -> int:
		fn = getattr(kernel_library, f"situ_{codec}_{direction}")
		fn.restype  = ctypes.c_uint32
		fn.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
		               ctypes.POINTER(ctypes.c_uint8)]
		buf = (ctypes.c_uint8 * max(len(data), 1))(*data)
		out = (ctypes.c_uint8 * 512)()
		return int(fn(buf, units, out))

	tables = [decl for decl in parsed.codecs()
	          if decl.kernel is not None
	          and decl.kernel.family is ast.KernelFamily.TABLE]
	assert len(tables) >= 8, (
		f"{len(tables)} table codecs found in std/kernels.situ; this guard is "
		f"reading the wrong thing, and a short population passes as loudly "
		f"as a whole one")

	for decl in tables:
		kernel = decl.kernel
		assert kernel is not None
		inputs  = getattr(kernel.argument("input_bits"), "value", 0)
		outputs = getattr(kernel.argument("output_bits"), "value", 0)
		assert inputs and outputs, decl.name

		if kernel.argument("pad") is not None:
			# Padded: whole input *bytes* into whole output groups, where a
			# group is the shortest run that is both a whole number of bytes
			# and a whole number of symbols. The declaration says
			# `ratio_padded`, and this is what that has to mean.
			group  = math.lcm(inputs, 8) // 8
			result = group * 8 // inputs
			for length in (1, group - 1, group, group + 1, group * 3):
				if length < 1:
					continue
				expected = -(-length // group) * result
				assert call(decl.name, "encode", b"\x5a" * length,
				            length) == expected, (
					f"{decl.name}: {length} bytes in should pad to "
					f"{expected} out")
			continue

		# Exact: bits in, bits out, and the ratio holds exactly.
		bits = inputs * 4
		coded = call(decl.name, "encode", b"\x5a" * 8, bits)
		assert coded == bits * outputs // inputs, (
			f"{decl.name}: {bits} bits in gave {coded} out, and the declared "
			f"{outputs}:{inputs} predicts {bits * outputs // inputs}")

		# A length that is not a whole number of symbols has no encoding, and
		# encoding part of it and reporting success is the shape this test
		# was written to close.
		if inputs > 1:
			assert call(decl.name, "encode", b"\x5a" * 8, bits + 1) == 0, (
				f"{decl.name}: encoded {bits + 1} bits, which is "
				f"{bits + 1} % {inputs} = {(bits + 1) % inputs} short of a "
				f"whole symbol")

		# The same in the other direction, over the encoder's own output so
		# that a refusal cannot be an undefined symbol instead.
		buf = (ctypes.c_uint8 * 8)(*(b"\x5a" * 8))
		out = (ctypes.c_uint8 * 512)()
		enc = getattr(kernel_library, f"situ_{decl.name}_encode")
		enc.restype  = ctypes.c_uint32
		enc.argtypes = [ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
		                ctypes.POINTER(ctypes.c_uint8)]
		enc(buf, bits, out)

		dec = getattr(kernel_library, f"situ_{decl.name}_decode")
		dec.restype  = ctypes.c_uint32
		dec.argtypes = enc.argtypes
		back = (ctypes.c_uint8 * 512)()
		assert int(dec(out, coded, back)) == bits, decl.name
		assert int(dec(out, coded - 1, back)) == 0, (
			f"{decl.name}: decoded {coded - 1} bits, which is a truncated "
			f"final symbol")


def test_every_polynomial_codec_is_checked_or_excused() -> None:
	"""No generated CRC joins the standard kernels unchecked and unremarked.

	The section above was deleted once without anybody noticing, because
	nothing asserted that it was still there. This reads the schema rather
	than a list of its own, so a tenth polynomial codec is covered the moment
	it is added -- and deleting the cases fails here rather than quietly.
	"""
	kernels = ROOT / "std" / "kernels.situ"
	parsed  = parse(Source(str(kernels), kernels.read_text(encoding="ascii")))

	polynomial = {codec.name for codec in parsed.codecs()
	              if codec.kernel is not None
	              and codec.kernel.family is ast.KernelFamily.POLYNOMIAL}
	assert polynomial, (
		"no polynomial codec found in std/kernels.situ -- this guard is "
		"reading the wrong thing, and an empty set passes exactly as loudly "
		"as a real one")
	assert CRC_CASES, "CRC_CASES is empty, so no generated CRC is checked"

	covered = ({case[0] for case in CRC_CASES} | set(CRC_CHECK_VALUES)
	           | set(CRC_UNCHECKED))
	missing = polynomial - covered
	assert not missing, (
		f"{sorted(missing)}: a generated CRC that nothing outside this "
		f"project checks. Add it to CRC_CASES if the standard library "
		f"implements it, to CRC_CHECK_VALUES with its published check value, "
		f"or to CRC_UNCHECKED with the reason it can have neither")


def test_the_corpus_is_not_this_project_s_opinion() -> None:
	"""The point of the whole file, asserted rather than trusted.

	If a corpus function ever starts returning bytes written here instead of
	bytes the third-party tool wrote, the test above keeps passing and stops
	meaning anything -- it would be comparing a schema against a vector again,
	with extra steps.
	"""
	import inspect

	import oracles

	# The ways a corpus may legitimately be produced: run a tool, delegate to
	# a helper that runs one, or call a third-party library. Anything else is
	# bytes chosen in this file, which is the thing that must not happen.
	#
	# `_pymodbus(` is here because Modbus's independent implementation is a
	# library rather than a command. The rule is "not written here", not
	# "spawned a process".
	elsewhere = ("subprocess", "_run(", "_randpkt(", "_pymodbus(",
	             "_paho_packets(")

	for oracle in ORACLES:
		corpus = DRIVERS[oracle.name][0]
		body   = inspect.getsource(corpus)
		assert any(mark in body for mark in elsewhere), (
			f"{oracle.name}: the corpus is not produced by `{oracle.tool}`")
	assert oracles.__doc__ is not None


def test_the_report_names_what_did_not_run(
		capsys: pytest.CaptureFixture[str]) -> None:
	"""A skipped oracle is a fact about this machine, and it is printed.

	`working-practice.md`: a passing check is not evidence until you know it
	checked something. A differential suite where every oracle skipped reports
	exactly as green as one where they all ran, so the count goes to stdout
	where `-s` and a CI log will carry it.
	"""
	ran     = [o.name for o in ORACLES if have(o.tool)]
	skipped = [f"{o.name} (no `{o.tool}`)" for o in ORACLES if not have(o.tool)]

	print(f"\ndifferential oracles: {len(ran)} ran"
	      f"{', ' + ', '.join(ran) if ran else ''}")
	if skipped:
		print(f"                      {len(skipped)} skipped: "
		      f"{', '.join(skipped)}")

	assert ran or skipped

	# The codec checks are the other half of this file and were not in this
	# report, which is the same shape as the fault it exists to prevent: they
	# all go through `kernel_library`, that fixture skips without a compiler,
	# and a machine with no `cc` reported a green suite in which nothing had
	# checked a single generated codec. Counted from the registries rather
	# than written down, so the number cannot go stale.
	kernels = ROOT / "std" / "kernels.situ"
	parsed  = parse(Source(str(kernels), kernels.read_text(encoding="ascii")))
	shifts  = sum(1 for codec in parsed.codecs()
	              if codec.kernel is not None
	              and codec.kernel.family is ast.KernelFamily.SHIFT)
	codec_checks = len(CRC_CASES) + len(CRC_CHECK_VALUES) + shifts

	if shutil.which("cc") or shutil.which("gcc"):
		print(f"codec checks:         {codec_checks} ran against the built "
		      f"kernel library")
	else:
		print(f"codec checks:         {codec_checks} SKIPPED -- no C "
		      f"compiler, so no generated codec was checked by anything")

	assert codec_checks > 0, (
		"the registries this counts are empty, so the report would announce "
		"coverage that does not exist")


def test_the_oracles_scratch_goes_away(tmp_path: Path) -> None:
	"""A run must leave no scratch directory behind, and the count is the
	assertion rather than the presence of a cleanup call.

	`oracles.py` used `tempfile.mkdtemp` with no `rmtree`, no `finally` and
	no `TemporaryDirectory`, and the three entry points that take no
	`tmp_path` to hand down leaked one directory each per run: 1591 of them
	accumulated over a fortnight, under a green suite. A cleanup that
	removes nothing looks exactly like one with nothing to remove, so what
	is checked here is what the run *left*, not what the source appears to
	do.

	A subprocess, because the directory goes when the interpreter does:
	inside one run the scratch is still legitimately present. The child
	prints the path it made and exits; a path that still exists after that
	is a leak.
	"""
	child = tmp_path / "child.py"
	child.write_text(
		"import sys\n"
		f"sys.path.insert(0, {str(Path(__file__).parent)!r})\n"
		"import oracles\n"
		"print(oracles._scratch('oracle-'))\n",
		encoding="ascii")

	ran = subprocess.run([sys.executable, str(child)],
	                     capture_output=True, text=True, timeout=120)
	assert ran.returncode == 0, ran.stderr

	made = Path(ran.stdout.strip())
	assert made.name.startswith("oracle-"), ran.stdout
	assert not made.exists(), (
		f"{made} outlived the process that made it: the scratch leaks")
