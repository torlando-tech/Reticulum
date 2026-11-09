import socket
import socketserver
import threading
import time
import unittest
from unittest import mock

import RNS
from RNS.Interfaces.AutoInterface import AutoInterface, AutoInterfacePeer
from RNS.Interfaces.Interface import Interface


class Config(dict):
    def as_list(self, key):
        return self[key]


class FailingSocket:
    def __init__(self, fail_on_setsockopt=False):
        self.closed = False
        self.fail_on_setsockopt = fail_on_setsockopt

    def settimeout(self, timeout):
        pass

    def setsockopt(self, *args):
        if self.fail_on_setsockopt:
            raise OSError("socket setup failed")

    def bind(self, address):
        pass

    def close(self):
        self.closed = True



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
        interface.discovery_sockets = []
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
        interface.discovery_sockets.append(listener)
        thread = interface._start_thread(interface.discovery_handler, listener, "lo")
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

    def test_queued_writer_cannot_recreate_socket_after_detach(self):
        interface = self.make_interface()
        writer_waiting = threading.Event()
        release_writer = threading.Event()

        class GatedLock:
            def __enter__(self):
                if threading.current_thread().name == "queued-writer":
                    writer_waiting.set()
                    release_writer.wait(1)
                return self

            def __exit__(self, *args):
                pass

        interface.write_lock = GatedLock()
        peer = AutoInterfacePeer.__new__(AutoInterfacePeer)
        peer.owner = interface
        peer.online = True
        peer.addr = "fe80::2"
        peer.ifname = "eth0"
        peer.peer_addr = None
        peer.addr_info = None
        peer.txb = 0
        created = mock.Mock()
        writer = threading.Thread(target=peer.process_outgoing, args=(b"data",), name="queued-writer")
        with mock.patch("RNS.Interfaces.AutoInterface.socket.socket", return_value=created):
            writer.start()
            self.assertTrue(writer_waiting.wait(1))
            interface.detach()
            release_writer.set()
            writer.join(1)
        self.assertFalse(writer.is_alive())
        self.assertIsNone(interface.outbound_udp_socket)
        created.sendto.assert_not_called()

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

    def test_final_init_thread_start_failure_closes_never_started_server(self):
        interface = self.make_interface()
        interface.adopted_interfaces = {"one": "fe80::1"}
        interface.data_port = 1234
        interface.interface_name_to_index = mock.Mock(return_value=1)

        class Server:
            def __init__(self):
                self.closed = False
                self.shutdown_called = False

            def serve_forever(self):
                pass

            def shutdown(self):
                self.shutdown_called = True

            def server_close(self):
                self.closed = True

        server = Server()
        address = (socket.AF_INET6, socket.SOCK_DGRAM, 0, "", ("::1", 1234, 0, 1))
        with mock.patch("RNS.Interfaces.AutoInterface.socket.getaddrinfo", return_value=[address]), \
             mock.patch("RNS.Interfaces.AutoInterface.socketserver.UDPServer", return_value=server), \
             mock.patch("RNS.Interfaces.AutoInterface.threading.Thread.start", side_effect=RuntimeError("thread start failed")):
            with self.assertRaises(RuntimeError):
                interface.final_init()
        self.assertTrue(server.closed)
        self.assertFalse(server.shutdown_called)
        self.assertTrue(interface._teardown_complete)
        self.assertFalse(any(thread.is_alive() for thread in interface._threads))

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
        first = FailingSocket()
        second = FailingSocket(fail_on_setsockopt=True)
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
        self.assertEqual([], interface.discovery_sockets)
        self.assertEqual({}, interface.adopted_interfaces)
        self.assertEqual([], interface.link_local_addresses)
        self.assertEqual({}, interface.multicast_echoes)

    def test_second_discovery_thread_start_failure_stops_prior_workers(self):
        interface = self.make_interface()
        interface.announce_interval = 60
        interface.peer_announce = mock.Mock()

        class TimeoutSocket:
            def __init__(self):
                self.closed = False

            def recvfrom(self, _size):
                if self.closed:
                    raise OSError("closed")
                raise socket.timeout()

            def close(self):
                self.closed = True

        multicast = TimeoutSocket()
        unicast = TimeoutSocket()
        interface_stop = threading.Event()
        real_start = interface._start_thread
        calls = 0

        def fail_third_start(*args):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise RuntimeError("injected unicast thread start failure")
            return real_start(*args)

        with mock.patch.object(interface, "_start_thread", side_effect=fail_third_start):
            with self.assertRaisesRegex(RuntimeError, "injected unicast"):
                interface._start_discovery_workers(multicast, unicast, "if0", interface_stop)

        self.assertTrue(interface_stop.is_set())
        self.assertTrue(multicast.closed)
        self.assertTrue(unicast.closed)
        self.assertTrue(all(not thread.is_alive() for thread in interface._threads))


if __name__ == "__main__":
    unittest.main(verbosity=2)
