"""Behavioral contract for Resource.assemble()'s bounded-memory assembly.

Guards the `fix: bound receive-assembly memory in Resource.assemble` commit:
the parts list is released after the join and the random-hash strip is a
memoryview slice, so peak RSS during assembly drops by ~1x payload without
changing observable behavior.

Run from the repo root:  python3 tests/test_resource_assemble.py
Plain asserts, no test framework required. Exit 0 = pass.
"""
import hashlib
import os
import random
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import RNS  # noqa: E402
from RNS.Resource import Resource  # noqa: E402


class LinkStub:
    def __init__(self):
        self.concluded = []

    def resource_concluded(self, res):
        self.concluded.append(res.status)

    def decrypt(self, stream):
        raise AssertionError("decrypt must not be called")


def make_resource(payload: bytes, corrupt_hash=False):
    """Build a Resource the way an inbound transfer would: parts are chunks of
    (random_hash + payload); hash is full_hash(payload + random_hash)."""
    random_hash = os.urandom(Resource.RANDOM_HASH_SIZE)
    full = random_hash + payload
    full_hash = hashlib.sha256(payload + random_hash).digest()
    if corrupt_hash:
        full_hash = bytes([full_hash[0] ^ 0xFF]) + full_hash[1:]

    chunk = 65536
    parts = [full[i:i + chunk] for i in range(0, len(full), chunk)] or [b""]

    tmp = tempfile.mkdtemp(prefix="asm-test-")
    res = Resource.__new__(Resource)
    res.status = Resource.TRANSFERRING
    res.encrypted = False
    res.compressed = False
    res.parts = parts
    res.random_hash = random_hash
    res.hash = full_hash
    res.total_size = len(full)
    res.received_size = len(full)
    res.storagepath = os.path.join(tmp, "res.bin")
    res.meta_storagepath = os.path.join(tmp, "res.meta")
    res.has_metadata = False
    res.segment_index = 1
    res.total_segments = 1
    res.callback = None
    res.link = LinkStub()
    res.prove = lambda: None
    return res


def run_case(name, payload, corrupt=False):
    res = make_resource(payload, corrupt)
    res.assemble()
    expected = Resource.CORRUPT if corrupt else Resource.COMPLETE
    assert res.status == expected, f"{name}: status {res.status} != {expected}"
    if not corrupt:
        # assemble() unlinks the storage file at the end; the assembled bytes
        # live on self.data (a memoryview over the joined buffer).
        assert bytes(res.data) == payload, f"{name}: assembled bytes differ from payload"
    return res


def peak_rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmHWM"):
                    return int(line.split()[1]) / 1024.0
    except OSError:
        pass
    return -1.0


def main():
    rng = random.Random(1337)
    small = bytes(rng.getrandbits(8) for _ in range(1024))
    big = bytes(rng.getrandbits(8) for _ in range(6 * 1024 * 1024))

    # 1. byte-exact assembly across sizes
    run_case("small", small)
    run_case("6MB", big)
    run_case("empty", b"")

    # 2. integrity: a corrupted full-hash must be rejected, not delivered
    run_case("corrupt", small, corrupt=True)

    # 3. sentinel safety: after assembly the parts list must stay subscriptable
    #    and non-None so a late duplicate part in receive_part is ignored
    #    exactly as before the memory patch.
    res = make_resource(small)
    res.assemble()
    assert len(res.parts) > 0
    assert all(p is not None for p in res.parts), "sentinel must not be None"
    assert res.parts[0] == b"", "sentinel must be empty bytes"

    # 4. informational: peak RSS after the 6 MB assembly. The patch removes one
    #    full payload copy (parts release) and one re-copy (memoryview strip);
    #    pre-patch code measured ~6 MB higher here on the same harness.
    print(f"correctness=PASS peak_rss_mb={peak_rss_mb():.1f}")


main()
