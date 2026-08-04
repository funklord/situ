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

The suggestion came from `suggestions/apt-emerge.md`, which arrived at it from
the other side: that project's two most valuable suites are differential
against `dpkg --compare-versions` and `diff3`, and both found bugs that
hand-written expectations had not.
"""

from __future__ import annotations

import re
import shutil
import subprocess
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


def proto_corpus(tmp: Path) -> bytes:
	proto = tmp / "user.proto"
	proto.write_text((ROOT / "examples" / "protobuf" / "user.proto")
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


ORACLES: tuple[Oracle, ...] = (
	Oracle(
		name   = "cpio",
		schema = ROOT / "examples" / "cpio" / "cpio.situ",
		tool   = "cpio",
		why    = ("GNU cpio writes the archive and lists it back. The newc "
		          "header is thirteen ASCII hex numbers, which is the only "
		          "text-number path any example exercises."),
	),
	Oracle(
		name   = "protobuf",
		schema = ROOT / "examples" / "protobuf" / "protobuf.situ",
		tool   = "protoc",
		why    = ("`protoc` encodes the message and decodes it back. The "
		          "varint is the part worth testing: 4294967297 is five "
		          "LEB128 bytes with the continuation bit set in four of "
		          "them."),
	),
	Oracle(
		name   = "sqlite",
		schema = ROOT / "examples" / "sqlite" / "sqlite.situ",
		tool   = "sqlite3",
		why    = ("sqlite3 writes the database and counts the rows; situ "
		          "reads the b-tree leaf page header and counts the cells. "
		          "Neither knows the other exists."),
	),
	Oracle(
		name   = "bmp-file",
		schema = ROOT / "examples" / "bmp" / "bmp.situ",
		tool   = "file",
		why    = ("A second reader of the same format, from a different "
		          "codebase than ImageMagick, and it reports two fields "
		          "ImageMagick does not: cbSize and bits offset."),
	),
	Oracle(
		name   = "bmp",
		schema = ROOT / "examples" / "bmp" / "bmp.situ",
		tool   = "identify",
		why    = ("ImageMagick writes the bitmap and reports its geometry. "
		          "`height` is signed and bottom-up, which is the field a "
		          "hand-written vector is most likely to agree with the "
		          "schema about and both be wrong."),
	),
)

#: How to drive each oracle. Kept beside `ORACLES` rather than in it so the
#: dataclass stays data.
DRIVERS = {
	"cpio": (cpio_corpus, cpio_says, cpio_situ),
	"bmp":  (bmp_corpus, bmp_says, bmp_situ),
	"protobuf": (proto_corpus, proto_says, proto_situ),
	"sqlite":   (sqlite_corpus, sqlite_says, sqlite_situ),
	"bmp-file": (bmp_corpus, file_says, file_situ),
}
