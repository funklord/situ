"""An exchange that expires is forgotten by the conversation too (26.1054).

C's driver keeps its slots and the conversation table's keys apart, and
dropped only its own on expiry. The key stayed: a late reply to the
expired exchange still matched, where `_match` says it answers
SITU_ERR_CONSTRAINT, and every expiry took one table slot for good -- so
after `cap` of them every `_send` answered SITU_ERR_BOUNDS. A sender that
outlives `cap` timeouts stopped sending. C++, Rust and Python keep the key
in the driver's own slot and were not affected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from situc.codegen.c import converse, drive
from situc.codegen.c import generate as generate_c
from test_relations import COMPILER, POLICY, RUNTIME, WARNINGS, analysed

PROGRAM = r"""#include <stdio.h>
#include <string.h>
#include "t_drive.h"
static situ_err_t sink(void *ctx, const uint8_t *d, uint32_t n) { (void)ctx; (void)d; (void)n; return SITU_OK; }
int main(void)
{
	enum { N = 2 };
	uint8_t bufs[3][17];
	situ_msg_t msgs[3];
	situ_view_t views[3];
	situ_drive_response_to_slot_t slots[N];
	situ_conv_response_to_slot_t keys[N];
	situ_drive_response_to_t drive;
	situ_io_t io = { sink, NULL };
	situ_drive_response_to_init(&drive, slots, keys, N, io, 100u, 0u);
	for (unsigned i = 0; i < 3; i++) {
		memset(bufs[i], 0, 17);
		bufs[i][0] = 0x10; bufs[i][1] = (uint8_t)i; bufs[i][3] = 4;
		situ_msg_init(&msgs[i], bufs[i], 17);
		if (situ_frame_view(&msgs[i], 0u, &views[i]) != SITU_OK) return 9;
	}
	printf("send1 %d\n", situ_drive_response_to_send(&drive, views[0], bufs[0], 17, 1u, 0u));
	printf("send2 %d\n", situ_drive_response_to_send(&drive, views[1], bufs[1], 17, 2u, 0u));
	uint32_t next = 0, expired = 0;
	situ_drive_response_to_step(&drive, 200u, &next, &expired);
	printf("expired %u\n", expired);
	printf("send3 %d\n", situ_drive_response_to_send(&drive, views[2], bufs[2], 17, 3u, 200u));
	uint32_t id = 0;
	printf("late reply to 1: %d id %u\n", situ_drive_response_to_on_message(&drive, views[0], &id), id);
	return 0;
}
"""


@pytest.mark.skipif(COMPILER is None, reason="no C compiler")
def test_c_forgets_an_expired_exchange(tmp_path: Path) -> None:
	"""Two slots, two sends, both expire; a third send and a late reply."""
	schema, resolved = analysed(POLICY)
	for name, text in generate_c(schema, resolved, "t").files().items():
		(tmp_path / name).write_text(text, encoding="ascii")
	for emitted in (converse.generate(schema, resolved, "t"),
	                drive.generate(schema, resolved, "t")):
		for name, text in emitted.items():
			(tmp_path / name).write_text(text, encoding="ascii")
	(tmp_path / "leak.c").write_text(PROGRAM, encoding="ascii")
	assert COMPILER is not None
	built = subprocess.run(
		[COMPILER, *WARNINGS, f"-I{tmp_path}", f"-I{RUNTIME}",
		 str(tmp_path / "leak.c"), str(tmp_path / "t.c"),
		 str(RUNTIME / "situ.c"), "-o", str(tmp_path / "leak")],
		capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	ran = subprocess.run([str(tmp_path / "leak")], capture_output=True,
	                     text=True, timeout=60)
	# 0 is SITU_OK, 2 SITU_ERR_CONSTRAINT.
	assert ran.stdout.splitlines() == [
		"send1 0", "send2 0", "expired 2", "send3 0",
		"late reply to 1: 2 id 0"], ran.stdout
