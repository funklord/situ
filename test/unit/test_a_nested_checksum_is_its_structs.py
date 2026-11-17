"""A checksum of a nested struct, asked of the struct that holds it.

C gives the holding struct its own `covered`, `compute` and `check` for a
nested struct's tag. They derived the span in the holder's terms, where the
region the tag covers is no member, so openmlx4's FS2 `gph_chain` handed
out `section.crc`'s coverage as bytes 0 to 0 -- and where the CRC sat
behind a variable-length payload, `check` called a `_section_crc_offset`
nothing emits and the header did not compile. They are the nested struct's
own now, over the nested view, with the offset moved into the holder's.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from situc.cli import main

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "runtime"

SCHEMA = ("target buffer;\nendian big;\n"
          "codec c8 {\n\tkernel = polynomial(width = 8, poly = 0x07);\n}\n"
          "impl c8 derived;\n"
          "struct sec {\n\tauthenticated body { u8 n; u8 data[n]; }\n"
          "\tchecksum u8 crc[1] covers(body) is c8;\n}\n"
          "struct outer {\n\tu8 tag;\n\tsec one;\n}\n")


def _crc8(data: bytes) -> int:
	crc = 0
	for byte in data:
		crc ^= byte
		for _ in range(8):
			crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
	return crc


@pytest.mark.skipif(shutil.which("gcc") is None, reason="no C compiler")
def test_c_answers_a_nested_checksum_over_the_nested_view(tmp_path: Path) -> None:
	schema = tmp_path / "nest.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	out = tmp_path / "c"
	assert main(["build", str(schema), "--target", "c", "--out", str(out)]) == 0
	assert main(["gen-derived", str(schema), "--target", "c",
	             "--out", str(out)]) == 0
	body = bytes([3, 1, 2, 3])
	good = bytes([0x55, *body, _crc8(body)])
	bad  = bytes([0x55, *body, _crc8(body) ^ 1])
	(out / "main.c").write_text(f'''#include <stdio.h>
#include <string.h>
#include "nest.h"
static void ask(const uint8_t *data, uint32_t n)
{{
	uint8_t     buf[16];
	situ_msg_t  msg;
	situ_view_t view;
	uint32_t    at = 99u, len = 99u, sum = 999u;

	memcpy(buf, data, n);
	situ_msg_init(&msg, buf, n);
	if (situ_outer_view(&msg, 0u, n, &view) != SITU_OK) {{
		printf("noview\\n");
		return;
	}}
	const situ_err_t covered = situ_outer_one_crc_covered(view, &at, &len);
	const situ_err_t computed = situ_outer_one_crc_compute(view, &sum);
	printf("%d %u %u %d %u %d\\n", (int)covered, at, len, (int)computed, sum,
	       (int)situ_outer_one_crc_check(view));
}}
int main(void)
{{
	static const uint8_t good[] = {{{", ".join(str(b) for b in good)}}};
	static const uint8_t bad[]  = {{{", ".join(str(b) for b in bad)}}};
	ask(good, sizeof good);
	ask(bad, sizeof bad);
	return 0;
}}
''', encoding="ascii")
	built = subprocess.run(
		["gcc", "-std=c99", "-Os", "-Wall", "-Wextra", "-Werror",
		 f"-I{RUNTIME / 'c'}", f"-I{out}", str(out / "main.c"),
		 str(out / "nest.c"), str(out / "nest_derived.c"),
		 str(RUNTIME / "c" / "situ.c"), "-o", str(out / "r")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(out / "r")], capture_output=True, text=True)
	ok = 0
	found = re.search(r"SITU_ERR_CHECKSUM\s*=\s*(\d+)",
	                  (RUNTIME / "c" / "situ.h").read_text())
	assert found is not None
	checksum = int(found.group(1))
	# The coverage is `body` at byte 1 of `outer`, four bytes long, and the
	# sum is the section's own; the corrupt copy computes the same and fails.
	assert ran.stdout.split("\n")[:2] == [
		f"{ok} 1 4 {ok} {_crc8(body)} {ok}",
		f"{ok} 1 4 {ok} {_crc8(body)} {checksum}",
	], ran.stdout
