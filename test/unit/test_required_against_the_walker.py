"""`required` never calls a valid message complete before it ends.

The corpus differential compares the four backends' `required` with each
other, and four answers that come from one shared predicate agree however
wrong they are: 26.744 and 26.745 were both in all four. This holds the
answer against a witness that shares nothing with them -- the walker's
`struct_extent`, measured from the image over the whole buffer.

Over buffers the Python backend's `validate` accepts, for every struct the
drivers frame:

- `required` of the whole buffer may not be COMPLETE at fewer bytes than the
  walker measures, and
- `required` of one byte less than the walker's extent may not be complete
  at all.

Both are the one-sided property `required` exists for: a caller told
"complete" parses what it was handed. The other direction -- asking for
more than a message turned out to need -- is allowed and is common: a token
that ends at a delimiter cannot be known to have ended until the next byte
arrives. The backends agree on `required` (the differential holds that), so
asking Python asks all four.

It found 26.745 on its first run: `hop step[] while (remaining > 4)` ends
its run where the FRAME ends, and on a prefix the frame is the prefix, so
all four called an eight-byte message complete at six.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pytest						# noqa: E402

import fourway						# noqa: E402
from situc.codegen import differ			# noqa: E402
from situc.codegen.c.names import c_name		# noqa: E402
from situc.codegen.python import generate as generate_py	# noqa: E402
from situc.pack import pack				# noqa: E402
from walker.image import load				# noqa: E402
from walker.walk import acquire, struct_extent		# noqa: E402

from every_schema import SCHEMAS			# noqa: E402
from test_four_backends_frame_alike import analysed, ids	# noqa: E402

#: Buffers per framed struct. Most do not validate; this is enough that the
#: corpus yields thousands that do (measured: 6,000 or so at this figure).
DRAWS = 60

#: schema -> valid buffers the property was asked about.
ASKED: dict[str, int] = {}


def _module(text: str, tmp_path: Path) -> tuple[Any, Any]:
	"""The generated module, with this tree's runtime beside it.

	The runtime is copied next to the module rather than found on
	`sys.path`, and the exception classes are taken FROM the module: other
	tests put their own copy of `situ_runtime` first, and an `except
	rt.SituError` naming one copy does not catch what the other raises --
	which is how this file first failed inside the full suite while passing
	alone. Returned beside the module, because a module imports only the
	runtime names it uses.
	"""
	(tmp_path / "situ_runtime.py").write_text(
		(ROOT / "runtime" / "python" / "situ_runtime.py").read_text(
			encoding="ascii"), encoding="ascii")
	path = tmp_path / "framed_unit.py"
	path.write_text(text, encoding="ascii")
	sys.path.insert(0, str(tmp_path))
	saved = sys.modules.pop("situ_runtime", None)
	try:
		spec = importlib.util.spec_from_file_location("framed_unit", path)
		assert spec is not None and spec.loader is not None
		module = importlib.util.module_from_spec(spec)
		spec.loader.exec_module(module)
		runtime = sys.modules["situ_runtime"]
	finally:
		sys.path.remove(str(tmp_path))
		sys.modules.pop("situ_runtime", None)
		if saved is not None:
			sys.modules["situ_runtime"] = saved
	return module, runtime


@pytest.mark.parametrize("schema", SCHEMAS, ids=ids(SCHEMAS))
def test_required_is_not_short_of_the_walker(schema: Path,
		tmp_path: Path) -> None:
	parsed, resolved = analysed(schema)
	try:
		names = differ.framed(resolved)
	except Exception:				# noqa: BLE001
		names = set()
	if not names:
		pytest.skip(f"{schema.name} frames nothing")

	module, runtime = _module(generate_py(parsed, resolved, "unit").module,
	                          tmp_path)
	image  = load(pack(parsed, resolved, metadata=True)[0])
	rng    = random.Random(f"{schema.name}-required-against-the-walker")
	situ_error = runtime.SituError
	truncated  = runtime.TruncatedError
	asked  = 0
	for name in sorted(names):
		view_class = getattr(module, c_name(name), None)
		if view_class is None or name not in image.struct_names:
			continue
		shape = image.struct_names.index(name)
		fixed = resolved.structs[name].layout.is_fixed_size
		for _ in range(DRAWS):
			buffer = fourway.planted(rng, parsed, resolved,
			                         fourway.draw(rng))
			message = runtime.Message(bytearray(buffer))
			try:
				view = (view_class.at(message, 0) if fixed
				        else view_class.at(message, 0, len(buffer)))
				view.validate()
			except situ_error:
				continue
			try:
				extent = struct_extent(acquire(image, buffer, shape))
			except Exception:			# noqa: BLE001
				continue		# the walker declines; nothing to hold
			asked += 1
			try:
				complete = view_class.required(bytes(buffer))
			except truncated:
				continue		# asking for more is allowed
			assert complete >= extent, (
				f"{schema.name}: `{name}` is complete at {complete} bytes "
				f"where the walker measures {extent}: {buffer.hex()}")
			if extent > 0:
				with pytest.raises(truncated):
					view_class.required(bytes(buffer[:extent - 1]))
	ASKED[schema.name] = asked


def test_the_property_was_asked_something() -> None:
	"""The non-vacuity companion: a buffer that never validates asks
	nothing, and every assertion above sits behind `validate`."""
	if not ASKED:
		pytest.skip("the per-schema checks did not run")
	assert sum(ASKED.values()) >= 2000, (
		f"only {sum(ASKED.values())} valid buffers over "
		f"{len(ASKED)} schemas")
