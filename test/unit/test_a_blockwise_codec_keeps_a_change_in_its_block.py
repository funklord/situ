"""`seekable = blockwise(N)` is tested as a claim (26.1065).

`blockwise` was parsed and read by nothing: its N was dropped, a field in a
`blockwise(16)` region mapped as one under CTR, a pipeline refused it, and
`gen-codec-tests` emitted nothing for it -- a declared property with no
test, which this suite's own rule reads as coverage it does not have.

The claim, as the codec suite now asks it: a byte changed in the input
changes the output only inside its own block. Run here against three real
encoders: one that keeps to its block, one that carries forward into the
next the way CBC does, and one that carries back -- the last two being
what a test that could not fail would pass too.
"""

from __future__ import annotations

import shutil
from pathlib import Path
import subprocess

import pytest

from situc.codegen.c import codectests
from test_codecs import PREAMBLE, parse_text

ROOT = Path(__file__).resolve().parents[2]
CC   = shutil.which("cc") or shutil.which("gcc")

SCHEMA = (PREAMBLE + "codec blk { length_preserving; seekable = blockwise(4);"
          " granularity = byte; deterministic; }\n"
          'impl blk extern "my_blk";\n')

#: Each output byte is its input byte plus the first byte of its block, so a
#: change reaches its whole block and no further.
LOCAL = "out[i] = (uint8_t)(in[i] + (i % 4u != 0u ? in[i - i % 4u] : 0u));"

#: Each output byte is its input byte plus the one before it, across block
#: edges as CBC chains: a change carries forward into the next block.
FORWARD = "out[i] = (uint8_t)(in[i] + (i > 0u ? in[i - 1u] : 0u));"

#: Each output byte is its input byte plus the one after it: a change
#: carries back into the block before.
BACKWARD = "out[i] = (uint8_t)(in[i] + (i + 1u < in_len ? in[i + 1u] : 0u));"


def _run(tmp_path: Path, encode: str) -> subprocess.CompletedProcess[str]:
	text = codectests.generate(parse_text(SCHEMA), "unit")
	(tmp_path / "unit_codec_tests.c").write_text(text, encoding="ascii")
	(tmp_path / "my_blk.c").write_text(f"""#include <stdint.h>
#include "situ.h"

situ_err_t my_blk_encode(const uint8_t *in, uint32_t in_len, uint8_t *out,
		uint32_t out_cap, uint32_t *out_len)
{{
	uint32_t i;
	if (out_cap < in_len) {{
		return SITU_ERR_BOUNDS;
	}}
	for (i = 0; i < in_len; i++) {{
		{encode}
	}}
	*out_len = in_len;
	return SITU_OK;
}}

situ_err_t my_blk_decode(const uint8_t *in, uint32_t in_len, uint8_t *out,
		uint32_t out_cap, uint32_t *out_len)
{{
	return my_blk_encode(in, in_len, out, out_cap, out_len);
}}
""", encoding="ascii")
	assert CC is not None
	built = subprocess.run(
		[CC, "-std=c11", "-Wall", "-Wextra", f"-I{ROOT / 'runtime' / 'c'}",
		 str(tmp_path / "unit_codec_tests.c"), str(tmp_path / "my_blk.c"),
		 str(ROOT / "runtime" / "c" / "situ.c"), "-o", str(tmp_path / "run")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	return subprocess.run([str(tmp_path / "run")], capture_output=True,
	                      text=True, timeout=60)


def test_the_claim_is_generated() -> None:
	text = codectests.generate(parse_text(SCHEMA), "unit")
	assert "test_blk_seekable_blockwise" in text


@pytest.mark.skipif(CC is None, reason="no C compiler")
def test_a_codec_keeping_to_its_block_passes(tmp_path: Path) -> None:
	ran = _run(tmp_path, LOCAL)
	assert "[       OK ] test_blk_seekable_blockwise" in ran.stdout, ran.stdout
	assert ran.returncode == 0, ran.stdout


@pytest.mark.skipif(CC is None, reason="no C compiler")
@pytest.mark.parametrize("encode", [FORWARD, BACKWARD],
                         ids=["forward", "backward"])
def test_a_codec_carrying_across_a_block_edge_fails(tmp_path: Path,
		encode: str) -> None:
	ran = _run(tmp_path, encode)
	assert "[  FAILED  ] test_blk_seekable_blockwise" in ran.stdout, ran.stdout
