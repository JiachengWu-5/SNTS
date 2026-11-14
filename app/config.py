"""Runtime configuration. Paths can be overridden with environment variables."""

from __future__ import annotations

import os
from pathlib import Path


def article_root() -> Path:
    override = os.environ.get("NOVEL_WRITER_ARTICLE_ROOT")
    if override:
        return Path(override)
    return Path.home() / "Article"


def template_dir() -> Path:
    override = os.environ.get("NOVEL_WRITER_TEMPLATE_DIR")
    if override:
        return Path(override)
    return article_root() / "template"


HOST = os.environ.get("NOVEL_WRITER_HOST", "127.0.0.1")
PORT = int(os.environ.get("NOVEL_WRITER_PORT", "8765"))
DEFAULT_LLM_URL = os.environ.get("NOVEL_WRITER_LLM_URL", "http://127.0.0.1:8080")
DEFAULT_TEMPERATURE = 0.8
DEFAULT_CHAPTER_CHARS = 2000
DEFAULT_ENABLE_THINKING = False

# Should match the llama-server context size (-c).
CONTEXT_TOKENS = int(os.environ.get("NOVEL_WRITER_CONTEXT_TOKENS", "65536"))
PROMPT_SAFETY_MARGIN = 512
THINKING_RESERVE_TOKENS = 4096

# Only samples that depict sexual content involving minors (under 18) are blocked.
# Adult characters aged 18+ are selectable by the user.
BLOCKED_TEMPLATE_FILES = frozenset()
BLOCKED_TEMPLATE_REASON = "涉及未成年人，不能作为文风参考"
# Backward-compatible aliases
UNSAFE_TEMPLATE_FILES = BLOCKED_TEMPLATE_FILES
UNSAFE_TEMPLATE_REASON = BLOCKED_TEMPLATE_REASON
# This project does not ship style-sample files. Add your own .txt samples
# to the template directory, or upload / paste them in the web UI.
SAFE_TEMPLATE_FILES = frozenset()

RESERVED_DIR_NAMES = frozenset({"template"})
RESERVED_FILE_NAMES = frozenset({"requirements.txt"})

DEFAULT_SETTINGS = {
    "llm_url": DEFAULT_LLM_URL,
    "temperature": DEFAULT_TEMPERATURE,
    "chapter_target_chars": DEFAULT_CHAPTER_CHARS,
    "enable_thinking": DEFAULT_ENABLE_THINKING,
    "selected_styles": [],
}


# Illustration / model-swap. The writing web service stays up; only llama-server / ComfyUI are started or stopped.
DEFAULT_COMFY_URL = os.environ.get("NOVEL_WRITER_COMFY_URL", "http://127.0.0.1:8188")
DEFAULT_COMFY_ROOT = os.environ.get("NOVEL_WRITER_COMFY_ROOT", r"D:\ComfyUI")
DEFAULT_COMFY_WORKFLOW = os.environ.get("NOVEL_WRITER_COMFY_WORKFLOW", "")
DEFAULT_LLAMA_RESTART = os.environ.get("NOVEL_WRITER_LLAMA_RESTART", "")
DEFAULT_COMFY_PYTHON = os.environ.get("NOVEL_WRITER_COMFY_PYTHON", "")

# Optional Qwen Image 2.1 filenames, used if you point a custom workflow at that family.
# The built-in default illustration workflow is Qwen Image Edit 2511 (see app/modelswap.py).
QWEN21_UNET = os.environ.get("NOVEL_WRITER_QWEN_UNET", "qwen_image_2.1_int8_convrot.safetensors")
QWEN21_CLIP = os.environ.get("NOVEL_WRITER_QWEN_CLIP", "qwen3vl_8b_int8_convrot.safetensors")
QWEN21_VAE = os.environ.get("NOVEL_WRITER_QWEN_VAE", "qwen_image_2.1_vae_bf16.safetensors")

