"""Desktop-notification adapter (a swappable subsystem, per the independence rule).

The bot's source of truth is always the JSON — toasts are a best-effort *overlay*, so every
notifier here is defensive: it never raises, it just returns whether the toast fired. Swap in a
different mechanism (email, Slack, a Linux notifier) by implementing `Notifier.notify`.

`WindowsToastNotifier` needs no dependency install: it prefers the BurntToast module if the
user happens to have it, otherwise falls back to the built-in WinRT toast manager via
PowerShell. Title/body are passed as environment variables, never string-interpolated into the
script, so a manager's reply text can't break or inject into the command.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    def notify(self, title: str, body: str) -> bool:
        """Show a desktop notification. Returns True if it fired; never raises."""
        ...


class NullNotifier:
    """No-op notifier — used off-Windows or when toasts are disabled. The JSON still carries
    every message, so nothing is lost."""

    def notify(self, title: str, body: str) -> bool:  # noqa: D401
        return False


# One PowerShell script: try BurntToast, fall back to the built-in WinRT toast. Reads the text
# from env vars so nothing is interpolated into the command line.
_PS_TOAST = r"""
$ErrorActionPreference = 'Stop'
$title = $env:DAMIA_TOAST_TITLE
$body  = $env:DAMIA_TOAST_BODY
try {
    if (Get-Module -ListAvailable -Name BurntToast) {
        Import-Module BurntToast -ErrorAction Stop
        New-BurntToastNotification -Text $title, $body | Out-Null
        exit 0
    }
} catch { }
# Fallback: built-in WinRT toast manager, hosted under the PowerShell AUMID.
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$tmpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $tmpl.GetElementsByTagName('text')
$texts.Item(0).AppendChild($tmpl.CreateTextNode($title)) | Out-Null
$texts.Item(1).AppendChild($tmpl.CreateTextNode($body)) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($tmpl)
$aumid = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($aumid).Show($toast)
"""


class WindowsToastNotifier:
    """Best-effort Windows toast via PowerShell. Never raises; returns whether it fired."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout

    @staticmethod
    def available() -> bool:
        return sys.platform == "win32"

    def notify(self, title: str, body: str) -> bool:
        if not self.available():
            return False
        env = dict(os.environ, DAMIA_TOAST_TITLE=title, DAMIA_TOAST_BODY=body)
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_TOAST],
                env=env, capture_output=True, timeout=self.timeout,
            )
            return proc.returncode == 0
        except Exception:
            return False


def make_notifier(enabled: bool = True) -> Notifier:
    """Pick a notifier: a Windows toaster when enabled on Windows, else the no-op."""
    if enabled and WindowsToastNotifier.available():
        return WindowsToastNotifier()
    return NullNotifier()
