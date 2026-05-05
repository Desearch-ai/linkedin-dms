#!/usr/bin/env python3
"""Deterministic local preflight for the LinkedIn DMs Chrome extension.

The script verifies the local API, optionally launches Chrome with the unpacked
extension source, checks CDP for the expected extension target, and opens
LinkedIn messaging so the extension can capture the current browser contract.
It intentionally prints only operational status and never dumps cookies,
headers, authorization values, or response bodies.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlparse
from urllib.request import Request, urlopen

EXTENSION_NAME = "Desearch LinkedIn DMs Bridge"
DEFAULT_BACKEND_URL = "http://127.0.0.1:8899"
EXTENSION_DEFAULT_BACKEND_URL = "http://localhost:8899"
DEFAULT_CDP_HOST = "127.0.0.1"
DEFAULT_CDP_PORT = 18800
LINKEDIN_MESSAGING_URL = "https://www.linkedin.com/messaging/"
SENSITIVE_TERMS = (
    "li_at",
    "JSESSIONID",
    "csrf-token",
    "csrf_token",
    "Authorization",
    "Bearer",
    "cookie",
    "token",
)


class PreflightError(Exception):
    """Expected preflight failure with an operator-safe message."""


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def default_extension_path() -> Path:
    return repo_root() / "chrome-extension"


def default_profile_dir() -> Path:
    return Path.home() / ".desearch-linkedin-dms" / "chrome-profile"


def sanitize_for_output(value: Any) -> str:
    """Return a conservative, single-line status string safe for operator logs."""
    text = str(value).replace("\n", " ").replace("\r", " ")
    if any(term.lower() in text.lower() for term in SENSITIVE_TERMS):
        return "redacted sensitive details"
    return text[:240]


def normalize_url(url: str) -> str:
    trimmed = url.strip().rstrip("/")
    parsed = urlparse(trimmed)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PreflightError(f"Invalid backend URL: {sanitize_for_output(url)}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PreflightError("Backend URL must be a plain local API base URL without credentials, query, or fragment.")
    return trimmed


def fetch_json(url: str, timeout_s: float = 5.0) -> Any:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - local operator URL/CDP only.
        raw = response.read(1024 * 1024)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def request_json(url: str, method: str = "GET", timeout_s: float = 5.0) -> Any:
    request = Request(url, method=method, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout_s) as response:  # noqa: S310 - local CDP endpoint.
        raw = response.read(1024 * 1024)
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def health_url(backend_url: str) -> str:
    return urljoin(f"{backend_url}/", "health")


def check_backend_health(backend_url: str, timeout_s: float) -> None:
    url = health_url(backend_url)
    try:
        data = fetch_json(url, timeout_s=timeout_s)
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        raise PreflightError(
            f"Backend health failed for {url}. Start the API with: "
            "uvicorn apps.api.main:app --host 127.0.0.1 --port 8899 "
            f"({sanitize_for_output(exc)})"
        ) from exc

    if not isinstance(data, dict) or data.get("ok") is not True:
        raise PreflightError(
            f"Backend health failed for {url}: expected JSON {{'ok': true}}; "
            "received non-OK health response."
        )


def cdp_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}"


def check_cdp_reachable(host: str, port: int, timeout_s: float) -> dict[str, Any]:
    url = f"{cdp_base_url(host, port)}/json/version"
    try:
        data = fetch_json(url, timeout_s=timeout_s)
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        raise PreflightError(
            f"Chrome CDP is not reachable on {host}:{port}. Run with --launch "
            "or start Chrome with --remote-debugging-port set. "
            f"({sanitize_for_output(exc)})"
        ) from exc
    if not isinstance(data, dict):
        raise PreflightError(f"Chrome CDP returned an invalid /json/version response on {host}:{port}.")
    return data


def extension_id_for_path(extension_path: Path) -> str:
    """Return Chromium's deterministic unpacked-extension id for a path.

    Chromium maps the first 16 SHA-256 bytes of the absolute extension path to
    letters a-p. This is the same id Chrome uses for unpacked extensions that do
    not declare a manifest `key`, letting the preflight verify the specific local
    source path instead of accepting any background.js service worker.
    """
    digest = hashlib.sha256(str(extension_path.resolve()).encode("utf-8")).digest()[:16]
    return "".join(chr(ord("a") + nibble) for byte in digest for nibble in (byte >> 4, byte & 0x0F))


def load_manifest_name(extension_path: Path) -> str:
    manifest_path = extension_path / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"Cannot read extension manifest at {manifest_path}: {sanitize_for_output(exc)}") from exc
    name = manifest.get("name")
    if name != EXTENSION_NAME:
        raise PreflightError(
            f"Extension manifest name mismatch at {manifest_path}: expected {EXTENSION_NAME!r}."
        )
    return name


def list_cdp_targets(host: str, port: int, timeout_s: float) -> list[dict[str, Any]]:
    url = f"{cdp_base_url(host, port)}/json/list"
    try:
        data = fetch_json(url, timeout_s=timeout_s)
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"Could not list Chrome CDP targets on {host}:{port}: {sanitize_for_output(exc)}") from exc
    if not isinstance(data, list):
        raise PreflightError(f"Chrome CDP /json/list returned an invalid target list on {host}:{port}.")
    return [target for target in data if isinstance(target, dict)]


def target_matches_extension(target: dict[str, Any], extension_id: str) -> bool:
    url = str(target.get("url") or "")
    return url.startswith(f"chrome-extension://{extension_id}/")


def find_extension_target(targets: list[dict[str, Any]], extension_id: str) -> dict[str, Any] | None:
    for target in targets:
        if target_matches_extension(target, extension_id):
            return target
    return None


def open_cdp_url(host: str, port: int, target_url: str, timeout_s: float = 5.0) -> Any:
    encoded = quote(target_url, safe=":/?=&%#")
    endpoint = f"{cdp_base_url(host, port)}/json/new?{encoded}"
    try:
        return request_json(endpoint, method="PUT", timeout_s=timeout_s)
    except HTTPError as exc:
        if exc.code not in {404, 405}:
            raise
        return request_json(endpoint, method="GET", timeout_s=timeout_s)


def wake_extension_target(host: str, port: int, extension_id: str, timeout_s: float) -> None:
    try:
        open_cdp_url(host, port, f"chrome-extension://{extension_id}/popup.html", timeout_s=timeout_s)
    except (HTTPError, URLError, OSError, json.JSONDecodeError):
        # Best effort only. The subsequent target check produces the actionable error.
        return


def verify_extension_loaded(
    host: str,
    port: int,
    extension_path: Path,
    extension_id: str | None,
    timeout_s: float,
    wake: bool = True,
) -> tuple[str, dict[str, Any]]:
    load_manifest_name(extension_path)
    expected_id = extension_id or extension_id_for_path(extension_path)

    target = find_extension_target(list_cdp_targets(host, port, timeout_s), expected_id)
    if not target and wake:
        wake_extension_target(host, port, expected_id, timeout_s)
        target = find_extension_target(list_cdp_targets(host, port, timeout_s), expected_id)

    if not target:
        raise PreflightError(
            f"{EXTENSION_NAME} extension is not loaded from {extension_path.resolve()} in Chrome CDP {host}:{port}. "
            "Launch Chrome with --load-extension pointing at that directory, or rerun this preflight with --launch. "
            f"Expected extension id: {expected_id}."
        )
    return expected_id, target


def find_chrome_binary(explicit: str | None) -> str:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.exists():
            raise PreflightError(f"Chrome binary not found: {path}")
        return str(path)

    candidates: list[str] = []
    if platform.system() == "Darwin":
        candidates.extend(
            [
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                "/Applications/Chromium.app/Contents/MacOS/Chromium",
            ]
        )
    candidates.extend(["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"])

    for candidate in candidates:
        if os.path.isabs(candidate):
            if Path(candidate).exists():
                return candidate
        else:
            from shutil import which

            resolved = which(candidate)
            if resolved:
                return resolved
    raise PreflightError("Chrome binary not found. Pass --chrome-binary explicitly.")


def launch_chrome(
    chrome_binary: str | None,
    host: str,
    port: int,
    profile_dir: Path,
    extension_path: Path,
    open_messaging: bool,
) -> subprocess.Popen[Any]:
    binary = find_chrome_binary(chrome_binary)
    profile_dir.mkdir(parents=True, exist_ok=True)
    extension_arg = str(extension_path.resolve())
    args = [
        binary,
        f"--remote-debugging-address={host}",
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir.expanduser().resolve()}",
        f"--disable-extensions-except={extension_arg}",
        f"--load-extension={extension_arg}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if open_messaging:
        args.append(LINKEDIN_MESSAGING_URL)
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603


def ensure_chrome(
    launch: bool,
    chrome_binary: str | None,
    host: str,
    port: int,
    profile_dir: Path,
    extension_path: Path,
    timeout_s: float,
    open_messaging: bool,
) -> dict[str, Any]:
    try:
        return check_cdp_reachable(host, port, timeout_s=min(timeout_s, 2.0))
    except PreflightError:
        if not launch:
            raise

    launch_chrome(chrome_binary, host, port, profile_dir, extension_path, open_messaging)
    deadline = time.monotonic() + max(timeout_s, 1.0)
    last_error: PreflightError | None = None
    while time.monotonic() < deadline:
        try:
            return check_cdp_reachable(host, port, timeout_s=1.0)
        except PreflightError as exc:
            last_error = exc
            time.sleep(0.25)
    raise PreflightError(
        f"Launched Chrome but CDP did not become reachable on {host}:{port}. "
        f"Last status: {sanitize_for_output(last_error or 'unknown')}"
    )


def maybe_open_messaging(host: str, port: int, timeout_s: float) -> None:
    try:
        open_cdp_url(host, port, LINKEDIN_MESSAGING_URL, timeout_s=timeout_s)
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        raise PreflightError(
            f"Could not open {LINKEDIN_MESSAGING_URL} through CDP on {host}:{port}: {sanitize_for_output(exc)}"
        ) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight/launch a local Chrome profile with the unpacked "
            "Desearch LinkedIn DMs Bridge extension loaded."
        ),
        epilog=(
            "Typical use: uv run python scripts/extension_preflight.py --launch. "
            f"The run checks backend /health, verifies the extension target through CDP, "
            f"and opens {LINKEDIN_MESSAGING_URL} for messaging contract capture. "
            "No LinkedIn session secrets or local API auth values are printed."
        ),
    )
    parser.add_argument("--backend-url", default=DEFAULT_BACKEND_URL, help="Local API base URL to health-check (default: %(default)s).")
    parser.add_argument("--cdp-host", default=DEFAULT_CDP_HOST, help="Chrome DevTools Protocol host (default: %(default)s).")
    parser.add_argument("--cdp-port", type=int, default=DEFAULT_CDP_PORT, help="Chrome DevTools Protocol port (default: %(default)s).")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=default_profile_dir(),
        help="Chrome user-data-dir for --launch (default: %(default)s). Use a signed-in profile for live validation.",
    )
    parser.add_argument(
        "--extension-path",
        type=Path,
        default=default_extension_path(),
        help="Unpacked extension source directory to load/verify (default: %(default)s).",
    )
    parser.add_argument("--extension-id", help="Override the deterministic unpacked extension id when verifying an already-loaded profile.")
    parser.add_argument("--chrome-binary", help="Chrome/Chromium executable path for --launch.")
    parser.add_argument("--launch", action="store_true", help="Launch Chrome if CDP is not already reachable on the requested host/port.")
    parser.add_argument("--timeout", type=float, default=15.0, help="Seconds to wait for local backend/CDP operations (default: %(default)s).")
    parser.add_argument(
        "--no-open-messaging",
        action="store_true",
        help=f"Do not open {LINKEDIN_MESSAGING_URL} after verifying the extension.",
    )
    parser.add_argument(
        "--no-extension-wake",
        action="store_true",
        help="Do not open the extension popup page to wake a dormant MV3 service worker before target verification.",
    )
    return parser


def print_status(message: str) -> None:
    print(message, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        backend_url = normalize_url(args.backend_url)
        extension_path = args.extension_path.expanduser().resolve()
        profile_dir = args.profile_dir.expanduser()
        open_messaging = not args.no_open_messaging

        check_backend_health(backend_url, timeout_s=args.timeout)
        print_status(f"OK Backend health OK: {health_url(backend_url)}")

        browser_info = ensure_chrome(
            launch=args.launch,
            chrome_binary=args.chrome_binary,
            host=args.cdp_host,
            port=args.cdp_port,
            profile_dir=profile_dir,
            extension_path=extension_path,
            timeout_s=args.timeout,
            open_messaging=open_messaging,
        )
        browser_name = sanitize_for_output(browser_info.get("Browser", "Chrome"))
        print_status(f"OK Chrome CDP reachable: {args.cdp_host}:{args.cdp_port} ({browser_name})")

        extension_id, target = verify_extension_loaded(
            host=args.cdp_host,
            port=args.cdp_port,
            extension_path=extension_path,
            extension_id=args.extension_id,
            timeout_s=args.timeout,
            wake=not args.no_extension_wake,
        )
        target_type = sanitize_for_output(target.get("type", "target"))
        print_status(f"OK Extension loaded: {EXTENSION_NAME} ({target_type}, id={extension_id})")

        if backend_url in {DEFAULT_BACKEND_URL, EXTENSION_DEFAULT_BACKEND_URL}:
            print_status(f"OK Extension backend default matches local API: {EXTENSION_DEFAULT_BACKEND_URL}")
        else:
            print_status(
                "ACTION Custom backend URL checked. Set the same service URL in the extension popup before Sync Now."
            )

        if open_messaging:
            maybe_open_messaging(args.cdp_host, args.cdp_port, timeout_s=args.timeout)
            print_status(f"OK Opened LinkedIn messaging for contract capture: {LINKEDIN_MESSAGING_URL}")
        else:
            print_status(f"ACTION Open LinkedIn messaging before Sync Now: {LINKEDIN_MESSAGING_URL}")

        print_status("OK Preflight complete. If signed in, use the extension popup Refresh/Sync Now flow next.")
        return 0
    except PreflightError as exc:
        print_status(f"FAIL {sanitize_for_output(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
