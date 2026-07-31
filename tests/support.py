"""Websocket helpers for tests.

`TestClient`'s websocket reads block forever, so a bug that stops the server
sending would hang the suite instead of failing it. Everything here reads with
a deadline and turns silence into an assertion.
"""

import threading

TIMEOUT = 5.0


def receive_json(ws, timeout=TIMEOUT, expecting="a message"):
    """Read one message, or fail with a useful error if none arrives."""
    box = {}

    def grab():
        try:
            box["msg"] = ws.receive_json()
        except Exception as exc:  # surface it on the calling thread
            box["err"] = exc

    # daemon=True: if the server never sends, this thread must not keep pytest
    # alive at interpreter exit.
    reader = threading.Thread(target=grab, daemon=True)
    reader.start()
    reader.join(timeout)

    if reader.is_alive():
        raise AssertionError(
            f"timed out after {timeout}s waiting for {expecting} — "
            "the server stopped sending mid-turn")
    if "err" in box:
        raise box["err"]
    return box["msg"]


def drain_turn(ws, timeout=TIMEOUT):
    """Collect messages up to and including the end of the current turn."""
    messages = []
    while True:
        message = receive_json(ws, timeout, expecting="the rest of a turn")
        messages.append(message)
        if message["type"] in ("stream_done", "error"):
            return messages
