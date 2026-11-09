# Generated with lc/glm5.3_flash_q6_k_xl
# Reviewed and approved by Mark Qvist

import unittest
import os
import time
import math
import random
import shutil
import tempfile
import threading
from unittest import skipIf

import RNS
from RNS import Stamps
import RNS.vendor.umsgpack as msgpack

# The stamper itself is intentionally seeded from os.urandom, but the
# value and target layers are pure deterministic math. Seeded test
# vectors therefore make the statistical tests exact and repeatable
# on any platform and any hash backend.
rng = random.Random(28411)

def rb(n):
    return bytes(rng.getrandbits(8) for _ in range(n))

def fp_of(value):
    # stamp_value returns cost_from_fp(value_fp), and division by
    # COST_FP (a power of two) is exact in binary floating point,
    # so the fixed-point representation can be recovered losslessly.
    fp = round(value * Stamps.COST_FP)
    assert fp == int(value * Stamps.COST_FP)
    return fp


class TestStampFixedPoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # A shared pool of deterministic workblock/stamp samples,
        # used to lock in the value layer invariants.
        cls.samples = []
        for _ in range(3000):
            wb    = rb(rng.randint(0, 48))
            stamp = rb(rng.choice((RNS.Identity.STAMPLENGTH, Stamps.STAMP_SIZE)))
            hn    = int.from_bytes(RNS.Identity.full_hash(wb+stamp), byteorder="big")
            vleg  = Stamps.stamp_value_legacy(wb, stamp)
            v     = Stamps.stamp_value(wb, stamp)
            exact = 256 - math.log2(hn)
            cls.samples.append((wb, stamp, hn, vleg, v, exact))

    def test_00_fixed_point_conversions(self):
        cap = RNS.Transport.STAMP_COST_CAP
        for cost in range(0, cap+1):
            self.assertEqual(Stamps.cost_to_fp(cost), cost*Stamps.COST_FP)
            self.assertEqual(Stamps.cost_from_fp(Stamps.cost_to_fp(cost)), cost)

        self.assertEqual(Stamps.cost_to_fp(8.5), 8.5*Stamps.COST_FP)
        self.assertEqual(Stamps.cost_from_fp(8.5*Stamps.COST_FP), 8.5)
        self.assertEqual(Stamps.cost_to_fp(0.5), 128)
        self.assertEqual(Stamps.cost_to_fp(-1), 0)
        self.assertEqual(Stamps.cost_to_fp(cap+1), cap*Stamps.COST_FP)

        for fp in list(range(0, 65536, 97)) + [0, 255, 256, 65535]:
            packed = Stamps.fp_to_bytes(fp)
            self.assertEqual(len(packed), 2)
            self.assertEqual(Stamps.fp_from_bytes(packed), fp)

        for cost in (0, 1, 4, 8.5, 64, 100):
            self.assertEqual(Stamps.cost_from_bytes(Stamps.cost_to_bytes(cost)), cost)

        self.assertEqual(Stamps.cost_int(8*Stamps.COST_FP + 128), 8)
        self.assertEqual(Stamps.cost_str(8*Stamps.COST_FP), "8")
        self.assertEqual(Stamps.cost_str(8*Stamps.COST_FP + 128), "8.5")
        self.assertEqual(Stamps.cost_str(0), "0")

    def test_01_target_for_integer_costs(self):
        cap = RNS.Transport.STAMP_COST_CAP
        for cost in range(0, cap+1):
            self.assertEqual(Stamps.target_from_cost(cost), 2**(256-cost))

        self.assertEqual(Stamps.target_from_fp(0), 2**256)
        self.assertRaises(AssertionError, Stamps.target_from_fp, -1)
        self.assertRaises(AssertionError, Stamps.target_from_fp, cap*Stamps.COST_FP + 1)

    def test_02_target_monotonic_and_exact(self):
        # Targets must strictly decrease as cost increases
        cap = RNS.Transport.STAMP_COST_CAP
        last = None
        for fp in range(0, cap*Stamps.COST_FP + 1):
            target = Stamps.target_from_fp(fp)
            if last != None:
                self.assertLess(target, last)
            last = target

        # Independent verification of the whole mantissa/shift
        # construction against floor(2^(256 - fp/256)), computed
        # with exact integer arithmetic: The 256th root of
        # 2^(33*256+f), obtained by a Newton iteration, is exactly
        # floor(2^(33+f/256)), and shifting by the integer part of
        # the exponent yields the exact target for every step.
        def iroot(n, k):
            x = 1 << ((n.bit_length() + k - 1)//k)
            while True:
                y = ((k-1)*x + n//x**(k-1))//k
                if y >= x:
                    return x
                x = y

        roots = [iroot(1 << (Stamps.MANTISSA_BITS*Stamps.COST_FP + f), Stamps.COST_FP) for f in range(Stamps.COST_FP)]
        for fp in range(0, cap*Stamps.COST_FP + 1):
            i, f = divmod(256*Stamps.COST_FP - fp, Stamps.COST_FP)
            self.assertEqual(Stamps.target_from_fp(fp), roots[f] << (i - Stamps.MANTISSA_BITS))

    def test_03_value_matches_legacy_integer_value(self):
        # The compatibility invariant: old clients parse fractional
        # stamps at their integer value. The legacy value function
        # must therefore always be the integer floor of the new one.
        for wb, stamp, hn, vleg, v, exact in self.samples:
            self.assertEqual(vleg, 256 - hn.bit_length())
            self.assertLessEqual(vleg, v)
            self.assertLess(v, vleg + 1)
            self.assertEqual(fp_of(v) // Stamps.COST_FP, vleg)

        # Empty and None workblocks are handled identically
        wb, stamp, hn, vleg, v, exact = self.samples[0]
        self.assertEqual(Stamps.stamp_value(None, stamp), Stamps.stamp_value(b"", stamp))
        self.assertEqual(Stamps.stamp_value_legacy(None, stamp), Stamps.stamp_value_legacy(b"", stamp))
        self.assertTrue(Stamps.stamp_valid(stamp, 0, None))

    def test_04_value_never_overreports(self):
        # The reported value must never exceed the exact value of
        # the stamp product, and must be within one quantisation
        # step (1/256th) below it.
        step = 1.0 / Stamps.COST_FP
        for wb, stamp, hn, vleg, v, exact in self.samples:
            self.assertLessEqual(v, exact + 1e-9)
            self.assertLessEqual(exact, v + step + 1e-9)

    def test_05_value_monotone_in_stamp_product(self):
        # A smaller stamp product (numerically) always means a more
        # rare hash, and must never yield a smaller value.
        ordered = sorted(self.samples, key=lambda sample: sample[2])
        last = None
        for wb, stamp, hn, vleg, v, exact in ordered:
            fp = fp_of(v)
            if last != None:
                self.assertLessEqual(fp, last)
            last = fp

    def test_06_validity_boundaries(self):
        cap = RNS.Transport.STAMP_COST_CAP
        for wb, stamp, hn, vleg, v, exact in self.samples:
            # Any stamp is always valid at its legacy integer value,
            # and always invalid at exactly one step above it. This
            # locks stamp_valid and stamp_value into one scheme.
            if vleg <= cap:
                self.assertTrue(Stamps.stamp_valid(stamp, vleg, wb))
            if vleg + 1 <= cap:
                self.assertFalse(Stamps.stamp_valid(stamp, vleg + 1, wb))

        # Directional consistency between value and validity
        for wb, stamp, hn, vleg, v, exact in self.samples[:500]:
            fp = fp_of(v)
            for cost in (1, 2, 3):
                if fp >= cost * Stamps.COST_FP:
                    self.assertTrue(Stamps.stamp_valid(stamp, cost, wb))
                if Stamps.stamp_valid(stamp, cost, wb):
                    self.assertGreaterEqual(fp, (cost - 1) * Stamps.COST_FP)

    def test_07_target_probability_calibration(self):
        # The defining semantic of cost: a random stamp is valid for
        # cost c with probability exactly 2^-c.
        for cost in (2, 4):
            trials  = 20000
            wb      = rb(32)
            valid   = sum(1 for _ in range(trials) if Stamps.stamp_valid(rb(14), cost, wb))
            fraction = valid / trials
            expected = 2.0**-cost
            sigma = math.sqrt(expected * (1-expected) / trials)
            self.assertLess(abs(fraction - expected), 5*sigma)

    def test_08_value_deterministic(self):
        for wb, stamp, hn, vleg, v, exact in self.samples[:100]:
            self.assertEqual(Stamps.stamp_value(wb, stamp), v)
            self.assertEqual(Stamps.stamp_value_legacy(wb, stamp), vleg)


class TestStampWorkblock(unittest.TestCase):
    def test_00_no_expand(self):
        material = rb(64)
        self.assertIs(Stamps.stamp_workblock(material, no_expand=True), material)
        self.assertIs(Stamps.stamp_workblock(material, expand_rounds=0), material)
        self.assertEqual(Stamps.stamp_workblock(material, expand_rounds=16, no_expand=True), material)

    def test_01_expansion(self):
        material = rb(32)
        wb1 = Stamps.stamp_workblock(material, expand_rounds=16)
        wb2 = Stamps.stamp_workblock(material, expand_rounds=16)
        self.assertEqual(len(wb1), 16*256)
        self.assertEqual(wb1, wb2)
        self.assertNotEqual(wb1, Stamps.stamp_workblock(rb(32), expand_rounds=16))

    def test_02_default_expansion(self):
        material = rb(32)
        wb = Stamps.stamp_workblock(material)
        self.assertEqual(len(wb), Stamps.WORKBLOCK_EXPAND_ROUNDS*256)
        self.assertEqual(wb, Stamps.stamp_workblock(material))


class TestStampGeneration(unittest.TestCase):
    def _wait_registered(self, mid, timeout=15):
        for _ in range(int(timeout/0.05)):
            if mid in Stamps.active_jobs:
                return True
            time.sleep(0.05)
        return False

    def test_00_generate_stamp_low_costs(self):
        for cost in (0, 4, 8):
            mid = rb(32)
            stamp, value = Stamps.generate_stamp(mid, cost, no_expand=True)
            self.assertNotEqual(stamp, None)
            self.assertIsInstance(stamp, bytes)
            self.assertEqual(len(stamp), Stamps.STAMP_SIZE)

            # The generation contract: the stamp must validate at
            # the requested cost, and the reported value must be
            # within one quantisation step of the requested cost.
            self.assertTrue(Stamps.stamp_valid(stamp, cost, mid))
            self.assertGreaterEqual(fp_of(value), (cost - 1) * Stamps.COST_FP)

            # Old clients parse the fractional value at its integer
            # value, so that must always be at least cost - 1, and
            # must match the legacy value function exactly.
            vleg = Stamps.stamp_value_legacy(mid, stamp)
            self.assertGreaterEqual(vleg, cost - 1)
            self.assertEqual(fp_of(value) // Stamps.COST_FP, vleg)
            self.assertLess(value, 256)
            self.assertEqual(value, Stamps.stamp_value(mid, stamp))

    def test_01_generate_stamp_expanded_workblock(self):
        mid = rb(32)
        stamp, value = Stamps.generate_stamp(mid, 4, expand_rounds=16)
        wb = Stamps.stamp_workblock(mid, expand_rounds=16)
        self.assertEqual(len(wb), 16*256)
        self.assertTrue(Stamps.stamp_valid(stamp, 4, wb))
        self.assertEqual(value, Stamps.stamp_value(wb, stamp))

    def test_02_generate_stamp_default_workblock(self):
        mid = rb(32)
        stamp, value = Stamps.generate_stamp(mid, 4)
        wb = Stamps.stamp_workblock(mid)
        self.assertEqual(len(wb), Stamps.WORKBLOCK_EXPAND_ROUNDS*256)
        self.assertTrue(Stamps.stamp_valid(stamp, 4, wb))

    def test_03_generate_stamped_key(self):
        material = rb(32)
        keypair, value = Stamps.generate_stamped_key(material, 4)
        private, public = keypair
        self.assertEqual(len(private), 32)
        self.assertEqual(len(public), 32)
        self.assertEqual(RNS.Cryptography.X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(), public)

        # Keystamp validation uses the public key bytes as the stamp
        self.assertTrue(Stamps.stamp_valid(public, 4, material))
        self.assertEqual(value, Stamps.stamp_value(material, public))
        self.assertGreaterEqual(fp_of(value), 3 * Stamps.COST_FP)

    def test_04_generate_stamped_key_empty_material(self):
        keypair, value = Stamps.generate_stamped_key(None, 2)
        private, public = keypair
        self.assertTrue(Stamps.stamp_valid(public, 2, b""))
        self.assertEqual(value, Stamps.stamp_value(b"", public))

    def test_05_invalid_costs_rejected(self):
        cap = RNS.Transport.STAMP_COST_CAP
        mid = rb(16)
        for cost in (-1, cap + 1):
            with self.assertRaises(ValueError):
                Stamps.generate_stamp(mid, cost, no_expand=True)
            with self.assertRaises(ValueError):
                Stamps.generate_stamped_key(mid, cost)

    def test_06_duplicate_job_rejected(self):
        mid = rb(16)
        Stamps.active_jobs[mid] = False
        try:
            with self.assertRaises(RuntimeError):
                Stamps.generate_stamp(mid, 4, no_expand=True)
            with self.assertRaises(RuntimeError):
                Stamps.generate_stamped_key(mid, 4)
        finally:
            del Stamps.active_jobs[mid]

    def test_07_cancel_unknown_job(self):
        # Cancelling a job that was never started must be a safe no-op
        Stamps.cancel_work(rb(16))

    def test_08_cancel_simple_job(self):
        mid = rb(16)
        wb  = rb(32)
        result = {}
        def work():
            result["r"] = Stamps.job_simple(30*Stamps.COST_FP, wb, mid, Stamps.STAMP_SIZE)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.assertTrue(self._wait_registered(mid))
        time.sleep(0.2)
        self.assertIs(Stamps.active_jobs[mid], False)   # Still running

        Stamps.cancel_work(mid)
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive())

        # Cost 30 can never complete, so a timely return with no
        # stamp proves cancellation worked, and that work was done.
        self.assertEqual(result["r"][0], None)
        self.assertGreater(result["r"][1], 0)
        self.assertNotIn(mid, Stamps.active_jobs)

    def test_09_cancel_generation_job(self):
        mid = rb(16)
        result = {}
        def work():
            result["r"] = Stamps.generate_stamp(mid, 30, no_expand=True)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        self.assertTrue(self._wait_registered(mid))
        time.sleep(0.3)
        Stamps.cancel_work(mid)
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive())

        stamp, value = result["r"]
        self.assertEqual(stamp, None)
        self.assertNotIn(mid, Stamps.active_jobs)

    @skipIf(not RNS.vendor.platformutils.is_linux(), "Direct multiprocess dispatchers are Linux-only paths")
    def test_10_direct_multiprocess_dispatch(self):
        # generate_stamp only routes to the multiprocess dispatchers
        # at cost >= POOLING_THRESHOLD_COST, so they are exercised
        # directly here at a trivial cost. This covers both the
        # classic and managed worker mechanisms regardless of the
        # Python version in use.
        wb   = rb(32)
        mid1 = rb(16)
        mid2 = rb(16)

        stamp, rounds = Stamps.job_linux(4*Stamps.COST_FP, wb, mid1, Stamps.STAMP_SIZE)
        self.assertNotEqual(stamp, None)
        self.assertTrue(Stamps.stamp_valid(stamp, 4, wb))
        self.assertGreater(rounds, 0)
        self.assertNotIn(mid1, Stamps.active_jobs)

        stamp, rounds = Stamps.job_linux_managed(5*Stamps.COST_FP, wb, mid2, Stamps.STAMP_SIZE)
        self.assertNotEqual(stamp, None)
        self.assertTrue(Stamps.stamp_valid(stamp, 5, wb))
        self.assertGreater(rounds, 0)
        self.assertNotIn(mid2, Stamps.active_jobs)


class TestNativeStampSearch(unittest.TestCase):
    def setUp(self):
        previous = Stamps.native_stamp_search
        self.addCleanup(setattr, Stamps, "native_stamp_search", previous)
        Stamps.native_stamp_search = self.search

    @staticmethod
    def search(workblock, target, size, stop_check):
        rounds = 0
        while not stop_check():
            stamp = rounds.to_bytes(size, byteorder="big")
            product = RNS.Identity.full_hash(workblock+stamp)
            if int.from_bytes(product, byteorder="big") <= target:
                return stamp, rounds
            rounds += 1
        return None, rounds

    def test_00_fractional_proofs(self):
        # Above the pooling threshold, the callback still owns the hash search.
        cost = Stamps.POOLING_THRESHOLD_COST + 0.5
        for size in (14, 32):
            with self.subTest(size=size):
                mid = b"native fractional proof" + bytes([size])
                stamp, value = Stamps.generate_stamp(mid, cost, expand_rounds=1, size=size)
                workblock = Stamps.stamp_workblock(mid, expand_rounds=1)
                self.assertEqual(len(stamp), size)
                self.assertTrue(Stamps.stamp_valid(stamp, cost, workblock))
                self.assertEqual(value, Stamps.stamp_value(workblock, stamp))
                self.assertNotIn(mid, Stamps.active_jobs)

    def test_01_cancellation_wins_over_late_proof(self):
        mid = b"native late proof"
        ready = threading.Event()
        release = threading.Event()
        result = {}

        def late_search(workblock, target, size, stop_check):
            proof = self.search(workblock, target, size, stop_check)
            ready.set()
            if not release.wait(5): raise TimeoutError("Proof release timed out")
            return proof

        def work():
            try: result["proof"] = Stamps.generate_stamp(mid, 4.5, no_expand=True)
            except Exception as e: result["error"] = e

        Stamps.native_stamp_search = late_search
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            self.assertTrue(ready.wait(5))
            self.assertTrue(Stamps.cancel_work(mid))
        finally:
            release.set()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", result)
        self.assertEqual(result["proof"], (None, None))
        self.assertNotIn(mid, Stamps.active_jobs)

    def test_02_exception_releases_job_for_retry(self):
        mid = b"native retry"

        def failed_search(workblock, target, size, stop_check):
            raise RuntimeError("Native search failed")

        Stamps.native_stamp_search = failed_search
        with self.assertRaisesRegex(RuntimeError, "Native search failed"):
            Stamps.generate_stamp(mid, 4.5, no_expand=True)
        self.assertNotIn(mid, Stamps.active_jobs)
        Stamps.native_stamp_search = self.search
        stamp, value = Stamps.generate_stamp(mid, 4.5, no_expand=True)
        self.assertTrue(Stamps.stamp_valid(stamp, 4.5, mid))

    def test_03_key_stamping_unaffected(self):
        def hash_only(workblock, target, size, stop_check):
            raise AssertionError("Hash search must not generate keys")

        Stamps.native_stamp_search = hash_only
        material = b"native key isolation"
        keypair, value = Stamps.generate_stamped_key(material, 4.5)
        private, public = keypair
        self.assertEqual(RNS.Cryptography.X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(), public)
        self.assertTrue(Stamps.stamp_valid(public, 4.5, material))
        self.assertEqual(value, Stamps.stamp_value(material, public))

    def test_04_rejects_integer_only_fractional_proof(self):
        mid = b"native invalid fractional proof"
        stamp = None
        for n in range(10000):
            candidate = n.to_bytes(32, byteorder="big")
            if Stamps.stamp_valid(candidate, 4, mid) and not Stamps.stamp_valid(candidate, 4.5, mid):
                stamp = candidate
                break
        self.assertIsNotNone(stamp)

        def invalid_search(workblock, target, size, stop_check):
            return stamp, 0

        Stamps.native_stamp_search = invalid_search
        with self.assertRaises(ValueError):
            Stamps.generate_stamp(mid, 4.5, no_expand=True)
        self.assertNotIn(mid, Stamps.active_jobs)

    def test_05_rejects_wrong_size_proof(self):
        def wrong_size_search(workblock, target, size, stop_check):
            return self.search(workblock, target, size+1, stop_check)

        Stamps.native_stamp_search = wrong_size_search
        mid = b"native wrong proof size"
        with self.assertRaises(ValueError):
            Stamps.generate_stamp(mid, 4.5, no_expand=True, size=14)
        self.assertNotIn(mid, Stamps.active_jobs)


class TestStampPersistence(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir        = None
        cls.owns_instance = False
        # The full test suite shares one Reticulum instance, created
        # by whichever test needed it first. If none is running, a
        # temporary instance is created for this class only, and
        # cleanly detached again afterwards.
        if not RNS.Reticulum.storagepath:
            cls.tmpdir = tempfile.mkdtemp(prefix="rns_stamp_tests_")
            RNS.Reticulum(configdir=cls.tmpdir, loglevel=RNS.LOG_CRITICAL)
            cls.owns_instance = True

    @classmethod
    def tearDownClass(cls):
        if cls.owns_instance:
            RNS.Reticulum._Reticulum__instance = None
            RNS.Reticulum.configpath    = ""
            RNS.Reticulum.storagepath   = ""
            RNS.Reticulum.cachepath     = ""
            RNS.Reticulum.interfacepath = ""
        if cls.tmpdir:
            shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _make_stamp(self, dest, cost=4):
        # Destination stamps are STAMPLENGTH bytes, and are generated
        # without workblock expansion, since the persistence layer
        # recomputes the workblock as the unexpanded material.
        return Stamps.generate_stamp(dest, cost, no_expand=True, size=RNS.Identity.STAMPLENGTH)

    def _cleanup(self, dest):
        path = Stamps.destination_stamp_file(dest)
        if path and os.path.isfile(path):
            os.remove(path)

    def test_00_store_load_roundtrip(self):
        dest = rb(16)
        self.addCleanup(self._cleanup, dest)
        stamp, value = self._make_stamp(dest)

        self.assertTrue(Stamps.store_destination_stamp(dest, stamp, value))
        loaded = Stamps.load_destination_stamp(dest, dest, 4)
        self.assertNotEqual(loaded, None)
        lstamp, lvalue = loaded
        self.assertEqual(lstamp, stamp)
        self.assertEqual(lvalue, value)

    def test_01_load_value_boundaries(self):
        dest = rb(16)
        self.addCleanup(self._cleanup, dest)
        stamp, value = self._make_stamp(dest)
        Stamps.store_destination_stamp(dest, stamp, value)

        # A stamp is always valid at its legacy integer value
        vleg = Stamps.stamp_value_legacy(dest, stamp)
        loaded = Stamps.load_destination_stamp(dest, dest, vleg)
        self.assertNotEqual(loaded, None)
        self.assertEqual(loaded[0], stamp)

        # ... and never at a full step above its own value
        self.assertEqual(Stamps.load_destination_stamp(dest, dest, value + 1), None)

    def test_02_load_nonexistent(self):
        self.assertEqual(Stamps.load_destination_stamp(rb(16), rb(16), 1), None)

    def test_03_load_corrupt_file(self):
        dest = rb(16)
        self.addCleanup(self._cleanup, dest)
        stamp, value = self._make_stamp(dest)
        Stamps.store_destination_stamp(dest, stamp, value)
        with open(Stamps.destination_stamp_file(dest), "wb") as file:
            file.write(rb(64))
        self.assertEqual(Stamps.load_destination_stamp(dest, dest, 1), None)

    def test_04_load_wrong_version(self):
        dest = rb(16)
        self.addCleanup(self._cleanup, dest)
        stamp, value = self._make_stamp(dest)
        Stamps.store_destination_stamp(dest, stamp, value)
        with open(Stamps.destination_stamp_file(dest), "wb") as file:
            file.write(msgpack.packb({"v": 99, "stamp": stamp, "value": value, "generated": time.time()}))
        self.assertEqual(Stamps.load_destination_stamp(dest, dest, 1), None)

    def test_05_load_wrong_stamp_length(self):
        dest = rb(16)
        self.addCleanup(self._cleanup, dest)
        stamp, value = self._make_stamp(dest)
        Stamps.store_destination_stamp(dest, stamp, value)
        with open(Stamps.destination_stamp_file(dest), "wb") as file:
            file.write(msgpack.packb({"v": Stamps.STAMP_FILE_VERSION, "stamp": rb(8), "value": value, "generated": time.time()}))
        self.assertEqual(Stamps.load_destination_stamp(dest, dest, 1), None)

    def test_06_stamp_paths(self):
        directory = Stamps.stamps_dir(create=True)
        self.assertNotEqual(directory, None)
        self.assertTrue(os.path.isdir(directory))

        dest = rb(16)
        path = Stamps.destination_stamp_file(dest)
        self.assertEqual(os.path.dirname(path), directory)
        self.assertTrue(path.endswith(RNS.hexrep(dest, delimit=False) + Stamps.DESTINATION_STAMP_EXTENSION))


if __name__ == '__main__':
    unittest.main(verbosity=2)
