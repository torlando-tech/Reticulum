import builtins
import multiprocessing
import socket
import socketserver
import threading
import time
import unittest
from unittest import mock

import RNS
from RNS.Destination import Destination
from RNS.Interfaces.AutoInterface import AutoInterface, AutoInterfacePeer
from RNS.Interfaces.BackboneInterface import BackboneClientInterface
from RNS.Interfaces.Interface import Interface
from RNS.Interfaces.TCPInterface import TCPClientInterface
from RNS.Link import Link


class Config(dict):
    def as_list(self, key):
        return self[key]


class FailingSocket:
    def __init__(self, fail_on_connect=True, fail_on_setsockopt=False):
        self.closed = False
        self.fail_on_connect = fail_on_connect
        self.fail_on_setsockopt = fail_on_setsockopt

    def settimeout(self, timeout):
        pass

    def setsockopt(self, *args):
        if self.fail_on_setsockopt:
            raise OSError("socket setup failed")

    def connect(self, address):
        if self.fail_on_connect:
            raise ConnectionRefusedError("refused")

    def bind(self, address):
        pass

    def close(self):
        self.closed = True


class ExplodingFile:
    def __init__(self, operation):
        self.operation = operation
        self.exited = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.exited = True

    def write(self, data):
        raise OSError("write failed")

    def read(self):
        raise OSError("read failed")


class RuntimeHardeningTests(unittest.TestCase):
    def _client(self, client_type):
        client = client_type.__new__(client_type)
        client.target_ip = "127.0.0.1"
        client.target_port = 1
        client.socket = None
        client.online = False
        client.never_connected = True
        client.name = "test"
        client.prefer_ipv6 = False
        client.i2p_tunneled = False
        return client

    def test_failed_initial_and_reconnect_sockets_are_closed_and_cleared(self):
        for client_type in (TCPClientInterface, BackboneClientInterface):
            for initial in (True, False):
                with self.subTest(client=client_type.__name__, initial=initial):
                    client = self._client(client_type)
                    failed_socket = FailingSocket()
                    address = (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 1))
                    module = "RNS.Interfaces.TCPInterface" if client_type is TCPClientInterface else "RNS.Interfaces.BackboneInterface"
                    with mock.patch(module + ".socket.getaddrinfo", return_value=[address]), \
                         mock.patch(module + ".socket.socket", return_value=failed_socket):
                        if initial:
                            self.assertFalse(client.connect(initial=True))
                        else:
                            with self.assertRaises(ConnectionRefusedError):
                                client.connect(initial=False)
                    self.assertTrue(failed_socket.closed)
                    self.assertIsNone(client.socket)

    def _link_for_phy_test(self):
        link = Link.__new__(Link)
        link._Link__track_phy_stats = True
        link.rssi = None
        link.snr = None
        link.q = None
        return link

    def test_expected_phy_rpc_failure_is_nonfatal_and_preserves_packet_stats(self):
        class Reticulum:
            def get_packet_rssi(self, packet_hash):
                raise multiprocessing.AuthenticationError("bad key")

            def get_packet_snr(self, packet_hash):
                raise AssertionError("must stop after auth failure")

        packet = mock.Mock(packet_hash=b"hash", rssi=None, snr=7, q=88)
        link = self._link_for_phy_test()
        with mock.patch.object(RNS.Reticulum, "get_instance", return_value=Reticulum()):
            link._Link__update_phy_stats(packet)
        self.assertIsNone(link.rssi)
        self.assertEqual(7, link.snr)
        self.assertEqual(88, link.q)

    def test_persistent_phy_auth_failure_uses_backoff_and_logs_once(self):
        reticulum = mock.Mock()
        reticulum.get_packet_rssi.side_effect = multiprocessing.AuthenticationError("bad key")
        packet = mock.Mock(packet_hash=b"hash", rssi=None, snr=None, q=None)
        link = self._link_for_phy_test()
        with mock.patch.object(RNS.Reticulum, "get_instance", return_value=reticulum), mock.patch("RNS.log") as log:
            link._Link__update_phy_stats(packet)
            link._Link__update_phy_stats(packet)
        self.assertEqual(1, reticulum.get_packet_rssi.call_count)
        self.assertEqual(1, log.call_count)

    def test_phy_programming_errors_are_not_hidden(self):
        reticulum = mock.Mock()
        reticulum.get_packet_rssi.side_effect = ValueError("bug")
        packet = mock.Mock(packet_hash=b"hash", rssi=None, snr=None, q=None)
        link = self._link_for_phy_test()
        with mock.patch.object(RNS.Reticulum, "get_instance", return_value=reticulum):
            with self.assertRaises(ValueError):
                link._Link__update_phy_stats(packet)

    def test_ratchet_write_handle_closes_when_write_raises(self):
        destination = Destination.__new__(Destination)
        destination.ratchet_file_lock = threading.Lock()
        destination.ratchets_path = "/tmp/ratchets"
        destination.ratchets = []
        destination.name = "test"
        destination.hexhash = "00"
        destination.sign = lambda data: b"signature"
        handle = ExplodingFile("write")
        with mock.patch.object(builtins, "open", return_value=handle), mock.patch("RNS.trace_exception"):
            with self.assertRaises(OSError):
                destination._persist_ratchets()
        self.assertTrue(handle.exited)

    def test_ratchet_read_handles_close_when_read_raises(self):
        destination = Destination.__new__(Destination)
        destination.ratchet_file_lock = threading.Lock()
        destination.name = "test"
        destination.hexhash = "00"
        handles = [ExplodingFile("read"), ExplodingFile("read")]
        with mock.patch("RNS.Destination.os.path.isfile", return_value=True), \
             mock.patch.object(builtins, "open", side_effect=handles), \
             mock.patch("RNS.Destination.time.sleep"), \
             mock.patch("RNS.trace_exception"), mock.patch("RNS.log"):
            with self.assertRaises(OSError):
                destination._reload_ratchets("/tmp/ratchets")
        self.assertTrue(all(handle.exited for handle in handles))


class AutoInterfaceTeardownTests(unittest.TestCase):
    def make_interface(self):
        interface = AutoInterface.__new__(AutoInterface)
        interface.name = "test"
        interface.owner = mock.Mock()
        interface.online = True
        interface.final_init_done = True
        interface.peers = {}
        interface.spawned_interfaces = {}
        interface.interface_servers = {}
        interface._server_threads = {}
        interface._discovery_sockets = []
        interface._threads = []
        interface.stop_event = threading.Event()
        interface._lifecycle_lock = threading.RLock()
        interface._detach_lock = threading.Lock()
        interface._stopping = False
        interface._teardown_complete = False
        interface.detached = False
        interface.write_lock = threading.Lock()
        interface.outbound_udp_socket = None
        interface.adopted_interfaces = {}
        interface.link_local_addresses = []
        interface.multicast_echoes = {}
        interface.initial_echoes = {}
        interface.timed_out_interfaces = {}
        interface.mif_deque = []
        interface.mif_deque_times = []
        interface.announce_interval = 0.01
        interface.peer_job_interval = 0.01
        interface.peering_timeout = 1
        interface.reverse_peering_interval = 1
        interface.multicast_echo_timeout = 1
        return interface

    def test_real_udp_listener_stops_and_port_can_be_rebound(self):
        interface = self.make_interface()
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        address = listener.getsockname()
        listener.settimeout(0.05)
        interface._discovery_sockets.append(listener)
        thread = interface._start_thread(interface.discovery_handler, listener, "lo", False)
        interface.detach()
        self.assertFalse(thread.is_alive())
        rebound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            rebound.bind(address)
        finally:
            rebound.close()

    def test_detach_tears_down_children_and_clears_registries(self):
        interface = self.make_interface()
        transport_registry = []

        class Child:
            def __init__(self):
                self.detach_count = 0
                self.teardown_count = 0

            def detach(self):
                self.detach_count += 1

            def teardown(self):
                self.teardown_count += 1
                interface.spawned_interfaces.pop("peer", None)
                transport_registry.remove(self)

        child = Child()
        interface.spawned_interfaces["peer"] = child
        interface.peers["peer"] = ["if", time.time(), time.time()]
        transport_registry.append(child)
        interface.detach()
        self.assertEqual(1, child.detach_count)
        self.assertEqual(1, child.teardown_count)
        self.assertEqual({}, interface.spawned_interfaces)
        self.assertEqual([], transport_registry)

    def test_detach_removes_real_peer_from_parent_and_transport(self):
        interface = self.make_interface()
        child = AutoInterfacePeer.__new__(AutoInterfacePeer)
        child.owner = interface
        child.parent_interface = interface
        child.addr = "peer"
        child.ifname = "eth0"
        child.detached = False
        child.online = True
        child.OUT = True
        child.IN = True
        interface.spawned_interfaces[child.addr] = child
        with mock.patch.object(RNS.Transport, "remove_interface") as remove_interface:
            interface.detach()
        remove_interface.assert_called_once_with(child)
        self.assertEqual({}, interface.spawned_interfaces)

    def test_repeat_detach_is_idempotent(self):
        interface = self.make_interface()
        outbound = mock.Mock()
        interface.outbound_udp_socket = outbound
        interface.detach()
        interface.detach()
        outbound.close.assert_called_once_with()

    def test_add_peer_is_rejected_after_stopping_begins(self):
        interface = self.make_interface()
        interface._stopping = True
        with mock.patch("RNS.Interfaces.AutoInterface.AutoInterfacePeer") as peer, \
             mock.patch("RNS.Transport.add_interface", create=True) as add_interface:
            self.assertFalse(interface.add_peer("fe80::2", "eth0"))
        peer.assert_not_called()
        add_interface.assert_not_called()
        self.assertEqual({}, interface.peers)

    def test_announce_and_peer_jobs_terminate_promptly(self):
        interface = self.make_interface()
        interface.peer_announce = mock.Mock()
        announce = interface._start_thread(interface.announce_handler, "eth0")
        jobs = interface._start_thread(interface.peer_jobs)
        time.sleep(0.03)
        interface.detach()
        self.assertFalse(announce.is_alive())
        self.assertFalse(jobs.is_alive())

    def test_detach_from_udp_server_thread_does_not_deadlock(self):
        interface = self.make_interface()
        detached = threading.Event()

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                interface.detach()
                detached.set()

        server = socketserver.UDPServer(("127.0.0.1", 0), Handler)
        interface.interface_servers["lo"] = server
        server_thread = interface._start_thread(server.serve_forever)
        interface._server_threads[server] = server_thread
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sender.sendto(b"stop", server.server_address)
            self.assertTrue(detached.wait(1.0))
            server_thread.join(1.0)
            self.assertFalse(server_thread.is_alive())
        finally:
            sender.close()

    def test_final_init_failure_cleans_up_already_started_server(self):
        interface = self.make_interface()
        interface.adopted_interfaces = {"one": "fe80::1", "two": "fe80::2"}
        interface.data_port = 1234
        interface.interface_name_to_index = mock.Mock(return_value=1)
        stopped = threading.Event()

        class Server:
            def serve_forever(self):
                stopped.wait()

            def shutdown(self):
                stopped.set()

            def server_close(self):
                pass

        server = Server()
        address = (socket.AF_INET6, socket.SOCK_DGRAM, 0, "", ("::1", 1234, 0, 1))
        with mock.patch("RNS.Interfaces.AutoInterface.socket.getaddrinfo", return_value=[address]), \
             mock.patch("RNS.Interfaces.AutoInterface.socketserver.UDPServer", side_effect=[server, OSError("bind failed")]):
            with self.assertRaises(OSError):
                interface.final_init()
        self.assertTrue(stopped.is_set())
        self.assertTrue(interface._teardown_complete)
        self.assertEqual({}, interface.interface_servers)

    def test_partial_discovery_initialisation_closes_created_sockets(self):
        class NetInfo:
            AF_INET6 = socket.AF_INET6

            @staticmethod
            def interfaces():
                return ["eth0"]

            @staticmethod
            def ifaddresses(ifname):
                return {socket.AF_INET6: [{"addr": "fe80::1"}]}

            @staticmethod
            def interface_name_to_nice_name(ifname):
                return ifname

        config = Config(name="test", devices=["eth0"])
        first = FailingSocket(fail_on_connect=False)
        second = FailingSocket(fail_on_connect=False, fail_on_setsockopt=True)
        reticulum = mock.Mock()
        with mock.patch.object(Interface, "get_config_obj", return_value=config), \
             mock.patch.object(RNS.Reticulum, "get_instance", return_value=reticulum), \
             mock.patch("RNS.Interfaces.netinfo.interfaces", NetInfo.interfaces), \
             mock.patch("RNS.Interfaces.netinfo.ifaddresses", NetInfo.ifaddresses), \
             mock.patch("RNS.Interfaces.netinfo.interface_name_to_nice_name", NetInfo.interface_name_to_nice_name), \
             mock.patch("RNS.Interfaces.AutoInterface.socket.if_nametoindex", return_value=1), \
             mock.patch("RNS.Interfaces.AutoInterface.socket.socket", side_effect=[first, second]), \
             mock.patch("RNS.Interfaces.AutoInterface.socket.getaddrinfo", return_value=[(socket.AF_INET6, socket.SOCK_DGRAM, 0, "", ("::1", 1, 0, 0))]):
            interface = AutoInterface(mock.Mock(), config)
        self.assertTrue(first.closed)
        self.assertTrue(second.closed)
        self.assertEqual([], interface._discovery_sockets)


if __name__ == "__main__":
    unittest.main(verbosity=2)
