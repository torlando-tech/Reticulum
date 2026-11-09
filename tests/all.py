import unittest

from .hashes import TestSHA256
from .hashes import TestSHA512
from .identity import TestIdentity
from .link import TestLink
from .channel import TestChannel
from .hkdf import TestHKDF
from .ifac import TestIFAC
from .stamps import TestStampFixedPoint
from .stamps import TestStampWorkblock
from .stamps import TestStampGeneration
from .stamps import TestStampPersistence
from .stamps import TestNativeStampSearch
from .coalesced_transmit import TestTransmitBuffer
from .hdlc import TestReceiveBuffer
from .egress import TestHWMLimiter
from .runtime_hardening import AutoInterfaceTeardownTests
from .test_resource_assemble import ResourceAssemblyTests

if __name__ == '__main__': unittest.main(verbosity=2)