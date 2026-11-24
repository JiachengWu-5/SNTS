# SHANYDOC / SNTS

SNTS（Shanydoc Novel Terminal System）是一套在本机运行的小说写作流水线。SNTS 是缩写，产品名是 SHANYDOC，用户名是 shanydoc。它用本地网页对接本机的 llama.cpp 兼容服务（例如 `llama-server` 提供的 OpenAI 风格接口），把从构思到成稿、插图和导出的步骤收在同一个应用里。

公开仓库：<https://github.com/JiachengWu-5/SNTS>

完整流程：

1. 写思路
2. 可选生成世界观设定和人物卡
3. 按章生成并编辑大纲，支持情节树和多条结局分支
4. 沿当前路径撰写各章正文，并滚动维护「本章摘要」
5. 续写、扩写、按改进意见修改，并保留版本时间线
6. 通过本机 ComfyUI 为场景出插图，出图时自动切换写作模型与图像模型
7. 内置导出全书 PDF

Python 包目录名仍是 `app`，启动方式见下方的 `start.bat` / `start.sh`。

**扩写**的含义是：保留原文中的句子、对话和情节，只在原句内部或句段之间插入更具体的描写与细节，而不是重写一遍，也不是只在章末追加一段。

生成大纲、写正文和按意见修改时，建议在页面左侧打开「思考」（thinking）。思考模式始终可用，不要把它从设置里拿掉。

成年向内容可以使用；涉及未满 18 岁未成年人的性描写不会被用作文风参考或出图依据。

本仓库不包含任何作品正文、大纲、思路、人物卡、本章摘要、插图或文风样本，也不包含模型权重。请自行准备模型，并在写作页中添加你自己的文风样本。

## 环境要求

- Windows 是主要开发与使用环境；也可用 Linux / macOS。需要 Python 3.10 或更高（在 Python 3.12 上开发）
- 一个与 llama.cpp 兼容、提供 OpenAI 风格接口的本地服务，默认地址为 `http://127.0.0.1:8080`
- 一个 GGUF 写作模型。推荐有 GPU。示例模型：`unsloth/Qwen3.8-27B-GGUF` 的 `UD-Q5_K_M` 量化（请自行从 Hugging Face 等来源下载，不要把权重放进本仓库）
- 导出全书 PDF 需要 `reportlab` 和 `pillow`（已写入 `requirements.txt`），以及本机的中文字体（Windows 优先使用微软雅黑 / 宋体；macOS / Linux 会尝试 Noto CJK 等常见字体）
- 出插图为可选项，需要本机安装 [ComfyUI](https://github.com/comfyanonymous/ComfyUI)，并备好 **Qwen Image Edit 2511** 及相关编码器、VAE、可选 Lightning LoRA
- 现代浏览器

可选依赖：从网页整本导入文风样本时，若需繁体转简体，可另行安装 `zhconv` 或 `opencc-python-reimplemented`（`pip install '.[zhconv]'`）。没有它们也可以导入，只是不会自动转换。

## 安装

```bash
git clone https://github.com/JiachengWu-5/SNTS.git
cd SNTS
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux / macOS:
source .venv/bin/activate
python -m pip install -r requirements.txt
```

如果是解压本目录的压缩包，进入 `SHANYDOC-oss` 后同样执行后面的虚拟环境和 `pip` 步骤即可。

可先复制 `.env.example` 为 `.env`，按自己的机器填写路径。`start.bat` / `start.sh` 启动时会读取 `.env`。

## 启动 llama-server

先单独启动写作模型服务。下面的参数是示例：监听本机、启用 jinja 聊天模板，并设置上下文长度与 GPU 层数。`-c` 应与写作页使用的上下文长度一致（默认 65536，可用环境变量 `NOVEL_WRITER_CONTEXT_TOKENS` 修改）。

```bash
# Linux / macOS 示例
llama-server -m /path/to/Qwen3.8-27B-UD-Q5_K_M.gguf \
  --host 127.0.0.1 --port 8080 --jinja -c 65536 -ngl 99

# Windows 示例（请改成你自己的模型路径，不要使用仓库里的占位路径）
llama-server -m C:\models\Qwen3.8-27B-UD-Q5_K_M.gguf --host 127.0.0.1 --port 8080 --jinja -c 65536 -ngl 99
```

请按你的机器显存调整 `-ngl` 和量化级别。不要把模型文件放进本仓库。

若要在出图时由写作页自动卸载并重新加载写作模型，还需要设置 `NOVEL_WRITER_LLAMA_EXE` 和 `NOVEL_WRITER_LLAMA_MODEL`，分别指向本机的 `llama-server` 可执行文件和 GGUF 文件，例如：

```bash
# Linux / macOS 示例
export NOVEL_WRITER_LLAMA_EXE="$HOME/bin/llama-server"
export NOVEL_WRITER_LLAMA_MODEL="$HOME/models/Qwen3.8-27B-UD-Q5_K_M.gguf"

# Windows (cmd) 示例，请改成你自己的路径
set NOVEL_WRITER_LLAMA_EXE=C:\llama.cpp\llama-server.exe
set NOVEL_WRITER_LLAMA_MODEL=C:\models\Qwen3.8-27B-UD-Q5_K_M.gguf
```

## 启动写作页

模型服务起来之后，在本仓库目录启动写作页（默认只监听本机，不绑定 `0.0.0.0`）：

```bash
# Windows：双击 start.bat，或在本目录执行
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765

# Linux / macOS
./start.sh
# 或：
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

然后用浏览器打开 <http://127.0.0.1:8765>。

启动脚本只负责拉起写作页，不会启动 `llama-server`。Windows 页面里也提供重新加载写作服务的入口（调用 `reload-server.bat`），不会卸载写作模型或图像模型。

## 作品存放位置

每本书一个文件夹。默认目录是当前用户主目录下的 `Article`，也就是 `~/Article`（Windows 上通常是 `%USERPROFILE%\Article`）。可用环境变量改到任何位置，例如：

```bash
# Linux / macOS
export NOVEL_WRITER_ARTICLE_ROOT="$HOME/Novels"

# Windows (cmd)
set NOVEL_WRITER_ARTICLE_ROOT=%USERPROFILE%\Article
```

每本书的目录结构大致包括：

- `思路.md`
- `大纲.md`
- `style/`（本作品的文风样本）
- `chapters/01.md` …
- `settings.json`
- 以及世界观、人物卡、本章摘要、情节树、插图和版本历史等由应用自动维护的文件

共用文风库默认在作品根目录下的 `template/`（可用 `NOVEL_WRITER_TEMPLATE_DIR` 单独指定）。把你自己的 `.txt` 样本放进去，或在页面里粘贴文本、上传文件、抓取公开网页，也可以按书/系列地址整本导入后再选用。本仓库不附带文风样本或示例书稿。

从网页导入时，只允许公开的 `http` / `https` 地址，不会访问本机或内网。

## 写作流水线

可在页面左侧（或每本书的 `settings.json`）调整目标字数、温度（temperature）、是否启用思考（thinking）、写作模型地址（默认 `http://127.0.0.1:8080`），以及选用的文风样本。

- **思路**：先写下故事的核心设想。
- **世界观与人物**（可选）：根据思路生成设定和人物卡，生成后可继续编辑；人物卡支持补丁与确认。
- **大纲与情节树**：按章生成大纲，支持多结局分支。写作沿当前打开的路径进行，而不是把所有分支混在一起。
- **正文**：按当前路径逐章撰写。可续写、扩写、按改进意见修改。
- **本章摘要**：沿当前情节线滚动更新，供后续章节接着用，避免把未采用的分支写进摘要。
- **版本时间线**：大纲和章节都可以回到先前保存的版本。
- **一致性检查**：对照已有设定、人物和前文做检查。

## 场景插图

正文页右侧有插图面板。可以先让写作模型拟画面说明，编辑后再出图。插图默认**不会**自动插入正文；需要时再由你放进对应位置。正文里的插图标记在送给写作模型时会被去掉，避免干扰续写。

写作模型和图像模型不要同时占着同一块 GPU。出图时，写作页会先卸载写作模型、加载图像模型，出完后再把写作模型加载回来。默认工作流是 **Qwen Image Edit 2511** 文生图（`builtin-qwen-image-edit-2511-t2i`）。第一张图加载模型可能较慢，排队等待时间为 900 秒。

点击插图可以放大预览；要保存文件请用旁边的「保存」，不要依赖浏览器对缩略图的另存为。

出图需要本机 ComfyUI（默认地址 `http://127.0.0.1:8188`）。请用环境变量指出 ComfyUI 的安装目录，例如：

```bash
# Windows 示例（请改成你自己的安装位置）
set NOVEL_WRITER_COMFY_ROOT=C:\ComfyUI

# Linux / macOS 示例
export NOVEL_WRITER_COMFY_ROOT="$HOME/ComfyUI"
```

请自行下载 Qwen Image Edit 2511 及相关编码器、VAE、可选 Lightning LoRA，放到 ComfyUI 的模型目录中。文件名可用 `NOVEL_WRITER_QWEN2511_*` 覆盖，默认值见 `app/modelswap.py`。不要把权重提交进本仓库。

## 导出全书 PDF

正文工具栏提供「导出全书 PDF」，这是应用内置功能。会按当前情节线把各章缝成一本：封面、可点击跳转的目录、页码；章首放置尚未插入正文的场景图，文中已插入的插图留在对应位置。若情节树有多条结局，共用的前半只出现一次，后面每条结局都会收进同一本，并在目录里分开列出。

PDF 写进该书的文件夹，同时开始下载。导出在服务端完成，不会打断正在进行的写作或出图任务。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `NOVEL_WRITER_ARTICLE_ROOT` | `~/Article`（即当前用户主目录下的 `Article`） | 作品根目录 |
| `NOVEL_WRITER_TEMPLATE_DIR` | `<作品根目录>/template` | 共用文风样本目录 |
| `NOVEL_WRITER_LLM_URL` | `http://127.0.0.1:8080` | 默认 llama-server 地址 |
| `NOVEL_WRITER_CONTEXT_TOKENS` | `65536` | 提示预算所用的上下文长度，应与 `llama-server -c` 一致 |
| `NOVEL_WRITER_HOST` | `127.0.0.1` | 供代码读取的监听地址（用 uvicorn 启动时仍请显式传入 `--host`） |
| `NOVEL_WRITER_PORT` | `8765` | 供代码读取的端口（用 uvicorn 启动时仍请显式传入 `--port`） |
| `NOVEL_WRITER_LLAMA_EXE` | （空，需自行设置） | 出图时用于重新拉起写作模型的 `llama-server` 路径 |
| `NOVEL_WRITER_LLAMA_MODEL` | （空，需自行设置） | 对应的 GGUF 模型文件路径 |
| `NOVEL_WRITER_LLAMA_RESTART` | （空） | 可选的自定义启动脚本；一般不必设置 |
| `NOVEL_WRITER_COMFY_URL` | `http://127.0.0.1:8188` | ComfyUI 地址 |
| `NOVEL_WRITER_COMFY_ROOT` | `D:\ComfyUI`（示例默认，请改成你的安装位置） | ComfyUI 根目录 |
| `NOVEL_WRITER_COMFY_PYTHON` | （自动检测） | 用于启动 ComfyUI 的 Python |
| `NOVEL_WRITER_COMFY_WORKFLOW` | （空，使用内置 2511 文生图） | 自定义 ComfyUI 工作流文件 |
| `NOVEL_WRITER_LOG_DIR` | 系统临时目录下的 `shanydoc` | llama-server / ComfyUI 由本程序拉起时的日志目录 |

更完整的示例见 `.env.example`。

## 在其他设备上打开（可选）

写作页默认只绑定 `127.0.0.1`。若要在同一台电脑以外的设备上使用，请自行用你信任的方式转发，例如你自己的反向代理，并注意访问控制。本项目不内置远程暴露或隧道配置。

手机竖屏下，页面会优先显示编辑区，「作品」和「章节」通过左右按钮打开。

## 许可证

以 MIT 许可证发布，版权所有 © 2026 SHANYDOC。详见 `LICENSE`。
