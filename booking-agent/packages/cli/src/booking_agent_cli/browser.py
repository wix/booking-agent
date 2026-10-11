"""Browser detection and launch configuration for redirect checkout."""
from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_MACOS_CHROME_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)

_SYSTEM_CHROME_NAMES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium-browser",
    "chromium",
)


@dataclass(frozen=True)
class BrowserLaunchSpec:
    """Playwright launch parameters resolved during prereq check."""

    channel: str  # playwright channel name or "chromium"
    source: str  # "system" | "playwright"
    executable_path: Optional[str] = None

    def playwright_launch_kwargs(self, *, headless: bool = True) -> dict:
        kwargs: dict = {"headless": headless}
        if self.channel == "chromium":
            return kwargs
        if self.channel == "chrome":
            # Prefer Playwright's Chrome channel over a raw executable_path;
            # headless integration is more reliable through the channel binding.
            kwargs["channel"] = "chrome"
            return kwargs
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
            return kwargs
        which = shutil.which(self.channel)
        if which:
            kwargs["executable_path"] = which
        else:
            kwargs["channel"] = "chrome"
        return kwargs


def find_system_chrome() -> Optional[BrowserLaunchSpec]:
    """Return launch spec for the first system Chrome/Chromium found."""
    for path in _MACOS_CHROME_PATHS:
        if Path(path).is_file():
            return BrowserLaunchSpec(
                channel="chrome",
                source="system",
                executable_path=path,
            )
    for name in _SYSTEM_CHROME_NAMES:
        if shutil.which(name):
            return BrowserLaunchSpec(channel=name, source="system")
    return None


def ensure_playwright_chromium() -> None:
    """Install Playwright bundled Chromium (idempotent)."""
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "playwright install chromium failed")


def resolve_browser_for_checkout() -> BrowserLaunchSpec:
    """Resolve browser: system Chrome first, else Playwright chromium."""
    system = find_system_chrome()
    if system:
        return system
    ensure_playwright_chromium()
    return BrowserLaunchSpec(channel="chromium", source="playwright")


def launch_spec_from_session(
    browser_channel: Optional[str],
    store_config: Optional[BrowserLaunchSpec],
) -> BrowserLaunchSpec:
    """Build launch spec from session channel or store-level config."""
    if store_config:
        return store_config
    if browser_channel == "chromium":
        return BrowserLaunchSpec(channel="chromium", source="playwright")
    if browser_channel:
        for path in _MACOS_CHROME_PATHS:
            if Path(path).is_file() and browser_channel in ("chrome", path):
                return BrowserLaunchSpec(
                    channel="chrome", source="system", executable_path=path
                )
        if shutil.which(browser_channel):
            return BrowserLaunchSpec(channel=browser_channel, source="system")
        return BrowserLaunchSpec(channel=browser_channel, source="system")
    raise ValueError("browser_not_ready")
