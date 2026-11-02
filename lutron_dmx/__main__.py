"""Command-line entry point.

    lutron-dmx                 run the web GUI (and DMX input if enabled)
    lutron-dmx discover        find the Lutron processor's IP address on the local network
    lutron-dmx pair [HOST]     pair with an RA3 / RA2 Select / Caseta processor (press its button);
                               HOST is found automatically when left out
    lutron-dmx backup [FILE]   save a JSON snapshot of the Lutron system
    lutron-dmx info            print where config and backups live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

from . import __version__
from .config import AppConfig, data_dir


def _run_server(args: argparse.Namespace) -> None:
    import uvicorn

    from .controller import Controller
    from .web.app import create_app

    config = AppConfig.load()
    if args.simulator:
        config.driver.kind = "simulator"
    host = args.host or config.web_host
    port = args.port or config.web_port
    controller = Controller(config)
    discover = None
    if args.simulator:
        from .discovery.fake import fake_discover as discover
    from .updater import Updater

    updater = Updater(config)
    app = create_app(controller, discover=discover, updater=updater, bind_host=host)

    async def serve() -> None:
        await controller.start()
        server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
        url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}/"
        print(f"Lutron DMX {__version__} running at {url}  (Ctrl+C to stop)")
        if not args.no_browser:
            asyncio.get_running_loop().call_later(1.0, webbrowser.open, url)
        try:
            await server.serve()
        finally:
            await controller.stop()

    asyncio.run(serve())


def _print_devices(devices) -> None:
    print(f"{'#':>2}  {'Address':<16} {'Port':<5} {'Type':<6} {'Match':<6} Details")
    for i, d in enumerate(devices, 1):
        kind = {"leap": "LEAP", "ra2": "RA2"}.get(d.kind, d.kind)
        details = " ".join(x for x in (d.system, d.name, d.mac, f"[{d.method}]") if x)
        print(f"{i:>2}  {d.host:<16} {d.port:<5} {kind:<6} {int(d.confidence * 100):>3}%   {details}")


def _find(scan, quiet: bool = False):
    from .discovery import discover

    progress = None if quiet else (lambda msg: print(f"  {msg}", flush=True))
    return asyncio.run(discover(scan=scan, on_progress=progress))


def _discover(args: argparse.Namespace) -> None:
    devices = _find(True if args.scan else "auto")
    if not devices:
        print("No Lutron processor found. Check that this computer is on the same network as the "
              "processor, or enter its IP address manually.")
        sys.exit(1)
    print()
    _print_devices(devices)
    best = devices[0]
    if best.kind == "leap":
        print(f"\nNext: lutron-dmx pair {best.host}")
    else:
        print(f"\nRadioRA 2 Main Repeater: enter {best.host} in Settings (needs the integration login).")


def _choose_leap():
    """Auto-discover a LEAP processor for `pair` with no HOST; ask when there are several."""
    print("Looking for a Lutron processor on the network...")
    devices = [d for d in _find("auto") if d.kind == "leap"]
    if not devices:
        raise SystemExit("No LEAP processor found. Check that this computer is on the same network "
                         "as the processor, or pass its address: lutron-dmx pair <ip>")
    if len(devices) == 1:
        return devices[0]
    _print_devices(devices)
    while True:
        answer = input(f"Pair with which one? [1-{len(devices)}] ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(devices):
            return devices[int(answer) - 1]


def _pair(args: argparse.Namespace) -> None:
    from .drivers.leap import pair

    config = AppConfig.load()
    hostname = ""
    if not args.host:
        found = _choose_leap()
        args.host, hostname = found.host, found.name
        print(f"Found {found.system or 'a Lutron processor'} at {args.host}")
    out_dir = data_dir() / "certs"
    out_dir.mkdir(exist_ok=True)
    print(f"Press the small button on the back of the Lutron processor at {args.host} now...")
    asyncio.run(pair(args.host, out_dir, trust_presented_cert=args.trust_controller_cert))
    config.driver.kind = "leap"
    config.driver.host = args.host
    config.driver.hostname = hostname
    config.driver.leap_cert_dir = str(out_dir)
    config.save()
    print(f"Paired. Certificates saved in {out_dir}")


def _backup(args: argparse.Namespace) -> None:
    from .drivers import create_driver

    config = AppConfig.load()

    async def run() -> dict:
        driver = create_driver(config.driver)
        await driver.connect()
        try:
            return await driver.backup()
        finally:
            await driver.close()

    data = asyncio.run(run())
    if args.file:
        path = Path(args.file)
    else:
        (data_dir() / "backups").mkdir(exist_ok=True)
        path = data_dir() / "backups" / f"lutron-backup-{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    print(f"Backup written to {path}")


def _info(_: argparse.Namespace) -> None:
    print(f"Lutron DMX {__version__}")
    print(f"Config:  {AppConfig.path()}")
    print(f"Backups: {data_dir() / 'backups'}")
    print(f"Logs:    {data_dir() / 'logs'} (written by the double-click launcher)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lutron-dmx", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", help="web GUI bind address (default from config, 127.0.0.1)")
    parser.add_argument("--port", type=int, help="web GUI port (default 8080)")
    parser.add_argument("--simulator", action="store_true", help="use the built-in simulated system")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("pair", help="pair with an RA3 / RA2 Select / Caseta processor")
    p.add_argument("host", nargs="?", help="processor IP address (found automatically if omitted)")
    p.add_argument("--trust-controller-cert", action="store_true",
                   help="if the controller's pairing certificate matches no known Lutron CA, "
                        "accept it for this one pairing (only for a controller you know is yours)")
    d = sub.add_parser("discover", help="find the Lutron processor's IP address on the local network")
    d.add_argument("--scan", action="store_true", help="also scan the subnet even if mDNS found something")
    b = sub.add_parser("backup", help="save a JSON snapshot of the Lutron system")
    b.add_argument("file", nargs="?")
    sub.add_parser("info", help="show config and backup locations")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = {"pair": _pair, "discover": _discover, "backup": _backup, "info": _info}
    try:
        handlers.get(args.cmd, _run_server)(args)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
