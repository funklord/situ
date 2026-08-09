"""Framing and conversations, walked (26.96, 26.97).

The two rungs that hold state, for an image rather than for compiled code.
Neither needed the image to grow, which is worth saying because the relation
sections of 26.95 did:

**Framing needs no framing section.** `struct_extent` already measures one
instance from its own bytes -- that is what `access = Sequential` costs and
what makes a run of them walkable -- so a reader is that function plus a
buffer it holds between calls.

**Conversations need no key.** The compiled backends pack the relation's
equality fields into a `u64` and hash it, because comparing every pending
request would be a loop in someone's hot path. A walker has no hot path and
does have the predicate, so it *runs the relation* against each outstanding
request instead. That is slower, exactly correct, and cannot disagree with
the compiled answer -- it is the same program the fifth column already
evaluates.

The cost is stated rather than hidden: matching is O(pending) here and O(1)
there, and a walker that grew a key to avoid it would be re-deriving what
`conversation_key` already knows in a place that cannot see the schema.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from walker.image import Image
from walker.report import OK, relate
from walker.walk import Refused, View, acquire, struct_extent


@dataclass
class Reader:
	"""A byte stream in, whole messages out (rung 4).

	Holds bytes between calls, which is the rung's whole permission. `next`
	does not consume: the view it returns reads this buffer, and `advance`
	is what drops the message -- the same two-call shape the compiled
	readers have, and for the same reason.
	"""

	image: Image
	struct: int
	buffer: bytearray = field(default_factory=bytearray)
	ready: int = 0

	def push(self, data: bytes) -> None:
		self.advance()
		self.buffer.extend(data)

	def advance(self) -> None:
		if self.ready:
			del self.buffer[:self.ready]
			self.ready = 0

	def next(self) -> View | None:
		"""The next whole message, or None where the stream has not carried
		one yet.

		None rather than an exception: "not yet" is the ordinary answer when
		feeding a stream, and a walker reporting it as a refusal would make
		every caller catch the common case.
		"""
		self.advance()
		if not self.buffer:
			return None

		try:
			probe = acquire(self.image, bytes(self.buffer), self.struct)
			need  = struct_extent(probe)
		except Refused:
			return None

		if need == 0 or need > len(self.buffer):
			return None

		self.ready = need
		return acquire(self.image, bytes(self.buffer[:need]), self.struct)


@dataclass
class Conversation:
	"""Pending requests, matched by running the relation (rung 5).

	`cap` is honoured for the reason 26.97 gives: the bound is not about
	representation but about refusing somebody who opens exchanges and never
	answers them, and that is the same problem whoever is walking.
	"""

	image: Image
	relation: int
	cap: int
	pending: list[tuple[View, int]] = field(default_factory=list)

	def record(self, request: View, handle: int) -> bool:
		"""Remember a request. False where the table is full."""
		if len(self.pending) >= self.cap:
			return False
		self.pending.append((request, handle))
		return True

	def take(self, response: View) -> int | None:
		"""The handle of the request this answers, forgotten as it is
		returned. None where nothing outstanding matches.

		Forgetting is what makes a duplicate answer None, exactly as the
		compiled tables answer CONSTRAINT for it.
		"""
		for index, (request, handle) in enumerate(self.pending):
			if relate(self.image, self.relation, request, response) == OK:
				del self.pending[index]
				return handle
		return None
