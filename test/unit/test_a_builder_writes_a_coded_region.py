"""A builder writes a coded region from its content (26.998).

Every builder refused a coded region: "the bytes on the wire are the codec's
output and not the ones a caller would hand over". The caller hands over the
CONTENT and the writer encodes it, which is the division `_escaped` already
makes for an escape byte. It needs a derived stuffing code whose encoder situ
generates, one delimiter that code protects, and an interior of one byte run.

The four builders are held to each other by `test_the_four_builders_agree`,
which now reaches `slip.frame` and `smtp.data_block`; this holds them to the
RFCs, since four writers agreeing is not four writers right.
"""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPILER = shutil.which("cc") or shutil.which("gcc")

#: Content, and the frame RFC 1055 sends for it: END and ESC inside the
#: payload are ESC ESC_END and ESC ESC_ESC, and END closes the frame.
SLIP = [
	(b"", b"\xc0"),
	(b"ab", b"ab\xc0"),
	(bytes([0x61, 0xC0, 0xDB, 0x00, 0x62]),
	 bytes([0x61, 0xDB, 0xDC, 0xDB, 0xDD, 0x00, 0x62, 0xC0])),
]

#: Content, and the DATA body RFC 5321 section 4.5.2 sends: a line that
#: begins with a period gets one more, and CRLF . CRLF ends it.
SMTP = [
	(b"hi", b"hi\r\n.\r\n"),
	(b"hi\r\n.dot\r\n..two\r\nend", b"hi\r\n..dot\r\n...two\r\nend\r\n.\r\n"),
	(b".lead", b"..lead\r\n.\r\n"),
]


def _module(tmp_path: Path, schema: str) -> tuple[Any, Any]:
	out = tmp_path / "gen"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(ROOT / schema),
		 "--target", "python", "--layer", "edit", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	sys.path[:0] = [str(out), str(ROOT / "runtime" / "python")]
	stem = Path(schema).stem
	for name in (stem, f"{stem}_build"):
		sys.modules.pop(name, None)
	try:
		return (importlib.import_module(stem),
		        importlib.import_module(f"{stem}_build"))
	finally:
		del sys.path[:2]


def test_python_writes_slip_as_rfc_1055_says(tmp_path: Path) -> None:
	base, build = _module(tmp_path, "example/slip/slip.situ")
	for content, wire in SLIP:
		out = bytearray(64)
		n = build.build_frame(out, content)
		assert bytes(out[:n]) == wire, content
		held = base.acquire(base.frame, base.Message(bytearray(wire)), 0, n)
		assert held.datagram_decode() == content


def test_python_writes_dot_stuffing_as_rfc_5321_says(tmp_path: Path) -> None:
	base, build = _module(tmp_path, "example/smtp/smtp.situ")
	for content, wire in SMTP:
		out = bytearray(128)
		n = build.build_data_block(out, content)
		assert bytes(out[:n]) == wire, content
		held = base.acquire(base.data_block, base.Message(bytearray(wire)),
		                    0, n)
		assert held.body_decode() == content


def test_the_room_is_the_codes_worst_case(tmp_path: Path) -> None:
	"""Two bytes a content byte and the END: 5 bytes need 11, and 10 is
	refused before anything is written, though these 5 encode in 6."""
	_, build = _module(tmp_path, "example/slip/slip.situ")
	with pytest.raises(build.BoundsError):
		build.build_frame(bytearray(10), b"abcde")
	assert build.build_frame(bytearray(11), b"abcde") == 6


def test_a_body_of_bare_dot_lines_fits_the_room_it_is_given(
		tmp_path: Path) -> None:
	"""The encoder takes a bare LF as a line end, so `.LF` lines grow 3:2.
	The bound said 4:3, from `.CRLF`, and room computed from it was 141
	bytes for these 155: an overrun in C and C++ (26.998). Exactly the
	corrected room builds, and a byte less is refused."""
	base, build = _module(tmp_path, "example/smtp/smtp.situ")
	content = b".\n" * 50
	n = build.build_data_block(bytearray(155), content)
	assert n == 155
	with pytest.raises(build.BoundsError):
		build.build_data_block(bytearray(154), content)


@pytest.mark.skipif(COMPILER is None, reason = "needs a C compiler")
def test_c_writes_slip_as_rfc_1055_says(tmp_path: Path) -> None:
	out = tmp_path / "gen"
	for command in (["build", "--target", "c", "--layer", "edit"],
	                ["gen-derived"]):
		done = subprocess.run(
			[sys.executable, "-m", "situc", command[0],
			 str(ROOT / "example/slip/slip.situ"), *command[1:],
			 "--out", str(out)], cwd=ROOT, capture_output=True, text=True)
		assert done.returncode == 0, done.stderr
	cases = "\n".join(
		"\t{ " + f"{len(content)}u, (const uint8_t *)\""
		+ "".join(f"\\x{byte:02x}" for byte in content) + "\", "
		+ f"{len(wire)}u, (const uint8_t *)\""
		+ "".join(f"\\x{byte:02x}" for byte in wire) + "\" },"
		for content, wire in SLIP)
	(tmp_path / "drive.c").write_text(f"""#include <stdio.h>
#include <string.h>
#include "slip_build.h"

static const struct {{ uint32_t n; const uint8_t *in; uint32_t m;
                       const uint8_t *wire; }} cases[] = {{
{cases}
}};

int main(void)
{{
	for (size_t i = 0; i < sizeof cases / sizeof cases[0]; i++) {{
		uint8_t out[64];
		uint32_t wrote = 0;

		if (situ_frame_build(out, sizeof out, cases[i].in, cases[i].n,
		                     &wrote) != SITU_OK
		    || wrote != cases[i].m
		    || memcmp(out, cases[i].wire, wrote) != 0) {{
			printf("case %u\\n", (unsigned)i);
			return 1;
		}}
	}}
	return 0;
}}
""", encoding = "ascii")
	runtime = ROOT / "runtime" / "c"
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
		 f"-I{runtime}", f"-I{out}", str(tmp_path / "drive.c"),
		 str(out / "slip.c"), str(out / "slip_derived.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "drive")],
		capture_output = True, text = True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "drive")], capture_output = True,
	                     text = True)
	assert ran.returncode == 0, ran.stdout


def test_an_interior_with_fields_is_still_refused() -> None:
	"""Content that is a struct would need composing first; the refusal
	says so rather than the old blanket one."""
	from situc.codegen import plain_build
	from situc.codegen.c.build import plan
	from situc.layout import solve
	from situc.parser import parse_text
	from situc.resolve import resolve

	schema = parse_text(
		"target buffer;\nendian big;\n"
		"codec slip { kernel = stuffing(worst_case = 2, per = 1,"
		" unit = byte, code = slip); }\nimpl slip derived;\n"
		'struct f { coded d(slip) until "\\xC0" { u8 kind; u8 rest[remaining]; } }\n',
		path = "f.situ")
	resolved = resolve(schema, solve(schema))
	_, why = plan(resolved.structs["f"], plain_build.shape(schema, resolved))
	assert why is not None and "interior is not one byte run" in why
