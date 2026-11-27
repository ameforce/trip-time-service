"""Select a free local port for the CI Playwright web server."""

import socket

with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    print(listener.getsockname()[1])
