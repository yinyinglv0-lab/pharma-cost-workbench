"""Launcher port selection must not interfere with existing services."""
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from launch_system import free_port


def test_free_port_skips_occupied_listener():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        occupied = listener.getsockname()[1]
        selected = free_port(occupied)
        assert selected != occupied
        assert occupied < selected < occupied + 30
        with socket.socket() as check:
            check.bind(('127.0.0.1', selected))
