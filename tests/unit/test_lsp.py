"""The language server (section 26.19).

Two things are worth testing here and they are not the protocol plumbing. That
a half-written schema still produces something useful -- an editor asks about a
document in whatever state the user left it, and half-written is the normal
state -- and that the answers are the ones `situc explain` and the blame chains
already give, rather than a second, weaker computation of the same thing.

The plumbing gets one test anyway, at the end, and it is the one an editor
performs: launch `situc lsp` as a process, talk to it over its own stdin and
stdout, and read the replies back. Everything else here drives `Server` in
process over `BytesIO`, which exercises the framing and none of what a
subprocess adds -- the CLI wiring, binary-mode streams, and whether anything is
flushed before the server waits for the next request. An editor that gets no
diagnostics because they are sitting in a buffer sees a server that does not
work.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from situc.lsp import (
	Server, analyse_text, code_actions, definition_at, hover_at, symbols,
	to_lsp_diagnostic,
)

ROOT = Path(__file__).resolve().parents[2]
URI  = "file:///tmp/unit.situ"

SCHEMA = """target buffer;
endian big;
bit_order msb_first;

enum protocol : u8 {
	tcp = 6,
	udp = 17,
}

struct header [allow_straddle] {
	u4        version  [must_eq = 4];
	u4        ihl;
	u16       total;
	bit       flag;
	u15       offset;
	protocol  proto;
}
"""


def frame(message: dict[str, Any]) -> bytes:
	body = json.dumps(message).encode()
	return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def converse(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
	"""Run the server over a scripted session and collect what it said."""
	out = io.BytesIO()
	Server(io.BytesIO(b"".join(frame(m) for m in messages)), out).serve()

	replies  = []
	raw      = out.getvalue()
	while raw:
		header, _, rest = raw.partition(b"\r\n\r\n")
		length = int(header.split(b":")[1])
		replies.append(json.loads(rest[:length]))
		raw = rest[length:]
	return replies


# -- analysis ---------------------------------------------------------------


def test_a_broken_document_still_analyses() -> None:
	"""An editor asks about whatever the user has left on screen, and a server
	that raises on half a struct is a server nobody keeps running."""
	analysis = analyse_text(URI, "target buffer;\nstruct s { u8 a")

	assert analysis.diagnostics
	assert analysis.resolved is None


def test_an_empty_document_analyses() -> None:
	analysis = analyse_text(URI, "")

	assert analysis.resolved is not None or analysis.diagnostics


def test_a_failing_requirement_is_a_diagnostic() -> None:
	analysis = analyse_text(URI, SCHEMA + "\nrequire atomic(header.offset);\n")

	assert any("atomic" in d.message or
	           (d.primary and "atomic" in d.primary.message)
	           for d in analysis.diagnostics)


def test_the_blame_chain_survives_the_translation() -> None:
	"""Section 17 makes the chain the product. Flattening it into the message
	would lose it at the first newline, so it travels as related information
	where an editor will show it."""
	analysis = analyse_text(URI, SCHEMA + "\nrequire atomic(header.offset);\n")
	failing  = [d for d in analysis.diagnostics if d.notes or d.labels]
	assert failing, "expected a diagnostic carrying a chain"

	converted = to_lsp_diagnostic(analysis.source, failing[0])

	assert converted["source"] == "situ"
	assert converted["relatedInformation"]
	assert all("message" in item and "location" in item
	           for item in converted["relatedInformation"])


def test_positions_are_zero_based() -> None:
	"""situ counts lines from one and the protocol counts from zero. Getting
	that wrong puts every diagnostic one line off, which is worse than putting
	it nowhere."""
	analysis = analyse_text(URI, "target buffer;\nstruct s { u8 a")
	converted = to_lsp_diagnostic(analysis.source, analysis.diagnostics[0])

	assert converted["range"]["start"]["line"] == 1		# the second line


# -- hover ------------------------------------------------------------------


def line_of(text: str, needle: str) -> tuple[int, int]:
	for number, line in enumerate(text.splitlines()):
		if needle in line:
			return number, line.index(needle) + 1
	raise AssertionError(f"{needle!r} not in the schema")


def test_hover_gives_the_capability_vector() -> None:
	"""The reason to run a server rather than a linter: thirteen axes of
	consequence the source text does not show."""
	analysis = analyse_text(URI, SCHEMA)
	line, col = line_of(SCHEMA, "u15       offset")

	text = hover_at(analysis, line, col + 12)

	assert text is not None
	assert "header.offset" in text
	assert "`atomic` = NonAtomic" in text
	assert "`repr` = ValueConverted" in text


def test_hover_separates_weakened_axes_from_the_rest() -> None:
	"""Thirteen axes listed flat is a wall. What a reader wants is the ones
	that cost something, which is what `situc explain` marks too."""
	analysis = analyse_text(URI, SCHEMA)
	line, col = line_of(SCHEMA, "u15       offset")

	text = hover_at(analysis, line, col + 12)

	assert text is not None
	assert "Unweakened:" in text
	assert text.index("`atomic` = NonAtomic") < text.index("Unweakened:")


def test_hover_on_nothing_returns_nothing() -> None:
	analysis = analyse_text(URI, SCHEMA)

	assert hover_at(analysis, 0, 0) is None


def test_hover_on_a_broken_document_returns_nothing() -> None:
	"""Rather than raising. There is no vector to show, which is not an error."""
	analysis = analyse_text(URI, "target buffer;\nstruct s { u8 a")

	assert hover_at(analysis, 1, 12) is None


# -- symbols ----------------------------------------------------------------


def test_symbols_outline_the_schema() -> None:
	analysis = analyse_text(URI, SCHEMA)
	found    = symbols(analysis)

	names = {item["name"]: item for item in found}
	assert "protocol" in names and "header" in names
	assert len(names["header"]["children"]) >= 6
	assert any(child["name"] == "offset" for child in names["header"]["children"])


# -- the protocol ------------------------------------------------------------


def test_it_answers_initialize_and_exits() -> None:
	replies = converse([
		{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
		{"jsonrpc": "2.0", "method": "exit", "params": {}},
	])

	assert replies[0]["id"] == 1
	assert replies[0]["result"]["capabilities"]["hoverProvider"] is True


def test_opening_a_document_publishes_diagnostics() -> None:
	replies = converse([
		{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
		{"jsonrpc": "2.0", "method": "textDocument/didOpen",
		 "params": {"textDocument": {"uri": URI, "text": "target buffer;\nstruct s { u8 a"}}},
		{"jsonrpc": "2.0", "method": "exit", "params": {}},
	])

	published = [r for r in replies
	             if r.get("method") == "textDocument/publishDiagnostics"]
	assert published
	assert published[0]["params"]["uri"] == URI
	assert published[0]["params"]["diagnostics"]


def test_a_fixed_document_clears_its_diagnostics() -> None:
	"""The loop a user is actually in: break it, see the error, fix it, see the
	error go. A server that only ever adds is a server that lies."""
	replies = converse([
		{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
		{"jsonrpc": "2.0", "method": "textDocument/didOpen",
		 "params": {"textDocument": {"uri": URI, "text": "target buffer;\nstruct s { u8 a"}}},
		{"jsonrpc": "2.0", "method": "textDocument/didChange",
		 "params": {"textDocument": {"uri": URI},
		            "contentChanges": [{"text": SCHEMA}]}},
		{"jsonrpc": "2.0", "method": "exit", "params": {}},
	])

	published = [r["params"]["diagnostics"] for r in replies
	             if r.get("method") == "textDocument/publishDiagnostics"]
	assert len(published) == 2
	assert published[0]        # broken
	assert not published[1]    # fixed


def test_an_unanswerable_request_still_gets_an_answer() -> None:
	"""An editor that asked and was never told waits forever."""
	replies = converse([
		{"jsonrpc": "2.0", "id": 7, "method": "textDocument/codeLens", "params": {}},
		{"jsonrpc": "2.0", "method": "exit", "params": {}},
	])

	assert any(r.get("id") == 7 for r in replies)


def test_a_notification_gets_no_reply() -> None:
	"""Answering one is a protocol error, not a courtesy."""
	replies = converse([
		{"jsonrpc": "2.0", "method": "initialized", "params": {}},
		{"jsonrpc": "2.0", "method": "exit", "params": {}},
	])

	assert replies == []


def test_hover_over_the_protocol() -> None:
	replies = converse([
		{"jsonrpc": "2.0", "method": "textDocument/didOpen",
		 "params": {"textDocument": {"uri": URI, "text": SCHEMA}}},
		{"jsonrpc": "2.0", "id": 3, "method": "textDocument/hover",
		 "params": {"textDocument": {"uri": URI},
		            "position": {"line": line_of(SCHEMA, "u15       offset")[0],
		                         "character": 14}}},
		{"jsonrpc": "2.0", "method": "exit", "params": {}},
	])

	answer = next(r for r in replies if r.get("id") == 3)
	assert answer["result"]["contents"]["kind"] == "markdown"
	assert "header.offset" in answer["result"]["contents"]["value"]


def test_every_example_analyses_without_raising() -> None:
	"""The server must survive whatever a user opens, including the schemas
	this project ships that fail their own requirements on purpose."""
	for path in sorted(ROOT.glob("examples/*/*.situ")):
		analysis = analyse_text(f"file://{path}", path.read_text(encoding="ascii"))

		assert analysis.source is not None
		symbols(analysis)		# must not raise either


# -- code actions and definitions -------------------------------------------


def test_code_actions_carry_the_advisor_s_costs() -> None:
	"""Section 18.2's catalog is already ranked and costed. An editor is where
	a reader would rather see it, because the suggestion is about a field they
	are looking at."""
	schema = ("target buffer;\nendian big;\n"
	          "struct h { u8 v; u16 n; }\n"
	          "struct s { h hdr; u8 opts[hdr.n]; u32 after; }\n")
	analysis = analyse_text(URI, schema)
	line, col = line_of(schema, "opts[hdr.n]")

	actions = code_actions(analysis, line, col + 1)

	assert actions
	assert all("cost" in action["data"] for action in actions)
	assert all(action["kind"] == "refactor" for action in actions)


def test_code_actions_are_offered_rather_than_applied() -> None:
	"""A suggestion like "reorder the members" is a change with a cost the
	author has to agree to. Applying it silently would be situ making a design
	decision on somebody's behalf."""
	schema = ("target buffer;\nendian big;\n"
	          "struct h { u8 v; u16 n; }\n"
	          "struct s { h hdr; u8 opts[hdr.n]; u32 after; }\n")
	analysis = analyse_text(URI, schema)
	line, col = line_of(schema, "opts[hdr.n]")

	for action in code_actions(analysis, line, col + 1):
		assert "edit" not in action
		assert "disabled" in action


def test_definition_finds_a_type() -> None:
	"""A schema names types far more than it declares them, and the
	declaration is usually what a reader wants next."""
	analysis = analyse_text(URI, SCHEMA)
	line, col = line_of(SCHEMA, "protocol  proto")

	found = definition_at(analysis, line, col + 1)

	assert found is not None
	declared, _ = line_of(SCHEMA, "enum protocol")
	assert found["range"]["start"]["line"] == declared


def test_definition_of_a_scalar_is_nothing() -> None:
	"""`u16` is not declared anywhere, so there is nowhere to go."""
	analysis = analyse_text(URI, SCHEMA)
	line, col = line_of(SCHEMA, "u16       total")

	assert definition_at(analysis, line, col + 1) is None


def test_hover_says_why_an_axis_is_weakened() -> None:
	"""The vector alone answers the smaller half. A reader looking at a field
	wants to know what did that and what they may do instead, and the
	propagation table has carried both since section 11.3 -- `situc explain`
	printed them and hover did not, so the editor gave a weaker answer than the
	CLI for the same field."""
	analysis = analyse_text(URI, SCHEMA)
	line, col = line_of(SCHEMA, "u15       offset")

	text = hover_at(analysis, line, col + 12)

	assert text is not None
	assert "**Why:**" in text
	assert "remedy:" in text


def test_hover_on_a_derived_field_names_the_invariant() -> None:
	"""`mutate = Immutable` is true and unhelpful on its own: an editor is
	exactly where somebody wonders why a field they can see has no setter."""
	source = ("target buffer;\nendian big;\n"
	          "struct s {\n\tu16 total;\n\tu8 a;\n\tu32 b;\n}\n"
	          "invariant s.total == size(s.a) + size(s.b);\n")
	analysis = analyse_text(URI, source)

	text = hover_at(analysis, 3, 7)

	assert text is not None
	assert "`mutate` = Immutable" in text
	assert "a field an invariant maintains" in text
	assert "call the generated recompute" in text


def test_hover_without_a_weakening_has_no_why_section() -> None:
	"""An empty heading is worse than none: it reads as information withheld."""
	analysis = analyse_text(URI, "target buffer;\nstruct s { u8 a; }\n")

	text = hover_at(analysis, 1, 12)

	assert text is not None
	assert "**Why:**" not in text


# -- the process an editor launches -----------------------------------------


def test_the_server_answers_over_its_own_stdio() -> None:
	"""`situc lsp`, as a subprocess, over real pipes.

	The session is the smallest real one: initialize, open a document, take the
	diagnostics, shut down. What this covers that the in-process tests cannot
	is everything between `main` and `Server` -- the subcommand, the streams it
	is handed, and whether a reply reaches the client before the server blocks
	on the next request.
	"""
	messages: list[dict[str, Any]] = [
		{"jsonrpc": "2.0", "id": 1, "method": "initialize",
		 "params": {"capabilities": {}}},
		{"jsonrpc": "2.0", "method": "textDocument/didOpen",
		 "params": {"textDocument": {
			 "uri": URI, "languageId": "situ", "version": 1,
			 "text": "target buffer;\nendian big;\n"
			         "struct s { u8 a; u8 b[missing]; }\n"}}},
		{"jsonrpc": "2.0", "id": 2, "method": "shutdown"},
		{"jsonrpc": "2.0", "method": "exit"},
	]

	result = subprocess.run(
		[sys.executable, "-m", "situc", "lsp"],
		input=b"".join(frame(one) for one in messages),
		capture_output=True, timeout=60)

	assert result.returncode == 0, result.stderr.decode()

	replies, raw = [], result.stdout
	while raw:
		header, _, rest = raw.partition(b"\r\n\r\n")
		if not _:
			break
		length = int(header.split(b":")[1])
		replies.append(json.loads(rest[:length]))
		raw = rest[length:]

	assert [reply.get("id") for reply in replies if "id" in reply] == [1, 2]

	first = replies[0]["result"]["capabilities"]
	assert first["hoverProvider"] is True

	published = [reply for reply in replies
	             if reply.get("method") == "textDocument/publishDiagnostics"]
	assert published, "an opened document produced no diagnostics at all"
	assert published[0]["params"]["uri"] == URI
	# The document names a length field that is not there, so there is
	# something to say about it -- a server that publishes an empty list for a
	# broken document is the failure this notices.
	assert published[0]["params"]["diagnostics"]
