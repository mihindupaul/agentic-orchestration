"""controller: run the controller service, or send it one request.

  python -m controller serve  [--sim-host H] [--deck FILE] [--api-port 8700]
  python -m controller status
  python -m controller do pick 3 A1        python -m controller do place scanner
  python -m controller do scan             python -m controller do heat 60
  python -m controller do temperature      python -m controller do heater_off
"""

from __future__ import annotations

import argparse
import asyncio
import json

from .api import ControllerClient, serve
from .controller import Controller
from .deck import Deck
from .errors import ControllerError


async def _serve(a) -> None:
    ctl = await Controller(a.sim_host, deck=Deck.load(a.deck)).start()
    server = await serve(ctl, a.api_host, a.api_port)
    print(f"the controller API on {a.api_host}:{a.api_port}, devices on {a.sim_host}", flush=True)
    async with server:
        await server.serve_forever()


def _request(words: list) -> dict:
    op, rest = words[0], words[1:]
    if op in ("pick", "place"):
        return {"cmd": op, "where": " ".join(rest)}
    if op == "heat":
        return {"cmd": op, "celsius": float(rest[0])}
    if op == "rack_map":
        return {"cmd": op, "bay": int(rest[0])}
    return {"cmd": op}


async def _client(a) -> None:
    c = ControllerClient(a.api_host, a.api_port)
    await c.open()
    try:
        req = {"cmd": "status"} if a.cmd == "status" else _request(a.words)
        out = await c.call(req.pop("cmd"), **req)
        out.pop("id", None)
        out.pop("ok", None)
        print(json.dumps(out, indent=2))
    except ControllerError as exc:
        print(f"{type(exc).__name__}: {exc}")
    finally:
        await c.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="workcell-controller")
    ap.add_argument("cmd", choices=["serve", "status", "do"])
    ap.add_argument("words", nargs="*", help="for `do`: an operation and its arguments")
    ap.add_argument("--sim-host", default="127.0.0.1")
    ap.add_argument("--deck", help="deck file (default: the packaged workcell deck)")
    ap.add_argument("--api-host", default="127.0.0.1")
    ap.add_argument("--api-port", type=int, default=8700)
    a = ap.parse_args()
    if a.cmd == "do" and not a.words:
        ap.error("do needs an operation, e.g. do pick 3 A1")
    try:
        asyncio.run(_serve(a) if a.cmd == "serve" else _client(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
