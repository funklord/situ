"""0034's ask, keyed on the list itself (26.600).

Decision 0034 says nothing the TUI or the GUI can do may be absent from
`situ-edit`, and `walker/report.py` names what there is to answer:
`SUPPORTED`, seventeen probes, *named rather than counted so that a kind
quietly dropping out cannot look like agreement*.

Three entries closed four of them one at a time -- 26.595 the variant and
the verdict, 26.597 the four placement kinds, 26.598 the argument nobody
could supply. Each was found by a different lens and none of them could
have said whether any were left. This module asks the whole question once:
the witness table is compared against `report.SUPPORTED` itself, so a
probe added there with no case here fails by name rather than going
unanswered.

The two that are not rows are the ones that bit. `report.listing` prints
`no-view` and `needs-arguments` in place of every row, and both reached
`acquire` through a LAZY view with nothing in between -- so both printed a
Python traceback out of `render`. 26.598 fixed the second by checking the
argument list at open and left the first, which is one symptom fixed and
the cause left standing; the view is acquired at open now, so neither can
escape.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402
from situc import pack as packer			# noqa: E402
from situc import parser				# noqa: E402
from situc.diagnostics import Source			# noqa: E402
from situc.layout import solve				# noqa: E402
from situc.resolve import resolve			# noqa: E402
from editor.document import open_document		# noqa: E402
from walker import report				# noqa: E402
from walker.walk import Refused, Unsupplied		# noqa: E402

#: Arbitrary but fixed, and long enough that a view exists for every fixed
#: struct named below. The question is whether a FACT reaches the frontend,
#: not whether these particular bytes are a valid message.
BUF = bytes((i * 37 + 11) & 0xFF for i in range(512))

#: probe -> (schema, struct, member). Derived by walking the corpus and
#: asking each of `report`'s own emitters for its first hit, rather than
#: picked by hand -- a witness chosen by its author is a witness chosen to
#: pass. `sealed` and `gated` share one because one construct answers both.
WITNESS: dict[str, tuple[str, str, str]] = {
	"no-view":         ("example/arp/arp.situ", "arp_packet", ""),
	"needs-arguments": ("test/schema/edges.situ", "negotiated", ""),
	"scalar":          ("example/arp/arp.situ", "arp_packet",
	                    "hardware_type"),
	"bytes":           ("example/arp/arp.situ", "arp_packet",
	                    "sender_hardware"),
	"element":         ("example/sqlite/sqlite.situ", "btree_leaf_page",
	                    "cells"),
	"run_element":     ("example/http/http.situ", "request_head", "fields"),
	"arm_value":       ("example/dnsname/dnsname.situ", "label", "body"),
	"sealed":          ("example/dtls/dtls.situ", "record", "sealed"),
	"gated":           ("example/dtls/dtls.situ", "record", "sealed"),
	"delimited":       ("example/http/http.situ", "header_field", "name"),
	"varint":          ("example/mqtt/mqtt.situ", "packet", "length"),
	"while_count":     ("example/dnsname/dnsname.situ", "name", "labels"),
	"nested":          ("example/bmp/bmp.situ", "bitmap_file", "file"),
	"tag":             ("example/dtls/dtls.situ", "record", "tag"),
	"marker":          ("example/tiff/tiff.situ", "tiff_header",
	                    "byte_order"),
	"validate":        ("example/arp/arp.situ", "arp_packet", ""),
	# The one row where the third column is not a member: a relation is
	# over two messages and belongs to neither struct, so it is the
	# relation's own name and `dns_header` is where to stand to see it.
	"relation":        ("example/dns/dns.situ", "dns_header", "reply_to"),
}


def packed(relative: str) -> bytes:
	path = ROOT / relative
	parsed   = parser.parse(Source(str(path), path.read_text(encoding="utf-8")))
	resolved = resolve(parsed, solve(parsed))
	blob, _  = packer.pack(parsed, resolved, metadata=True)
	return blob


def test_every_supported_probe_has_a_case_here() -> None:
	"""The exhaustiveness check, over the tool's own list and not a copy.

	`SUPPORTED` is a tuple in `walker/report.py`, so this compares against
	the thing a new probe is added to -- which is what makes the failure
	land on whoever adds one rather than on whoever next goes looking.
	"""
	assert set(WITNESS) == set(report.SUPPORTED)


def test_no_witness_is_a_struct_the_frontend_cannot_open() -> None:
	"""A table of witnesses nothing can open would pass every test below
	by skipping. `no-view` and `needs-arguments` are the two that must not
	open, and every other witness must."""
	for probe, (schema, struct, _member) in WITNESS.items():
		if probe in ("no-view", "needs-arguments"):
			continue
		args = ()
		open_document(packed(schema), BUF, struct, args=args)


@pytest.mark.parametrize("probe", sorted(WITNESS))
def test_the_frontend_answers_the_probe(probe: str) -> None:
	schema, struct, member = WITNESS[probe]
	image_bytes = packed(schema)

	if probe == "no-view":
		# A frame too short for the struct. Refused at OPEN -- the whole
		# of 26.600 -- and the message names the struct, which `acquire`
		# cannot: "frame of 0 does not reach 28" says nothing about which
		# layout wanted 28, and a reader who passed `--struct` has just
		# chosen between several.
		with pytest.raises(Refused) as caught:
			open_document(image_bytes, b"", struct)
		assert struct in str(caught.value)
		return

	if probe == "needs-arguments":
		# `wanted`, not `caught` again: mypy narrows the name from the
		# `Refused` block above, and a short generic name reused in one
		# function is how a shadow gets written here (26.595).
		with pytest.raises(Unsupplied) as wanted:
			open_document(image_bytes, BUF, struct)
		assert "argument" in str(wanted.value)
		return

	document = open_document(image_bytes, BUF, struct)

	if probe == "validate":
		# The header's verdict (26.595). `None` is what a schema carrying
		# no checks answers, so the witness must be one that has them.
		assert document.verdict is not None
		return

	if probe == "relation":
		assert member in [name for name, _why, _how in document.relations()]
		return

	rows = {field.name for field in document.fields()}
	assert member in rows, f"{probe}: {schema}:{struct} has no `{member}` row"


def test_a_frame_too_short_is_refused_at_open_rather_than_at_render() -> None:
	"""Where the traceback came from, and why fixing it in the frontend
	would have been the wrong place: every consumer of `Document` -- the
	TUI, the Qt window, the JSON -- reaches the view the same way."""
	image_bytes = packed("example/arp/arp.situ")
	with pytest.raises(Refused):
		open_document(image_bytes, b"")


def test_neither_non_row_verdict_escapes_the_cli_as_a_traceback(
		tmp_path: Path) -> None:
	"""Both arms, from outside. They failed identically and for one
	reason, and 26.598 fixed one of them."""
	(tmp_path / "empty.bin").write_bytes(b"")

	short = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situ-edit"),
		 str(ROOT / "example" / "arp" / "arp.situ"),
		 str(tmp_path / "empty.bin")],
		capture_output=True, text=True, cwd=ROOT, timeout=300)
	assert short.returncode == 1
	assert "Traceback" not in short.stderr
	assert "arp_packet" in short.stderr

	(tmp_path / "some.bin").write_bytes(BUF[:32])
	unsupplied = subprocess.run(
		[sys.executable, str(ROOT / "bin" / "situ-edit"),
		 str(ROOT / "test" / "schema" / "edges.situ"),
		 str(tmp_path / "some.bin"), "--struct", "negotiated"],
		capture_output=True, text=True, cwd=ROOT, timeout=300)
	assert unsupplied.returncode == 1
	assert "Traceback" not in unsupplied.stderr
	assert "--arg" in unsupplied.stderr
