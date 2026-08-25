"""`[self_as]`: a checksum that covers its own bytes (14.2).

Four formats in `example/` carry the Internet checksum -- IPv4, ICMP, TCP and
UDP -- and none of them could be described until this existed: the sum is
defined over the header *including* the checksum field, taken as zero (RFC
1071), and a tag inside the region it covers was a flat error.

The behaviour is checked where it can be: `test/generated/test_icmp.c` writes
RFC 1071 over the span and the hole these emit, and compares the answer against
one the kernel's own ICMP stack computed. What is here is the language surface.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from situc.codegen.c import generate as generate_c
from situc.codegen.cpp import generate as generate_cpp
from situc.codegen.python import generate as generate_py
from situc.codegen.rust import generate as generate_rs
from situc.diagnostics import SituError
from situc.layout import solve
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT

PREAMBLE = "target buffer;\nendian big;\n"

COVERING = """struct m {
	authenticated hdr {
		u8   type;
		u8   code;
		checksum u8 sum[2] covers(hdr) [self_as = 0];
		u16  identifier;
	}
}
"""


def build(body: str) -> dict[str, str]:
	schema   = parse_text(PREAMBLE + body)
	resolved = resolve(schema, solve(schema))
	return {
		"c":      generate_c(schema, resolved, "unit").header,
		"cpp":    generate_cpp(schema, resolved, "unit").header,
		"python": generate_py(schema, resolved, "unit").module,
		"rust":   generate_rs(schema, resolved, "unit").module,
	}


def rejected(body: str) -> str:
	with pytest.raises(SituError) as caught:
		schema = parse_text(PREAMBLE + body)
		resolve(schema, solve(schema))
	return caught.value.diagnostic.render()


def test_a_tag_inside_its_coverage_is_still_an_error() -> None:
	"""The rule `self_as` is the exception to, unchanged for a tag that does
	not claim to be one."""
	text = rejected(COVERING.replace(" [self_as = 0]", ""))

	assert "is inside the region it covers" in text
	assert "computing it would take its own bytes as input" in text
	assert "`[self_as = 0]`" in text, "the diagnostic should name the way out"


def test_self_as_without_self_coverage_is_an_error() -> None:
	"""An attribute that stands in for nothing. Invariant 12: a declared
	property that cannot arise is worse than silence, and this one would read
	as a claim about how the checksum is computed."""
	text = rejected("""struct m {
	authenticated hdr {
		u8   type;
		u16  identifier;
	}
	checksum u8 sum[2] covers(hdr) [self_as = 0];
}
""")

	assert "is not inside the region it covers" in text
	assert "has nothing to stand in for" in text


def test_the_tag_is_not_covered_by_itself() -> None:
	"""Writing the checksum must not mark the checksum stale, or a caller
	could never clear the bit they were told to clear."""
	schema   = parse_text(PREAMBLE + COVERING)
	resolved = resolve(schema, solve(schema))

	held = resolved.find("m.sum")
	assert held is not None
	assert "sum" not in held.placement.covered_by


#: What each backend calls the two things only the compiler knows.
EMITS = {
	"c":      ("situ_m_sum_self_span", "SITU_M_SUM_SELF_AS"),
	"cpp":    ("sum_self_span", "self_as_sum"),
	"python": ("sum_self_span", "SELF_AS_SUM"),
	"rust":   ("sum_self_span", "SELF_AS_SUM"),
}


@pytest.mark.parametrize("backend", sorted(EMITS))
def test_every_backend_says_where_the_hole_is(backend: str) -> None:
	"""The span alone is not enough: a caller cannot compute the sum without
	knowing which bytes to substitute, and guessing is how an implementation
	ends up agreeing with itself and nothing else."""
	output = build(COVERING)[backend]

	for name in EMITS[backend]:
		assert name in output, f"{backend} does not emit {name}"


#: The same echo reply `test/generated/test_icmp.c` reads, and the same
#: reason: the checksum in it is the kernel's, not this repository's.
REPLY = bytes([0x00, 0x00, 0xFF, 0xF9, 0x00, 0x05, 0x00, 0x01])


def test_python_computes_the_checksum_the_kernel_did(tmp_path: Path) -> None:
	"""Everything above this line reads the generated text, and invariant 22
	is the standing warning about that: a test that greps does not run.

	The C backend's half is run in `test/generated/test_icmp.c`. This is the
	same RFC 1071 loop over the same bytes, through the Python module -- so
	two of the four are executed and the other two are asserted to emit the
	names, which is a weaker thing and is said here rather than assumed.
	"""
	schema   = parse_text(ROOT.joinpath("example/icmp/icmp.situ").read_text())
	resolved = resolve(schema, solve(schema))
	(tmp_path / "unit.py").write_text(
		generate_py(schema, resolved, "unit").module, encoding="ascii")

	if "situ_runtime" not in sys.modules:
		spec = importlib.util.spec_from_file_location(
			"situ_runtime", ROOT / "runtime" / "python" / "situ_runtime.py")
		assert spec is not None and spec.loader is not None
		runtime = importlib.util.module_from_spec(spec)
		sys.modules["situ_runtime"] = runtime
		spec.loader.exec_module(runtime)
	runtime = sys.modules["situ_runtime"]

	sys.path.insert(0, str(tmp_path))
	try:
		spec = importlib.util.spec_from_file_location("unit", tmp_path / "unit.py")
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
	finally:
		sys.path.remove(str(tmp_path))

	view = module.icmp_message.at(runtime.Message(bytearray(REPLY)), 0)
	at, length = view.checksum_covered()
	hole, held = view.checksum_self_span()
	filler     = module.icmp_message.SELF_AS_CHECKSUM

	total = 0
	for index in range(0, length - 1, 2):
		pair = []
		for byte in (at + index, at + index + 1):
			pair.append(filler if hole <= byte < hole + held
			            else view._msg.buffer[byte])
		total += (pair[0] << 8) | pair[1]
	while total >> 16:
		total = (total & 0xFFFF) + (total >> 16)

	assert (~total) & 0xFFFF == (REPLY[2] << 8) | REPLY[3]


@pytest.mark.parametrize("backend", sorted(EMITS))
def test_no_hole_where_the_tag_is_outside(backend: str) -> None:
	output = build("""struct m {
	authenticated hdr {
		u8   type;
		u16  identifier;
	}
	checksum u8 sum[2] covers(hdr);
}
""")[backend]

	assert "self_span" not in output
