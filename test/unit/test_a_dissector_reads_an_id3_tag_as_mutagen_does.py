"""The dissector walks an ID3v2.4 tag's frames as mutagen reads them (26.940).

A frame's content is sized by its synchsafe header, `(size_0 << 21) |
(size_1 << 14) | (size_2 << 7) | size_3`, and `<<` and `|` are operators
decision 0021 keeps out of the dissector, so the size was declined and with
it every frame: a tag showed its header and nothing after. Each `size_k` is
a `u7`, so the shifted terms share no bit and `|` is `+`, which is what the
dissector now renders from the expression's tree.

Held to mutagen, which nothing in situ wrote, on the tag `id3.vectors`
takes from a real MP3: the same nine frames in the same order, untranslated
-- mutagen's default folds the v2.3 `TYER` into `TDRC`.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from test_dissector import LUA, dissect

mutagen_id3 = pytest.importorskip("mutagen.id3")

ROOT = Path(__file__).resolve().parents[2]
ID3  = ROOT / "example" / "id3" / "id3.situ"


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
def test_the_frames_are_mutagens_frames(tmp_path: Path) -> None:
	line = next(one for one in (ROOT / "example/id3/id3.vectors")
	            .read_text(encoding="ascii").splitlines()
	            if one.startswith("id3_tag "))
	tag = bytes.fromhex("".join(line.split()[2:]))
	consumed, rows = dissect(tmp_path, ID3, "id3_tag", tag)

	shown: list[str] = []
	held: list[int] = []
	for field, _, _, value in rows:
		if field.startswith("id3_frame.identifier_"):
			held.append(int(value))
			if len(held) == 4:
				shown.append(bytes(held).decode("ascii"))
				held = []

	# mutagen reads the whole declared size and then some, as a reader of a
	# file would; the padding is what follows a tag in one.
	read = mutagen_id3.ID3(io.BytesIO(tag + bytes(4096)), translate=False)

	assert consumed == len(tag)
	assert shown == [frame.FrameID for frame in read.values()]
	assert len(shown) == 9
