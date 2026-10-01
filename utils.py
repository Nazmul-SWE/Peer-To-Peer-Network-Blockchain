"""
utils.py - small, dependency-free helpers shared by the node and the GUI.

Input validation (name / IP / port), filename safety, human-readable sizes,
LAN-address discovery and "open this folder" support live here so that the
networking code and the GUI stay focused on their own jobs.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import sys
import time
from typing import Optional

from protocol import MAX_NAME_LENGTH


class ValidationError(ValueError):
    """User-supplied input is not acceptable (message is user-friendly)."""


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


def validate_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise ValidationError("Peer name cannot be empty.")
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(f"Peer name must be at most {MAX_NAME_LENGTH} characters.")
    if any(ord(ch) < 32 for ch in name):
        raise ValidationError("Peer name contains invalid characters.")
    return name


def validate_port(raw: str, minimum: int = 1) -> int:
    text = (raw or "").strip()
    if not re.fullmatch(r"[0-9]{1,5}", text):
        raise ValidationError("Port must be a whole number between 1 and 65535.")
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValidationError("Port must be between 1 and 65535.")
    if port < minimum:
        raise ValidationError(f"Use a port between {minimum} and 65535 (lower ports are reserved).")
    return port


def validate_host(raw: str) -> str:
    """Accept a dotted IPv4 address, 'localhost' or a plain hostname."""
    host = (raw or "").strip()
    if not host:
        raise ValidationError("IP address cannot be empty.")
    if re.fullmatch(r"[0-9.]+", host):          # looks numeric -> must be a real IPv4
        try:
            address = ipaddress.IPv4Address(host)
        except ValueError:
            raise ValidationError(f"'{host}' is not a valid IPv4 address.") from None
        if address.is_unspecified:
            raise ValidationError("0.0.0.0 cannot be used as a destination address.")
        return host
    if not _HOSTNAME_RE.match(host):
        raise ValidationError(f"'{host}' is not a valid IP address or hostname.")
    return host


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)}


def sanitize_filename(name: str) -> str:
    """
    Make a filename received from the network safe to write to disk:
    no directories (blocks '../../evil'), no illegal characters, no Windows
    reserved names, bounded length.
    """
    name = str(name).replace("\\", "/").split("/")[-1]
    name = _BAD_CHARS.sub("_", name).strip().strip(".").strip()
    if not name:
        name = "received_file"
    stem, ext = os.path.splitext(name)
    if stem.upper() in _RESERVED:
        name = "_" + name
    if len(name) > 150:
        name = stem[: 150 - len(ext)] + ext
    return name


def unique_path(directory: str, filename: str) -> str:
    """Return a path inside ``directory`` that does not exist yet
    ('photo.jpg' -> 'photo (1).jpg' ...), so received files never overwrite."""
    stem, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    counter = 1
    while os.path.exists(candidate) or os.path.exists(candidate + ".part"):
        candidate = os.path.join(directory, f"{stem} ({counter}){ext}")
        counter += 1
    return candidate


def format_size(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_time(timestamp: Optional[float] = None) -> str:
    return time.strftime("%H:%M", time.localtime(timestamp))


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------
def get_local_ip() -> str:
    """Best-effort LAN address of this machine (no packet is actually sent)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 1))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def open_folder(path: str, select_file: bool = False) -> bool:
    """Open a folder (or reveal a file) in the OS file manager."""
    try:
        if sys.platform.startswith("win"):
            if select_file and os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(os.path.dirname(path) if os.path.isfile(path) else path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path] if select_file and os.path.isfile(path) else ["open", path])
        else:
            target = os.path.dirname(path) if os.path.isfile(path) else path
            subprocess.Popen(["xdg-open", target])
        return True
    except (OSError, ValueError):
        return False
