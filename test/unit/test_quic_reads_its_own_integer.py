"""QUIC's integer, against the numbers its RFCs state (26.919).

`varint_type` had two encodings with a continuation bit and QUIC's has none, so
no length in QUIC or HTTP/3 could be described. Every expected value here is
RFC 9000's or RFC 9001's rather than this tree's: four readers agreeing would
prove nothing about a fifth encoding they were all written from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import fourway
from situc.diagnostics import SituError, Source
from situc.parser import parse
from test_nesting_costs_no_more_than_its_length import pack_text
from test_walker_c import (VERDICT, _build_driver, c_verdict, python_verdict,
                           shape_named_text)

RUNTIME = Path(__file__).resolve().parents[2] / "runtime"

SCHEMA = """target buffer;
endian big;
varint_type q  { encoding = quic; max_bits = 62; }
varint_type qm { encoding = quic; max_bits = 62; minimal; }
varint_type q2 { encoding = quic; max_bits = 14; }
struct one { q v; u8 last; }
struct strict { qm v; u8 last; }
struct small { q2 v; u8 last; }
struct initial {
	u8 flags;
	u32 version;
	u8 dcid_length;
	u8 dcid[dcid_length];
	u8 scid_length;
	u8 scid[scid_length];
	q token_length;
	u8 token[token_length];
	q length;
	u8 rest[length];
}
struct frames {
	tlv items (
		tag_type     = qm,
		tag_decode   = { kind = tag },
		tag_identity = kind,
		value_size   = switch (kind) { default: prefixed(q) },
	);
}
"""

#: RFC 9000, appendix A.1, and the two-byte encoding of 37 it gives as well.
SAMPLES = {
	"c2197c5eff14e88c": (8, 151288809941952652),
	"9d7f3e7d":         (4, 494878333),
	"7bbd":             (2, 15293),
	"25":               (1, 37),
	"4025":             (2, 37),
}

#: RFC 9001, appendix A.2: the client Initial's header, then its 1182 bytes.
INITIAL = bytes.fromhex("c300000001088394c8f03e5157080000449e") + bytes(1182)


def _section(answers: str, struct: str) -> str:
	return answers[answers.index(f"-- {struct}\n"):].split("\n-- ")[0]


def _field(section: str, name: str) -> str:
	return next(line.split(" ", 1)[1] for line in section.splitlines()
	            if line.startswith(name + " "))


def _asked(tmp_path: Path) -> dict[str, list[str]]:
	schema = tmp_path / "unit.situ"
	schema.write_text(SCHEMA, encoding = "ascii")
	return fourway.build(tmp_path, schema)


def _all(command: dict[str, list[str]], message: bytes, struct: str,
		name: str, cwd: Path) -> str:
	given = {backend: _field(_section(fourway.answers(argv, message, cwd),
	                                  struct), name)
	         for backend, argv in command.items()}
	assert len(set(given.values())) == 1, (struct, name, message.hex(), given)
	return given["c"]


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_all_four_read_what_the_rfc_states(tmp_path: Path) -> None:
	command = _asked(tmp_path)
	for sample, (size, value) in SAMPLES.items():
		message = bytes.fromhex(sample) + b"\x07"
		assert _all(command, message, "one", "v", tmp_path) \
			== f"len={size} value={value}"
		assert _all(command, message, "one", "last", tmp_path) == "7"
		assert _all(command, message, "one", "validate", tmp_path) == "0"


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_a_type_of_fourteen_bits_stops_at_two_bytes(tmp_path: Path) -> None:
	command = _asked(tmp_path)
	assert _all(command, bytes.fromhex("7bbd07"), "small", "v", tmp_path) \
		== "len=2 value=15293"
	assert _all(command, bytes.fromhex("9d7f3e7d07"), "small", "validate",
	            tmp_path) == "1"


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_minimal_refuses_the_longer_of_two_spellings(tmp_path: Path) -> None:
	command = _asked(tmp_path)
	assert _all(command, bytes.fromhex("2507"), "strict", "v", tmp_path) \
		== "len=1 value=37"
	assert _all(command, bytes.fromhex("402507"), "strict", "v", tmp_path) \
		== "len=2 value=0"


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_the_client_initial_measures_as_the_rfc_says(tmp_path: Path) -> None:
	command = _asked(tmp_path)
	assert _all(command, INITIAL, "initial", "length", tmp_path) \
		== "len=2 value=1182"
	assert _all(command, INITIAL, "initial", "validate", tmp_path) == "0"
	assert _all(command, INITIAL[:-1], "initial", "validate", tmp_path) == "1"


@pytest.mark.skipif(not fourway.COMPLETE, reason = "needs gcc, g++ and rustc")
def test_a_tag_and_a_length_are_quic_too(tmp_path: Path) -> None:
	"""An HTTP/3 frame is both. `40 04` is a padded type and `minimal` is on
	it, so the walk has to have read it as what it is."""
	command = _asked(tmp_path)
	frame = bytes.fromhex("04" "4003" "aabbcc" "00" "00")
	assert _all(command, frame, "frames", "items", tmp_path) == "count=2"
	assert _all(command, frame, "frames", "validate", tmp_path) == "0"
	assert _all(command, frame[:4], "frames", "validate", tmp_path) == "1"


def test_both_walkers_measure_what_the_backends_do(tmp_path: Path) -> None:
	blob  = pack_text(SCHEMA)
	drive = _build_driver(tmp_path, blob, VERDICT)
	asked = {
		("initial", INITIAL): "0",
		("initial", INITIAL[:-1]): "1",
		("one", bytes.fromhex("c2197c5eff14e88c07")): "0",
		("one", bytes.fromhex("c2197c5eff14e8")): "1",
		("small", bytes.fromhex("7bbd07")): "0",
		("small", bytes.fromhex("9d7f3e7d07")): "1",
		("frames", bytes.fromhex("04" "4003" "aabbcc")): "0",
		("frames", bytes.fromhex("04" "4003" "aabb")): "1",
	}
	for (struct, message), verdict in asked.items():
		at = shape_named_text(SCHEMA, struct)
		assert c_verdict(tmp_path, blob, message, at, drive) == verdict, \
			(struct, message.hex())
		assert python_verdict(blob, message, at) == verdict, \
			(struct, message.hex())


BOUNDARIES = [0, 63, 64, 16383, 16384, (1 << 30) - 1, 1 << 30, (1 << 62) - 1]

C_WRITER = """#include "situ.h"
#include <stdio.h>
int main(void)
{
	static const uint64_t values[] = { VALUES };
	for (unsigned i = 0; i < sizeof values / sizeof *values; i++) {
		uint8_t out[8];
		uint32_t n = situ_varint_quic_put(out, 8u, values[i]);
		for (uint32_t k = 0; k < n; k++) {
			printf("%02x", out[k]);
		}
		printf(" %u\\n", situ_varint_quic_len(values[i]));
	}
	uint8_t one[1];
	printf("%u %u\\n", situ_varint_quic_put(one, 1u, 64u),
	       situ_varint_quic_put(one, 1u, UINT64_C(1) << 62));
	return 0;
}
"""

RUST_WRITER = """#[path = "RT"] mod situ_rt;
fn main() {
	for value in [VALUES] {
		let mut out = [0u8; 8];
		let n = situ_rt::varint_quic_put(&mut out, value);
		let text: String = out[..n].iter().map(|b| format!("{:02x}", b)).collect();
		println!("{} {}", text, situ_rt::varint_quic_len(value));
	}
	let mut one = [0u8; 1];
	println!("{} {}", situ_rt::varint_quic_put(&mut one, 64),
	         situ_rt::varint_quic_put(&mut one, 1 << 62));
}
"""


def _wanted() -> list[str]:
	"""By hand from section 16: the bits that say the length, then the value."""
	lines = []
	for value in BOUNDARIES:
		size = next(n for n, bits in ((1, 6), (2, 14), (4, 30), (8, 62))
		            if value < 1 << bits)
		tag  = {1: 0, 2: 1, 4: 2, 8: 3}[size] << (size * 8 - 2)
		lines.append(f"{(tag | value).to_bytes(size, 'big').hex()} {size}")
	return [*lines, "0 0"]


def test_all_three_runtimes_write_what_section_16_says(tmp_path: Path) -> None:
	import sys
	sys.path.insert(0, str(RUNTIME / "python"))
	from situ_runtime import varint_quic_get, varint_quic_len, varint_quic_put
	lines = []
	for value in BOUNDARIES:
		out = bytearray(8)
		n   = varint_quic_put(out, 0, value)
		assert varint_quic_get(out, 0, 8) == (value, n)
		lines.append(f"{out[:n].hex()} {varint_quic_len(value)}")
	room = bytearray(1)
	lines.append(f"{varint_quic_put(room, 0, 64)}"
	             f" {varint_quic_put(room, 0, 1 << 62)}")
	assert lines == _wanted()

	values = ", ".join(f"{value}u" for value in BOUNDARIES)
	if fourway.HOST_CC is not None:
		source = tmp_path / "w.c"
		source.write_text(C_WRITER.replace("VALUES",
		                  values.replace("u", "ull")), encoding = "ascii")
		subprocess.run([fourway.HOST_CC, f"-I{RUNTIME / 'c'}", str(source),
		                "-o", str(tmp_path / "wc")], check = True)
		said = subprocess.run([str(tmp_path / "wc")], capture_output = True,
		                      text = True, check = True).stdout
		assert said.splitlines() == _wanted()
	if fourway.RUSTC is not None:
		source = tmp_path / "w.rs"
		source.write_text(RUST_WRITER.replace("VALUES",
		                  values.replace("u", "u64")).replace(
		                  "RT", str(RUNTIME / "rust" / "situ_rt.rs")),
		                  encoding = "ascii")
		subprocess.run([fourway.RUSTC, "--edition", "2021", "-A", "warnings",
		                str(source), "-o",
		                str(tmp_path / "wr")], check = True)
		said = subprocess.run([str(tmp_path / "wr")], capture_output = True,
		                      text = True, check = True).stdout
		assert said.splitlines() == _wanted()


@pytest.mark.parametrize("body,wanted", [
	("encoding = quic; max_bits = 20;", "expected 6, 14, 30 or 62"),
	("encoding = quic; max_bits = 64;", "expected 6, 14, 30 or 62"),
	("encoding = quic; max_bits = 62; max_bytes = 8;", "`max_bits` decides it"),
])
def test_a_width_the_first_byte_cannot_say_is_refused(body: str,
		wanted: str) -> None:
	with pytest.raises(SituError) as caught:
		parse(Source("unit.situ", f"varint_type q {{ {body} }}\n"))
	assert wanted in str(caught.value.diagnostic) \
		or wanted in repr(caught.value)
