"""The tokio driver, held to a real tokio runtime over real sockets (0033).

`--driver tokio` is the second host runtime and the first Rust cell. It is
the other resolution of the problem the Qt driver posed: Qt's loop belongs to
the application, so that driver installs a notifier and returns and the
outcome arrives at a callback; an `async fn` *is* the awaitable, so this one
returns the outcome and the caller `.await`s it inside the runtime it
already has.

The behavioural test builds a real cargo project against tokio and drives an
exchange over two UDP sockets on loopback, with an in-process peer that drops
datagrams to force retransmission. It skips where cargo or the tokio crate is
unavailable, so an offline machine does not fail the suite.

The exchange it drives is keyed on a plain integer rather than on `dns`'s
`opcode`. That is not an arbitrary choice: the Rust `relate`, `converse` and
`drive` layers do not compile for a relation keyed on an enum-typed field --
see `test_the_rust_key_does_not_compile_for_an_enum_field` below, which pins
the defect rather than working around it silently.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from every_schema import ROOT

CARGO   = shutil.which("cargo")
RUNTIME = ROOT / "runtime" / "rust" / "situ_rt.rs"

#: An exchange whose key is a plain integer. `dns`'s `reply_to` keys on
#: `opcode`, which the Rust backend cannot compile (see the pinned defect at
#: the foot of this file), so the driver is exercised on a schema that does
#: reach the compiler.
SCHEMA = """target buffer;
endian big;

struct echo_msg {
\tu16  id;
\tu16  seq;
}

relation reply_to(query: echo_msg, reply: echo_msg)
\t\t[timeout_ms = 150, retries = 2] {
\tmust reply.id == query.id;
}
"""

CARGO_TOML = """[package]
name = "tokv"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1", features = ["full"] }

[[bin]]
name = "tokv"
path = "src/main.rs"
"""

#: Drives the generated artifact and prints what happened. The peer drops the
#: first two datagrams and echoes the third, so a reply arrives only if the
#: driver actually retransmitted -- which is what makes this a test of the
#: loop rather than of the socket.
MAIN = r"""
mod situ_rt;
mod echo;
mod echo_relate;
mod echo_frame;
mod echo_converse;
mod echo_drive;
mod echo_tokio;

use std::sync::Arc;
use std::sync::atomic::{AtomicU32, Ordering};

async fn peer(socket: Arc<tokio::net::UdpSocket>, seen: Arc<AtomicU32>,
              drop_first: u32) {
	let mut buf = [0u8; 2048];
	loop {
		let (n, from) = match socket.recv_from(&mut buf).await {
			Ok(pair) => pair,
			Err(_) => return,
		};
		let count = seen.fetch_add(1, Ordering::SeqCst) + 1;
		if count > drop_first {
			let _ = socket.send_to(&buf[..n], from).await;
		}
	}
}

#[tokio::main]
async fn main() {
	let peer_sock = Arc::new(tokio::net::UdpSocket::bind("127.0.0.1:0")
		.await.expect("peer bind"));
	let peer_addr = peer_sock.local_addr().expect("peer addr");

	// Bind as a std socket so the sink can keep a handle that performs the
	// real send(2): tokio's try_send reports WouldBlock from its own
	// readiness, which costs a retransmission and its retry budget together.
	let std_sock = std::net::UdpSocket::bind("127.0.0.1:0").expect("our bind");
	std_sock.connect(peer_addr).expect("connect");
	let send_on = Arc::new(std_sock.try_clone().expect("clone for sending"));
	std_sock.set_nonblocking(true).expect("nonblocking for tokio");
	let ours = Arc::new(tokio::net::UdpSocket::from_std(std_sock)
		.expect("adopt into tokio"));

	let seen = Arc::new(AtomicU32::new(0));
	let peer_task = tokio::spawn(peer(peer_sock.clone(), seen.clone(), 2));

	// A 4-byte echo_msg the peer echoes verbatim, so the reply's id equals
	// the query's and the exchange correlates.
	let request: [u8; 4] = [0x4e, 0x4f, 0x00, 0x01];
	let query = echo::EchoMsg::new(&request).expect("query view");

	let sink = echo_tokio::TokioIo::new(send_on.clone());
	let mut store = [echo_drive::ReplyToSlot::default(); 4];
	let mut drive = echo_drive::ReplyToDriver::new(&mut store, sink, 150, 2);

	let base = std::time::Instant::now();
	let handle: u32 = 0x1234;
	drive.send(&query, &request, handle, echo_tokio::now_ms(base))
		.expect("send");

	let replies = std::cell::Cell::new(0u32);
	let reply_id = std::cell::Cell::new(0u32);
	let expired = std::cell::Cell::new(0u32);

	// A watchdog, so a loop that never completes fails rather than hangs.
	let outcome = tokio::time::timeout(
		std::time::Duration::from_secs(5),
		echo_tokio::run_reply_to(
			&ours, &mut drive, base,
			|id| { replies.set(replies.get() + 1); reply_id.set(id); },
			|n| expired.set(expired.get() + n)),
	).await;

	peer_task.abort();

	match outcome {
		Ok(Ok(())) => {}
		Ok(Err(e)) => { println!("FAIL loop errored: {:?}", e); return; }
		Err(_) => { println!("FAIL watchdog fired"); return; }
	}

	println!("replies={} id=0x{:x} expired={} datagrams={}",
	         replies.get(), reply_id.get(), expired.get(),
	         seen.load(Ordering::SeqCst));
}
"""


def _cargo_project(tmp_path: Path) -> Path | None:
	"""Generate the driver into a cargo project, or None where the crate
	cannot be built -- an offline machine skips rather than fails."""
	schema = tmp_path / "echo.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	gen = tmp_path / "gen"
	built = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "rust", "--layer", "drive", "--driver", "tokio",
		 "--out", str(gen)],
		cwd=ROOT, capture_output=True, text=True)
	assert built.returncode == 0, built.stderr
	assert (gen / "echo_tokio.rs").exists(), built.stderr

	proj = tmp_path / "proj"
	(proj / "src").mkdir(parents=True)
	(proj / "Cargo.toml").write_text(CARGO_TOML, encoding="ascii")
	for part in gen.glob("*.rs"):
		(proj / "src" / part.name).write_text(
			part.read_text(encoding="ascii"), encoding="ascii")
	# The runtime carries `#![no_std]`, which is a crate-root attribute and
	# cannot travel in a module.
	(proj / "src" / "situ_rt.rs").write_text(
		RUNTIME.read_text(encoding="ascii").replace("#![no_std]\n", ""),
		encoding="ascii")
	(proj / "src" / "main.rs").write_text(MAIN, encoding="ascii")
	return proj


@pytest.mark.skipif(CARGO is None, reason="no cargo")
def test_the_tokio_driver_retransmits_and_correlates(tmp_path: Path) -> None:
	"""The peer drops two datagrams and echoes the third, so a reply arrives
	only if the driver retransmitted. Watched failing with the deadline arm
	removed from the generated `select!`: the watchdog fires, because nothing
	ever resends."""
	proj = _cargo_project(tmp_path)
	assert proj is not None and CARGO is not None

	compiled = subprocess.run([CARGO, "build", "--quiet"], cwd=proj,
	                          capture_output=True, text=True, timeout=900)
	if compiled.returncode != 0:
		pytest.skip(f"the tokio crate is unavailable here: {compiled.stderr[:200]}")

	ran = subprocess.run([CARGO, "run", "--quiet"], cwd=proj,
	                     capture_output=True, text=True, timeout=300)
	assert ran.returncode == 0, ran.stdout + ran.stderr
	assert "FAIL" not in ran.stdout, ran.stdout
	assert "replies=1" in ran.stdout, ran.stdout
	assert "id=0x1234" in ran.stdout, ran.stdout
	# Three datagrams: the send and the two retransmissions the peer dropped.
	assert "datagrams=3" in ran.stdout, ran.stdout


def test_tokio_is_refused_on_c(tmp_path: Path) -> None:
	"""A driver crosses the backend axis, and the refusal names both."""
	schema = tmp_path / "echo.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "c", "--layer", "drive", "--driver", "tokio",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "tokio" in refused.stderr and "rust" in refused.stderr


def test_tokio_requires_the_drive_layer(tmp_path: Path) -> None:
	"""The driver adds a module over the drive layer rather than pulling it
	in, so it must be asked for alongside `--layer drive`."""
	schema = tmp_path / "echo.situ"
	schema.write_text(SCHEMA, encoding="ascii")

	refused = subprocess.run(
		[sys.executable, "-m", "situc.cli", "build", str(schema),
		 "--target", "rust", "--driver", "tokio",
		 "--out", str(tmp_path / "unused")],
		cwd=ROOT, capture_output=True, text=True)
	assert refused.returncode != 0
	assert "tokio" in refused.stderr and "drive" in refused.stderr


# -- the two defects this driver found, both fixed -------------------------
#
# Neither is pinned here any more, and each left by the same door. Rust's
# `step` dropped the expiry count on the call that gives up on the last
# exchange; its regression test lives beside C's and Python's in
# `test_relations.py`, which is where a claim about all three backends
# belongs. The Rust key builders cast an enum field's `Option<Enum>`, so a
# relation keyed on one did not compile at all; that is a claim about Rust
# codegen and its tests live in `test_codegen_rust.py`. Both pins flipped to
# XPASS the moment their fix landed, which is what `strict=True` is for.
