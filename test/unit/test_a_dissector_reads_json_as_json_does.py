"""The dissector reads a JSON document as Python's `json` reads it (26.938).

`example/json` had no extent anywhere in the dissector: a member skips the
whitespace in front of each token, and `skip` had no length here; an
object's run ends on `sep`, which stands behind a key and a value of any
length; and the value's discriminant was captured before its lead, so
`kind` read the space in front of `[` and chose the number arm. Each is
fixed, and these documents are held to `json.loads` -- which nothing in
situ wrote -- by the keys and numbers in the order they stand.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from test_dissector import LUA, dissect

ROOT = Path(__file__).resolve().parents[2]
JSON = ROOT / "example" / "json" / "json.situ"

DOCUMENTS = [
	b'{"a":[1,2],"bb":"x"}',
	b'{ "a" :\n [ 1 , 2 ] , "b":true}',
	b'[{"k":7},{"k":8,"m":[9,[10]]}]',
	b'\t{"deep":{"er":{"est":[1,2,3]}},"n":null}',
]


def _expected(node: Any, keys: list[bytes], numbers: list[bytes]) -> None:
	"""Keys and number spellings, in the order the text holds them."""
	if isinstance(node, dict):
		for key, value in node.items():
			keys.append(key.encode())
			_expected(value, keys, numbers)
	elif isinstance(node, list):
		for value in node:
			_expected(value, keys, numbers)
	elif isinstance(node, int) and not isinstance(node, bool):
		numbers.append(str(node).encode())


@pytest.mark.skipif(LUA is None, reason="no Lua interpreter")
@pytest.mark.parametrize("document", DOCUMENTS)
def test_a_document_reads_as_json_loads_reads_it(tmp_path: Path,
		document: bytes) -> None:
	consumed, rows = dissect(tmp_path, JSON, "value", document)

	keys: list[bytes] = []
	numbers: list[bytes] = []
	_expected(json.loads(document), keys, numbers)

	assert consumed == len(document)
	assert [bytes.fromhex(value) for field, _, _, value in rows
	        if field == "member.key"] == keys
	assert [bytes.fromhex(value) for field, _, _, value in rows
	        if field == "number.value"] == numbers
