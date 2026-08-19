# 文字稿美化功能实施方案

> 本文档是给后续 AI 会话的执行蓝图。在现有三页 GUI（配置页 → 转写页）基础上，
> 新增「美化页」作为第三页，并集成繁简转换 + LLM API 精修两大功能。

## 0. 背景与目标

### 现状问题
1. Whisper small 模型中文输出**不带标点符号**
2. 输出存在**繁简混存**（如"面機"/"面基"、"霧石"/"务实"随机切换）
3. 同音错别字较多（如"面机"→"面基"、"输"→"书"）

### 目标
- **自动繁简转换**：转写完成后立即将繁体统一为简体（零成本、秒级）
- **美化页 UI**：转写完成后自动跳转到美化页，显示完成状态 + 提供精修入口
- **LLM API 精修**：用户配置自己的 API Key，调用 LLM 加标点 + 纠错 + 统一简体
- **保留原始转写**：精修结果写入新文件 `_精修.md`，原始 MD 不动

---

## 1. 繁简转换（opencc）— 转写后自动执行

### 1.1 依赖

```
pip install opencc-python-reimplemented
```

> ⚠️ 用 `opencc-python-reimplemented` 而非 `opencc`，前者是纯 Python 实现，
> 无 C 扩展依赖，PyInstaller 打包更简单。包名 import 时仍为 `from opencc import OpenCC`。

包大小：~5MB（含繁简字典）。

### 1.2 改动位置：`xyz2md.py`

在 `write_markdown()` 函数的 `write_seg()` 内部，对每段 text 做繁简转换：

```python
# 文件顶部（延迟导入，避免未安装时崩溃）
_cc = None
def _get_cc():
    global _cc
    if _cc is None:
        try:
            from opencc import OpenCC
            _cc = OpenCC('t2s')  # 繁体→简体
        except ImportError:
            _cc = False  # 标记不可用
    return _cc

def write_seg(f, seg) -> None:
    text = re.sub(r"\s+", " ", seg.text or "").strip()
    if not text:
        return
    cc = _get_cc()
    if cc:
        text = cc.convert(text)
    f.write(f"**[{fmt_ts(seg.start)} → {fmt_ts(seg.end)}]** {text}\n\n")
```

### 1.3 效果

- 速度：整篇 3 万字 < 0.1 秒（纯字典查表）
- 准确率：>99%（标准繁简映射表）
- 内存：忽略不计
- 如果 opencc 未安装，静默跳过（不崩溃）

### 1.4 PyInstaller 打包

```powershell
--collect-data opencc
```

把字典数据文件（`opencc/config/`、`opencc/dictionary/`）带进 exe。

### 1.5 requirements.txt 追加

```
opencc-python-reimplemented>=0.1.7
```

---

## 2. 美化页 UI — 第三页

### 2.1 页面流转

```
配置页 ──开始转换──→ 转写页 ──转写完成──→ 美化页
  ↑                    ↑                    │
  └────── 返回 ────────┘←──── 返回 ────────┘
```

转写完成后**自动跳转**到美化页（不再停留在转写页）。

### 2.2 美化页布局

```
┌──────────────────────────────────────────────┐
│  ← 返回                                       │
├──────────────────────────────────────────────┤
│  ✅ 转换完成                                   │  ← 大标题
│  E169.A股的春夏秋冬：种树、种粮、种菜            │  ← 单集标题
│  播客：面基 · 时长：2h25m · 300 段 · 耗时 18m   │  ← 统计信息
├──────────────────────────────────────────────┤
│  ┌─ 繁简转换 ─────────────────────────────┐   │
│  │ ✅ 已自动转换为简体中文                   │   │  ← 状态标签
│  │ [🔄 重新转换]                            │   │  ← 可选按钮
│  └────────────────────────────────────────┘   │
│                                                │
│  ┌─ API 精修（加标点 + 纠错）──────────────┐   │
│  │ 服务商  ▾ [MiniMax / DeepSeek / OpenAI / 自定义] │
│  │ API Key  [________________________] [保存] │   │
│  │ 模型     [________________________]        │   │
│  │ Base URL [________________________]        │   │  ← 仅"自定义"时显示
│  │           [🚀 开始精修]                     │   │
│  │  进度: ████████░░ 67%  已处理 200/300 段   │   │  ← 精修时显示
│  │  日志: (滚动文本框, 显示每段处理结果)       │   │
│  └────────────────────────────────────────┘   │
│                                                │
│  [📂 打开输出文件夹]    [📄 打开精修文档]       │
└──────────────────────────────────────────────┘
```

### 2.3 类结构

```python
class BeautifyPage(ctk.CTkFrame):
    def __init__(self, master, on_back, on_open_folder, on_open_polished):
        ...

    def set_result(self, meta: dict, md_path: str, segment_count: int, elapsed: float):
        """转写完成后调用, 填充标题/统计/状态"""
        ...

    def set_api_config(self, config: dict):
        """从 config.json 加载 API 配置, 填充输入框"""
        ...

    def get_api_config(self) -> dict:
        """从输入框收集 API 配置"""
        ...

    def start_polish(self, segments_data: list, api_config: dict, output_path: str):
        """启动 API 精修线程"""
        ...

    def append_polish_log(self, line: str):
        """追加精修日志"""
        ...

    def set_polish_progress(self, current: int, total: int):
        """更新精修进度条"""
        ...

    def polish_done(self, success: bool, output_path: str = ""):
        """精修完成回调"""
        ...
```

### 2.4 App 类改动

```python
class App(ctk.CTk):
    def __init__(self):
        ...
        # 新增第三页
        self.beautify_page = BeautifyPage(
            self.container,
            on_back=self._go_config,
            on_open_folder=self._open_output_folder,
            on_open_polished=self._open_polished_file,
        )
        self.beautify_page.grid(row=0, column=0, sticky="nsew")

        # 转写完成后的跳转逻辑（在 _drain 中）
        # 原来: set_buttons(finished=True) 后停留
        # 改为: set_buttons(finished=True) 后自动跳转美化页

    def _on_transcribe_done(self):
        """转写完成回调: 跳转到美化页"""
        self.beautify_page.set_result(
            meta=self._last_meta,          # 预取的元数据
            md_path=self._last_md_path,    # 生成的 MD 路径
            segment_count=self._last_count,
            elapsed=self._elapsed,
        )
        self.beautify_page.set_api_config(self._load_api_config())
        self._show(self.beautify_page)

    def _load_api_config(self) -> dict:
        """从 config.json 读取 API 配置"""
        config_path = xyz2md.BASE_DIR / "config.json"
        if config_path.exists():
            return json.loads(config_path.read_text(encoding="utf-8"))
        return {}

    def _save_api_config(self, config: dict):
        """保存 API 配置到 config.json"""
        config_path = xyz2md.BASE_DIR / "config.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2),
                               encoding="utf-8")
```

### 2.5 转写完成跳转时机

在 `App._drain()` 中，当 `self.worker` 结束且 `final_code == 0` 时：

```python
if self.worker and not self.worker.is_alive():
    self.worker = None
    if self.final_code == 0 and not self.stop_event.is_set():
        # 转写成功 → 跳转美化页
        self._on_transcribe_done()
    else:
        # 失败/停止 → 留在转写页
        self.progress_page.set_status(...)
        self.progress_page.set_buttons(...)
    return
```

需要在 `_run()` 中记录 `_last_meta`、`_last_md_path`、`_last_count`：

```python
def _run(self, cmd):
    # 预取 meta 时保存
    self._last_meta = meta
    # xyz2md.main 返回后, 从日志或返回值获取 md_path 和 count
    # 方案: 让 xyz2md.main 返回 (code, md_path, count) 元组
    # 或者: 从日志行解析 "Markdown: <path>" 和 "共 N 段"
```

**推荐方案**：修改 `xyz2md.main()` 的返回值，从 `int` 改为 `tuple[int, str, int]`（exit_code, md_path, segment_count）。但这会破坏 CLI 模式的 `sys.exit(code)`。

**更安全的方案**：不改 main 返回值，在 `_run()` 中从队列日志解析：

```python
# 在 _drain 中匹配日志行
m = re.search(r"Markdown:\s+(.+)", line)
if m:
    self._last_md_path = m.group(1).strip()
m = re.search(r"共 (\d+) 段", line)
if m:
    self._last_count = int(m.group(1))
```

---

## 3. LLM API 精修

### 3.1 支持的 API 服务商

所有服务商都兼容 OpenAI `/v1/chat/completions` 格式：

| 服务商 | Base URL | 推荐模型 | 参考价格 |
| --- | --- | --- | --- |
| DeepSeek | `https://api.deepseek.com` | `deepseek-chat` | ¥0.001/千token |
| MiniMax | `https://api.minimax.chat` | `abab6.5s-chat` | ¥0.014/千token |
| OpenAI | `https://api.openai.com` | `gpt-4o-mini` | $0.15/百万token |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode` | `qwen-turbo` | ¥0.008/千token |
| 自定义 | 用户自填 | 用户自填 | — |

### 3.2 API 调用核心函数

新建 `xyz2md_polish.py`（独立模块，方便测试和复用）：

```python
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文字稿精修: 调用 LLM API 加标点 + 纠错 + 统一简体"""

import json
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed


POLISH_PROMPT = """请给以下中文播客转录文本加上标点符号，修正明显的同音错别字，统一为简体中文，保持原意和口语风格不变。直接输出修改后的文本，不要加任何解释：

{text}"""

# 预设服务商
PROVIDERS = {
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "MiniMax": {"base_url": "https://api.minimax.chat/v1", "model": "abab6.5s-chat"},
    "OpenAI": {"base_url": "https://api.openai.com", "model": "gpt-4o-mini"},
    "通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode", "model": "qwen-turbo"},
    "自定义": {"base_url": "", "model": ""},
}


def call_llm(text: str, api_key: str, base_url: str, model: str,
             timeout: int = 60, retries: int = 2) -> str:
    """调用 LLM API 精修一段文本, 返回精修后的文本"""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": POLISH_PROMPT.format(text=text)}],
        "temperature": 0.1,
        "max_tokens": max(len(text) * 3, 500),
    }).encode("utf-8")

    url = base_url.rstrip("/") + "/chat/completions"
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=payload, headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
                return data["choices"][0]["message"]["content"].strip()
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 ** attempt)  # 指数退避
    raise RuntimeError(f"API 调用失败: {last_err}")


def polish_segments(segments: list[dict], api_key: str, base_url: str,
                    model: str, concurrency: int = 5,
                    progress_cb=None, log_cb=None) -> list[dict]:
    """并行精修多段文本

    Args:
        segments: [{"start": float, "end": float, "text": str}, ...]
        progress_cb: callback(current: int, total: int)
        log_cb: callback(message: str)
    Returns:
        精修后的 segments (text 字段被替换)
    """
    total = len(segments)
    results = [None] * total
    completed = 0

    def _process(idx, seg):
        polished = call_llm(seg["text"], api_key, base_url, model)
        return idx, polished

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_process, i, seg): i
                   for i, seg in enumerate(segments)}
        for future in as_completed(futures):
            idx = futures[future]
            try:
                _, polished_text = future.result()
                results[idx] = {**segments[idx], "text": polished_text}
                if log_cb:
                    log_cb(f"✅ 第 {idx+1}/{total} 段完成")
            except Exception as e:
                results[idx] = segments[idx]  # 失败保留原文
                if log_cb:
                    log_cb(f"❌ 第 {idx+1}/{total} 段失败: {e}")
            completed += 1
            if progress_cb:
                progress_cb(completed, total)

    return results


def read_segments_from_md(md_path: str) -> list[dict]:
    """从 MD 文件中解析出 segments 列表

    匹配格式: **[HH:MM:SS → HH:MM:SS]** text
    """
    pattern = re.compile(
        r"\*\*\[(\d+):(\d+):(\d+)\s*→\s*(\d+):(\d+):(\d+)\]\*\*\s*(.+)")
    segments = []
    with open(md_path, encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line.strip())
            if m:
                start = int(m.group(1))*3600 + int(m.group(2))*60 + int(m.group(3))
                end = int(m.group(4))*3600 + int(m.group(5))*60 + int(m.group(6))
                segments.append({"start": start, "end": end, "text": m.group(7)})
    return segments


def write_polished_md(original_md_path: str, polished_segments: list[dict],
                      output_path: str) -> str:
    """将精修结果写入新 MD 文件

    保留原 MD 的头部（元数据/简介/配图/关于播客），只替换文字稿部分。
    """
    with open(original_md_path, encoding="utf-8") as f:
        content = f.read()

    # 找到 "## 文字稿" 的位置
    marker = "## 文字稿"
    idx = content.find(marker)
    if idx == -1:
        # 没找到标记, 整个文件重写
        header = ""
    else:
        header = content[:idx + len(marker)] + "\n\n"

    # 重建文字稿部分
    lines = []
    for seg in polished_segments:
        start_ts = fmt_ts(seg["start"])
        end_ts = fmt_ts(seg["end"])
        lines.append(f"**[{start_ts} → {end_ts}]** {seg['text']}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))

    return output_path


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
```

### 3.3 精修流程（GUI 线程模型）

```
用户点「开始精修」
  → 读取 config.json 获取 API 配置
  → 保存配置到 config.json
  → 从 MD 文件解析 segments (read_segments_from_md)
  → 启动后台线程:
      → polish_segments(segments, ..., progress_cb, log_cb)
          → 每完成一段: queue.put(("polish_progress", (current, total)))
          → 每完成一段: queue.put(("polish_log", "✅ 第 N/M 段完成"))
      → 完成后: write_polished_md(original, polished, output_path)
      → queue.put(("polish_done", (success, output_path)))
  → 主线程 _drain 消费队列, 更新美化页 UI
```

### 3.4 队列消息协议扩展

在现有 `("log", ...)`, `("meta", ...)`, `("cover", ...)` 基础上新增：

```python
("polish_progress", (current: int, total: int))   # 精修进度
("polish_log", message: str)                        # 精修日志
("polish_done", (success: bool, output_path: str))  # 精修完成
```

### 3.5 API 配置存储

文件：`{BASE_DIR}/config.json`

```json
{
  "provider": "DeepSeek",
  "api_key": "sk-xxx...",
  "model": "deepseek-chat",
  "base_url": "https://api.deepseek.com"
}
```

- 选择预设服务商时自动填充 base_url 和 model
- 选择"自定义"时 base_url 和 model 输入框可编辑
- 点「保存」按钮写入 config.json
- 下次启动自动读取

### 3.6 精修输出

- 文件名：`{eid}_{title}_精修.md`（与原 MD 同目录）
- 内容：保留原 MD 头部（元数据/简介/配图/关于播客），文字稿部分替换为精修结果
- 原始 MD 不动

### 3.7 成本估算

2 小时节目 ≈ 300 段 ≈ 3-5 万 token：

| 服务商 | 费用 |
| --- | --- |
| DeepSeek | ¥0.03-0.05 |
| MiniMax | ¥0.5-0.7 |
| OpenAI (gpt-4o-mini) | ¥0.05-0.08 |

几乎可以忽略。

---

## 4. 文件改动范围

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `xyz2md.py` | 小改 | `write_seg()` 加 opencc 繁简转换 |
| `xyz2md_gui.py` | 中改 | 新增 `BeautifyPage` 类；`App` 加第三页 + 跳转逻辑 + 精修线程 |
| `xyz2md_polish.py` | **新建** | LLM API 精修核心逻辑（独立模块） |
| `requirements.txt` | 追加 | `opencc-python-reimplemented>=0.1.7` |
| `README.md` | 更新 | 美化功能说明 |
| `config.json` | 运行时生成 | API 配置（不入 git） |
| `.gitignore` | 追加 | `config.json` |

---

## 5. PyInstaller 打包变更

在原命令基础上加：

```powershell
--collect-data opencc
```

完整命令：

```powershell
.\.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name xyz2md `
  --workpath build --distpath dist --specpath build `
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime `
  --collect-all av --collect-all huggingface_hub --collect-all tokenizers `
  --collect-all customtkinter --collect-all PIL `
  --collect-data opencc `
  xyz2md_gui.py
```

预计 exe 体积增加 ~5MB（opencc 字典）。

---

## 6. 测试清单

| 测试 | 命令/操作 | 预期 |
| --- | --- | --- |
| 繁简转换 | 转写一段含繁体的音频 | MD 中无繁体字 |
| opencc 未安装 | 卸载 opencc 后运行 | 静默跳过，不崩溃 |
| 美化页跳转 | 转写完成后 | 自动跳到美化页，显示标题/统计 |
| 美化页返回 | 点「← 返回」 | 回到配置页 |
| API 配置保存 | 填写 key 点保存 | config.json 生成 |
| API 配置加载 | 重启程序 | 输入框自动填充上次配置 |
| 精修-DeepSeek | 配 DeepSeek key，点精修 | 逐段处理，进度条更新，生成 _精修.md |
| 精修-MiniMax | 配 MiniMax key，点精修 | 同上 |
| 精修-自定义 | 选自定义，填 base_url/model/key | 同上 |
| 精修-失败 | 填错误 key | 错误提示，保留原文，不崩溃 |
| 精修-超时 | 网络断开 | 重试 2 次后跳过该段，继续处理 |
| 精修-停止 | 精修中途点停止 | 已处理的段保留，未处理的保留原文 |
| 打开精修文档 | 精修完成后点按钮 | 打开 _精修.md |
| exe 冒烟 | `xyz2md.exe --smoke` | exit 0 |
| exe 完整 | `xyz2md.exe --auto <url> --limit-minutes 3 --model tiny` | exit 0 + 产出 |

---

## 7. 验收标准

- [ ] 转写完成后 MD 中的文字全部为简体中文（无繁体字）
- [ ] 转写完成后自动跳转到美化页，显示单集标题、播客名、段数、耗时
- [ ] 美化页显示「✅ 已自动转换为简体中文」状态
- [ ] 可选择服务商（DeepSeek / MiniMax / OpenAI / 通义千问 / 自定义）
- [ ] API Key 可保存到 config.json，下次启动自动加载
- [ ] 点「开始精修」后逐段调用 LLM，进度条实时更新
- [ ] 精修完成后生成 `{eid}_{title}_精修.md`，原始 MD 不变
- [ ] 精修后的文本有标点符号、无繁简混存、同音错别字减少
- [ ] API 调用失败时优雅降级（跳过该段，保留原文，不崩溃）
- [ ] 精修中途可停止，已处理段保留
- [ ] `--cli` / `--smoke` / `--auto` 三种隐藏模式正常工作
- [ ] onefile + onedir 两种 exe 均通过冒烟 + 完整链路测试

---

## 8. 实施顺序建议

1. **第一步**：装 opencc，改 `write_seg()` 加繁简转换，跑一次转写验证
2. **第二步**：新建 `xyz2md_polish.py`，实现 `call_llm` + `polish_segments` + `read_segments_from_md` + `write_polished_md`，用命令行单独测试
3. **第三步**：在 `xyz2md_gui.py` 中加 `BeautifyPage` 类 + 页面跳转逻辑
4. **第四步**：在美化页集成 API 精修按钮 + 进度显示 + config.json 读写
5. **第五步**：端到端测试（转写 → 自动繁简 → 美化页 → API 精修 → 精修 MD）
6. **第六步**：打包 exe + 发 Release

---

## 9. 给后续会话的提示

1. `opencc-python-reimplemented` 的 import 路径是 `from opencc import OpenCC`，跟原生 opencc 一样
2. PyInstaller 打包必须 `--collect-data opencc`，否则字典文件丢失，运行时静默跳过（不崩溃但也不转换）
3. API 调用用标准库 `urllib.request`，不要引入 `requests` / `openai` SDK（减少打包体积）
4. 精修线程用 `ThreadPoolExecutor` 并发 5 路，300 段约 2 分钟完成
5. `config.json` 存 API Key 是明文——本地工具可接受，但要加进 `.gitignore`
6. 精修 prompt 强调"保持原意和口语风格"，避免 LLM 过度改写
7. 精修失败的段保留原文（不丢数据），日志里标红提示
8. 美化页的「打开精修文档」按钮用 `os.startfile(path)` 打开默认编辑器
9. 当前仓库 remote 已配好（含嵌入凭据），直接 `git push` 即可
10. 本环境沙箱限制：onefile exe 需设 `$env:TMP` 到工作区；git push 用嵌入凭据的 remote URL；api.github.com 用 Python urllib（PowerShell/curl 的 TLS 栈在此环境不可靠）
