"""Find out whether a newer release exists on GitHub and, on request, install it.

This is the one thing in the app that talks to the internet, so it is deliberately narrow:

* It asks GitHub's public "latest release" endpoint for this repository, nothing else, on start-up
  and every few hours.  Automatic checks can be turned off in Settings; nothing is sent but the
  request itself (no identifiers, no analytics).  Offline, rate-limited or not-found (the
  repository is private) are all quietly "no update" - never an error banner.
* Installing happens only after an explicit click.  The download must come from this repository's
  own release assets over HTTPS, and its SHA-256 must match the ``SHA256SUMS.txt`` published in the
  same release.  The unpacked app is started once (``--self-check``) before the running one is touched.
* Settings, scenes and certificates live in the user's data folder, outside the app, and are never
  modified by an update.  The previous version is kept for rollback.

How the running copy gets replaced depends on how it was installed:

``macos-app`` / ``windows-exe``  the packaged app.  The new copy is unpacked next to the data folder,
    checked, and then *it* is started in ``--apply-update`` mode as a small helper (Python only exists
    inside the app itself, and a running exe cannot replace itself).  The old app quits; the helper
    waits for it to be gone, moves it aside, copies the new one in, starts it, and leaves a result
    file for the new app to report.  If anything fails the old copy is moved back and started again.
``git``  a developer checkout: fast-forward ``main`` to the release tag and ``pip install -e .``.
``source``  anything else: show instructions only.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .config import AppConfig, data_dir

log = logging.getLogger(__name__)

#: The repository releases are published from.  Public information; the only place it is written down.
REPO = "m3636/Lutron-to-DMX-Bridge"
API_BASE = "https://api.github.com"
WEB_BASE = "https://github.com"

FIRST_CHECK_DELAY_S = 20.0
CHECK_INTERVAL_S = 12 * 3600.0
HTTP_TIMEOUT_S = 15.0
MAX_ZIP_BYTES = 1 << 30
SELF_CHECK_TIMEOUT_S = 180.0
EXIT_DELAY_S = 1.5  # lets the browser receive "restarting" before the server goes away
HELPER_WAIT_S = 90.0
PLAN_NAME = "plan.json"
RESULT_NAME = "update-result.json"
#: Setting this turns the automatic checks off whatever the config says (used by the test suite).
NO_CHECK_ENV = "LUTRON_TO_DMX_BRIDGE_NO_UPDATE_CHECK"

APP_DIR_NAME = "Lutron To DMX Bridge"
PLATFORM_LABEL = {"darwin": "macOS", "win32": "Windows"}


class UpdateError(Exception):
    """A problem worth showing to the user in plain words."""


# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------
_SEMVER = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def parse_version(text: str) -> tuple[int, int, int] | None:
    """"v1.2.3" / "1.2.3" -> (1, 2, 3); anything else -> None."""
    m = _SEMVER.match((text or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def is_newer(latest: str, current: str) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def parse_sha256sums(text: str) -> dict[str, str]:
    """``sha256sum`` output ("<hex>  <name>" or "<hex> *<name>") -> {name: hex}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
        if m:
            out[m.group(2)] = m.group(1).lower()
    return out


# ---------------------------------------------------------------------------
# how this copy is installed
# ---------------------------------------------------------------------------
@dataclass
class InstallInfo:
    #: "macos-app", "windows-exe", "git" or "source"
    kind: str
    label: str
    #: True when the Update button can do everything itself
    can_auto: bool
    #: what to do by hand when ``can_auto`` is False
    manual: str = ""
    #: the bundle / app folder / checkout this copy runs from
    target: Path | None = None

    def to_dict(self) -> dict:
        return {"kind": self.kind, "label": self.label, "can_auto": self.can_auto, "manual": self.manual}


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def detect_install(executable: str | None = None, frozen: bool | None = None, platform: str | None = None,
                   root: Path | None = None) -> InstallInfo:
    executable = executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    platform = platform or sys.platform
    if frozen and platform == "darwin":
        exe = Path(executable)
        app = exe.parents[2] if len(exe.parents) > 2 else exe
        if app.suffix != ".app":
            return InstallInfo("source", "macOS app", False,
                               "This copy of the app is not in the usual place, so it can't replace itself. "
                               "Download the new version from the release page.", None)
        if "AppTranslocation" in app.parts:
            return InstallInfo("macos-app", "macOS app", False,
                               "macOS is running Lutron To DMX Bridge from a temporary location because it was opened "
                               "straight from Downloads. Drag Lutron To DMX Bridge.app into your Applications folder, "
                               "open it from there, and it can update itself.", app)
        if not (os.access(app.parent, os.W_OK) and os.access(app, os.W_OK)):
            return InstallInfo("macos-app", "macOS app", False,
                               f"The app can't write to {app.parent}. Download the new version from the release "
                               "page and replace Lutron To DMX Bridge.app by hand.", app)
        return InstallInfo("macos-app", "macOS app", True, "", app)
    if frozen and platform == "win32":
        folder = Path(executable).parent
        if not os.access(folder.parent, os.W_OK):
            return InstallInfo("windows-exe", "Windows app", False,
                               f"The app can't write to {folder.parent}. Download the new version from the release "
                               "page and replace the Lutron To DMX Bridge folder by hand.", folder)
        return InstallInfo("windows-exe", "Windows app", True, "", folder)
    root = root or repo_root()
    if (root / ".git").exists():
        return InstallInfo("git", "git checkout", shutil.which("git") is not None,
                           "git was not found. Run `git pull` in the project folder, then reinstall with "
                           "`pip install -e .`.", root)
    return InstallInfo("source", "Python install", False,
                       "This copy was not installed from a git checkout, so it can't update itself. "
                       "Download the new version from the release page.", None)


# ---------------------------------------------------------------------------
# GitHub access
# ---------------------------------------------------------------------------
@dataclass
class ReleaseInfo:
    version: str
    tag: str
    url: str
    published_at: str
    #: asset name -> {"size", "api_url", "download_url"}
    assets: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"version": self.version, "tag": self.tag, "url": self.url, "published_at": self.published_at}


class _GuardedRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to acceptable URLs, and never hand the token to another host."""

    def __init__(self, url_ok: Callable[[str], bool]) -> None:
        super().__init__()
        self.url_ok = url_ok

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not self.url_ok(newurl):
            raise urllib.error.URLError(f"refusing to follow a redirect to {newurl}")
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            for h in ("Authorization", "authorization"):
                new.headers.pop(h, None)
                new.unredirected_hdrs.pop(h, None)
        return new


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# the updater
# ---------------------------------------------------------------------------
class Updater:
    def __init__(self, config: AppConfig, *, repo: str = REPO, api_base: str = API_BASE, web_base: str = WEB_BASE,
                 current: str = __version__, install: InstallInfo | None = None, platform: str | None = None,
                 check_interval: float = CHECK_INTERVAL_S, first_check_delay: float = FIRST_CHECK_DELAY_S,
                 smoke_test: bool = True) -> None:
        self.config = config
        self.repo = repo
        self.api_base = api_base.rstrip("/")
        self.web_base = web_base.rstrip("/")
        self.current = current
        self.platform = platform or sys.platform
        self.install = install or detect_install()
        self.check_interval = check_interval
        self.first_check_delay = first_check_delay
        self.smoke_test = smoke_test
        #: set by whoever owns the process: ends the app so the helper can swap it (any thread)
        self.request_exit: Callable[[], None] | None = None

        self.latest: ReleaseInfo | None = None
        self.checked_at: str | None = None
        self.check_error: str | None = None
        self.phase = "idle"  # idle, downloading, verifying, staging, testing, restarting, installed, error
        self.progress = 0.0
        self.message = ""
        #: what the previous update attempt did, read from the result file at start-up
        self.last_result: dict | None = None
        self._busy = False
        self._announced: str | None = None
        self._last_error_logged: str | None = None
        self._strict = self.api_base.startswith("https://")
        self._read_result()

    # -- settings ------------------------------------------------------------
    @property
    def auto_check(self) -> bool:
        return bool(self.config.updates.check) and not os.environ.get(NO_CHECK_ENV)

    @property
    def token(self) -> str:
        return (self.config.updates.token or "").strip()

    def available(self) -> ReleaseInfo | None:
        rel = self.latest
        return rel if rel is not None and is_newer(rel.version, self.current) else None

    def public_state(self) -> dict:
        rel = self.available()
        return {
            "auto_check": self.auto_check,
            "current": self.current,
            "latest": rel.version if rel else None,
            "available": rel is not None,
            "dismissed": bool(rel) and self.config.updates.dismissed_version == rel.version,
            "notes_url": rel.url if rel else None,
            "published_at": rel.published_at if rel else None,
            "checked_at": self.checked_at,
            "check_error": self.check_error,
            "install": self.install.to_dict(),
            "phase": self.phase,
            "progress": round(self.progress, 3),
            "message": self.message,
            "last_result": self.last_result,
        }

    # -- the periodic check ----------------------------------------------------
    async def run(self) -> None:
        """Background loop, for the lifetime of the web app."""
        try:
            await asyncio.sleep(self.first_check_delay)
            while True:
                if self.auto_check:
                    await self.check()
                self._cleanup_staging()
                await asyncio.sleep(self.check_interval)
        except asyncio.CancelledError:
            pass

    async def check(self) -> ReleaseInfo | None:
        """Ask GitHub now.  Never raises: the outcome is in ``latest`` / ``check_error``."""
        try:
            rel = await asyncio.to_thread(self.fetch_latest)
        except UpdateError as exc:
            self.check_error = str(exc)
            self.checked_at = _utcnow()
            if self._last_error_logged != self.check_error:
                self._last_error_logged = self.check_error
                log.info("Update check: %s", self.check_error)
            else:
                log.debug("Update check: %s", self.check_error)
            return None
        except Exception as exc:  # a bug here must never take the app down
            self.check_error = "The update check failed unexpectedly."
            self.checked_at = _utcnow()
            log.debug("Update check crashed: %r", exc, exc_info=True)
            return None
        self.latest, self.check_error, self._last_error_logged = rel, None, None
        self.checked_at = _utcnow()
        if rel is not None and is_newer(rel.version, self.current) and self._announced != rel.version:
            self._announced = rel.version
            log.info("Update available: Lutron To DMX Bridge %s (this is %s). Open the web GUI and click \"Update now\"; "
                     "release notes: %s", rel.version, self.current, rel.url)
        return rel

    def fetch_latest(self) -> ReleaseInfo:
        """Blocking.  The latest published, non-prerelease release of this repository."""
        url = f"{self.api_base}/repos/{self.repo}/releases/latest"
        try:
            data = json.loads(self._get(url, accept="application/vnd.github+json", max_bytes=2_000_000))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise UpdateError("No release found. If the repository is still private, updates can't be "
                                  "checked without an access token.") from exc
            if exc.code in (401, 403):
                raise UpdateError("GitHub refused the update check (rate limit, or the access token is not "
                                  "valid).") from exc
            raise UpdateError(f"GitHub answered the update check with an error ({exc.code}).") from exc
        except (urllib.error.URLError, OSError, TimeoutError, ssl.SSLError) as exc:
            raise UpdateError("Could not reach GitHub to check for updates (offline?).") from exc
        except ValueError as exc:
            raise UpdateError("GitHub's answer to the update check could not be read.") from exc
        if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
            raise UpdateError("No stable release found.")
        tag = str(data.get("tag_name") or "")
        if parse_version(tag) is None:
            raise UpdateError(f"The latest release tag {tag!r} is not a version number.")
        assets = {}
        for a in data.get("assets") or []:
            if isinstance(a, dict) and isinstance(a.get("name"), str):
                assets[a["name"]] = {"size": int(a.get("size") or 0), "api_url": str(a.get("url") or ""),
                                     "download_url": str(a.get("browser_download_url") or "")}
        html_url = str(data.get("html_url") or f"{self.web_base}/{self.repo}/releases/tag/{tag}")
        if not self._release_page_ok(html_url):
            html_url = f"{self.web_base}/{self.repo}/releases/tag/{tag}"
        version = tag[1:] if tag.startswith("v") else tag
        return ReleaseInfo(version, tag, html_url, str(data.get("published_at") or ""), assets)

    # -- HTTP --------------------------------------------------------------------
    def _release_page_ok(self, url: str) -> bool:
        """The link shown next to the banner must point at this repository on GitHub."""
        return url.lower().startswith(f"{self.web_base}/{self.repo}/".lower())  # GitHub keeps the repo's own letter case

    def _url_ok(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        if self._strict:
            host = (parts.hostname or "").lower()
            return parts.scheme == "https" and (host in ("github.com", "api.github.com") or
                                                host.endswith(".githubusercontent.com"))
        # tests point the updater at a local fake server
        bases = {urllib.parse.urlsplit(b).netloc for b in (self.api_base, self.web_base)}
        return parts.scheme in ("http", "https") and parts.netloc in bases

    def _asset_url_ok(self, url: str) -> bool:
        """Where a release asset may be fetched from: this repository's own release downloads only."""
        return url.lower().startswith((f"{self.web_base}/{self.repo}/releases/download/".lower(),
                                       f"{self.api_base}/repos/{self.repo}/releases/assets/".lower()))

    def _request(self, url: str, accept: str) -> urllib.request.Request:
        if not self._url_ok(url):
            raise UpdateError(f"Refusing to contact {url}")
        headers = {"User-Agent": f"LutronToDMXBridge/{self.current}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return urllib.request.Request(url, headers=headers)

    def _open(self, req: urllib.request.Request):
        ctx = ssl.create_default_context() if self._strict else None
        handlers: list = [_GuardedRedirects(self._url_ok)]
        if ctx is not None:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        # no proxies: a local-network tool should not pick up a surprise proxy from the environment
        opener = urllib.request.build_opener(*handlers)
        return opener.open(req, timeout=HTTP_TIMEOUT_S)

    def _get(self, url: str, accept: str, max_bytes: int) -> bytes:
        with self._open(self._request(url, accept)) as resp:
            data = resp.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise UpdateError("GitHub's answer was unexpectedly large.")
        return data

    def _asset_source(self, asset: dict) -> str:
        """With a token the API URL is needed (private repositories); otherwise the public download URL."""
        url = asset["api_url"] if self.token and asset["api_url"] else asset["download_url"]
        if not url or not self._asset_url_ok(url):
            raise UpdateError("The release lists a file outside this repository's own release downloads; "
                              "not downloading it.")
        return url

    # -- confirmation ---------------------------------------------------------------
    def dismiss(self, version: str) -> None:
        self.config.updates.dismissed_version = version
        self.config.save()

    # -- applying -------------------------------------------------------------------
    def asset_name(self, rel: ReleaseInfo) -> str:
        os_name = PLATFORM_LABEL.get(self.platform)
        if os_name is None:
            raise UpdateError("There is no packaged build for this operating system.")
        return f"LutronToDMXBridge-{rel.version}-{os_name}.zip"

    def work_dir(self) -> Path:
        d = data_dir() / "updates"
        d.mkdir(parents=True, exist_ok=True)
        return d

    async def apply(self, version: str, warnings: list[str], confirmed: bool) -> dict:
        """Start installing ``version``.  Returns at once; progress is in the public state.

        Returns ``{"started": True}`` or ``{"needs_confirm": True, "reasons": [...]}``."""
        rel = self.available()
        if rel is None or rel.version != version:
            raise UpdateError("That version is not the latest release any more. Check for updates again.")
        if self._busy:
            raise UpdateError("An update is already in progress.")
        if not self.install.can_auto:
            raise UpdateError(self.install.manual or "This copy can't update itself.")
        if warnings and not confirmed:
            return {"needs_confirm": True, "reasons": warnings}
        self._busy = True
        self.phase, self.progress, self.message = "downloading", 0.0, f"Downloading Lutron To DMX Bridge {rel.version}…"
        asyncio.get_running_loop().create_task(self._apply_task(rel))
        return {"started": True}

    async def _apply_task(self, rel: ReleaseInfo) -> None:
        try:
            await asyncio.to_thread(self._apply_blocking, rel)
        except UpdateError as exc:
            self._fail(str(exc))
        except Exception as exc:
            log.exception("Update failed")
            self._fail(f"The update failed unexpectedly: {exc}")
        else:
            if self.phase == "restarting" and self.request_exit is not None:
                await asyncio.sleep(EXIT_DELAY_S)
                log.info("Closing so the new version can take over…")
                self.request_exit()
        finally:
            self._busy = False

    def _fail(self, message: str) -> None:
        log.error("Update failed: %s", message)
        self.phase, self.message = "error", message

    def _set(self, phase: str, progress: float, message: str) -> None:
        self.phase, self.progress, self.message = phase, progress, message
        log.info("Update: %s", message)

    def _apply_blocking(self, rel: ReleaseInfo) -> None:
        if self.install.kind == "git":
            self._apply_git(rel)
            return
        if self.install.kind not in ("macos-app", "windows-exe") or self.install.target is None:
            raise UpdateError(self.install.manual or "This copy can't update itself.")
        work = self.work_dir() / "staging"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        zip_path = self._download_verified(rel, work)
        staged = self._stage(rel, zip_path, work)
        with contextlib.suppress(OSError):
            zip_path.unlink()  # the unpacked copy is what counts; free the disk space
        if self.smoke_test:
            self._set("testing", 0.9, "Checking that the new version starts…")
            run_self_check(executable_in(staged, self.platform), rel.version, work)
        plan = self._write_plan(rel, staged, work)
        self._set("restarting", 1.0, f"Installing {rel.version}: Lutron To DMX Bridge will close and reopen in a moment.")
        spawn_helper(executable_in(staged, self.platform), plan, self.platform)

    # -- download + verify ------------------------------------------------------------
    def _download_verified(self, rel: ReleaseInfo, work: Path) -> Path:
        name = self.asset_name(rel)
        asset, sums = rel.assets.get(name), rel.assets.get("SHA256SUMS.txt")
        if asset is None or sums is None:
            raise UpdateError(f"Release {rel.version} has no {name if asset is None else 'SHA256SUMS.txt'} "
                              "attached (the build may still be running). Try again in a few minutes.")
        try:
            sums_text = self._get(self._asset_source(sums), "application/octet-stream", 1_000_000).decode("utf-8")
        except (urllib.error.URLError, OSError, UnicodeDecodeError, TimeoutError) as exc:
            raise UpdateError(f"Could not download SHA256SUMS.txt: {exc}") from exc
        expected = parse_sha256sums(sums_text).get(name)
        if expected is None:
            raise UpdateError(f"SHA256SUMS.txt does not list {name}; not installing an unverifiable file.")
        if asset["size"] and asset["size"] > MAX_ZIP_BYTES:
            raise UpdateError("The download is unexpectedly large; not installing it.")
        dest = work / name
        part = work / (name + ".part")
        digest = hashlib.sha256()
        total = asset["size"] or 0
        done = 0
        try:
            with self._open(self._request(self._asset_source(asset), "application/octet-stream")) as resp, \
                    open(part, "wb") as out:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    done += len(chunk)
                    if done > MAX_ZIP_BYTES:
                        raise UpdateError("The download is unexpectedly large; stopped.")
                    digest.update(chunk)
                    out.write(chunk)
                    if total:
                        self.progress = min(0.7, 0.7 * done / total)
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise UpdateError(f"The download failed: {exc}") from exc
        self._set("verifying", 0.7, "Checking the download…")
        if digest.hexdigest() != expected:
            part.unlink(missing_ok=True)
            raise UpdateError("The downloaded file does not match the checksum published with the release. "
                              "Nothing was installed.")
        if total and done != total:
            part.unlink(missing_ok=True)
            raise UpdateError("The downloaded file is not the size GitHub reported. Nothing was installed.")
        part.replace(dest)
        return dest

    def _stage(self, rel: ReleaseInfo, zip_path: Path, work: Path) -> Path:
        self._set("staging", 0.8, "Unpacking…")
        out = work / "unpacked"
        return unpack_release(zip_path, out, self.platform)

    def _write_plan(self, rel: ReleaseInfo, staged: Path, work: Path) -> Path:
        assert self.install.target is not None
        target = self.install.target
        updates = self.work_dir()
        plan = {
            "pid": os.getpid(),
            "platform": self.platform,
            "from_version": self.current,
            "to_version": rel.version,
            "target": str(target),
            "staged": str(staged),
            "backup": str(updates / "previous" / f"{self.current}" / target.name),
            "result": str(data_dir() / RESULT_NAME),
            "log": str(data_dir() / "logs" / "update.log"),
            "relaunch": relaunch_command(target, self.platform),
            "wait_s": HELPER_WAIT_S,
        }
        path = work / PLAN_NAME
        path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
        return path

    # -- developer checkout ----------------------------------------------------------------
    def _apply_git(self, rel: ReleaseInfo) -> None:
        root = self.install.target
        if root is None or not self.install.can_auto:
            raise UpdateError(self.install.manual)

        def git(*args: str) -> str:
            proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=300)
            if proc.returncode:
                raise UpdateError(f"git {' '.join(args[:2])} failed: {(proc.stderr or proc.stdout).strip()[:300]}")
            return proc.stdout.strip()

        self._set("downloading", 0.2, "Updating the git checkout…")
        if git("status", "--porcelain"):
            raise UpdateError("This checkout has uncommitted changes, so it was not updated. Commit or stash "
                              "them, or run `git pull` yourself.")
        branch = git("rev-parse", "--abbrev-ref", "HEAD")
        if branch != "main":
            raise UpdateError(f"This checkout is on branch '{branch}', not main, so it was not updated. "
                              "Run `git checkout main && git pull`.")
        remote = git("remote", "get-url", "origin")
        if self.repo.lower() not in remote.replace("\\", "/").lower():
            raise UpdateError(f"The 'origin' remote ({remote}) is not {self.repo}, so it was not updated.")
        git("fetch", "origin", f"refs/tags/{rel.tag}:refs/tags/{rel.tag}")
        git("merge", "--ff-only", f"refs/tags/{rel.tag}")
        self._set("staging", 0.6, "Reinstalling the package…")
        self._pip_install(root)
        text = (root / "lutron_to_dmx_bridge" / "__init__.py").read_text(encoding="utf-8")
        m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
        got = parse_version(m.group(1)) if m else None
        if got is None or got < (parse_version(rel.version) or (0, 0, 0)):
            raise UpdateError(f"After updating, the checkout is at {m.group(1) if m else '?'}, not {rel.version}.")
        self._set("installed", 1.0, f"Updated to {rel.version}. Restart Lutron To DMX Bridge to start using it.")

    @staticmethod
    def _pip_install(root: Path) -> None:
        proc = subprocess.run([sys.executable, "-m", "pip", "install", "-e", str(root)],
                              capture_output=True, text=True, timeout=600)
        if proc.returncode:
            raise UpdateError("`pip install -e .` failed: " + (proc.stderr or proc.stdout).strip()[-300:])

    # -- housekeeping --------------------------------------------------------------------------
    def _cleanup_staging(self) -> None:
        if self._busy:
            return
        shutil.rmtree(data_dir() / "updates" / "staging", ignore_errors=True)

    def _read_result(self) -> None:
        """Pick up the helper's note about the update that led to this start, and report it once."""
        path = data_dir() / RESULT_NAME
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        with contextlib.suppress(OSError):
            path.unlink()
        if not isinstance(result, dict):
            return
        self.last_result = result
        if result.get("ok"):
            log.info("Updated Lutron To DMX Bridge from %s to %s.", result.get("from_version"), result.get("to_version"))
        else:
            log.error("The update to %s did not complete: %s. The previous version (%s) was restored.",
                      result.get("to_version"), result.get("error"), result.get("from_version"))


# ---------------------------------------------------------------------------
# unpacking and testing the new copy
# ---------------------------------------------------------------------------
def app_top_dir(platform: str) -> str:
    return f"{APP_DIR_NAME}.app" if platform == "darwin" else APP_DIR_NAME


def executable_in(staged: Path, platform: str) -> Path:
    if platform == "darwin":
        return staged / "Contents" / "MacOS" / APP_DIR_NAME
    return staged / (APP_DIR_NAME + (".exe" if platform == "win32" else ""))


def relaunch_command(target: Path, platform: str) -> list[str]:
    """How to start the freshly installed app.  --no-browser: the browser tab that asked for the update
    reconnects by itself, so a second tab would only be clutter."""
    if platform == "darwin":
        return ["open", str(target), "--args", "--no-browser"]
    return [str(executable_in(target, platform)), "--no-browser"]


def unpack_release(zip_path: Path, out: Path, platform: str) -> Path:
    """Unzip a release into ``out`` and return the app inside it ("Lutron To DMX Bridge.app" / "Lutron To DMX Bridge")."""
    top = app_top_dir(platform)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
            for n in names:  # "__MACOSX" is where ditto --sequesterRsrc (the macOS build) puts resource forks
                parts = Path(n).parts
                if n.startswith(("/", "\\")) or ".." in parts or (parts and parts[0] not in (top, "__MACOSX")) or ":" in parts[0]:
                    raise UpdateError(f"The download contains an unexpected file ({n!r}); not installing it.")
            if not any(n.startswith(top + "/") for n in names):
                raise UpdateError(f"The download does not contain {top}; not installing it.")
    except zipfile.BadZipFile as exc:
        raise UpdateError("The downloaded file is not a valid zip archive.") from exc
    if platform == "darwin":
        # zipfile would drop the executable bits and symlinks a .app depends on; ditto restores them
        proc = subprocess.run(["ditto", "-x", "-k", str(zip_path), str(out)], capture_output=True, text=True)
        if proc.returncode:
            raise UpdateError("Unpacking failed: " + (proc.stderr or proc.stdout).strip()[:200])
        # unsigned app downloaded by us: make sure Gatekeeper does not treat it as a quarantined download
        subprocess.run(["xattr", "-dr", "com.apple.quarantine", str(out / top)], capture_output=True)
    else:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(out)
    staged = out / top
    exe = executable_in(staged, platform)
    if not exe.is_file():
        raise UpdateError(f"The download is missing {exe.relative_to(out)}; not installing it.")
    if platform != "win32":
        exe.chmod(exe.stat().st_mode | 0o111)
    return staged


def run_self_check(exe: Path, expected_version: str, work: Path, timeout: float = SELF_CHECK_TIMEOUT_S) -> None:
    """Start the unpacked app's own ``--self-check`` (simulator up, GUI fetched, clean stop)."""
    report = work / "self-check.txt"
    report.unlink(missing_ok=True)
    try:
        subprocess.run([str(exe), "--self-check", str(report)], capture_output=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise UpdateError("The new version did not finish its start-up check in time; it was not installed.") from exc
    except OSError as exc:
        raise UpdateError(f"The new version could not be started on this computer ({exc}); it was not installed.") from exc
    text = report.read_text(encoding="utf-8") if report.exists() else ""
    lines = text.splitlines()
    if not lines or f"{APP_DIR_NAME} {expected_version} " not in lines[0] or "PASS" not in lines[-1:]:
        detail = next((ln for ln in lines if ln.startswith("FAIL")), "no report was written" if not text else "")
        raise UpdateError("The new version failed its start-up check, so it was not installed"
                          + (f" ({detail.strip()})." if detail else "."))


# ---------------------------------------------------------------------------
# the helper: runs from the new copy, after the old app has quit
# ---------------------------------------------------------------------------
def spawn_helper(exe: Path, plan: Path, platform: str) -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                    "close_fds": True}
    if platform == "win32":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x8) |
                                   getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200) |
                                   getattr(subprocess, "CREATE_NO_WINDOW", 0x8000000))
    else:
        kwargs["start_new_session"] = True  # survives the old app's exit
    try:
        subprocess.Popen([str(exe), "--apply-update", str(plan)], **kwargs)
    except OSError as exc:
        raise UpdateError(f"Could not start the installer step ({exc}); nothing was changed.") from exc


def pid_exited(pid: int, timeout: float) -> bool:
    """Wait up to ``timeout`` seconds for process ``pid`` to end."""
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            return kernel32.GetLastError() != 5  # 5 = access denied, i.e. it exists
        try:
            return kernel32.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)


def _copy_tree(src: Path, dst: Path, platform: str) -> None:
    if platform == "darwin" and shutil.which("ditto"):
        proc = subprocess.run(["ditto", str(src), str(dst)], capture_output=True, text=True)
        if proc.returncode:
            raise OSError(proc.stderr.strip() or "ditto failed")
        return
    shutil.copytree(src, dst, symlinks=True)


def _move_with_retries(src: Path, dst: Path, attempts: int = 20, delay: float = 1.0) -> None:
    """Moving a folder can fail for a moment on Windows while antivirus or the indexer holds a file."""
    for i in range(attempts):
        try:
            shutil.move(str(src), str(dst))
            return
        except OSError:
            if i == attempts - 1:
                raise
            time.sleep(delay)


def swap_in(plan: dict) -> None:
    """Replace ``target`` by ``staged``, keeping the old one at ``backup``.  Restores the old one on failure."""
    target, staged, backup = Path(plan["target"]), Path(plan["staged"]), Path(plan["backup"])
    platform = plan.get("platform", sys.platform)
    if not executable_in(staged, platform).is_file():
        raise OSError("the new version is missing from the staging folder")
    previous_root = backup.parent.parent
    shutil.rmtree(backup.parent, ignore_errors=True)
    backup.parent.mkdir(parents=True)
    _move_with_retries(target, backup)
    try:
        _copy_tree(staged, target, platform)
        if not executable_in(target, platform).is_file():
            raise OSError("the copied app is incomplete")
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        _move_with_retries(backup, target)
        raise
    # only one older version is kept
    for old in previous_root.iterdir():
        if old != backup.parent:
            shutil.rmtree(old, ignore_errors=True)


def run_helper(plan_path: str, launch: Callable[[list[str]], None] | None = None) -> int:
    """``--apply-update PLAN``: finish an update after the old app has quit.  Returns an exit code."""
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    log_file = Path(plan.get("log", ""))
    handler = None
    if str(log_file):
        with contextlib.suppress(OSError):
            log_file.parent.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(log_file, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            logging.getLogger().addHandler(handler)
            logging.getLogger().setLevel(logging.INFO)
    launch = launch or _launch
    result = {"ok": False, "from_version": plan["from_version"], "to_version": plan["to_version"],
              "at": _utcnow(), "error": ""}
    relaunch_old = False
    try:
        log.info("Update helper: waiting for process %s to exit", plan["pid"])
        if not pid_exited(plan["pid"], float(plan.get("wait_s", HELPER_WAIT_S))):
            result["error"] = "the old version did not close in time, so nothing was changed"
            return 1
        relaunch_old = True
        log.info("Update helper: replacing %s", plan["target"])
        swap_in(plan)
        result["ok"] = True
        log.info("Update helper: installed %s", plan["to_version"])
        return 0
    except BaseException as exc:
        result["error"] = str(exc) or type(exc).__name__
        log.exception("Update helper failed")
        return 1
    finally:
        with contextlib.suppress(OSError):
            Path(plan["result"]).parent.mkdir(parents=True, exist_ok=True)
            Path(plan["result"]).write_text(json.dumps(result), encoding="utf-8")
        if relaunch_old:  # on success this is the new app, on failure the restored old one
            try:
                launch(plan["relaunch"])
            except Exception:
                log.exception("Update helper: could not start the app again")
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


def _launch(argv: list[str]) -> None:
    kwargs: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(argv, **kwargs)

