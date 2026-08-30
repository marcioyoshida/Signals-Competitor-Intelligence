"""Test-suite guards (issue #29).

Fail-fast any test that reaches the network without being marked ``@pytest.mark.network``,
so the default (hermetic) suite stays deterministic and CI-safe. Live tests opt in with
the marker and are excluded in CI via ``-m "not network"``.
"""
import socket

import pytest

_real_socket = socket.socket


class NetworkBlockedError(RuntimeError):
    pass


@pytest.fixture(autouse=True)
def _guard_network(request):
    """Block socket creation for non-network tests (raises, not hangs)."""
    if request.node.get_closest_marker("network"):
        yield
        return

    def _blocked(*args, **kwargs):
        raise NetworkBlockedError(
            f"{request.node.nodeid} tried to open a socket without @pytest.mark.network "
            "— mark it `network` or mock the I/O (requests/boto3)."
        )

    socket.socket = _blocked
    try:
        yield
    finally:
        socket.socket = _real_socket
