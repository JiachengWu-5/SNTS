"""Restart the local writing service without touching the writing or image models."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("novel_writer.serverctl")

ROOT = Path(__file__).resolve().parent.parent
RELOAD_BAT = ROOT / "reload-server.bat"


def schedule_reload() -> None:
    """Start a detached helper that restarts uvicorn after this request finishes."""
    if sys.platform != "win32":
        raise RuntimeError("当前系统不支持从页面重新加载")
    if not RELOAD_BAT.is_file():
        raise FileNotFoundError("找不到重新加载脚本")
    flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NO_WINDOW
    )
    subprocess.Popen(
        ["cmd.exe", "/c", str(RELOAD_BAT)],
        cwd=str(ROOT),
        env=os.environ.copy(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    logger.info("scheduled writing service reload")
