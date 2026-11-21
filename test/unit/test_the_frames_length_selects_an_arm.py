"""The frame's length selects an arm: `switch (remaining)` (26.1125).

fuzznet (inbox 20261120T2230Z) keeps a record that gained a member, and
journals still hold the shorter one; only the record's length says which.
NTP's datagram is the same shape: RFC 5905 7.3's authenticator is absent
at 48 bytes, an MD5 digest at 68 and a SHA-1 at 72, and nothing in the
packet says so. Both are a variant switched on `remaining`.

The four backends and both walkers are held to one answer for each
length, through the four-way drivers and the walker's listing; the layout
is held to refusing the three schemas that cannot mean what they say.
"""

from __future__ import annotations

import pytest

from pathlib import Path

from situc.diagnostics import SituError
from situc.layout import solve
from situc.pack import pack
from situc.parser import parse_text
from situc.resolve import resolve

from every_schema import ROOT, load_schema
from fourway import COMPLETE, answers, build
from test_walker import _by_member

import sys
sys.path.insert(0, str(ROOT))
from walker import report				# noqa: E402
from walker.image import load			# noqa: E402

NTP = ROOT / "example" / "ntp" / "ntp.situ"

SETTING = """target buffer;
endian big;

struct ranked { u8 rank; u8 signature[64]; }
struct unranked { u8 signature[64]; }

struct setting [fills_frame] {
	u16 key;
	u32 value;
	variant tail switch (remaining) {
		case 65: ranked   with_rank;
		default: unranked without_rank;
	}
}
"""


def _refused(source: str) -> str:
	parsed = parse_text(source)
	with pytest.raises(SituError) as caught:
		solve(parsed)
	return str(caught.value)


def test_a_setting_of_either_length_lays_out() -> None:
	parsed = parse_text(SETTING)
	resolve(parsed, solve(parsed))


def test_a_case_arm_is_the_size_its_case_says() -> None:
	assert "is read when 64 bytes remain, and is 65 bytes" in _refused(
		SETTING.replace("case 65:", "case 64:"))


def test_a_default_arm_is_no_cases_size() -> None:
	assert "which a `case` claims" in _refused(SETTING.replace(
		"case 65: ranked   with_rank;", "case 64: unranked with_rank;"))


def test_the_variant_ends_the_frame() -> None:
	assert "is not the frame's last member" in _refused(
		SETTING.replace("\t}\n}\n", "\t}\n\tu8 trailer;\n}\n"))


def _datagram(authenticator: int) -> bytes:
	"""An NTPv4 server reply, then `authenticator` bytes of key and digest."""
	packet = bytes([0x24, 2, 6, 0xEC]) + bytes(44)
	return packet + bytes(range(1, authenticator + 1))


LENGTHS = (0, 20, 24, 2, 21, 30)


@pytest.mark.skipif(not COMPLETE, reason="a backend is missing")
def test_four_backends_and_the_walker_read_one_datagram_alike(
		tmp_path: Path) -> None:
	command = build(tmp_path, NTP)
	parsed = load_schema(NTP)
	blob, _ = pack(parsed, resolve(parsed, solve(parsed)), metadata=True)
	image = load(blob)
	verdicts = []
	for extra in LENGTHS:
		packet = _datagram(extra)
		said = {backend: {key: line for key, line in
		                  _by_member(answers(argv, packet, tmp_path)).items()
		                  if key[0] == "ntp_datagram"}
		        for backend, argv in command.items()}
		first = said["c"]
		assert first, extra
		for backend, lines in said.items():
			assert lines == first, (extra, backend)
		walked = _by_member(report.listing(image, packet))
		assert ("ntp_datagram", "validate") in walked, extra
		for key, line in walked.items():
			if key[0] == "ntp_datagram":
				assert first.get(key) == line, (extra, key)
		verdicts.append(first[("ntp_datagram", "validate")])
	# Three lengths the variant names, and three it refuses as an
	# unknown discriminant -- `default: error`.
	assert verdicts[:3] == ["validate 0"] * 3, verdicts
	assert len(set(verdicts[3:])) == 1 and verdicts[3] != "validate 0", \
		verdicts


BUILD = r"""#include <stdio.h>
#include <string.h>
#include "ntp_build.h"
int main(void)
{
	uint8_t packet[48], md5[20], sha1[24], out[80];
	uint32_t wrote = 0;
	situ_msg_t m;
	situ_view_t v, arm;
	memset(packet, 0, sizeof packet);
	packet[0] = 0x24;
	memset(md5, 0x11, sizeof md5);
	memset(sha1, 0x22, sizeof sha1);
	if (situ_ntp_datagram_build_md5(out, sizeof out, packet, 48u, md5, 20u,
	                                &wrote) != SITU_OK || wrote != 68u) {
		return 1;
	}
	situ_msg_init(&m, out, wrote);
	if (situ_ntp_datagram_view(&m, 0u, wrote, &v) != SITU_OK
	    || situ_ntp_datagram_authenticator_md5_view(v, &arm) != SITU_OK
	    || situ_ntp_datagram_authenticator_sha1_view(v, &arm) == SITU_OK) {
		return 2;
	}
	if (situ_ntp_datagram_build_sha1(out, sizeof out, packet, 48u, sha1, 24u,
	                                 &wrote) != SITU_OK || wrote != 72u) {
		return 3;
	}
	if (situ_ntp_datagram_build_none(out, sizeof out, packet, 48u, NULL, 0u,
	                                 &wrote) != SITU_OK || wrote != 48u) {
		return 4;
	}
	puts("built");
	return 0;
}
"""


@pytest.mark.skipif(not COMPLETE, reason="a backend is missing")
def test_each_arms_builder_writes_its_length(tmp_path: Path) -> None:
	"""A struct ending in a length-selected variant has a builder per arm,
	`<struct>_build_<arm>`, writing no discriminant: the arm's length is
	what a reader selects it by (fuzznet looked for `<struct>_build`,
	inbox 20261121T0700Z)."""
	import shutil
	import subprocess
	import sys
	compiler = shutil.which("cc") or shutil.which("gcc")
	assert compiler is not None
	out = tmp_path / "c"
	done = subprocess.run(
		[sys.executable, "-m", "situc", "build", str(NTP), "--target", "c",
		 "--layer", "edit", "--out", str(out)],
		cwd=ROOT, capture_output=True, text=True)
	assert done.returncode == 0, done.stderr
	(tmp_path / "b.c").write_text(BUILD, encoding="ascii")
	runtime = ROOT / "runtime" / "c"
	compiled = subprocess.run(
		[compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{runtime}",
		 f"-I{out}", str(tmp_path / "b.c"), str(out / "ntp.c"),
		 str(runtime / "situ.c"), "-o", str(tmp_path / "b")],
		capture_output=True, text=True)
	assert compiled.returncode == 0, compiled.stderr
	ran = subprocess.run([str(tmp_path / "b")], capture_output=True,
	                     text=True, timeout=30)
	assert ran.returncode == 0 and ran.stdout.strip() == "built", ran.returncode


OLD_SETTING = """target buffer;
endian big;
struct setting { u16 key; u8 rank; u8 signature[64]; }
"""

NEW_SETTING = """target buffer;
endian big;
struct ranked { u8 rank; u8 signature[64]; }
struct unranked { u8 signature[64]; }
struct setting {
	u16 key;
	variant tail switch (remaining) {
		case 65: ranked   with_rank;
		default: unranked without_rank;
	}
}
"""


def _wire(source: str) -> str:
	from situc.wire import render
	parsed = parse_text(source)
	return render(parsed, resolve(parsed, solve(parsed)), "s.situ")


def test_members_moved_into_a_length_arm_are_not_removed() -> None:
	"""fuzznet (inbox 20261121T0700Z): the setting's last two members moved
	into the 65-byte arm, every byte where it was, and `wire --check` said
	BREAKING -- "removed rank ... everything after them shifts". What
	changed is that a shorter shape is admitted too: backward-compatible."""
	from situc.wire import compare
	said = [(one.kind, one.subject)
	        for one in compare(_wire(OLD_SETTING), _wire(NEW_SETTING)).findings]
	assert ("backward", "setting") in said, said
	assert not any(kind == "breaking" for kind, _ in said), said


def test_an_arm_that_does_not_reproduce_them_still_breaks() -> None:
	from situc.wire import compare
	wider = OLD_SETTING.replace("u8 rank;", "u16 rank;")
	said = compare(_wire(wider), _wire(NEW_SETTING)).findings
	assert any(one.kind == "breaking" for one in said), said


def test_equalize_is_refused_and_never_advised() -> None:
	"""The arms' sizes are what selects them: `[equalize]` would leave the
	frame's length nothing to tell them apart by, so the layout refuses it
	and the advisor does not suggest it -- it did, for NTP's authenticator,
	advice whose schema the compiler would then refuse."""
	assert "asks for `[equalize]`" in _refused(SETTING.replace(
		"switch (remaining) {", "switch (remaining) [equalize] {"))
	import subprocess
	import sys
	advice = subprocess.run(
		[sys.executable, "-m", "situc", "advise", str(NTP)],
		cwd=ROOT, capture_output=True, text=True)
	assert advice.returncode == 0, advice.stderr
	assert "authenticator" not in advice.stdout, advice.stdout
