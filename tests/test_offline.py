"""The suite-wide offline guard in conftest.py actually blocks the network."""

import socket

import pytest


def test_network_guard_blocks_outbound() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(RuntimeError, match="must not use the network"),
    ):
        sock.connect(("93.184.215.14", 443))
