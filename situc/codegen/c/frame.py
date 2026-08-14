"""Stream framing in C: rung 4 of the layer ladder (26.96).

A byte stream in, whole messages out. This is the rung whose new permission is
holding *bytes* between calls -- not messages, which is rung 5, and not memory
of its own, which is rung 2. The buffer is the caller's and its capacity is
the caller's, so nothing here allocates.

**Most of the work already existed.** Every struct already carries
`situ_X_required(data, have, &need)`, which answers "how many bytes does a
whole one need" from the bytes themselves. It is generated even for fixed
structs, on the stated reasoning that a caller framing a stream should not
write one loop for the fixed messages and another for the rest -- reasoning
written before anything framed a stream. This is the caller it described.

WHY `next` DOES NOT CONSUME. The view it returns points into the reader's
buffer, and dropping a message means moving what follows down over it. A
reader that consumed as it returned would hand out a view and invalidate it
on the next call, which is the lifetime bug that makes streaming parsers
miserable. `advance` is separate, and the comment says exactly when the view
dies. Two calls, and the cost is visible rather than surprising.
"""

from __future__ import annotations

from situc import ast
from situc.codegen.c.names import ident, macro
from situc.resolve import ResolvedSchema, ResolvedStruct
from situc.traverse import frameable

__all__ = ["framed_structs", "generate"]


def framed_structs(resolved: ResolvedSchema) -> list[ResolvedStruct]:
	"""Every struct a stream can be framed into.

	A register is a bus transaction rather than bytes off a wire, and a
	zero-byte struct would frame an unbounded number of empty messages out of
	an empty stream.

	**And a struct has to be frameable**, which is a different question from
	whether it is a struct: `traverse.frameable` asks whether a whole one can
	be recognised in a prefix of a stream, and the reader is built out of the
	`_required` that only a frameable struct has. Emitting one for the rest
	produced a reader calling a function nobody declared -- which compiled
	nowhere, and was noticed nowhere either, because until the checks suite
	learned to include this header nothing ever compiled it.
	"""
	return [struct for struct in resolved.structs.values()
	        if struct.layout.register is None
	        and not (struct.layout.is_fixed_size
	                 and struct.layout.size_bytes == 0)
	        and frameable(resolved.structs, struct)]


def _one(struct: ResolvedStruct, prefix: str) -> list[str]:
	name     = ident(prefix, struct.name, "reader")
	required = ident(prefix, struct.name, "required")
	view     = ident(prefix, struct.name, "view")

	return [
		f"/* A stream reader for `{struct.name}`.",
		" *",
		" * The buffer is yours and so is its size. Push bytes as they arrive,",
		" * call `next` until it answers SITU_ERR_TRUNCATED, and call",
		" * `advance` when you are finished with each message.",
		" */",
		f"typedef struct {{",
		"\tuint8_t  *buf;",
		"\tuint32_t  cap;",
		"\tuint32_t  have;",
		"\tuint32_t  ready;\t/* bytes `next` handed out, 0 when none */",
		f"}} {name}_t;",
		"",
		f"static inline void {name}_init({name}_t *reader, uint8_t *buf,",
		"                               uint32_t cap)",
		"{",
		"\treader->buf   = buf;",
		"\treader->cap   = cap;",
		"\treader->have  = 0u;",
		"\treader->ready = 0u;",
		"}",
		"",
		"/* Drop the message `next` last returned.",
		" *",
		" * THE VIEW `next` HANDED BACK IS DEAD FROM HERE ON. It pointed into",
		" * this buffer, and what follows has moved down over it.",
		" */",
		f"static inline void {name}_advance({name}_t *reader)",
		"{",
		"\tif (reader->ready == 0u) {",
		"\t\treturn;",
		"\t}",
		"\tfor (uint32_t i = reader->ready; i < reader->have; i++) {",
		"\t\treader->buf[i - reader->ready] = reader->buf[i];",
		"\t}",
		"\treader->have -= reader->ready;",
		"\treader->ready = 0u;",
		"}",
		"",
		"/* Append what arrived. SITU_ERR_BOUNDS where it does not fit, which",
		" * says the buffer is smaller than the message being assembled -- a",
		" * capacity question rather than a malformed one. */",
		f"static inline situ_err_t {name}_push({name}_t *reader,",
		"                                     const uint8_t *data, uint32_t len)",
		"{",
		f"\t{name}_advance(reader);",
		"",
		"\tif (len > reader->cap - reader->have) {",
		"\t\treturn SITU_ERR_BOUNDS;",
		"\t}",
		"\tfor (uint32_t i = 0u; i < len; i++) {",
		"\t\treader->buf[reader->have + i] = data[i];",
		"\t}",
		"\treader->have += len;",
		"\treturn SITU_OK;",
		"}",
		"",
		"/* The next whole message, or SITU_ERR_TRUNCATED where the stream has",
		" * not carried one yet.",
		" *",
		" * SITU_ERR_BOUNDS where a whole one needs more than this buffer can",
		" * ever hold. That never becomes true by waiting, so it is reported",
		" * rather than left to spin on a stream that will never satisfy it. */",
		f"static inline situ_err_t {name}_next({name}_t *reader,",
		"                                     situ_msg_t *msg, situ_view_t *out)",
		"{",
		f"\t{name}_advance(reader);",
		"",
		"\tuint32_t need = 0u;",
		f"\tsitu_err_t err = {required}(reader->buf, reader->have, &need);",
		"\tif (err != SITU_OK) {",
		"\t\tif (need > reader->cap) {",
		"\t\t\treturn SITU_ERR_BOUNDS;",
		"\t\t}",
		"\t\treturn err;",
		"\t}",
		"",
		"\tsitu_msg_init(msg, reader->buf, need);",
		f"\terr = {view}(msg, 0u, out);"
		if struct.layout.is_fixed_size else
		f"\terr = {view}(msg, 0u, need, out);",
		"\tif (err != SITU_OK) {",
		"\t\treturn err;",
		"\t}",
		"\treader->ready = need;",
		"\treturn SITU_OK;",
		"}",
		"",
	]


def generate(schema: ast.Schema, resolved: ResolvedSchema, basename: str,
		prefix: str = "situ") -> dict[str, str]:
	"""The framing header, or nothing where no struct can be framed."""
	structs = framed_structs(resolved)
	if not structs:
		return {}

	guard = macro(prefix, basename, "FRAME_H")
	lines = [
		f"/* Generated by situc from {basename}.situ -- do not edit.",
		" *",
		" * Stream framing: a byte stream in, whole messages out. Rung 4 of the",
		" * layer ladder, whose new permission is holding bytes between calls.",
		" * The buffer is the caller's and nothing here allocates.",
		" *",
		" * A separate header on purpose -- a rung adds files rather than",
		" * changing them (decision 0032).",
		" */",
		"",
		f"#ifndef {guard}",
		f"#define {guard}",
		"",
		"#include \"situ.h\"",
		f"#include \"{basename}.h\"",
		"",
		"#ifdef __cplusplus",
		"extern \"C\" {",
		"#endif",
		"",
	]

	for struct in structs:
		lines.extend(_one(struct, prefix))

	lines += [
		"#ifdef __cplusplus",
		"}",
		"#endif",
		"",
		f"#endif /* {guard} */",
	]

	return {f"{basename}_frame.h": "\n".join(lines) + "\n"}
