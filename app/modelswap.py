"""Swap the GPU-resident writing model and ComfyUI Qwen Image Edit 2511, then generate.

The writing web service stays up; only llama-server / ComfyUI are started or stopped.
Callers must not invoke start/stop during ordinary unit tests without mocks.
Paths to llama-server, the GGUF model, and log files come from environment
variables (see README); this module does not hard-code a personal machine path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

from app.books import BookError
from app.config import (
    CONTEXT_TOKENS,
    DEFAULT_COMFY_PYTHON,
    DEFAULT_COMFY_ROOT,
    DEFAULT_COMFY_URL,
    DEFAULT_COMFY_WORKFLOW,
    DEFAULT_LLM_URL,
    QWEN21_CLIP,
    QWEN21_UNET,
    QWEN21_VAE,
)

logger = logging.getLogger("novel_writer.modelswap")

DEFAULT_NEGATIVE = "模糊，低质量，水印，变形，文字乱码"
DEFAULT_STEPS = 25
DEFAULT_CFG = 1.0
DEFAULT_SAMPLER = "euler"
DEFAULT_SCHEDULER = "simple"
DEFAULT_SIZE = 1024
QWEN2511_UNET = os.environ.get("NOVEL_WRITER_QWEN2511_UNET", "qwen_image_edit_2511_int8_convrot.safetensors")
QWEN2511_CLIP = os.environ.get("NOVEL_WRITER_QWEN2511_CLIP", "qwen_2.5_vl_7b_fp8_scaled.safetensors")
QWEN2511_VAE = os.environ.get("NOVEL_WRITER_QWEN2511_VAE", "qwen_image_vae.safetensors")
QWEN2511_LORA = os.environ.get("NOVEL_WRITER_QWEN2511_LORA", "Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors")
QWEN2511_STEPS = int(os.environ.get("NOVEL_WRITER_QWEN2511_STEPS", "4"))
QWEN2511_CFG = float(os.environ.get("NOVEL_WRITER_QWEN2511_CFG", "1.0"))
QWEN2511_SHIFT = float(os.environ.get("NOVEL_WRITER_QWEN2511_SHIFT", "3.1"))
QWEN2511_LORA_STRENGTH = float(os.environ.get("NOVEL_WRITER_QWEN2511_LORA_STRENGTH", "1.0"))

IMG2IMG_NODE_TYPES = frozenset({
    "LoadImage",
    "LoadImageMask",
    "ImageToImage",
    "QwenImage21Edit",
})
TXT_PROMPT_NODE_TYPES = frozenset({
    "TextEncodeQwenImage21",
    "TextEncodeQwenImageEdit",
    "TextEncodeQwenImageEditPlus",
    "CLIPTextEncode",
    "CLIPTextEncodeSDXL",
    "CLIPTextEncodeFlux",
})
LATENT_NODE_TYPES = frozenset({
    "EmptyLatentImage",
    "EmptySD3LatentImage",
    "EmptyFluxLatentImage",
    "TextEncodeQwenImage21",
})


class SwapState:
    def __init__(self) -> None:
        self.session_active = False
        self.comfy_pid: int | None = None
        self.llama_stopped_by_us = False
        self.comfy_started_by_us = False
        self.book: str | None = None
        self.progress: dict[str, Any] = {
            "phase": "idle",
            "label": "",
            "percent": None,
            "current": 0,
            "total": 0,
        }


state = SwapState()


def set_progress(
    phase: str,
    label: str = "",
    *,
    percent: int | None = None,
    current: int = 0,
    total: int = 0,
) -> None:
    state.progress = {
        "phase": phase,
        "label": label,
        "percent": percent,
        "current": current,
        "total": total,
    }


def clear_progress() -> None:
    set_progress("idle", "")


def comfy_url() -> str:
    return (os.environ.get("NOVEL_WRITER_COMFY_URL") or DEFAULT_COMFY_URL).rstrip("/")


def comfy_root() -> Path:
    return Path(os.environ.get("NOVEL_WRITER_COMFY_ROOT") or DEFAULT_COMFY_ROOT)


def llm_base(url: str | None = None) -> str:
    return (url or os.environ.get("NOVEL_WRITER_LLM_URL") or DEFAULT_LLM_URL).rstrip("/")


def llama_host_port(url: str | None = None) -> tuple[str, int]:
    parsed = urlparse(llm_base(url) if "://" in llm_base(url) else "http://" + llm_base(url))
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 8080
    return host, port


def comfy_host_port() -> tuple[str, int]:
    parsed = urlparse(comfy_url())
    return parsed.hostname or "127.0.0.1", parsed.port or 8188


def port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def llama_is_up(llm_url: str | None = None) -> bool:
    host, port = llama_host_port(llm_url)
    return port_open(host, port)


def comfy_is_up() -> bool:
    host, port = comfy_host_port()
    return port_open(host, port)


def session_status(llm_url: str | None = None) -> dict:
    writing = llama_is_up(llm_url)
    comfy = comfy_is_up()
    # If the writing service was restarted mid-session, recover the flag
    # from the still-running image service.
    if comfy and not writing:
        state.session_active = True
    return {
        "writing_model": writing,
        "comfyui": comfy,
        "illustration_session": bool(state.session_active),
        "book": state.book,
        "progress": dict(state.progress),
    }


def resolve_llama_restart_script() -> Path:
    override = (os.environ.get("NOVEL_WRITER_LLAMA_RESTART") or DEFAULT_LLAMA_RESTART or "").strip()
    if override:
        path = Path(override)
        if not path.exists():
            raise BookError(f"未找到写作模型启动脚本：{path}")
        return path
    raise BookError(
        "未找到写作模型启动脚本。请设置环境变量 NOVEL_WRITER_LLAMA_RESTART，"
        "指向本机用于拉起 llama-server 的脚本。"
    )


def resolve_comfy_python(root: Path | None = None) -> str:
    override = (os.environ.get("NOVEL_WRITER_COMFY_PYTHON") or DEFAULT_COMFY_PYTHON or "").strip()
    if override:
        return override
    base = root or comfy_root()
    candidates = [
        base / "venv" / "Scripts" / "python.exe",
        base / ".venv" / "Scripts" / "python.exe",
        base / "venv" / "bin" / "python",
        base / "python_embeded" / "python.exe",
        base / "python_embedded" / "python.exe",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return "python"


def pid_on_port(host: str, port: int) -> int | None:
    """Best-effort PID lookup for a listening TCP port (Windows first, then Linux)."""
    if sys.platform.startswith("win"):
        try:
            completed = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        suffix = f":{port}"
        for line in (completed.stdout or "").splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            if "LISTENING" not in line.upper() and "LISTEN" not in line.upper():
                continue
            local = parts[1] if len(parts) > 1 else ""
            if local.endswith(suffix) or local.endswith(f"]:{port}"):
                try:
                    return int(parts[-1])
                except ValueError:
                    return None
        return None
    # Linux (tests / the shared box). Not used to kill anything in this task.
    try:
        completed = subprocess.run(
            ["ss", "-ltnp", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        for line in (completed.stdout or "").splitlines():
            if "pid=" in line:
                try:
                    return int(line.split("pid=")[1].split(",")[0])
                except (IndexError, ValueError):
                    continue
    except (OSError, subprocess.TimeoutExpired):
        return None
    return None


def process_name(pid: int) -> str:
    if pid <= 0:
        return ""
    if sys.platform.startswith("win"):
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            line = (completed.stdout or "").strip().splitlines()
            if line:
                name = line[0].split(",")[0].strip().strip('"')
                return name
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return ""
    try:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        return (completed.stdout or "").strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def stop_pid(pid: int, *, force: bool = False) -> None:
    if pid <= 0:
        return
    if sys.platform.startswith("win"):
        args = ["taskkill", "/PID", str(pid)]
        if force:
            args.append("/F")
        subprocess.run(args, capture_output=True, text=True, timeout=15, check=False)
        return
    sig = 9 if force else 15
    try:
        os.kill(pid, sig)
    except OSError:
        pass


async def stop_llama_server(llm_url: str | None = None) -> bool:
    """Stop the llama-server that serves the writing-model port. Windows: taskkill that exe."""
    if not llama_is_up(llm_url):
        return False
    host, port = llama_host_port(llm_url)
    pid = pid_on_port(host, port)
    if not pid:
        raise BookError("找不到占用写作模型端口的进程，无法卸载写作模型")
    name = process_name(pid).lower()
    if name and "llama" not in name and "server" not in name:
        raise BookError(f"端口 {port} 上的进程不是 llama-server（{name}），已中止卸载，以免误关其他程序")
    logger.info("stopping llama-server pid=%s name=%s port=%s", pid, name, port)
    set_progress("unloading", "正在卸载写作模型，显存释放可能需要一两分钟…", percent=8)
    stop_pid(pid, force=False)
    deadline = time.time() + 45
    while time.time() < deadline:
        if not llama_is_up(llm_url):
            state.llama_stopped_by_us = True
            return True
        await asyncio.sleep(0.5)
    set_progress("unloading", "正在强制卸载写作模型…", percent=12)
    stop_pid(pid, force=True)
    deadline = time.time() + 45
    while time.time() < deadline:
        if not llama_is_up(llm_url):
            state.llama_stopped_by_us = True
            return True
        await asyncio.sleep(0.5)
    raise BookError("未能卸载写作模型，请稍后重试")


def llama_exe_path() -> Path:
    raw = (os.environ.get("NOVEL_WRITER_LLAMA_EXE") or "").strip()
    if not raw:
        raise BookError(
            "未设置写作模型程序路径。请设置环境变量 NOVEL_WRITER_LLAMA_EXE，"
            "指向本机的 llama-server 可执行文件。"
        )
    return Path(raw)


def llama_model_path() -> Path:
    raw = (os.environ.get("NOVEL_WRITER_LLAMA_MODEL") or "").strip()
    if not raw:
        raise BookError(
            "未设置写作模型文件路径。请设置环境变量 NOVEL_WRITER_LLAMA_MODEL，"
            "指向本机的 GGUF 模型文件。"
        )
    return Path(raw)


def llama_context_tokens() -> int:
    raw = os.environ.get("NOVEL_WRITER_CONTEXT_TOKENS") or str(CONTEXT_TOKENS or 65536)
    try:
        value = int(raw)
    except ValueError:
        value = 65536
    return value if value > 0 else 65536


def modelswap_log_dir() -> Path:
    override = (os.environ.get("NOVEL_WRITER_LOG_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "shanydoc"


def start_llama_server(llm_url: str | None = None) -> subprocess.Popen | None:
    """Start llama-server directly via NOVEL_WRITER_LLAMA_EXE and NOVEL_WRITER_LLAMA_MODEL.

    Do not use a helper script that also restarts the writing web service, which
    can drop the configured article root.
    """
    if llama_is_up(llm_url):
        return None
    exe = llama_exe_path()
    model = llama_model_path()
    if not exe.exists():
        raise BookError(f"未找到写作模型程序：{exe}")
    if not model.exists():
        raise BookError(f"未找到写作模型文件：{model}")
    host, port = llama_host_port(llm_url)
    ctx = llama_context_tokens()
    args = [
        str(exe),
        "-m",
        str(model),
        "-c",
        str(ctx),
        "-ngl",
        "999",
        "--parallel",
        "1",
        "--host",
        host,
        "--port",
        str(port),
        "--jinja",
    ]
    logger.info("starting writing model: %s", " ".join(args))
    log_dir = modelswap_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        out_f = open(log_dir / "llama_server.log", "a", encoding="utf-8")
        err_f = open(log_dir / "llama_server.err.log", "a", encoding="utf-8")
        out_f.write(f"\n--- modelswap start c={ctx} ---\n")
        err_f.write(f"\n--- modelswap start c={ctx} ---\n")
        out_f.flush()
        err_f.flush()
    except OSError:
        out_f = subprocess.DEVNULL
        err_f = subprocess.DEVNULL
    popen_kw: dict[str, Any] = {
        "cwd": str(exe.parent),
        "stdout": out_f,
        "stderr": err_f,
    }
    if sys.platform.startswith("win"):
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        popen_kw["start_new_session"] = True
    return subprocess.Popen(args, **popen_kw)


def start_comfyui() -> subprocess.Popen:
    root = comfy_root()
    if not root.exists():
        raise BookError(f"未找到 ComfyUI 目录：{root}。请设置 NOVEL_WRITER_COMFY_ROOT")
    main_py = root / "main.py"
    if not main_py.exists():
        raise BookError(f"ComfyUI 目录里没有 main.py：{root}")
    python = resolve_comfy_python(root)
    host, port = comfy_host_port()
    cmd = [python, str(main_py), "--listen", host, "--port", str(port)]
    logger.info("starting ComfyUI: %s (cwd=%s)", " ".join(cmd), root)
    try:
        log_dir = modelswap_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "comfyui_illust.log"
        log_f = open(log_path, "a", encoding="utf-8")
        log_f.write("\n--- start ComfyUI ---\n")
        log_f.flush()
        out_f = log_f
        err_f = log_f
    except OSError:
        out_f = subprocess.DEVNULL
        err_f = subprocess.DEVNULL
    popen_kw: dict[str, Any] = {
        "cwd": str(root),
        "stdout": out_f,
        "stderr": err_f,
    }
    if sys.platform.startswith("win"):
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    else:
        popen_kw["start_new_session"] = True
    proc = subprocess.Popen(cmd, **popen_kw)
    state.comfy_pid = proc.pid
    state.comfy_started_by_us = True
    return proc


def stop_comfyui() -> bool:
    host, port = comfy_host_port()
    pid = state.comfy_pid or pid_on_port(host, port)
    if not pid:
        return False
    logger.info("stopping ComfyUI pid=%s port=%s", pid, port)
    stop_pid(pid, force=False)
    deadline = time.time() + 10
    while time.time() < deadline:
        if not comfy_is_up():
            state.comfy_pid = None
            state.comfy_started_by_us = False
            return True
        time.sleep(0.3)
    stop_pid(pid, force=True)
    state.comfy_pid = None
    state.comfy_started_by_us = False
    return not comfy_is_up()


async def wait_http_ok(
    urls: list[str],
    *,
    timeout: float = 120.0,
    interval: float = 1.0,
    on_wait=None,
) -> str:
    deadline = time.time() + timeout
    last_error = ""
    started = time.time()
    async with httpx.AsyncClient(timeout=3.0) as client:
        while time.time() < deadline:
            for url in urls:
                try:
                    response = await client.get(url)
                    if response.status_code < 500:
                        return url
                    last_error = f"HTTP {response.status_code}"
                except httpx.HTTPError as exc:
                    last_error = str(exc) or exc.__class__.__name__
            if on_wait is not None:
                on_wait(time.time() - started, timeout)
            await asyncio.sleep(interval)
    raise BookError(f"等待服务就绪超时。{last_error}".strip())


async def wait_llama(llm_url: str | None = None, timeout: float = 180.0) -> None:
    set_progress("restoring", "正在重新加载写作模型…", percent=20)

    def on_wait(elapsed: float, total: float) -> None:
        pct = 20 + int(min(70, (elapsed / max(total, 1.0)) * 70))
        set_progress("restoring", "正在重新加载写作模型，首次加载会稍慢…", percent=pct)

    base = llm_base(llm_url)
    await wait_http_ok([f"{base}/health", f"{base}/v1/models"], timeout=timeout, on_wait=on_wait)
    set_progress("restoring", "写作模型已就绪", percent=100)


async def wait_comfy(timeout: float = 180.0) -> None:
    set_progress("starting_comfy", "正在启动出图服务…", percent=18)

    def on_wait(elapsed: float, total: float) -> None:
        pct = 18 + int(min(20, (elapsed / max(total, 1.0)) * 20))
        set_progress("starting_comfy", "正在启动出图服务，首次启动会稍慢…", percent=pct)

    base = comfy_url()
    await wait_http_ok([f"{base}/system_stats", f"{base}/object_info"], timeout=timeout, on_wait=on_wait)
    set_progress("starting_comfy", "出图服务已就绪", percent=40)


async def comfy_free_memory() -> None:
    url = f"{comfy_url()}/free"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(url, json={"unload_models": True, "free_memory": True})
    except httpx.HTTPError as exc:
        logger.warning("ComfyUI /free failed: %s", exc)


async def ensure_image_model(llm_url: str | None = None) -> dict:
    """Unload the writing model if needed, then make ComfyUI ready for Qwen Image 2.1."""
    if state.session_active and comfy_is_up():
        return session_status(llm_url)
    try:
        if llama_is_up(llm_url):
            await stop_llama_server(llm_url)
        if not comfy_is_up():
            set_progress("starting_comfy", "正在启动出图服务…", percent=16)
            start_comfyui()
            await wait_comfy()
        else:
            # Confirm the API actually answers, not just that the port is open.
            await wait_comfy(timeout=20.0)
        state.session_active = True
        set_progress("ready", "出图模型已就绪，开始出图…", percent=42)
        return session_status(llm_url)
    except Exception:
        logger.exception("failed to switch to the image model")
        raise


async def finish_session(llm_url: str | None = None) -> dict:
    """Unload the image model and bring the writing model back."""
    set_progress("restoring", "正在结束出图并加载写作模型…", percent=8)
    if comfy_is_up():
        await comfy_free_memory()
        # llama-server needs the same GPU. /free is not always enough to release VRAM.
        stop_comfyui()
    start_llama_server(llm_url)
    await wait_llama(llm_url)
    state.session_active = False
    state.book = None
    set_progress("idle", "写作模型已重新加载", percent=100)
    return session_status(llm_url)


async def restore_writing_model(llm_url: str | None = None) -> dict:
    """Bring the writing model back if an illustration attempt left the GPU empty."""
    set_progress("restoring", "正在重新加载写作模型…", percent=8)
    if llama_is_up(llm_url):
        state.session_active = False
        clear_progress()
        return session_status(llm_url)
    if comfy_is_up():
        await comfy_free_memory()
        stop_comfyui()
    start_llama_server(llm_url)
    await wait_llama(llm_url)
    state.session_active = False
    state.book = None
    clear_progress()
    return session_status(llm_url)


def build_qwen21_txt2img_workflow(
    prompt: str,
    *,
    negative: str = DEFAULT_NEGATIVE,
    seed: int | None = None,
    width: int = DEFAULT_SIZE,
    height: int = DEFAULT_SIZE,
    steps: int = DEFAULT_STEPS,
    cfg: float = DEFAULT_CFG,
    unet: str | None = None,
    clip: str | None = None,
    vae: str | None = None,
) -> dict:
    """API-format graph for Qwen Image 2.1 text-to-image (no reference image)."""
    if seed is None:
        seed = int(time.time() * 1000) % (2**32)
    resolution = max(width, height)
    resolution = max(64, min(4096, (resolution // 32) * 32))
    return {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": unet or QWEN21_UNET, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": clip or QWEN21_CLIP,
                "type": "qwen_image",
                "device": "default",
            },
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae or QWEN21_VAE},
        },
        "4": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["2", 0],
                "prompt": prompt,
                "negative_prompt": negative or DEFAULT_NEGATIVE,
                "resolution": resolution,
            },
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["4", 0],
                "negative": ["4", 1],
                "latent_image": ["4", 2],
                "seed": int(seed),
                "steps": int(steps),
                "cfg": float(cfg),
                "sampler_name": DEFAULT_SAMPLER,
                "scheduler": DEFAULT_SCHEDULER,
                "denoise": 1.0,
            },
        },
        "6": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["5", 0], "vae": ["3", 0]},
        },
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": "snts_illust"},
        },
    }


def _workflow_looks_like_img2img(graph: dict) -> bool:
    types = { _node_type(node) for node in _iter_nodes(graph) }
    has_loader = bool(types & {"LoadImage", "LoadImageMask"})
    has_edit = bool(types & {"QwenImage21Edit", "TextEncodeQwenImageEdit", "TextEncodeQwenImageEditPlus"})
    has_txt_latent = bool(types & LATENT_NODE_TYPES)
    # 2511 / Edit Plus can do text-to-image when no reference image is loaded.
    if has_edit and has_loader:
        return True
    if has_loader and not has_txt_latent:
        return True
    if has_loader and "TextEncodeQwenImage21" not in types and "CLIPTextEncode" not in types and not has_edit:
        return True
    return False


def _iter_nodes(graph: dict) -> list[tuple[str, dict]]:
    if not isinstance(graph, dict):
        return []
    if "class_type" in graph or "inputs" in graph:
        return []
    # API prompt format: {id: {class_type, inputs}}
    if any(isinstance(v, dict) and "class_type" in v for v in graph.values()):
        return [(str(k), v) for k, v in graph.items() if isinstance(v, dict)]
    # UI format
    nodes = graph.get("nodes")
    if isinstance(nodes, list):
        result = []
        for node in nodes:
            if isinstance(node, dict):
                result.append((str(node.get("id", "")), node))
        return result
    return []


def _node_type(item: tuple[str, dict]) -> str:
    node = item[1]
    return str(node.get("class_type") or node.get("type") or "")


def inject_prompt_and_seed(graph: dict, prompt: str, *, seed: int | None = None, negative: str | None = None) -> dict:
    """Copy a workflow (API or simple UI-converted) and fill prompt + a fresh seed."""
    if seed is None:
        seed = int(time.time() * 1000) % (2**32)
    payload = json.loads(json.dumps(graph))
    nodes = _iter_nodes(payload)
    if not nodes and "nodes" not in payload:
        raise BookError("工作流文件格式无法识别")
    wrote_prompt = False
    for _nid, node in nodes:
        ntype = _node_type((_nid, node))
        inputs = node.setdefault("inputs", {}) if "inputs" in node or "class_type" in node else None
        if ntype == "TextEncodeQwenImage21":
            if inputs is not None:
                inputs["prompt"] = prompt
                if negative is not None:
                    inputs["negative_prompt"] = negative
            wrote_prompt = True
        elif ntype in {"TextEncodeQwenImageEditPlus", "TextEncodeQwenImageEdit"}:
            title = str(node.get("_meta", {}).get("title") or node.get("title") or "").lower()
            is_neg = "negative" in title or "负" in title
            if inputs is not None:
                if is_neg:
                    if negative is not None:
                        inputs["prompt"] = negative
                elif not wrote_prompt:
                    inputs["prompt"] = prompt
                    wrote_prompt = True
                else:
                    # second untitled encode is treated as the negative prompt
                    if negative is not None:
                        inputs["prompt"] = negative
        elif ntype in {"CLIPTextEncode", "CLIPTextEncodeSDXL", "CLIPTextEncodeFlux"}:
            title = str(node.get("_meta", {}).get("title") or node.get("title") or "").lower()
            if inputs is not None:
                # Prefer the first positive / untitled encode; skip obvious negatives unless none other exists.
                is_neg = "negative" in title or "负" in title
                if not is_neg:
                    if "text" in inputs or "class_type" in node:
                        inputs["text"] = prompt
                        wrote_prompt = True
                elif negative is not None:
                    inputs["text"] = negative
        elif ntype in {"KSampler", "KSamplerAdvanced", "RandomNoise"}:
            if inputs is not None:
                if "seed" in inputs or ntype in {"KSampler", "RandomNoise"}:
                    inputs["seed"] = int(seed)
                if "noise_seed" in inputs:
                    inputs["noise_seed"] = int(seed)
        # UI widgets_values fallback
        if ntype == "TextEncodeQwenImage21" and "widgets_values" in node:
            values = list(node.get("widgets_values") or [])
            if not values:
                values = [prompt, negative or DEFAULT_NEGATIVE, 1024]
            else:
                values[0] = prompt
            node["widgets_values"] = values
            wrote_prompt = True
        if ntype in {"CLIPTextEncode", "CLIPTextEncodeSDXL"} and "widgets_values" in node:
            title = str(node.get("title") or "").lower()
            values = list(node.get("widgets_values") or [])
            if values and "negative" not in title and "负" not in title:
                values[0] = prompt
                node["widgets_values"] = values
                wrote_prompt = True
        if ntype == "KSampler" and "widgets_values" in node:
            values = list(node.get("widgets_values") or [])
            if values:
                values[0] = int(seed)
                node["widgets_values"] = values
    if not wrote_prompt:
        raise BookError("工作流里找不到可写入画面说明的文本节点")
    return payload


def to_api_prompt(graph: dict) -> dict:
    """Return a Comfy /prompt-ready graph. UI format is converted when possible."""
    if any(isinstance(v, dict) and "class_type" in v for v in graph.values()):
        return {str(k): v for k, v in graph.items() if isinstance(v, dict) and "class_type" in v}
    nodes = graph.get("nodes")
    links = graph.get("links") or []
    if not isinstance(nodes, list):
        raise BookError("工作流文件格式无法识别")
    # links: [link_id, from_node, from_slot, to_node, to_slot, type]
    incoming: dict[tuple[int, int | str], tuple[str, int]] = {}
    for link in links:
        if not isinstance(link, list) or len(link) < 5:
            continue
        _lid, src, src_slot, dst, dst_slot = link[0], link[1], link[2], link[3], link[4]
        incoming[(int(dst), dst_slot)] = (str(src), int(src_slot))
    prompt: dict[str, dict] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") in {"Note", "Reroute", "PrimitiveNode"}:
            continue
        nid = str(node.get("id"))
        class_type = str(node.get("type") or "")
        inputs: dict[str, Any] = {}
        for item in node.get("inputs") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            link = item.get("link")
            if link is None:
                if "widget" in item or item.get("type") in {"INT", "FLOAT", "STRING", "COMBO"}:
                    continue
                continue
            # resolve via links
            key = None
            for cand in incoming:
                if cand[0] == int(node.get("id")) and (
                    cand[1] == name or incoming.get(cand)
                ):
                    pass
            # Prefer mapping by destination slot index
        # widget values in order of widget-bearing inputs
        widgets = list(node.get("widgets_values") or [])
        widget_names = []
        for item in node.get("inputs") or []:
            if isinstance(item, dict) and item.get("widget"):
                widget_names.append(item.get("name"))
            elif isinstance(item, dict) and item.get("link") is None and item.get("name"):
                # some exports list widgets only in widgets_values
                pass
        # linked inputs
        for item in node.get("inputs") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            link_id = item.get("link")
            if name is None or link_id is None:
                continue
            for link in links:
                if isinstance(link, list) and len(link) >= 5 and link[0] == link_id:
                    inputs[name] = [str(link[1]), int(link[2])]
                    break
        # widgets: assign remaining values to unconnected / widget inputs in order
        w_index = 0
        possible = []
        for item in node.get("inputs") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name or name in inputs:
                continue
            if item.get("link") is not None:
                continue
            possible.append(name)
        if not possible and class_type in TXT_PROMPT_NODE_TYPES:
            possible = ["prompt", "negative_prompt", "resolution"] if class_type == "TextEncodeQwenImage21" else ["text"]
        if not possible and class_type == "KSampler":
            possible = ["seed", "steps", "cfg", "sampler_name", "scheduler", "denoise"]
        if not possible and class_type in {"UNETLoader"}:
            possible = ["unet_name", "weight_dtype"]
        if not possible and class_type == "CLIPLoader":
            possible = ["clip_name", "type", "device"]
        if not possible and class_type == "VAELoader":
            possible = ["vae_name"]
        if not possible and class_type == "SaveImage":
            possible = ["filename_prefix"]
        if not possible and class_type in {"EmptyLatentImage", "EmptySD3LatentImage"}:
            possible = ["width", "height", "batch_size"]
        for name in possible:
            if w_index >= len(widgets):
                break
            if name not in inputs:
                inputs[name] = widgets[w_index]
                w_index += 1
        prompt[nid] = {"class_type": class_type, "inputs": inputs}
    return prompt


def load_workflow_file(path: Path) -> dict:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise BookError(f"无法读取出图工作流：{path.name}") from exc
    if not isinstance(data, dict):
        raise BookError("出图工作流格式无效")
    return data


def known_workflow_candidates() -> list[Path]:
    home = Path.home()
    desktop = home / "Desktop"
    root = comfy_root()
    names = (
        "qwen_image_2.1_t2i.json",
        "Qwen_Image_2.1_Text_to_Image.json",
        "qwen_image_2_1_t2i.json",
        "image_qwen_image_2_1_t2i.json",
    )
    paths: list[Path] = []
    for folder in (
        desktop,
        root / "user" / "default" / "workflows",
        root / "user" / "default" / "user" / "workflows",
        root / "workflows",
        Path(__file__).resolve().parent / "workflows",
    ):
        if not folder.exists():
            continue
        for name in names:
            candidate = folder / name
            if candidate.exists():
                paths.append(candidate)
    return paths


def workflow_is_usable_txt2img(graph: dict) -> bool:
    try:
        api = to_api_prompt(graph) if "nodes" in graph else graph
    except BookError:
        return False
    if _workflow_looks_like_img2img(api):
        return False
    types = {_node_type(item) for item in _iter_nodes(api)}
    has_prompt = bool(types & TXT_PROMPT_NODE_TYPES)
    has_sampler = "KSampler" in types or "KSamplerAdvanced" in types
    return has_prompt and has_sampler



def build_qwen2511_txt2img_workflow(
    prompt: str,
    *,
    negative: str = DEFAULT_NEGATIVE,
    seed: int | None = None,
    width: int = DEFAULT_SIZE,
    height: int = DEFAULT_SIZE,
    steps: int | None = None,
    cfg: float | None = None,
    unet: str | None = None,
    clip: str | None = None,
    vae: str | None = None,
    lora: str | None = None,
) -> dict:
    """API-format graph for Qwen Image Edit 2511 text-to-image (no reference image)."""
    if seed is None:
        seed = int(time.time() * 1000) % (2**32)
    width = max(64, min(4096, (int(width) // 32) * 32))
    height = max(64, min(4096, (int(height) // 32) * 32))
    steps = int(steps if steps is not None else QWEN2511_STEPS)
    cfg = float(cfg if cfg is not None else QWEN2511_CFG)
    lora_name = (lora or QWEN2511_LORA or "").strip()
    graph = {
        "1": {
            "class_type": "UNETLoader",
            "inputs": {"unet_name": unet or QWEN2511_UNET, "weight_dtype": "default"},
        },
        "2": {
            "class_type": "CLIPLoader",
            "inputs": {
                "clip_name": clip or QWEN2511_CLIP,
                "type": "qwen_image",
                "device": "default",
            },
        },
        "3": {
            "class_type": "VAELoader",
            "inputs": {"vae_name": vae or QWEN2511_VAE},
        },
    }
    model_node = "1"
    if lora_name:
        graph["4"] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": [model_node, 0],
                "lora_name": lora_name,
                "strength_model": float(QWEN2511_LORA_STRENGTH),
            },
        }
        model_node = "4"
    graph["5"] = {
        "class_type": "ModelSamplingAuraFlow",
        "inputs": {"model": [model_node, 0], "shift": float(QWEN2511_SHIFT), "sampling": "flow"},
    }
    graph["6"] = {
        "class_type": "TextEncodeQwenImageEditPlus",
        "inputs": {
            "clip": ["2", 0],
            "prompt": prompt,
            "vae": ["3", 0],
        },
        "_meta": {"title": "Positive"},
    }
    graph["7"] = {
        "class_type": "TextEncodeQwenImageEditPlus",
        "inputs": {
            "clip": ["2", 0],
            "prompt": negative or DEFAULT_NEGATIVE,
            "vae": ["3", 0],
        },
        "_meta": {"title": "Negative"},
    }
    graph["8"] = {
        "class_type": "EmptySD3LatentImage",
        "inputs": {"width": width, "height": height, "batch_size": 1},
    }
    graph["9"] = {
        "class_type": "KSampler",
        "inputs": {
            "model": ["5", 0],
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["8", 0],
            "seed": int(seed),
            "steps": steps,
            "cfg": cfg,
            "sampler_name": DEFAULT_SAMPLER,
            "scheduler": DEFAULT_SCHEDULER,
            "denoise": 1.0,
        },
    }
    graph["10"] = {
        "class_type": "VAEDecode",
        "inputs": {"samples": ["9", 0], "vae": ["3", 0]},
    }
    graph["11"] = {
        "class_type": "SaveImage",
        "inputs": {"images": ["10", 0], "filename_prefix": "snts_illust"},
    }
    return graph

def resolve_workflow(prompt: str, *, seed: int | None = None, negative: str | None = None) -> tuple[dict, str]:
    """Return (api graph, source label). Prefer env override, else the built-in Qwen Image Edit 2511 t2i graph.

    Configured img2img / edit workflows without a source image are rejected.
    Desktop img2img JSON files are never used silently.
    """
    override = (os.environ.get("NOVEL_WRITER_COMFY_WORKFLOW") or DEFAULT_COMFY_WORKFLOW or "").strip()
    if override:
        path = Path(override)
        if not path.exists():
            raise BookError(f"未找到指定的出图工作流：{path}")
        graph = load_workflow_file(path)
        api = to_api_prompt(graph) if "nodes" in graph else graph
        if _workflow_looks_like_img2img(api):
            raise BookError(
                "当前指定的工作流是图生图或编辑流程，缺少源图片，不能用来为正文配图。"
                "请改用文生图工作流，或取消 NOVEL_WRITER_COMFY_WORKFLOW 以使用内置的 Qwen Image Edit 2511 文生图。"
            )
        filled = inject_prompt_and_seed(api, prompt, seed=seed, negative=negative)
        return to_api_prompt(filled) if "nodes" in filled else filled, str(path)

    for path in known_workflow_candidates():
        try:
            graph = load_workflow_file(path)
        except BookError:
            continue
        if not workflow_is_usable_txt2img(graph):
            continue
        api = to_api_prompt(graph) if "nodes" in graph else graph
        filled = inject_prompt_and_seed(api, prompt, seed=seed, negative=negative)
        return to_api_prompt(filled) if "nodes" in filled else filled, str(path)

    built = build_qwen2511_txt2img_workflow(prompt, negative=negative or DEFAULT_NEGATIVE, seed=seed)
    return built, "builtin-qwen-image-edit-2511-t2i"


async def fetch_object_info() -> dict:
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{comfy_url()}/object_info")
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, dict) else {}
    except httpx.HTTPError as exc:
        raise BookError("无法读取 ComfyUI 节点信息，请确认出图服务已启动") from exc


def validate_graph_nodes(graph: dict, object_info: dict | None) -> None:
    if not object_info:
        return
    missing = []
    for _nid, node in _iter_nodes(graph):
        ntype = _node_type((_nid, node))
        if ntype and ntype not in object_info:
            missing.append(ntype)
    if not missing:
        return
    names = "、".join(dict.fromkeys(missing))
    if "TextEncodeQwenImageEditPlus" in missing or "TextEncodeQwenImage21" in missing:
        raise BookError(
            "当前 ComfyUI 缺少 Qwen 文生图所需节点，无法使用 Qwen Image Edit 2511。"
            "请先把 ComfyUI 更新到带有 TextEncodeQwenImageEditPlus 的版本。"
        )
    raise BookError(f"当前 ComfyUI 缺少出图所需节点：{names}")


async def _ws_connect_text(host: str, port: int, path: str):
    import base64
    import os as _os
    key = base64.b64encode(_os.urandom(16)).decode()
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    writer.write(request.encode())
    await writer.drain()
    header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    if b"101" not in header.split(b"\r\n", 1)[0]:
        writer.close()
        raise OSError("websocket handshake failed")
    return reader, writer


async def _ws_recv_text(reader, writer=None) -> str | None:
    while True:
        header = await reader.readexactly(2)
        opcode = header[0] & 0x0F
        masked = header[1] & 0x80
        length = header[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await reader.readexactly(8), "big")
        mask = await reader.readexactly(4) if masked else b""
        payload = await reader.readexactly(length)
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        if opcode == 0x9:
            continue
        if opcode == 0xA:
            continue
        if opcode == 0x8:
            return None
        if opcode == 0x1:
            return payload.decode("utf-8", "replace")
        return ""


def _progress_label_for_node(node_type: str, value: int = 0, maximum: int = 0) -> tuple[str, int]:
    ntype = node_type or ""
    if "Loader" in ntype or ntype in {"UNETLoader", "CLIPLoader", "VAELoader", "CheckpointLoaderSimple"}:
        return "正在加载生图模型…", 50
    if "TextEncode" in ntype or "CLIPText" in ntype:
        return "正在编码画面说明…", 58
    if "KSampler" in ntype or "Sampler" in ntype:
        if maximum:
            percent = 60 + int(35 * (value or 0) / maximum)
            return f"正在出图 {value}/{maximum} 步", min(95, percent)
        return "正在出图…", 70
    if "VAEDecode" in ntype or "SaveImage" in ntype:
        return "正在导出图片…", 96
    return "正在出图…", 62


async def _listen_comfy_progress(graph: dict, prompt_id: str, stop: asyncio.Event) -> None:
    host, port = comfy_host_port()
    node_labels = {}
    for nid, node in (graph or {}).items():
        if isinstance(node, dict):
            node_labels[str(nid)] = str(node.get("class_type") or "")
    try:
        reader, writer = await _ws_connect_text(host, port, "/ws?clientId=snts-illust")
    except Exception as exc:
        logger.info("comfy progress socket unavailable: %s", exc)
        return
    try:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(_ws_recv_text(reader, writer), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if raw is None:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            typ = msg.get("type")
            data = msg.get("data") or {}
            if typ == "progress":
                if prompt_id and data.get("prompt_id") and str(data.get("prompt_id")) != str(prompt_id):
                    continue
                value = int(data.get("value") or 0)
                maximum = int(data.get("max") or 0)
                ntype = node_labels.get(str(data.get("node") or ""), "")
                label, percent = _progress_label_for_node(ntype, value, maximum)
                set_progress("generating", label, percent=percent, current=value, total=maximum)
            elif typ == "executing":
                node = data.get("node")
                if node:
                    ntype = node_labels.get(str(node), "")
                    label, percent = _progress_label_for_node(ntype)
                    set_progress("generating", label, percent=percent)
                else:
                    set_progress("generating", "正在收尾…", percent=97)
    except Exception:
        logger.debug("comfy progress listener ended", exc_info=True)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def queue_and_collect_image(graph: dict, *, timeout: float = 900.0) -> bytes:
    base = comfy_url()
    client_id = "snts-illust"
    set_progress("submitting", "正在提交出图任务…", percent=45)
    stop = asyncio.Event()
    listener: asyncio.Task | None = None
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            submitted = await client.post(f"{base}/prompt", json={"prompt": graph, "client_id": client_id})
        except httpx.HTTPError as exc:
            raise BookError("无法连接到 ComfyUI 出图服务") from exc
        if submitted.status_code >= 400:
            detail = submitted.text[:300]
            raise BookError(f"出图工作流提交失败。{detail}".strip())
        payload = submitted.json()
        prompt_id = str(payload.get("prompt_id") or payload.get("promptId") or "")
        if not prompt_id:
            raise BookError("出图服务没有返回任务编号")
        set_progress("generating", "正在加载生图模型并出图…", percent=50)
        listener = asyncio.create_task(_listen_comfy_progress(graph, prompt_id, stop))
        deadline = time.time() + timeout
        outputs = None
        last_error = ""
        try:
            while time.time() < deadline:
                try:
                    history = await client.get(f"{base}/history/{prompt_id}")
                    data = history.json()
                except httpx.HTTPError as exc:
                    last_error = str(exc)
                    await asyncio.sleep(1.0)
                    continue
                entry = data.get(prompt_id) if isinstance(data, dict) else None
                if isinstance(entry, dict):
                    status = entry.get("status") or {}
                    if status.get("status_str") == "error" or status.get("completed") is False and status.get("messages"):
                        raise BookError("出图失败，请检查 ComfyUI 窗口中的报错")
                    outputs = entry.get("outputs")
                    if outputs:
                        break
                await asyncio.sleep(0.8)
        finally:
            stop.set()
            if listener is not None:
                listener.cancel()
                try:
                    await listener
                except (asyncio.CancelledError, Exception):
                    pass
        if not outputs:
            raise BookError(f"等待出图结果超时。{last_error}".strip())
        image_info = _first_image_ref(outputs)
        if not image_info:
            raise BookError("出图完成但没有找到图片文件")
        set_progress("generating", "正在取回图片…", percent=98)
        query = urlencode(
            {
                "filename": image_info.get("filename") or "",
                "subfolder": image_info.get("subfolder") or "",
                "type": image_info.get("type") or "output",
            }
        )
        view = await client.get(f"{base}/view?{query}")
        if view.status_code >= 400 or not view.content:
            raise BookError("无法取回已经生成的插图")
        set_progress("done", "出图完成", percent=100)
        return view.content


def _first_image_ref(outputs: dict) -> dict | None:
    for _nid, payload in (outputs or {}).items():
        if not isinstance(payload, dict):
            continue
        images = payload.get("images") or payload.get("gifs") or []
        if isinstance(images, list):
            for item in images:
                if isinstance(item, dict) and item.get("filename"):
                    return item
    return None


async def generate_image(prompt: str, *, negative: str | None = None, seed: int | None = None) -> tuple[bytes, str]:
    graph, source = resolve_workflow(prompt, seed=seed, negative=negative)
    if not comfy_is_up():
        raise BookError("出图服务尚未就绪。请稍后再试，或先结束出图会话后重试")
    try:
        info = await fetch_object_info()
    except BookError:
        info = {}
    validate_graph_nodes(graph, info)
    png = await queue_and_collect_image(graph)
    return png, source
