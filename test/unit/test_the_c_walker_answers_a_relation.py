"""The C walker answers a relation, as the sixth column (26.755).

Rung 3 is built in all four compiled backends and the Python walker
(26.95), and the C walker refused `arg_field` by name: a relation's must
reads a field of one of TWO messages, and its evaluator's callback held
one. So `relate` had five answers and one refusal, and `project.md` named
that as what was left of the layer ladder.

`situ_walk_relate` loads the image's relation tables, and the evaluator
asks its callback for `arg_field` by parameter. The ordinary walk's
callback refuses those asks rather than answering from the one message it
holds -- which would answer a question about a different pair.

Every pair is held against `walker.report.relate`, which already agrees
with the four backends (`test_relations.py`), so this is agreement with
the Python walk on each must and on the temporal order.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from situc.pack import pack
from test_relations import GOOD, analysed
from walker import report
from walker.image import load
from walker.walk import acquire

COMPILER = shutil.which("cc") or shutil.which("gcc")
ROOT     = Path(__file__).resolve().parents[2]
WALKER   = ROOT / "walker" / "c"

DRIVER = r"""#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "situ_walk.h"

static uint32_t unhex(const char *text, uint8_t **out)
{
	const uint32_t n = (uint32_t)(strlen(text) / 2u);
	*out = malloc(n != 0u ? n : 1u);
	for (uint32_t i = 0u; i < n; i++) {
		char pair[3] = { text[i * 2u], text[i * 2u + 1u], '\0' };
		(*out)[i] = (uint8_t)strtoul(pair, NULL, 16);
	}
	return n;
}

int main(int argc, char **argv)
{
	static uint8_t img[65536];
	if (argc < 4) {
		return 2;
	}
	FILE *f = fopen(argv[1], "rb");
	if (f == NULL) {
		return 2;
	}
	const size_t n = fread(img, 1, sizeof img, f);
	fclose(f);

	situ_walk_image image;
	memset(&image, 0xAA, sizeof image);
	if (situ_walk_open(&image, img, (uint32_t)n) != SITU_WALK_OK) {
		puts("malformed");
		return 1;
	}
	uint8_t *request, *response;
	const uint32_t rl = unhex(argv[2], &request);
	const uint32_t sl = unhex(argv[3], &response);
	situ_walk_err verdict = SITU_WALK_OK;
	const situ_walk_err e = situ_walk_relate(&image, 0u, request, rl,
	                                         response, sl, &verdict);
	printf("%s\n", e != SITU_WALK_OK ? "cannot-say"
	       : (verdict == SITU_WALK_OK ? "0" : "2"));
	free(request);
	free(response);
	return 0;
}
"""


def message(msg: int, index: int, chunks: int) -> bytes:
	return msg.to_bytes(2, "big") + bytes([index, chunks]) + bytes(13)


#: Request, response: both musts hold; the message ids differ; the index
#: is out of range; and the order reversed, which `0 < 0` refuses.
PAIRS = [
	(message(0x1234, 0, 4), message(0x1234, 2, 4)),
	(message(0x1234, 0, 4), message(0x9999, 2, 4)),
	(message(0x1234, 0, 4), message(0x1234, 9, 4)),
	(message(0x1234, 0, 4), message(0x1234, 2, 0)),
	(message(0x1234, 2, 0), message(0x1234, 0, 4)),
]


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_the_c_walker_agrees_with_the_python_walk(tmp_path: Path) -> None:
	schema, resolved = analysed(GOOD)
	blob, _ = pack(schema, resolved, metadata=True)
	(tmp_path / "img").write_bytes(blob)
	(tmp_path / "drive.c").write_text(DRIVER, encoding="ascii")
	built = subprocess.run(
		[COMPILER or "cc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
		 "-Wconversion", "-Wsign-conversion", f"-I{WALKER}",
		 str(tmp_path / "drive.c"), str(WALKER / "situ_walk.c"),
		 "-o", str(tmp_path / "drive")], capture_output=True, text=True)
	assert built.returncode == 0, built.stderr[-2000:]

	image = load(blob)
	shape = image.relations[0].request
	answers = []
	for request, response in PAIRS:
		walked = report.relate(image, 0, acquire(image, request, shape),
		                       acquire(image, response, shape))
		ran = subprocess.run([str(tmp_path / "drive"), str(tmp_path / "img"),
		                      request.hex(), response.hex()],
		                     capture_output=True, text=True)
		assert ran.returncode == 0, ran.stdout + ran.stderr
		answers.append(ran.stdout.strip())
		assert ran.stdout.strip() == str(walked), (
			f"{request.hex()} / {response.hex()}: C {ran.stdout.strip()},"
			f" the Python walk {walked}")
	# Non-vacuity: both verdicts occur, so agreement is about something.
	assert set(answers) == {"0", "2"}, answers
