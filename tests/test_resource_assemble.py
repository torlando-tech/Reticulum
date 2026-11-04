"""Receive assembly contracts, adapted from the pinned Columba regression suite."""
import bz2
import hashlib
import os
import tempfile
import threading
import tracemalloc
import unittest
from unittest import mock

import RNS
from RNS.Resource import Resource
from RNS.vendor import umsgpack


class LinkStub:
    def __init__(self):
        self.concluded = []

    def resource_concluded(self, resource):
        self.concluded.append(resource.status)

    def decrypt(self, stream):
        raise AssertionError("unencrypted resource must not be decrypted")


class ResourceAssemblyTests(unittest.TestCase):
    def make_resource(self, payload, compressed=False, metadata=None, corrupt=False):
        directory = tempfile.TemporaryDirectory(prefix="resource-assemble-")
        self.addCleanup(directory.cleanup)
        random_hash = b"r" * Resource.RANDOM_HASH_SIZE
        if metadata is not None:
            packed = umsgpack.packb(metadata)
            payload = len(packed).to_bytes(3, "big") + packed + payload
        wire = random_hash + (bz2.compress(payload) if compressed else payload)
        resource = Resource.__new__(Resource)
        resource.status = Resource.TRANSFERRING
        resource.encrypted = False
        resource.compressed = compressed
        resource.max_decompressed_size = len(payload) + 1
        resource.parts = [wire[i:i+65536] for i in range(0, len(wire), 65536)]
        resource.random_hash = random_hash
        resource.hash = hashlib.sha256(payload + random_hash).digest()
        if corrupt:
            resource.hash = bytes([resource.hash[0] ^ 255]) + resource.hash[1:]
        resource.total_size = len(wire)
        resource.received_size = len(wire)
        resource.storagepath = os.path.join(directory.name, "resource")
        resource.meta_storagepath = os.path.join(directory.name, "metadata")
        resource.has_metadata = metadata is not None
        resource.segment_index = resource.total_segments = 1
        resource.callback = None
        resource.link = LinkStub()
        resource.ensure_link = lambda: True
        return resource, payload

    def assemble(self, resource):
        with mock.patch.object(RNS, "Packet") as packet, \
             mock.patch.object(RNS.Transport, "cache"):
            resource.assemble()
        return packet

    def test_byte_exact_assembly_and_proofs_for_empty_small_and_large_resources(self):
        for payload in (b"", b"Columba resource\x00\xff", bytes(range(256)) * 24576):
            with self.subTest(size=len(payload)):
                resource, expected = self.make_resource(payload)
                packet = self.assemble(resource)
                self.assertEqual(Resource.COMPLETE, resource.status)
                self.assertIsInstance(resource.data, bytes)
                self.assertEqual(expected, resource.data)
                self.assertEqual([Resource.COMPLETE], resource.link.concluded)
                self.assertEqual(resource.hash + hashlib.sha256(expected + resource.hash).digest(), packet.call_args.args[1])
                packet.return_value.send.assert_called_once_with()

    def test_compressed_metadata_callback_retains_file_consumer_contract(self):
        payload = b"compressed body\x00" * 1000
        metadata = {"name": "message", "number": 42}
        resource, expected = self.make_resource(payload, compressed=True, metadata=metadata)
        observed = []
        def received(result):
            self.assertTrue(hasattr(result.data, "read"))
            self.assertEqual(0, result.data.tell())
            observed.append((result.data.read(), result.metadata))
        resource.callback = received
        packet = self.assemble(resource)
        self.assertEqual(Resource.COMPLETE, resource.status)
        self.assertEqual([(payload, metadata)], observed)
        self.assertTrue(resource.data.closed)
        self.assertFalse(os.path.exists(resource.storagepath))
        self.assertFalse(os.path.exists(resource.meta_storagepath))
        self.assertEqual(resource.hash + hashlib.sha256(expected + resource.hash).digest(), packet.call_args.args[1])

    def test_encrypted_resource_decrypts_before_hashing_and_proving(self):
        resource, payload = self.make_resource(b"encrypted payload")
        plaintext = b"".join(resource.parts)
        resource.parts = [b"cipher", b"text"]
        resource.encrypted = True
        resource.link.decrypt = mock.Mock(return_value=plaintext)
        self.assemble(resource)
        resource.link.decrypt.assert_called_once_with(b"ciphertext")
        self.assertEqual(Resource.COMPLETE, resource.status)
        self.assertEqual(payload, resource.data)

    def test_corrupt_hash_is_rejected_without_proof_or_output(self):
        resource, _ = self.make_resource(b"corrupt payload", corrupt=True)
        packet = self.assemble(resource)
        self.assertEqual(Resource.CORRUPT, resource.status)
        self.assertEqual([Resource.CORRUPT], resource.link.concluded)
        packet.assert_not_called()
        self.assertFalse(os.path.exists(resource.storagepath))

    def test_decompression_limit_still_rejects_resource(self):
        resource, _ = self.make_resource(b"oversized" * 1000, compressed=True)
        resource.max_decompressed_size = 8
        resource.cancel = mock.Mock()
        packet = self.assemble(resource)
        self.assertEqual(Resource.CORRUPT, resource.status)
        resource.cancel.assert_called_once_with()
        packet.assert_not_called()

    def test_late_duplicate_part_does_not_repopulate_released_parts(self):
        resource, _ = self.make_resource(b"duplicate payload")
        duplicate = resource.parts[0]
        self.assemble(resource)
        resource.receive_lock = threading.Lock()
        resource.req_resp = 1
        resource.max_retries = 3
        resource.consecutive_completed_height = -1
        resource.hashmap = [b"part hash"]
        resource.window = 1
        resource.get_map_hash = lambda data: b"part hash"
        resource.received_count = resource.total_parts = 1
        resource.outstanding_parts = 1
        resource.assembly_lock = True
        resource.rtt_rxd_bytes = 0
        resource.receive_part(mock.Mock(data=duplicate))
        self.assertEqual(1, resource.received_count)
        self.assertEqual(0, resource.rtt_rxd_bytes)

    def test_assembly_releases_parts_and_bounds_transient_payload_copies(self):
        size = 6 * 1024 * 1024
        tracemalloc.start()
        try:
            resource, payload = self.make_resource(bytes(range(256)) * (size // 256))
            del payload
            before, _ = tracemalloc.get_traced_memory()
            tracemalloc.reset_peak()
            self.assemble(resource)
            retained, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(Resource.COMPLETE, resource.status)
        # Retained storage is the public bytes buffer, not a second payload in
        # received parts. Assembly may briefly hold joined and stripped buffers.
        self.assertLess(retained, size + 512 * 1024)
        self.assertLess(peak - before, size * 2 + 512 * 1024)


if __name__ == "__main__":
    unittest.main(verbosity=2)
