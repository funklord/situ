"""The dissector walks a varint as the walker does, in every form (26.932).

It said "a varint, whose width is in its own bytes and which this dissector
does not walk" and stopped the cursor: 51 members across the corpus, and
every struct holding one had no extent, so CBOR, WebAssembly, SQLite,
Matroska and protobuf showed nothing past their first number. Each form is
now read in Lua by `walk.decode_varint`'s rules, and this holds every form
to the walker over bytes chosen to spell long numbers: half the bytes carry
a continuation bit, so widths of one to ten all occur.

The width is compared directly, the walker listing `v len=N`, and `last`
after it as well, so the cursor the width moves is checked too. Where the
bytes are no number the walker refuses and the dissector places nothing.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
from pathlib import Path
from struct import pack as pack_bytes

import pytest

from situc import pack as packer
from situc.cli import analyse
from situc.dissector import generate
from situc.parser import parse
from test_dissector import LUA, _walked, compare, read_back
from walker.image import load

ROOT = Path(__file__).resolve().parents[2]

SCHEMA = """target buffer;
endian big;
bit_order msb_first;
varint_type leb   { encoding = leb128; max_bits = 64; }
varint_type be    { encoding = be128; max_bits = 64; max_bytes = 9; }
varint_type sleb  { encoding = sleb128; max_bits = 64; }
varint_type quic  { encoding = quic; max_bits = 62; }
varint_type der   { encoding = der; max_bits = 32; }
varint_type cs    { encoding = compactsize; max_bits = 64; }
varint_type lenc  { encoding = lenenc; max_bits = 64; }
varint_type head  { encoding = cbor; max_bits = 64; }
varint_type hp    { encoding = hpack; prefix = 5; max_bits = 32; }
varint_type tagno { encoding = bertag; max_bits = 28; }
varint_type size  { encoding = ebml; max_bits = 56; }
varint_type id    { encoding = ebml_id; max_bits = 32; }
varint_type utf   { encoding = utf8; max_bits = 36; }
varint_type bij   { encoding = bijective128; max_bits = 64; }
varint_type lp    { encoding = lebprefix; prefix = 4; max_bits = 64; }
struct a_leb   { leb v; u8 last; }
struct a_be    { be v; u8 last; }
struct a_sleb  { sleb v; u8 last; }
struct a_quic  { quic v; u8 last; }
struct a_der   { der v; u8 last; }
struct a_cs    { cs v; u8 last; }
struct a_lenc  { lenc v; u8 last; }
struct a_head  { u3 major; head v; u8 last; }
struct a_hp    { u3 flags; hp v; u8 last; }
struct a_tag   { u2 class; u1 constructed; tagno v; u8 last; }
struct a_size  { size v; u8 last; }
struct a_id    { id v; u8 last; }
struct a_utf   { utf v; u8 last; }
struct a_bij   { bij v; u8 last; }
struct a_lp    { u1 more; u3 kind; lp v; u8 last; }
struct two     { u8 first; leb a; head_pair b; u8 last; }
struct s_leb   { u8 first; leb n; u8 data[n]; u8 last; }
struct s_be    { be n; u8 data[n]; u8 last; }
struct s_quic  { u8 first; quic n; u8 data[n]; u8 last; }
struct s_der   { u8 first; der n; u8 data[n]; u8 last; }
struct s_cs    { u8 first; cs n; u8 data[n]; u8 last; }
struct s_head  { u3 major; head n; u8 data[n]; u8 last; }
struct s_hp    { u3 flags; hp n; u8 data[n]; u8 last; }
struct s_size  { u8 first; size n; u8 data[n]; u8 last; }
struct s_utf   { utf n; u8 data[n]; u8 last; }
struct s_lp    { u1 more; u3 kind; lp n; u8 data[n]; u8 last; }
struct head_pair { u3 major; head v; }
"""


def _packet(rng: random.Random) -> bytes:
	"""Bytes that spell long numbers about as often as short ones."""
	return bytes(rng.randrange(128, 256) if rng.random() < 0.5
	             else rng.randrange(0, 128)
	             for _ in range(rng.randrange(0, 16)))


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_every_form_is_as_wide_as_the_walker_reads_it(tmp_path: Path) -> None:
	schema = tmp_path / "forms.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	source, resolved, _ = analyse(schema)
	parsed = parse(source)
	lua    = tmp_path / "forms.lua"
	lua.write_text(generate(parsed, resolved, "forms"), encoding="ascii")
	image  = load(packer.pack(parsed, resolved, metadata=True)[0])

	rng      = random.Random(20261108)
	compared = 0
	differ: list[str] = []
	for name in sorted(resolved.structs):
		if not name.startswith("a_"):
			continue
		placed = 0
		for _ in range(300):
			packet  = _packet(rng)
			# Below the minimum the dissector declines the frame whole, as
			# it does every struct's, and the walker still lists it.
			if len(packet) < resolved.structs[name].layout.size_bytes:
				continue
			_, rows = read_back(lua, name, packet)
			_, read = _walked(image, name, packet)
			# Where the walker refuses the number it lists no `v` and then
			# places `last` over the number's own bytes, in a message it
			# calls refused. The dissector places nothing past it, which is
			# the answer `_uncomputable` exists to give, so only numbers
			# the walker decoded are compared.
			# A one-byte number is listed as a plain value, `v 54`.
			if "v#len" not in read and "v" not in read:
				assert not any(abbrev == f"{name}.last"
				               for abbrev, _, _, _ in rows), packet.hex()
				continue
			width = next((length for abbrev, _, length, _ in rows
			              if abbrev == f"{name}.v"), None)
			last  = next((value for abbrev, _, _, value in rows
			              if abbrev == f"{name}.last"), None)
			wide  = read.get("v#len", 1)
			if width != wide or last != (
					str(read["last"]) if "last" in read else None):
				differ.append(f"{name}: the dissector shows {width} bytes"
				              f" then {last}, the walker {wide}"
				              f" then {read.get('last')}, in {packet.hex()}")
			placed  += 1
			compared += 1
		# A form that never got past its number would agree vacuously.
		assert placed > 20, f"{name}: decoded only {placed} times"

	assert not differ, "\n".join(differ[:10])
	assert compared > 16 * 40


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_numbers_in_a_row_agree_with_the_walker(tmp_path: Path) -> None:
	"""Two numbers in a row, the second in a struct of its own, and a
	number sizing the bytes after it, through the corpus differential: its
	random bytes, every member compared. `data` and `last` are placed by the
	number's VALUE, which is what the dissector reads it for."""
	schema = tmp_path / "forms.situ"
	schema.write_text(SCHEMA, encoding="ascii")
	result = compare(schema)
	assert not result.differ, result.differ
	assert result.compared > 0


@pytest.mark.skipif(shutil.which("tshark") is None, reason="no tshark")
def test_wireshark_reads_a_whole_transaction(tmp_path: Path) -> None:
	"""In Wireshark, the genesis coinbase from `bitcoin.vectors`: a
	CompactSize count of inputs, each sized by a CompactSize script length,
	then the outputs and the lock time. It showed the version and stopped
	at the first count. The values are the block's own: an outpoint of all
	ones, 50 BTC out, lock time 0."""
	subprocess.run([sys.executable, "-m", "situc", "gen-dissector",
	                str(ROOT / "example/bitcoin/bitcoin.situ"), "--out",
	                str(tmp_path)], check=True, cwd=ROOT, capture_output=True)
	line = next(one for one in (ROOT / "example/bitcoin/bitcoin.vectors")
	            .read_text(encoding="ascii").splitlines()
	            if one.startswith("transaction genesis_coinbase"))
	packet  = bytes.fromhex("".join(line.split()[2:]))
	capture = pack_bytes("<IHHiIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 147) \
		+ pack_bytes("<IIII", 0, 0, len(packet), len(packet)) + packet
	(tmp_path / "tx.pcap").write_bytes(capture)
	ran = subprocess.run(
		["tshark", "-X", f"lua_script:{tmp_path / 'bitcoin.lua'}", "-o",
		 'uat:user_dlts:"User 0 (DLT=147)","situ_bitcoin_transaction","0",'
		 '"","0",""',
		 "-r", str(tmp_path / "tx.pcap"), "-T", "fields",
		 "-e", "situ.transaction.version", "-e", "situ.input.previous_index",
		 "-e", "situ.input.sequence", "-e", "situ.output.value",
		 "-e", "situ.transaction.lock_time"],
		check=True, capture_output=True, text=True, timeout=60)
	assert ran.stdout.split() == ["1", "4294967295", "4294967295",
	                              "5000000000", "0"]
