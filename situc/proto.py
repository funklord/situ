"""Import a `.proto` file as a description of its wire format (section 19.2).

A transpiler to a description of an encoding, not an attempt to preserve
protobuf semantics. The output describes the bytes a protobuf encoder produces,
in the style of section 9.7 -- which is why the capability map it yields is so
unflattering: those are the properties of the wire format, and situ's job is to
report them rather than to soften them.

**The fidelity report is the feature.** Section 19.2 is blunt about it: an
importer that silently produces a plausible-looking schema is worse than no
importer, because the user will trust it. So every construct that cannot be
represented is listed with its location and its reason, and the run exits
non-zero unless the user says `--accept-lossy` -- which downgrades to warnings
rather than hiding them.

The parser here is deliberately small. It reads the subset of `.proto` that has
a wire-format meaning and refuses the rest by name; a fuller parse would mean
understanding constructs the translation could not use anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Protobuf wire types, which decide how a value's extent is found.
WIRE_VARINT   = 0
WIRE_64BIT    = 1
WIRE_LENGTH   = 2
WIRE_START    = 3	# groups: not translatable
WIRE_END      = 4
WIRE_32BIT    = 5

# Scalar type -> (wire type, the situ type that describes the value).
SCALARS: dict[str, tuple[int, str]] = {
	"int32":    (WIRE_VARINT, "pb_varint"),
	"int64":    (WIRE_VARINT, "pb_varint"),
	"uint32":   (WIRE_VARINT, "pb_varint"),
	"uint64":   (WIRE_VARINT, "pb_varint"),
	"bool":     (WIRE_VARINT, "pb_varint"),
	"sint32":   (WIRE_VARINT, "pb_zigzag"),
	"sint64":   (WIRE_VARINT, "pb_zigzag"),
	"fixed64":  (WIRE_64BIT,  "u64"),
	"sfixed64": (WIRE_64BIT,  "i64"),
	"double":   (WIRE_64BIT,  "f64"),
	"fixed32":  (WIRE_32BIT,  "u32"),
	"sfixed32": (WIRE_32BIT,  "i32"),
	"float":    (WIRE_32BIT,  "f32"),
	"string":   (WIRE_LENGTH, "u8"),
	"bytes":    (WIRE_LENGTH, "u8"),
}

# Well-known types that depend on reflection, which a layout cannot express.
REFLECTIVE = frozenset({
	"google.protobuf.Any", "Any",
	"google.protobuf.Struct", "Struct",
	"google.protobuf.Value", "Value",
	"google.protobuf.ListValue", "ListValue",
})


@dataclass(frozen=True)
class Loss:
	"""One construct the translation could not represent."""

	line: int
	construct: str
	reason: str
	remedy: str = ""

	def render(self, path: str) -> str:
		lines = [f"{path}:{self.line}: {self.construct}", f"    {self.reason}"]
		if self.remedy:
			lines.append(f"    remedy: {self.remedy}")
		return "\n".join(lines)


@dataclass
class Field:
	number: int
	name: str
	proto_type: str
	repeated: bool = False
	line: int = 0


@dataclass
class Message:
	name: str
	line: int
	fields: list[Field]          = field(default_factory=list)
	oneofs: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class Enum:
	name: str
	line: int
	values: list[tuple[str, int]] = field(default_factory=list)


@dataclass
class Imported:
	"""Everything the translation needs, plus everything it had to give up."""

	syntax: str                = "proto2"
	package: str               = ""
	messages: list[Message]    = field(default_factory=list)
	enums: list[Enum]          = field(default_factory=list)
	losses: list[Loss]         = field(default_factory=list)

	@property
	def lossy(self) -> bool:
		return bool(self.losses)


# ---------------------------------------------------------------------------
# Reading the .proto
# ---------------------------------------------------------------------------

_COMMENT   = re.compile(r"//[^\n]*|/\*.*?\*/", re.S)
_SYNTAX    = re.compile(r'^\s*syntax\s*=\s*"([^"]+)"\s*;')
_PACKAGE   = re.compile(r"^\s*package\s+([A-Za-z0-9_.]+)\s*;")
_MESSAGE   = re.compile(r"^\s*message\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_ENUM      = re.compile(r"^\s*enum\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_ONEOF     = re.compile(r"^\s*oneof\s+([A-Za-z_][A-Za-z0-9_]*)\s*\{")
_SERVICE   = re.compile(r"^\s*(service|rpc)\s+([A-Za-z_][A-Za-z0-9_]*)")
_GROUP     = re.compile(r"^\s*(?:optional|required|repeated)?\s*group\s+"
	r"([A-Za-z_][A-Za-z0-9_]*)")
_MAP       = re.compile(r"^\s*map\s*<\s*([A-Za-z0-9_.]+)\s*,\s*([A-Za-z0-9_.]+)\s*>"
	r"\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+)")
_FIELD     = re.compile(r"^\s*(optional|required|repeated)?\s*"
	r"([A-Za-z0-9_.]+)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\d+)")
_ENUM_VAL  = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(-?\d+)")
#: Statements with no wire-format meaning, which are skipped in silence
#: because there is nothing to report: `reserved` forbids field numbers
#: nobody may use, and an `option` outside a field configures a code
#: generator rather than an encoding.
_RESERVED  = re.compile(r"^\s*(reserved|option)\b")

#: Statements that mean *there are more bytes on the wire than this file
#: describes*, which is the one thing 19.2 says an importer must not hide.
#: They were in `_RESERVED` above, swallowed by a pattern named for the
#: harmless case (26.186).
_IMPORT    = re.compile(r'^\s*import\s+(?:public\s+|weak\s+)?"([^"]+)"')
_EXTENSIONS = re.compile(r"^\s*extensions\s+([0-9]+)\s*to\s*([0-9]+|max)")
_EXTEND    = re.compile(r"^\s*extend\s+([A-Za-z_][A-Za-z0-9_.]*)")


def _statements(text: str) -> list[tuple[int, str]]:
	"""One statement at a time, with the line it came from.

	`.proto` is not line-oriented -- `message M { optional int32 a = 1; }` is
	legal on one line -- so scanning physical lines would silently miss a whole
	message. Splitting on the punctuation that ends a statement costs nothing
	and keeps the line number, which the fidelity report needs.
	"""
	found: list[tuple[int, str]] = []

	for number, raw in enumerate(_COMMENT.sub("", text).splitlines(), start=1):
		buffer = ""
		for piece in re.split(r"([{};])", raw):
			if piece in ("{", "}", ";"):
				statement = (buffer + piece).strip()
				if statement:
					found.append((number, statement))
				buffer = ""
			else:
				buffer += piece

		if buffer.strip():
			found.append((number, buffer.strip()))

	return found


def read(text: str) -> Imported:
	"""Parse the subset of `.proto` that has a wire-format meaning.

	Deliberately small. A fuller parse would mean understanding constructs the
	translation could not use, and every construct it refuses is refused by
	name rather than by falling off the end of a grammar.
	"""
	result   = Imported()
	stack: list[object] = []
	oneof: str | None   = None

	for number, raw in _statements(text):
		line = raw.strip()
		if not line:
			continue

		if line.startswith("}"):
			if oneof is not None:
				oneof = None
			elif stack:
				stack.pop()
			continue

		match = _SYNTAX.match(raw)
		if match:
			result.syntax = match.group(1)
			continue

		match = _PACKAGE.match(raw)
		if match:
			result.package = match.group(1)
			continue

		if _RESERVED.match(raw):
			continue

		match = _IMPORT.match(raw)
		if match:
			result.losses.append(Loss(
				number, f'import "{match.group(1)}"',
				"this importer reads one file: the types that file defines "
				"are not resolved, so a field using one is described by a "
				"guess rather than by its declaration",
				"import the referenced file separately, or paste the types "
				"it defines into this one"))
			continue

		match = _EXTENSIONS.match(raw)
		if match:
			result.losses.append(Loss(
				number, f"extensions {match.group(1)} to {match.group(2)}",
				"the range is reserved for fields declared in other files, "
				"so a message may carry fields this schema does not list",
				"the tlv region keeps unknown fields; what is lost is their "
				"names and types, not their bytes"))
			continue

		match = _EXTEND.match(raw)
		if match:
			result.losses.append(Loss(
				number, f"extend `{match.group(1)}`",
				"the fields it adds appear on the wire in the extended "
				"message and are not listed there",
				"declare them in the message itself, where the import can "
				"see them"))
			if line.endswith("{"):
				stack.append("extend")
			continue

		match = _SERVICE.match(raw)
		if match:
			result.losses.append(Loss(
				number, f"{match.group(1)} `{match.group(2)}`",
				"services and RPC definitions describe behaviour, not bytes, "
				"and are out of scope entirely",
				"drop it; the messages it carries import on their own"))
			if line.endswith("{"):
				stack.append("service")
			continue

		match = _GROUP.match(raw)
		if match:
			result.losses.append(Loss(
				number, f"group `{match.group(1)}`",
				"groups use wire types 3 and 4, which have no length prefix: "
				"an unknown group cannot be skipped without parsing it",
				"groups are deprecated in protobuf; replace it with a nested "
				"message"))
			if line.endswith("{"):
				stack.append("group")
			continue

		match = _MESSAGE.match(raw)
		if match:
			message = Message(match.group(1), number)
			result.messages.append(message)
			stack.append(message)
			continue

		match = _ENUM.match(raw)
		if match:
			enum = Enum(match.group(1), number)
			result.enums.append(enum)
			stack.append(enum)
			continue

		match = _ONEOF.match(raw)
		if match:
			oneof = match.group(1)
			if stack and isinstance(stack[-1], Message):
				stack[-1].oneofs.setdefault(oneof, [])
			continue

		match = _MAP.match(raw)
		if match:
			result.losses.append(Loss(
				number, f"map<{match.group(1)}, {match.group(2)}> "
				f"`{match.group(3)}`",
				"a map is unordered and its entry encoding is an implementation "
				"detail, so the same content has many encodings and none of "
				"them is the canonical one",
				"model it as `repeated` entries of an explicit message with "
				"`key` and `value` fields, which is what the wire format "
				"already is"))
			continue

		if isinstance(stack[-1] if stack else None, Enum):
			match = _ENUM_VAL.match(raw)
			if match:
				enum = stack[-1]		# type: ignore[assignment]
				enum.values.append((match.group(1), int(match.group(2))))
			continue

		match = _FIELD.match(raw)
		if match and isinstance(stack[-1] if stack else None, Message):
			label, proto_type, name, index = match.groups()
			message = stack[-1]		# type: ignore[assignment]

			if proto_type in REFLECTIVE:
				result.losses.append(Loss(
					number, f"field `{name}` of type `{proto_type}`",
					"reflection-dependent well-known types carry a type name "
					"and a payload whose meaning is resolved at run time, which "
					"a layout cannot express",
					"replace it with the concrete message it actually carries"))
				continue

			message.fields.append(Field(
				number     = int(index),
				name       = name,
				proto_type = proto_type,
				repeated   = label == "repeated",
				line       = number,
			))
			if oneof is not None:
				message.oneofs[oneof].append(int(index))

	# A field whose type is neither a scalar nor declared in this file. The
	# translation's fallback is "a nested message: length-prefixed", which is
	# a *guess*, and it is wrong whenever the name turns out to be an enum or
	# a fixed-width type: the same enum field reads `wire = 0` when it is
	# declared here and `wire = 2, type = u8` when it comes from an import.
	# Reported rather than guessed in silence, because 19.2's whole argument
	# is that "an importer that silently produces a plausible-looking schema
	# is worse than no importer, because the user will trust it" (26.186).
	declared = ({held.name for held in result.messages}
	            | {held.name for held in result.enums})
	for message in result.messages:
		for held in message.fields:
			bare = held.proto_type.rpartition(".")[2]
			if held.proto_type in SCALARS or bare in declared \
					or held.proto_type in declared:
				continue
			result.losses.append(Loss(
				held.line,
				f"field `{held.name}` of type `{held.proto_type}`",
				"the type is not declared in this file, so its wire type is "
				"a guess: it is described as a length-delimited message, and "
				"an enum or a fixed-width type is neither",
				"declare the type here, or check the guess against the file "
				"that does declare it"))

	if result.syntax == "proto3":
		result.losses.append(Loss(
			1, 'syntax = "proto3"',
			"proto3 cannot distinguish an absent scalar from one set to zero, "
			"and that is a semantic property with no expression in a layout: "
			"both encode as no bytes at all",
			"the wire format still imports; what does not survive is the "
			"distinction, so treat every scalar as present-with-a-default"))

	return result


# ---------------------------------------------------------------------------
# Translating to situ
# ---------------------------------------------------------------------------


PREAMBLE = """// Generated by `situc import-proto` from {source}.
//
// This describes the protobuf *wire format* for those messages, in the style of
// project.md section 9.7. It is a description of an encoding rather than a
// translation of protobuf's semantics, and the capability map it produces is
// unflattering on purpose: those are the properties of the format.
//
// Read the map before trusting the schema. `situc explain` will enumerate every
// reason this format is not canonical, each with a source location here.

target buffer;
endian little;

// No `minimal`: the protobuf wire format accepts non-minimal varint encodings,
// so a value has more than one legal encoding.
varint_type pb_varint {{
	encoding = leb128;
	max_bits = 64;
}}
"""

ZIGZAG = """
varint_type pb_zigzag {
	encoding = leb128;
	transform = zigzag;
	max_bits = 64;
}
"""


def translate(imported: Imported, source: str) -> str:
	"""Render the imported messages as a situ schema.

	Every message becomes a struct holding one `tlv` region, because that is
	what a protobuf message is on the wire: a run of self-describing items with
	no positional structure at all. The `known` map is what recovers the field
	names, and it is the only part of a protobuf encoding that a schema can
	pin down.
	"""
	lines = [PREAMBLE.format(source=source)]

	if _needs_zigzag(imported):
		lines.append(ZIGZAG.rstrip())

	for enum in imported.enums:
		lines.append(_enum(enum))

	for message in imported.messages:
		lines.append(_message(message, imported))

	lines.append(_requirements(imported))
	return "\n".join(lines).rstrip() + "\n"


def _needs_zigzag(imported: Imported) -> bool:
	return any(SCALARS.get(held.proto_type, (0, ""))[1] == "pb_zigzag"
	           for message in imported.messages for held in message.fields)


def _enum(enum: Enum) -> str:
	"""`default = pass`, which is protobuf's semantics and a cost worth naming.

	An unknown enum value is accepted and preserved, so the encoding admits
	values the schema does not name -- which is one of the reasons a protobuf
	message is not canonical.
	"""
	lines = [
		f"// Imported from `enum {enum.name}`. `default = pass` is protobuf's",
		"// semantics: an unknown value is accepted and preserved, which makes",
		"// the enclosing struct NonCanonical. That is a property of protobuf,",
		"// not of this translation.",
		"//",
		"// Backed by `i32` because that is what a protobuf enum value is; on",
		"// the wire it is varint-encoded, which is what the `known` entry says.",
		f"enum {_snake(enum.name)} : i32 {{",
	]
	for name, value in enum.values:
		lines.append(f"\t{name} = {value},")
	lines.append("\tdefault = pass,")
	lines.append("}")
	return "\n".join(lines) + "\n"


def _message(message: Message, imported: Imported) -> str:
	"""One struct holding one `tlv` region.

	There is nothing positional to describe: a protobuf message is a run of
	self-describing items in any order, so the struct has exactly one member
	and every field name is recovered through the `known` map.
	"""
	known: list[str] = []
	notes: list[str] = []

	for held in sorted(message.fields, key=lambda f: f.number):
		wire, situ_type = _wire_and_type(held, imported)
		known.append(f"\t\t\t{held.number} : {{ name = {held.name}, "
		             f"wire = {wire}, type = {situ_type} }},")

	repeated = any(held.repeated for held in message.fields)

	for name, numbers in message.oneofs.items():
		listed = ", ".join(str(number) for number in sorted(numbers))
		notes.append(
			f"// `oneof {name}` covered fields {listed}. At most one of them is\n"
			"// set, which is a semantic constraint the layout cannot enforce:\n"
			"// on the wire they are ordinary fields, and an encoder that set\n"
			"// two would produce bytes this schema accepts.")

	lines = [
		f"// Imported from `message {message.name}`.",
		"//",
		"// One `tlv` region and nothing else, because that is what the message",
		"// is on the wire: a run of self-describing items in any order, with no",
		"// positional structure to describe.",
	]
	lines.extend(notes)
	lines.extend([
		f"struct {_snake(message.name)} {{",
		"\ttlv fields (",
		"\t\ttag_type       = pb_varint,",
		"\t\ttag_decode     = { field = tag >> 3, wire = tag & 0x7 },",
		"\t\ttag_identity   = field,         // what a `known` key is (0023)",
		"\t\tvalue_size     = switch (wire) {",
		"\t\t\tcase 0: self_delimiting,",
		"\t\t\tcase 1: 8,",
		"\t\t\tcase 2: prefixed(pb_varint),",
		"\t\t\tcase 5: 4,",
		"\t\t\tdefault: error,             // groups (3, 4) unsupported",
		"\t\t},",
	])

	if repeated:
		lines.append("\t\tduplicate_tags = allowed,       // `repeated` fields")
	else:
		lines.append("\t\tduplicate_tags = allowed,       // protobuf accepts a")
		lines.append("\t\t                                // repeat of any field")

	if known:
		lines.append("\t\tknown = {")
		lines.extend(known)
		lines.append("\t\t},")

	lines.extend([
		"\t\tunknown = preserve              // protobuf semantics",
		"\t);",
		"}",
	])
	return "\n".join(lines) + "\n"


def _wire_and_type(held: Field, imported: Imported) -> tuple[int, str]:
	scalar = SCALARS.get(held.proto_type)
	if scalar is not None:
		return scalar

	if any(enum.name == held.proto_type for enum in imported.enums):
		return WIRE_VARINT, _snake(held.proto_type)

	# A nested message: length-prefixed, and its interior is another tlv.
	return WIRE_LENGTH, "u8"


def _requirements(imported: Imported) -> str:
	"""What the schema asserts, and what it can actually require.

	The assertions fail, and are assertions for that reason: each one is a true
	statement about protobuf that the schema exists to record rather than a
	budget the format could meet.
	"""
	if not imported.messages:
		return ""

	first = _snake(imported.messages[0].name)
	return "\n".join([
		"// Every one of these fails, and that is the point: each is a true",
		"// statement about the protobuf wire format, recorded rather than",
		"// wished away. `situc explain` names the cause of each.",
		f"assert canonical({first});",
		f"assert random_access({first}.fields);",
		f"assert stable_address({first}.fields);",
		"",
		"// This one holds, and is the useful half: the region starts where it",
		"// starts, and situ can say so even about a format this hostile.",
		f"require absolute_static({first}.fields);",
	]) + "\n"


def _snake(name: str) -> str:
	"""`UserProfile` -> `user_profile`, matching the tree's convention.

	Casing is not prescribed (decision 0013), but a generated schema has to
	pick one, and the one every other schema here uses is the least surprising.
	"""
	out = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
	return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", out).lower()


def report(imported: Imported, path: str) -> str:
	"""The fidelity report, which is the feature (section 19.2).

	An importer that silently produced a plausible-looking schema would be
	worse than no importer, because the user would trust it.
	"""
	if not imported.losses:
		return ""

	lines = [
		f"{len(imported.losses)} construct(s) in {path} have no expression in a "
		"byte layout.",
		"",
	]
	for loss in imported.losses:
		lines.append(loss.render(path))
		lines.append("")

	lines.append("The generated schema describes everything else faithfully. "
	             "Pass --accept-lossy")
	lines.append("to take it anyway, with these as warnings.")
	return "\n".join(lines) + "\n"
