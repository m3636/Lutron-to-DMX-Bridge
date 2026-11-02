# Third-party software

Lutron DMX is built on the open-source packages below. Each is used under its own license; the license
texts ship inside the installed packages. This list names the license of each package at the time of writing.

| Package | License | Project |
|---|---|---|
| fastapi, starlette, pydantic, pydantic-core, annotated-types, annotated-doc, typing-inspection | MIT | https://github.com/fastapi/fastapi |
| uvicorn, click, idna, websockets, pyserial | BSD-3-Clause / BSD | https://www.uvicorn.org |
| h11, anyio, ifaddr | MIT | https://github.com/python-hyper/h11 |
| cryptography, cffi, pycparser | Apache-2.0 or BSD-3-Clause / MIT-0 / BSD-3-Clause | https://github.com/pyca/cryptography |
| pylutron-caseta | Apache-2.0 | https://github.com/gurumitts/pylutron-caseta |
| orjson | MPL-2.0 and (Apache-2.0 or MIT) | https://github.com/ijl/orjson |
| zeroconf | LGPL-2.1-or-later | https://github.com/python-zeroconf/python-zeroconf |
| certifi, if present (pulled in by dependencies) | MPL-2.0 | https://github.com/certifi/python-certifi |
| typing-extensions | PSF-2.0 | https://github.com/python/typing_extensions |
| opentelemetry-api | Apache-2.0 | https://github.com/open-telemetry/opentelemetry-python |
| Python, tkinter / Tcl/Tk (in the built app) | PSF-2.0 / Tcl/Tk license | https://www.python.org |
| PyInstaller (used to build the app, bootloader included in it) | GPL-2.0-or-later with the bootloader exception | https://pyinstaller.org |

## Notes

* **zeroconf (LGPL-2.1-or-later)** is used unmodified as a separate library. In the built app it is a
  separate file inside the app folder, so you can replace it with another build of the same library.
  Its source is at the project link above.
* **MPL-2.0 packages (orjson, certifi)** are used unmodified; their source is at the project links above.
* Lutron's Integration Protocol is described in Lutron's own documentation, which is not included here.
