"""Independent implementations of formats this repository describes.

Every test that reads bytes here was written by whoever wrote the schema. That
is the failure mode a differential test exists to break: a hand-authored vector
encodes *what the author believed the format says*, so when the author misreads
the specification the schema and the vector are wrong in the same direction and
agree with each other forever. Nothing about a green suite says otherwise.

An independent implementation is wrong in a *different* direction, which is why
disagreement becomes visible. So each oracle here does two things situ never
does:

  * **writes the corpus**, using the third-party tool rather than bytes chosen
    here -- `cpio -o` lays out the archive, ImageMagick lays out the bitmap.
    Nothing in the input is this project's opinion.
  * **reads it back**, again with the third-party tool, into facts that are
    compared against what situ's accessors say.

Neither end is authored here. What is authored here is only the
*correspondence* -- that cpio's "size" and `cpio_header.filesize` are the same
number -- which is a short, checkable list rather than a table of expected
values.

The suggestion came from `suggestion/apt-emerge.md`, which arrived at it from
the other side: that project's two most valuable suites are differential
against `dpkg --compare-versions` and `diff3`, and both found bugs that
hand-written expectations had not.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any
from dataclasses import dataclass
from pathlib import Path

from every_schema import ROOT


@dataclass(frozen=True)
class Oracle:
	"""One format, one third-party implementation of it."""

	#: What this is called in test ids and in the report.
	name: str
	#: The schema whose accessors are on trial.
	schema: Path
	#: The command this needs. Absent means skip, loudly.
	tool: str
	#: Why this pairing is worth having, for the reader of a failure.
	why: str


def have(tool: str) -> bool:
	"""Whether the oracle's implementation is on this machine.

	Most are commands. `pymodbus` is a library, so asking the PATH about it
	would report it missing on every machine and skip the oracle silently --
	which is the failure this whole file is about.
	"""
	if tool == "pymodbus":
		import importlib.util
		return importlib.util.find_spec("pymodbus") is not None
	return shutil.which(tool) is not None


def _run(args: list[str], cwd: Path | None = None) -> str:
	"""Run a tool and hand back its stdout, or raise."""
	done = subprocess.run(args, capture_output=True, text=True, check=True,
	                      cwd=cwd)
	return done.stdout


# -- cpio ---------------------------------------------------------------------
#
# The strongest pairing here: GNU cpio writes the archive and lists it back,
# and the newc header is thirteen ASCII hex numbers -- which is `hex u32 x[8]`
# in situ and the only text-number path any example exercises.

def cpio_corpus(tmp: Path) -> bytes:
	"""An archive GNU cpio laid out, over files of deliberately awkward sizes.

	One byte, six, seventeen and zero: the padding after the name and after
	the data rounds to four, so a run of sizes mod 4 is what exercises it. A
	zero-length file is here because an empty run is the case a walk skips.
	"""
	src = tmp / "src"
	(src / "sub").mkdir(parents=True)
	(src / "tiny.bin").write_bytes(b"x")
	(src / "greeting.txt").write_bytes(b"hello\n")
	(src / "sub" / "deep.txt").write_bytes(b"nested data here\n")
	(src / "empty").write_bytes(b"")

	listing = _run(["find", ".", "-type", "f"], cwd=src)
	made    = subprocess.run(["cpio", "-o", "-H", "newc", "--quiet"],
	                         input=listing.encode("ascii"), cwd=src,
	                         capture_output=True)
	assert made.returncode == 0, made.stderr
	return made.stdout


def _plain(name: str) -> str:
	"""One spelling of a path, so a leading `./` is not mistaken for a finding.

	`find` emits `./greeting.txt` and cpio stores exactly that; `cpio -tv`
	prints it back without the prefix. Both are the same entry, and the
	normalisation is applied to both sides rather than to whichever one is
	inconvenient -- a comparison that edits one side is a comparison that can
	be made to pass.
	"""
	return name[2:] if name.startswith("./") else name


def cpio_says(archive: bytes, tmp: Path) -> list[dict[str, object]]:
	"""What `cpio -tv` reports: one row per entry."""
	shown = subprocess.run(["cpio", "-tv", "--quiet"], input=archive,
	                       capture_output=True)
	assert shown.returncode == 0, shown.stderr

	found: list[dict[str, object]] = []
	for line in shown.stdout.decode("utf-8", "replace").splitlines():
		parts = line.split()
		if len(parts) < 9:
			continue
		# `-rw-rw-r-- 1 user group SIZE Mon DD HH:MM name`
		found.append({"name": _plain(parts[-1]), "filesize": int(parts[4]),
		              "nlink": int(parts[1])})
	return sorted(found, key=lambda row: str(row["name"]))


def cpio_situ(module: object, archive: bytes) -> list[dict[str, object]]:
	"""The same rows, read through situ's accessors and nothing else."""
	from situ_runtime import Message  # type: ignore[import-not-found]

	msg   = Message(bytearray(archive))
	at    = 0
	found: list[dict[str, object]] = []

	while at < len(archive):
		entry = module.cpio_entry.at(msg, at, len(archive) - at)   # type: ignore[attr-defined]
		entry.validate()
		header = entry.header
		name   = bytes(entry.name).split(b"\0")[0].decode("ascii")
		if name == "TRAILER!!!":
			break
		found.append({"name": _plain(name), "filesize": header.filesize,
		              "nlink": header.nlink})
		# The entry ends where its data ends, rounded up to four.
		at = (at + entry.data_offset + header.filesize + 3) & ~3

	return sorted(found, key=lambda row: str(row["name"]))


# -- bmp ----------------------------------------------------------------------
#
# ImageMagick writes the bitmap and reports its geometry. The interesting part
# is `height`, which BMP stores signed and bottom-up.

def bmp_corpus(tmp: Path) -> bytes:
	"""A bitmap ImageMagick laid out. 7x5 so neither dimension is a round
	number and the row padding to four bytes is not trivially zero."""
	made = tmp / "made.bmp"
	_run(["convert", "-size", "7x5", "xc:red", "BMP3:" + str(made)])
	return made.read_bytes()


def bmp_says(image: bytes, tmp: Path) -> dict[str, object]:
	path = tmp / "read.bmp"
	path.write_bytes(image)
	shown = _run(["identify", "-format", "%w %h %z %B", str(path)])
	width, height, depth, size = shown.split()
	return {"width": int(width), "height": int(height),
	        "file_size": int(size), "bits_per_pixel": int(depth) * 3}


def bmp_situ(module: object, image: bytes) -> dict[str, object]:
	from situ_runtime import Message

	msg  = Message(bytearray(image))
	view = module.bitmap_file.at(msg, 0)                             # type: ignore[attr-defined]
	view.validate()
	head = view.info

	# BMP stores height negative for a top-down image; the magnitude is the
	# pixel count either way, and that is what `identify` reports.
	return {"width": head.width, "height": abs(head.height),
	        "file_size": view.file.file_size,
	        "bits_per_pixel": head.bits_per_pixel}


# -- protobuf -----------------------------------------------------------------
#
# `protoc` encodes the message and decodes it back, and the example's own
# `user.proto` says why that matters: "a description that agrees only with its
# own compiler has demonstrated nothing." The varint is the part worth testing
# -- 4294967297 is five LEB128 bytes with continuation bits set in four of
# them, which is where an off-by-one in a shift shows up.

PROTO_TEXT = (
	'user_id: 4294967297\n'
	'username: "situ-oracle"\n'
	'score: 0.5\n'
)


def tiff_corpus_little(tmp: Path) -> bytes:
	"""A TIFF ImageMagick laid out, little-endian.

	7x5 for `bmp_corpus`'s reason. The endianness is asked for rather than
	taken, because the pair is the point: one schema, two byte orders, and a
	marker in the first two bytes deciding which.
	"""
	return _tiff(tmp, "lsb", "7x5")


def tiff_corpus_big(tmp: Path) -> bytes:
	"""The same image, big-endian.

	`endian_marker` is a first-class construct with exactly one example in
	this tree (section 8.3), and until this nothing outside situ had ever
	confirmed it reads either order correctly -- let alone both.

	A different size, which is not decoration. The two images must not put
	their IFD at the same offset, or `ifd_offset` replaced by the constant
	218 satisfies both and the comparison cannot tell a field that was read
	from a number that was typed. 7x5 lands at 218 and 13x11 at 866.
	"""
	return _tiff(tmp, "msb", "13x11")


def _tiff(tmp: Path, endian: str, size: str) -> bytes:
	made = tmp / f"made-{endian}.tif"
	_run(["convert", "-size", size, "xc:red",
	      "-define", f"tiff:endian={endian}", "TIFF:" + str(made)])
	return made.read_bytes()



def png_corpus(tmp: Path) -> bytes:
	"""A PNG ImageMagick wrote.

	Small, because what is on trial is the chunk walk rather than the image:
	a 7x5 solid colour is a signature, an IHDR, one IDAT and an IEND, which
	is every chunk shape this schema describes and four independent CRCs.
	"""
	made = tmp / "made.png"
	_run(["convert", "-size", "7x5", "xc:red", "PNG:" + str(made)])
	return made.read_bytes()


def png_says(image: bytes, tmp: Path) -> list[object]:
	"""Every chunk, and `zlib`'s CRC over the bytes PNG says are covered.

	`zlib.crc32` is the independent implementation here, and it is a real
	one: it is not situ's derived codec, it is the CRC every PNG encoder on
	the planet was checked against, and the file was written by a third
	tool. What the comparison is really asking is whether situ agrees about
	*which bytes are covered* -- the length is outside the CRC and the CRC
	field is outside it too, so a span that is one field wrong produces a
	number that is entirely wrong.
	"""
	import zlib

	del tmp
	found: list[object] = []
	at = 8					# past the signature
	while at + 8 <= len(image):
		length = int.from_bytes(image[at:at + 4], "big")
		kind   = image[at + 4:at + 8]
		body   = image[at + 8:at + 8 + length]
		stored = int.from_bytes(image[at + 8 + length:at + 12 + length], "big")
		found.append((kind.decode("latin-1"), length,
		              zlib.crc32(kind + body) & 0xFFFFFFFF, stored))
		at += 12 + length
	return found


def png_situ(module: object, image: bytes) -> list[object]:
	"""The same chunks, walked through the generated accessors.

	The CRC is computed by `zlib` over the span situ's `crc_covered` names,
	so situ supplies the boundaries and something else supplies the
	arithmetic. Three backends used to say that span ran to the end of the
	buffer (26.244); against a real file that is a wrong number rather than
	a wrong-looking one, and this is the check that would have said so.
	"""
	import zlib

	from situ_runtime import Message

	held = Message(bytearray(image))
	sig  = module.png_signature.at(held, 0)		# type: ignore[attr-defined]
	sig.validate()					# the eight-byte preamble

	found: list[object] = []
	at = 8
	while at + 8 <= len(image):
		# A chunk is not fixed-size, so its view is acquired with an extent:
		# 12 bytes of frame plus the length this chunk declares.
		length = int.from_bytes(image[at:at + 4], "big")
		view = module.chunk.at(held, at, 12 + length)	# type: ignore[attr-defined]
		view.validate()
		# View-relative, so the chunk's own offset goes back on: `crc_covered`
		# answers about the view it was asked of, not about the file.
		start, count = view.crc_covered()
		start += at
		kind   = bytes(view.kind)
		assert int(view.length) == length, "situ read a different length"
		stored = int.from_bytes(bytes(view.crc), "big")
		found.append((kind.decode("latin-1"), length,
		              zlib.crc32(bytes(image[start:start + count])) & 0xFFFFFFFF,
		              stored))
		at += 12 + length
	return found


def tiff_says(image: bytes, tmp: Path) -> dict[str, object]:
	"""`file`'s TIFF line: the byte order, and how many IFD entries follow.

	`direntries` is the fact worth having. It is not in the eight bytes situ
	parses -- it sits *at* the offset those bytes carry -- so agreeing about
	it means the marker resolved, the magic was where it should be and the
	offset was read in the order the marker named. One number that only comes
	out right if the whole header did.
	"""
	path = tmp / "read.tif"
	path.write_bytes(image)
	shown = _run(["file", "-b", str(path)])

	facts: dict[str, object] = {}
	for part in (piece.strip() for piece in shown.split(",")):
		if part in ("little-endian", "big-endian"):
			facts["little"] = part == "little-endian"
		elif part.startswith("direntries="):
			facts["direntries"] = int(part.split("=", 1)[1])
	return facts


def tiff_situ(module: object, image: bytes) -> dict[str, object]:
	"""The same two facts, out of the generated accessors.

	The entry count is read here rather than by the schema because the schema
	stops at the header: `tiff_header` is eight bytes and the IFD is a
	structure situ does not describe. What it does describe is where the IFD
	is and which order to read it in, and that is exactly what this uses.
	"""
	from situ_runtime import Message

	view = module.tiff_header.at(Message(bytearray(image)), 0)   # type: ignore[attr-defined]
	little = bool(view.byte_order_is_little)
	at     = int(view.ifd_offset)

	assert int(view.magic) == 42, "not a TIFF, or the marker did not resolve"
	count = (int.from_bytes(image[at:at + 2], "little") if little
	         else int.from_bytes(image[at:at + 2], "big"))
	return {"little": little, "direntries": count}


def proto_corpus(tmp: Path) -> bytes:
	proto = tmp / "user.proto"
	proto.write_text((ROOT / "example" / "protobuf" / "user.proto")
	                 .read_text(encoding="ascii"), encoding="ascii")

	made = subprocess.run(["protoc", "--encode=User", "user.proto"],
	                      input=PROTO_TEXT.encode("ascii"), cwd=tmp,
	                      capture_output=True)
	assert made.returncode == 0, made.stderr
	return made.stdout


def proto_says(message: bytes, tmp: Path) -> dict[int, str]:
	"""`protoc --decode_raw`: field number to printed value."""
	shown = subprocess.run(["protoc", "--decode_raw"], input=message,
	                       capture_output=True)
	assert shown.returncode == 0, shown.stderr

	found: dict[int, str] = {}
	for line in shown.stdout.decode("ascii").splitlines():
		number, _, value = line.partition(":")
		found[int(number.strip())] = value.strip()
	return found


def _leb128(raw: bytes) -> int:
	"""A second implementation of the varint, deliberately not situ's.

	Three lines, and the point of them is that they were written from the
	encoding rather than from `situc/varint.py`. A harness that decoded
	through situ would be comparing situ against itself again.
	"""
	value = 0
	for index, byte in enumerate(raw):
		value |= (byte & 0x7F) << (7 * index)
	return value


def proto_situ(module: object, message: bytes) -> dict[int, str]:
	from situ_runtime import Message

	view = module.proto_message.at(Message(bytearray(message)), 0,          # type: ignore[attr-defined]
	                               len(message))
	view.validate()

	found: dict[int, str] = {}
	for name in ("user_id", "username", "score"):
		item = getattr(view, name)()
		raw  = message[item.value_at:item.value_at + item.value_len]

		if item.wire == 0:
			found[item.field] = str(_leb128(raw))
		elif item.wire == 2:
			found[item.field] = '"' + raw.decode("ascii") + '"'
		else:
			# Wire type 5 is four bytes; `--decode_raw` prints them as a
			# big-endian hex word because it cannot know the declared type.
			found[item.field] = "0x" + raw[::-1].hex()
	return found


# -- bmp, read by a second implementation --------------------------------------
#
# `file` is a different codebase from ImageMagick and reports two fields
# ImageMagick does not: `cbSize` and `bits offset`, which are the header's own
# `file_size` and `pixel_offset`. Two independent readers of one format is
# worth more than one -- and if the two ever disagree with each other, that is
# a finding about them rather than about situ, which is also worth knowing.

def file_says(image: bytes, tmp: Path) -> dict[str, object]:
	"""`file`'s BMP line, which is a comma-separated list of facts."""
	path = tmp / "read.bmp"
	path.write_bytes(image)
	shown = _run(["file", "-b", str(path)])

	facts: dict[str, object] = {}
	for part in (piece.strip() for piece in shown.split(",")):
		if re.fullmatch(r"\d+ x \d+ x \d+", part):
			width, height, depth = (int(n) for n in part.split(" x "))
			facts.update(width=width, height=height, bits_per_pixel=depth)
		elif part.startswith("cbSize "):
			facts["file_size"] = int(part.split()[1])
		elif part.startswith("bits offset "):
			facts["pixel_offset"] = int(part.split()[2])
	return facts


def file_situ(module: object, image: bytes) -> dict[str, object]:
	from situ_runtime import Message

	view = module.bitmap_file.at(Message(bytearray(image)), 0)       # type: ignore[attr-defined]
	view.validate()
	head = view.info

	return {"width": head.width, "height": abs(head.height),
	        "bits_per_pixel": head.bits_per_pixel,
	        "file_size": view.file.file_size,
	        "pixel_offset": view.file.pixel_offset}


# -- sqlite -------------------------------------------------------------------
#
# sqlite3 writes the database and reports how many rows are in it; situ reads
# the b-tree leaf page header and reports how many cells the page holds. For a
# table small enough to sit on one page those are the same number, and nothing
# in either tool knows that the other exists.

ROWS = (("42", "'situ'"), ("-7", "'oracle'"), ("999999", "'a longer value'"))


def sqlite_corpus(tmp: Path) -> bytes:
	made   = tmp / "made.db"
	values = ", ".join(f"({a}, {b})" for a, b in ROWS)
	_run(["sqlite3", str(made),
	      f"create table t(a integer, b text); insert into t values {values};"])
	return made.read_bytes()


def sqlite_says(database: bytes, tmp: Path) -> dict[str, object]:
	path = tmp / "read.db"
	path.write_bytes(database)
	rows = _run(["sqlite3", str(path), "select count(*) from t;"]).strip()
	size = _run(["sqlite3", str(path), "pragma page_size;"]).strip()
	return {"cell_count": int(rows), "page_size": int(size)}


def sqlite_situ(module: object, database: bytes) -> dict[str, object]:
	from situ_runtime import Message

	# The file header names the page size at offset 16, big endian; the table
	# lives on page 2, which starts one page in. Read from the format rather
	# than assumed, because a different sqlite3 could choose differently.
	page_size = int.from_bytes(database[16:18], "big")
	page      = database[page_size:page_size * 2]

	view = module.btree_leaf_page.at(Message(bytearray(page)), 0, len(page))  # type: ignore[attr-defined]
	view.validate()
	return {"cell_count": view.cell_count, "page_size": page_size}


# -- the network formats ------------------------------------------------------
#
# The corpus problem these had until now: something has to *write* a packet,
# and capturing one needs a network and root. `randpkt` solves it -- it ships
# with Wireshark but is a separate program from the dissection engine, so the
# bytes and the interpretation still come from different code.
#
# Its packets carry random field values, which is better here than a
# well-known capture would be: a hand-picked packet exercises the values
# somebody thought of. What both sides must agree on is whatever number is at
# whatever offset.
#
# `validate()` is deliberately not called. A randpkt ARP frame is not a valid
# ARP frame -- `arp_generic` requires `protocol_type == 0x0800` and randpkt
# fills it with noise -- and situ is right to refuse it. What is under test is
# whether the accessors *read the same bytes tshark read*, which is a
# different question from whether the packet is well-formed.

#: How many packets a network oracle asks `randpkt` for.
#:
#: Forty rather than a handful, because randpkt truncates and the comparable
#: frames are a fraction of what it writes: with eight, a tcp run produced no
#: usable frame about one time in twenty and the "compared nothing" guard --
#: correctly -- failed the suite. The cure for a flake is its cause, and the
#: cause here was too small a sample rather than the randomness (invariant 99).
RANDPKT_COUNT = 40


def _randpkt(tmp: Path, kind: str, count: int = RANDPKT_COUNT) -> Path:
	out = tmp / f"{kind}.pcap"
	_run(["randpkt", "-b", "120", "-c", str(count), "-t", kind,
	      "-F", "pcap", str(out)])
	return out


def _frames(capture: Path) -> list[dict[str, object]]:
	"""Every frame, as tshark's own JSON, raw bytes included."""
	import json

	shown = _run(["tshark", "-r", str(capture), "-T", "json", "-x"])
	return [frame["_source"]["layers"] for frame in json.loads(shown)]


def _layer(frame: dict[str, object], name: str) -> tuple[bytes, int]:
	"""One layer's bytes and where they start, as tshark reports them.

	Taken from tshark rather than computed here on purpose: an offset this
	file worked out for itself would be this project's opinion about the
	framing, which is the thing being checked.
	"""
	raw = frame[f"{name}_raw"]
	return bytes.fromhex(raw[0]), int(raw[1])      # type: ignore[index]


def eth_corpus(tmp: Path) -> bytes:
	"""The capture file itself is the corpus; the readers below open it."""
	return _randpkt(tmp, "arp").read_bytes()


#: Scratch directories this process made, kept alive so that each one's
#: finalizer runs when the interpreter exits.
#:
#: `tempfile.mkdtemp` leaves the directory behind for ever, and the three
#: oracle entry points that take no `tmp_path` -- `eth_situ`, `arp_situ` and
#: the TCP reader -- called it on every run. Measured before this was fixed:
#: 1591 `/tmp/oracle-*` directories, the oldest a fortnight old, each holding
#: one `read.pcap`. Small on disk and unbounded in count, which is the shape
#: that goes unnoticed until an inode table or a full `/` makes it somebody's
#: afternoon.
#:
#: A `TemporaryDirectory` rather than a sweep over a glob at exit: it removes
#: the directory *this* process created and nothing that merely looks like
#: it, which is the difference between vouching for a name and vouching for a
#: pattern. A concurrent run's scratch is not ours to delete.
_SCRATCH: list[tempfile.TemporaryDirectory[str]] = []


def _scratch(prefix: str) -> Path:
	"""A directory that goes away when this process does."""
	made = tempfile.TemporaryDirectory(prefix=prefix)
	_SCRATCH.append(made)
	return Path(made.name)


def _capture(corpus: bytes, tmp: Path | None = None) -> Path:
	"""The capture on disk, because tshark reads files rather than stdin."""
	where = _scratch("oracle-") if tmp is None else tmp
	path  = where / "read.pcap"
	path.write_bytes(corpus)
	return path


def eth_says(corpus: bytes, tmp: Path) -> list[dict[str, object]]:
	found = []
	for frame in _frames(_capture(corpus, tmp)):
		eth = frame["eth"]
		found.append({
			"destination": str(eth["eth.dst"]).replace(":", ""),   # type: ignore[index]
			"source":      str(eth["eth.src"]).replace(":", ""),   # type: ignore[index]
			"ethertype":   int(str(eth["eth.type"]), 16),          # type: ignore[index]
		})
	return found


def eth_situ(module: object, corpus: bytes) -> list[dict[str, object]]:
	from situ_runtime import Message

	found = []
	for frame in _frames(_capture(corpus)):
		raw, _ = _layer(frame, "eth")
		view   = module.ethernet_header.at(Message(bytearray(raw)), 0)   # type: ignore[attr-defined]
		found.append({
			"destination": bytes(view.destination.octets).hex(),
			"source":      bytes(view.source.octets).hex(),
			"ethertype":   int(view.ethertype),
		})
	return found


#: What tshark calls the five ARP header fields, against what situ calls them.
ARP_FIELDS = (
	("arp.hw.type",    "hardware_type",   10),
	("arp.proto.type", "protocol_type",   16),
	("arp.hw.size",    "hardware_length", 10),
	("arp.proto.size", "protocol_length", 10),
	("arp.opcode",     "operation",       10),
)


#: `arp_generic`'s own minimum: eight fixed bytes and four runs of at least
#: one. Asserted against the generated module in `arp_situ` rather than trusted,
#: so a schema change fails here instead of quietly filtering every frame away.
ARP_MIN_BYTES = 12


def _whole_arp(frame: dict[str, object]) -> bool:
	"""Whether this frame is one both tools answer the same question about.

	Two ways it is not, and `randpkt` produces both because it truncates:

	  * tshark reached only some of the header, and reports the fields it got.
	    Comparing against a field it never produced would be comparing against
	    nothing.
	  * the ARP layer is shorter than `arp_generic`'s minimum -- ten bytes,
	    say. tshark dissects the eight-byte fixed header it can see; situ
	    declines to acquire a view for a struct that does not fit, which is
	    correct and is a different question rather than a different answer.

	Both sides apply this same predicate, so neither chooses its own subset,
	and the caller asserts the comparison was not left empty.
	"""
	arp = frame.get("arp")
	if not isinstance(arp, dict) or not all(name in arp for name, _, _ in ARP_FIELDS):
		return False

	raw = frame.get("arp_raw")
	return isinstance(raw, list) and len(bytes.fromhex(raw[0])) >= ARP_MIN_BYTES


def arp_says(corpus: bytes, tmp: Path) -> list[dict[str, int]]:
	found = []
	for frame in _frames(_capture(corpus, tmp)):
		if not _whole_arp(frame):
			continue
		arp = frame["arp"]
		found.append({ours: int(str(arp[theirs]), base)             # type: ignore[index]
		              for theirs, ours, base in ARP_FIELDS})
	return found


def arp_situ(module: object, corpus: bytes) -> list[dict[str, int]]:
	from situ_runtime import Message

	assert module.arp_generic.SIZE_MIN == ARP_MIN_BYTES, (        # type: ignore[attr-defined]
		"arp_generic's minimum moved; ARP_MIN_BYTES must follow it or this "
		"oracle silently stops comparing frames")

	found = []
	for frame in _frames(_capture(corpus)):
		if not _whole_arp(frame):
			continue
		raw, _ = _layer(frame, "arp")
		view   = module.arp_generic.at(Message(bytearray(raw)), 0, len(raw))  # type: ignore[attr-defined]
		found.append({
			"hardware_type":   int(view.hardware_type),
			"protocol_type":   int(view.protocol_type),
			"hardware_length": int(view.hardware_length),
			"protocol_length": int(view.protocol_length),
			"operation":       int(view.operation),
		})
	return found


# -- the rest of the network stack --------------------------------------------
#
# One shape, five formats. Each names the randpkt type that writes the bytes,
# the tshark layer that reads them back, and the fields the two sides both
# name. `situ` is a function because what it takes out of the layer differs --
# a bit-packed nibble is not a `u16` -- but everything else is a table.

def _comparable(frame: dict[str, object], layer: str,
		fields: tuple[tuple[str, str, int], ...], least: int) -> bool:
	"""Whether both tools are answering the same question about this frame.

	Two ways they are not, and `randpkt` produces both because it truncates:
	tshark reached only part of the header and reports what it got, or the
	layer is shorter than the struct's own minimum, where situ declines to
	hand out a view at all. Neither is a disagreement about the bytes
	(invariant 98).

	One predicate, evaluated identically on both sides, so neither gets to
	choose its own subset -- which is what a `try/except: continue` on the
	situ side amounts to, and it is how the udp oracle first read seven rows
	against tshark's eight.
	"""
	seen = frame.get(layer)
	if not isinstance(seen, dict):
		return False
	if not all(name in seen for name, _, _ in fields):
		return False

	raw = frame.get(f"{layer}_raw")
	return isinstance(raw, list) and len(bytes.fromhex(raw[0])) >= least


def _reader(layer: str, fields: tuple[tuple[str, str, int], ...],
		least: int) -> Callable[[bytes, Path], list[dict[str, int]]]:
	"""What tshark says about `layer`, for frames both tools can answer."""

	def says(corpus: bytes, tmp: Path) -> list[dict[str, int]]:
		return [{ours: int(str(frame[layer][theirs]), base)      # type: ignore[index]
		         for theirs, ours, base in fields}
		        for frame in _frames(_capture(corpus, tmp))
		        if _comparable(frame, layer, fields, least)]

	return says


def _situ_reader(layer: str, fields: tuple[tuple[str, str, int], ...],
		struct: str, read: Callable[[Any], dict[str, int]],
		least: int) -> Callable[[object, bytes], list[dict[str, int]]]:
	"""The same frames, through situ's accessors on the same byte span.

	`least` is asserted against the generated module rather than trusted: a
	schema whose minimum moves would otherwise start filtering frames neither
	side mentions, and a comparison that quietly shrinks to nothing passes.
	"""

	def situ(module: object, corpus: bytes) -> list[dict[str, int]]:
		from situ_runtime import Message

		held  = getattr(module, struct)
		# A fixed-size struct's `at` takes no length: its extent is the
		# constant, and passing one is a TypeError rather than a bounds
		# check.
		fixed = getattr(held, "SIZE_BYTES", 0)
		floor = getattr(held, "SIZE_MIN", 0) or fixed
		assert floor == least, (
			f"{struct}'s minimum is {floor}, not the {least} this oracle "
			f"filters on; update it or the comparison silently shrinks")

		found = []
		for frame in _frames(_capture(corpus)):
			if not _comparable(frame, layer, fields, least):
				continue
			raw  = bytes.fromhex(frame[f"{layer}_raw"][0])  # type: ignore[index]
			view = (held.at(Message(bytearray(raw)), 0) if fixed
			        else held.at(Message(bytearray(raw)), 0, len(raw)))
			found.append(read(view))
		return found

	return situ


#: tshark's name for a field, situ's name for it, and the base tshark prints
#: it in. Everything else about these five oracles is shared machinery.
IPV4_FIELDS = (
	("ip.version", "version", 10),
	("ip.hdr_len", "ihl", 10),
	("ip.ttl", "time_to_live", 10),
	("ip.proto", "protocol", 10),
	("ip.len", "total_length", 10),
	("ip.id", "identification", 16),
)

ICMP_FIELDS = (
	("icmp.type", "type", 10),
	("icmp.code", "code", 10),
)

UDP_FIELDS = (
	("udp.srcport", "source_port", 10),
	("udp.dstport", "destination_port", 10),
	("udp.length", "length", 10),
)

#: The raw sequence numbers. tshark's `tcp.seq` is relative to the stream it
#: has been tracking, which is its own bookkeeping rather than the bytes.
TCP_FIELDS = (
	("tcp.srcport", "source_port", 10),
	("tcp.dstport", "destination_port", 10),
	("tcp.seq_raw", "sequence", 10),
	("tcp.ack_raw", "acknowledgement", 10),
)

DNS_FIELDS = (
	("dns.id", "id", 16),
	("dns.count.queries", "question_count", 10),
	("dns.count.answers", "answer_count", 10),
	("dns.count.auth_rr", "authority_count", 10),
	("dns.count.add_rr", "additional_count", 10),
)


def _ipv4_read(view: Any) -> dict[str, int]:
	"""An `authenticated` region flattens onto its struct, so these are
	`view.version` rather than `view.header.version`."""
	# tshark reports `ip.hdr_len` in bytes; the field on the wire is words.
	return {"version": int(view.version), "ihl": int(view.ihl) * 4,
	        "time_to_live": int(view.time_to_live),
	        "protocol": int(view.protocol),
	        "total_length": int(view.total_length),
	        "identification": int(view.identification)}


# -- modbus -------------------------------------------------------------------
#
# `pymodbus` builds the frames and decodes them back, and unlike every other
# oracle here the corpus is a *stream*: several ADUs end to end, which is what
# a Modbus TCP connection carries. Both sides therefore have to agree on where
# each frame stops, and `example/modbus/modbus.situ` names that as the one
# arithmetic fact implementations get wrong -- `length` counts the unit
# identifier and the PDU, so the next frame begins at `6 + length`.
#
# It is a library rather than a command, so there is no `shutil.which` to ask;
# `have("pymodbus")` is special-cased on the import.

MODBUS_REQUESTS = (
	("read_holding", 0x1234, 7, 3, None),
	("read_coils",   0x0020, 9, 17, None),
	("write_multi",  0x0005, 2, 4, [9, 8]),
	("read_input",   0x00FF, 125, 1, None),
)


def _pymodbus() -> tuple[Any, Any, list[Any]]:
	import pymodbus.pdu as pdu
	import pymodbus.pdu.bit_message as bits
	import pymodbus.pdu.register_message as regs
	from pymodbus.framer import FramerSocket

	made: list[Any] = []
	for name, address, count, unit, values in MODBUS_REQUESTS:
		if name == "read_holding":
			made.append(regs.ReadHoldingRegistersRequest(
				address=address, count=count, dev_id=unit,
				transaction_id=len(made) + 1))
		elif name == "read_coils":
			made.append(bits.ReadCoilsRequest(
				address=address, count=count, dev_id=unit,
				transaction_id=len(made) + 1))
		elif name == "write_multi":
			made.append(regs.WriteMultipleRegistersRequest(
				address=address, count=count, registers=values, dev_id=unit,
				transaction_id=len(made) + 1))
		else:
			made.append(regs.ReadInputRegistersRequest(
				address=address, count=count, dev_id=unit,
				transaction_id=len(made) + 1))

	# `is_server=True` decodes *requests*. The other way round it fails on
	# frames pymodbus itself built, reading a read-holding request's address
	# as a response's byte count.
	return FramerSocket, pdu.DecodePDU, made


def modbus_corpus(tmp: Path) -> bytes:
	framer_class, decode_class, made = _pymodbus()
	framer = framer_class(decode_class(True))
	return b"".join(framer.buildFrame(one) for one in made)


def modbus_says(stream: bytes, tmp: Path) -> list[dict[str, int]]:
	framer_class, decode_class, _ = _pymodbus()
	framer = framer_class(decode_class(True))

	found: list[dict[str, int]] = []
	rest = stream
	while rest:
		used, one = framer.processIncomingFrame(rest)
		if not used or one is None:
			break
		found.append({"transaction_id": one.transaction_id,
		              "unit_id": one.dev_id,
		              "function": one.function_code,
		              "frame_bytes": used})
		rest = rest[used:]
	return found


def modbus_situ(module: object, stream: bytes) -> list[dict[str, int]]:
	from situ_runtime import Message

	msg   = Message(bytearray(stream))
	at    = 0
	found: list[dict[str, int]] = []

	while at < len(stream):
		head = module.mbap_header.at(msg, at)           # type: ignore[attr-defined]
		head.validate()
		# 6 + length: the three fields ahead of `length` are not counted by
		# it, and the unit identifier is.
		whole = 6 + int(head.length)
		body  = module.request.at(msg, at + 7, whole - 7)  # type: ignore[attr-defined]

		found.append({"transaction_id": int(head.transaction_id),
		              "unit_id": int(head.unit_id),
		              "function": int(body.function),
		              "frame_bytes": whole})
		at += whole

	return found


# -- mqtt ---------------------------------------------------------------------
#
# Three implementations touch these bytes and none of them is this file.
# `paho-mqtt` lays out the packets -- it is a client, so it is given a socket
# that records instead of sending. `text2pcap` wraps each one in a TCP frame,
# which is carriage rather than interpretation. `tshark` dissects them, from a
# codebase entirely unrelated to paho's. situ reads the raw MQTT bytes.
#
# One packet per frame on purpose: several MQTT packets in one TCP segment are
# collapsed into a single `mqtt` layer by tshark's JSON output, and the
# comparison would silently be about the last one.
#
# The remaining length is the field worth the trouble. It is a varint, and
# paho emits 207 as two bytes with the continuation bit set -- the case a
# fixed vector never happens to contain.

MQTT_PACKETS = (
	("connect", 60, None, None, 0),
	("publish", 0, b"sensor/temp", b"21.5", 0),
	("publish", 7, b"a/b", b"x" * 200, 1),
	("subscribe", 0, b"topic/#", None, 2),
	("pingreq", 0, None, None, 0),
	("disconnect", 0, None, None, 0),
)


class _Recorder:
	"""A socket that keeps what was written to it."""

	def __init__(self) -> None:
		self.out = bytearray()

	def send(self, data: bytes) -> int:
		self.out += data
		return len(data)

	def fileno(self) -> int:
		return -1

	def close(self) -> None:
		pass


def _paho_packets() -> list[bytes]:
	import paho.mqtt.client as mqtt

	# `CallbackAPIVersion` is re-exported rather than declared in
	# `paho.mqtt.client`, which mypy is right to point out and which is
	# paho's business rather than ours.
	client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,   # type: ignore[attr-defined]
	                     client_id="situ-oracle")
	sock   = _Recorder()
	client._sock = sock                                # type: ignore[assignment]

	made: list[bytes] = []
	for kind, number, topic, payload, qos in MQTT_PACKETS:
		sock.out.clear()
		if kind == "connect":
			client._send_connect(number)
		elif kind == "publish":
			client._send_publish(number, topic or b"", payload or b"", qos=qos,
			                     info=mqtt.MQTTMessageInfo(number))
		elif kind == "subscribe":
			client._send_subscribe(False, [(topic or b"", qos)])
		elif kind == "pingreq":
			client._send_pingreq()
		else:
			client._send_disconnect()
		made.append(bytes(sock.out))
	return made


def mqtt_corpus(tmp: Path) -> bytes:
	"""Every packet, one per line, as text2pcap's hex-dump input.

	The corpus is that text rather than a pcap: `_capture` writes what it is
	given to a file, and turning it into frames is the reader's job -- both
	readers get the identical bytes out of it.
	"""
	lines = []
	for packet in _paho_packets():
		lines.append("000000 " + " ".join(f"{byte:02x}" for byte in packet))
	return ("\n".join(lines) + "\n").encode("ascii")


def _mqtt_pcap(corpus: bytes, tmp: Path | None = None) -> Path:
	where = _scratch("mqtt-") if tmp is None else tmp
	text  = where / "mqtt.txt"
	text.write_bytes(corpus)
	made = where / "mqtt.pcap"
	_run(["text2pcap", "-q", "-T", "45000,1883", str(text), str(made)])
	return made


MQTT_FIELDS = (("mqtt.msgtype", "kind"), ("mqtt.len", "length"))


def mqtt_says(corpus: bytes, tmp: Path) -> list[dict[str, int]]:
	found = []
	for frame in _frames(_mqtt_pcap(corpus, tmp)):
		seen = frame.get("mqtt")
		for one in (seen if isinstance(seen, list) else [seen]):
			if not isinstance(one, dict):
				continue
			flags = one.get("mqtt.hdrflags_tree")
			if not isinstance(flags, dict):
				continue
			found.append({"kind": int(str(flags["mqtt.msgtype"])),
			              "length": int(str(one["mqtt.len"]))})
	return found


def _unhex(corpus: bytes) -> list[bytes]:
	"""The corpus back into packets.

	Read from the corpus rather than by calling paho again: a reader that
	regenerates its own input is not reading the bytes the other side read,
	and the two only agree for as long as the generator stays deterministic.
	The hex dump is this file's own carriage format, so reversing it is not
	parsing a protocol.
	"""
	found = []
	for line in corpus.decode("ascii").splitlines():
		_, _, body = line.partition(" ")
		if body:
			found.append(bytes.fromhex(body.replace(" ", "")))
	return found


def mqtt_situ(module: object, corpus: bytes) -> list[dict[str, int]]:
	from situ_runtime import Message

	found = []
	for packet in _unhex(corpus):
		view = module.packet.at(Message(bytearray(packet)), 0, len(packet))  # type: ignore[attr-defined]
		view.validate()
		found.append({"kind": int(view.kind), "length": int(view.length)})
	return found


ORACLES: tuple[Oracle, ...] = (
	Oracle(
		name   = "cpio",
		schema = ROOT / "example" / "cpio" / "cpio.situ",
		tool   = "cpio",
		why    = ("GNU cpio writes the archive and lists it back. The newc "
		          "header is thirteen ASCII hex numbers, which is the only "
		          "text-number path any example exercises."),
	),
	Oracle(
		name   = "png",
		schema = ROOT / "example" / "png" / "png.situ",
		tool   = "convert",
		why    = ("ImageMagick writes the file and `zlib` checks the CRCs. "
		          "situ supplies the boundaries and something else supplies "
		          "the arithmetic, so what is on trial is which bytes a "
		          "chunk's CRC covers -- the length is outside it and the "
		          "CRC field is outside it too, and three backends used to "
		          "say the span ran to the end of the buffer (26.244)."),
	),
	Oracle(
		name   = "tiff",
		schema = ROOT / "example" / "tiff" / "tiff.situ",
		tool   = "convert",
		why    = ("ImageMagick writes the file and `file` reads it back. "
		          "`endian_marker` is the construct on trial: the same two "
		          "bytes are 42 either way round, and the offset after them "
		          "is only right if the marker resolved."),
	),
	Oracle(
		name   = "tiff-be",
		schema = ROOT / "example" / "tiff" / "tiff.situ",
		tool   = "convert",
		why    = ("The same image the other way round. One order alone "
		          "cannot show a marker was read at all -- a backend that "
		          "ignored it and guessed would still pass whichever guess "
		          "it made."),
	),
	Oracle(
		name   = "protobuf",
		schema = ROOT / "example" / "protobuf" / "protobuf.situ",
		tool   = "protoc",
		why    = ("`protoc` encodes the message and decodes it back. The "
		          "varint is the part worth testing: 4294967297 is five "
		          "LEB128 bytes with the continuation bit set in four of "
		          "them."),
	),
	Oracle(
		name   = "ethernet",
		schema = ROOT / "example" / "ethernet" / "ethernet.situ",
		tool   = "tshark",
		why    = ("`randpkt` writes the frames and tshark dissects them; the "
		          "two are separate programs. Fourteen fixed bytes, so any "
		          "disagreement is about byte order or about the MAC "
		          "sub-struct."),
	),
	Oracle(
		name   = "arp",
		schema = ROOT / "example" / "arp" / "arp.situ",
		tool   = "tshark",
		why    = ("Random field values from `randpkt`, which is the point: a "
		          "hand-picked packet exercises the values somebody thought "
		          "of. `validate()` is not called -- randpkt's ARP is not "
		          "valid ARP, and situ is right to say so."),
	),
	Oracle(
		name   = "ipv4",
		schema = ROOT / "example" / "ipv4" / "ipv4.situ",
		tool   = "tshark",
		why    = ("A bit-packed version and header length in the first byte, "
		          "which is where a nibble read from the wrong end shows up."),
	),
	Oracle(
		name   = "icmp",
		schema = ROOT / "example" / "icmp" / "icmp.situ",
		tool   = "tshark",
		why    = "Type and code, ahead of a variant that switches on the type.",
	),
	Oracle(
		name   = "udp",
		schema = ROOT / "example" / "udp" / "udp.situ",
		tool   = "tshark",
		why    = "Four fixed u16s; any disagreement is byte order.",
	),
	Oracle(
		name   = "tcp",
		schema = ROOT / "example" / "tcp" / "tcp.situ",
		tool   = "tshark",
		why    = ("32-bit sequence and acknowledgement numbers, read raw: "
		          "tshark's relative ones are its own bookkeeping, not the "
		          "bytes."),
	),
	Oracle(
		name   = "dns",
		schema = ROOT / "example" / "dns" / "dns.situ",
		tool   = "tshark",
		why    = ("Four counts behind a bit-packed flags word, so a wrong "
		          "flags width moves all four."),
	),
	Oracle(
		name   = "mqtt",
		schema = ROOT / "example" / "mqtt" / "mqtt.situ",
		tool   = "tshark",
		why    = ("paho lays the packets out, tshark dissects them, and "
		          "neither knows the other. The remaining length is a "
		          "varint: paho emits 207 as two bytes with the continuation "
		          "bit set, which a fixed vector never happens to contain."),
	),
	Oracle(
		name   = "modbus",
		schema = ROOT / "example" / "modbus" / "modbus.situ",
		tool   = "pymodbus",
		why    = ("The corpus is a stream of ADUs, so both sides must agree "
		          "where each frame stops. modbus.situ names `6 + length` as "
		          "the one arithmetic fact implementations get wrong."),
	),
	Oracle(
		name   = "sqlite",
		schema = ROOT / "example" / "sqlite" / "sqlite.situ",
		tool   = "sqlite3",
		why    = ("sqlite3 writes the database and counts the rows; situ "
		          "reads the b-tree leaf page header and counts the cells. "
		          "Neither knows the other exists."),
	),
	Oracle(
		name   = "bmp-file",
		schema = ROOT / "example" / "bmp" / "bmp.situ",
		tool   = "file",
		why    = ("A second reader of the same format, from a different "
		          "codebase than ImageMagick, and it reports two fields "
		          "ImageMagick does not: cbSize and bits offset."),
	),
	Oracle(
		name   = "bmp",
		schema = ROOT / "example" / "bmp" / "bmp.situ",
		tool   = "identify",
		why    = ("ImageMagick writes the bitmap and reports its geometry. "
		          "`height` is signed and bottom-up, which is the field a "
		          "hand-written vector is most likely to agree with the "
		          "schema about and both be wrong."),
	),
)

#: A way to make each schema lie, for the test that requires the comparison to
#: be capable of disagreeing (invariant 97). Each is a pair of adjacent members
#: swapped: the fields stay, the offsets move, and any oracle actually reading
#: bytes must notice. An oracle with no entry here is one nobody has shown can
#: fail.
LIES = {
	"bmp": ("\ti32          width;\n\ti32          height;",
	        "\ti32          height;\n\ti32          width;"),
	"bmp-file": ("\ti32          width;\n\ti32          height;",
	             "\ti32          height;\n\ti32          width;"),
	# Not a swap of two like-sized fields: `magic` is a u16 and `ifd_offset`
	# a u32, so exchanging them moves both and the header still measures
	# eight bytes, which is what `require size(tiff_header) == 8` insists on.
	# The byte order still resolves -- the marker has not moved -- so an
	# oracle comparing only that would not notice. `direntries` is what does.
	"tiff":    ("\tu16            magic       [must_eq = 42];\n"
	            "\tu32            ifd_offset;",
	            "\tu32            ifd_offset;\n"
	            "\tu16            magic       [must_eq = 42];"),
	"tiff-be": ("\tu16            magic       [must_eq = 42];\n"
	            "\tu32            ifd_offset;",
	            "\tu32            ifd_offset;\n"
	            "\tu16            magic       [must_eq = 42];"),
	"ethernet": ("\tmac_address  destination;\n\tmac_address  source;",
	             "\tmac_address  source;\n\tmac_address  destination;"),
	# Two u16s, so the layout keeps its size and only the values move -- the
	# mutation has to be one the schema still compiles under, or the test
	# would be proving the parser rejects nonsense rather than proving the
	# oracle compares bytes.
	# Swapping `length` and `unit_id` moves the length field by a byte and
	# changes its width, so the frame walk lands in the wrong place -- which
	# is exactly the arithmetic this oracle exists to check.
	# Two four-bit fields in one byte: swapping them keeps every offset and
	# reads the flags nibble as the packet kind.
	"mqtt": ("\tcontrol_kind      kind;                         // 2.2.1\n"
	         "\tu4                flags;                        // 2.2.2",
	         "\tu4                flags;                        // 2.2.2\n"
	         "\tcontrol_kind      kind;                         // 2.2.1"),
	"modbus": ("\tu16  length       [min = 2, max = 254]; // unit + PDU, 1 + 253 at most\n"
	           "\tu8   unit_id;",
	           "\tu8   unit_id;\n"
	           "\tu16  length       [min = 2, max = 254]; // unit + PDU, 1 + 253 at most"),
	"udp": ("\t\tu16  source_port;\n\t\tu16  destination_port;",
	        "\t\tu16  destination_port;\n\t\tu16  source_port;"),
	"tcp": ("\t\tu32       sequence;\n\t\tu32       acknowledgement;",
	        "\t\tu32       acknowledgement;\n\t\tu32       sequence;"),
	"dns": ("\tu16           question_count;\n\tu16           answer_count;",
	        "\tu16           answer_count;\n\tu16           question_count;"),
	"ipv4": ("\t\tu16       total_length;\n\t\tu16       identification;",
	         "\t\tu16       identification;\n\t\tu16       total_length;"),
	"icmp": ("\t\ticmp_type  type;\n\t\tu8         code;",
	         "\t\tu8         code;\n\t\ticmp_type  type;"),
	"arp": ("\thardware_type  hardware_type    [must_eq = hardware_type.ethernet];\n"
	        "\tu16           protocol_type    [must_eq = 0x0800];",
	        "\tu16           protocol_type    [must_eq = 0x0800];\n"
	        "\thardware_type  hardware_type    [must_eq = hardware_type.ethernet];"),
}

#: How to drive each oracle. Kept beside `ORACLES` rather than in it so the
#: dataclass stays data.
DRIVERS = {
	"cpio": (cpio_corpus, cpio_says, cpio_situ),
	"bmp":  (bmp_corpus, bmp_says, bmp_situ),
	"protobuf": (proto_corpus, proto_says, proto_situ),
	"sqlite":   (sqlite_corpus, sqlite_says, sqlite_situ),
	"bmp-file": (bmp_corpus, file_says, file_situ),
	"png":      (png_corpus, png_says, png_situ),
	"tiff":     (tiff_corpus_little, tiff_says, tiff_situ),
	"tiff-be":  (tiff_corpus_big, tiff_says, tiff_situ),
	"modbus":   (modbus_corpus, modbus_says, modbus_situ),
	"mqtt":     (mqtt_corpus, mqtt_says, mqtt_situ),
	"ethernet": (eth_corpus, eth_says, eth_situ),
	"arp":      (eth_corpus, arp_says, arp_situ),
	# From the udp capture rather than the `ip` one: `randpkt -t ip` fills the
	# header with noise, so tshark reports `ip.version` and gives up, and the
	# comparison filters to nothing. The udp generator emits a well-formed IP
	# header underneath -- the corpus still comes from randpkt, and the layer
	# under test is still IPv4.
	"ipv4": (lambda tmp: _randpkt(tmp, "udp").read_bytes(),
	         _reader("ip", IPV4_FIELDS, 20),
	         _situ_reader("ip", IPV4_FIELDS, "ipv4_header", _ipv4_read, 20)),
	"icmp": (lambda tmp: _randpkt(tmp, "icmp").read_bytes(),
	         _reader("icmp", ICMP_FIELDS, 8),
	         _situ_reader("icmp", ICMP_FIELDS, "icmp_message",
	                      lambda v: {"type": int(v.type),
	                                 "code": int(v.code)}, 8)),
	"udp":  (lambda tmp: _randpkt(tmp, "udp").read_bytes(),
	         _reader("udp", UDP_FIELDS, 8),
	         _situ_reader("udp", UDP_FIELDS, "udp_header",
	                      lambda v: {"source_port": int(v.source_port),
	                                 "destination_port": int(v.destination_port),
	                                 "length": int(v.length)}, 8)),
	"tcp":  (lambda tmp: _randpkt(tmp, "tcp").read_bytes(),
	         _reader("tcp", TCP_FIELDS, 20),
	         _situ_reader("tcp", TCP_FIELDS, "tcp_header",
	                      lambda v: {"source_port": int(v.source_port),
	                                 "destination_port": int(v.destination_port),
	                                 "sequence": int(v.sequence),
	                                 "acknowledgement": int(v.acknowledgement)}, 20)),
	"dns":  (lambda tmp: _randpkt(tmp, "dns").read_bytes(),
	         _reader("dns", DNS_FIELDS, 12),
	         _situ_reader("dns", DNS_FIELDS, "dns_header",
	                      lambda v: {"id": int(v.id),
	                                 "question_count": int(v.question_count),
	                                 "answer_count": int(v.answer_count),
	                                 "authority_count": int(v.authority_count),
	                                 "additional_count": int(v.additional_count)}, 12)),
}
