import unittest

from .hashes import TestSHA256
from .hashes import TestSHA512
from .identity import TestIdentity
from .link import TestLink
from .channel import TestChannel
from .runtime_hardening import AutoInterfaceTeardownTests
from .runtime_hardening import RuntimeHardeningTests

if __name__ == '__main__':
    unittest.main(verbosity=2)
