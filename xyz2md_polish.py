#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文字稿精修: 调用 LLM API 加标点 + 纠错 + 统一简体

所有服务商都兼容 OpenAI /v1/chat/completions 格式, 一套 urllib 代码通吃。
"""
import json
import re
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

POLISH_PROMPT = """请给以下中文播客转录文本加上标点符号，修正明显的同音错别字，统一为简体中文，
保持原意和口语风格不变。直接输出修改后的文本，不要加任何解释：

{text}"""

PROVIDERS = {
    "DeepSeek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat"},
    "MiniMax": {"base_url": "https://api.minimax.chat/v1", "model": "abab6.5s-chat"},
    "OpenAI": {"base_url": "https://api.openai.com", "model": "gpt-4o-mini"},
    "通义千问": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode",
              "model": "qwen-turbo"},
    "自定义": {"base_url": "", "model": ""},
}


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def call_llm(text: str, api_key: str, base_url: str, model: str,
             timeout: int = 60, retries: int = 2) -> str:
    """调用 LLM API 精修一段文本, 返回精修后的文本。失败抛异常。"""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": POLISH_PROMPT.format(text=text)}],
        "temperature": 0.1,
        "max_tokens": max(len(text) * 3, 500),
    }, ensure_ascii=False).encode("utf-8")

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
        except urllib.error.HTTPError as e:
            # 4xx 不重试
            if 400 <= e.code < 500 and e.code != 429:
                body = e.read().decode("utf-8", errors="replace")[:300]
                raise RuntimeError(f"API 错误 {e.code}: {body}") from e
            last_err = e
        except Exception as e:  # noqa: BLE001
            last_err = e
        if attempt < retries:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"API 调用失败 (重试 {retries} 次后): {last_err}")


def polish_segments(
    segments: list,
    api_key: str,
    base_url: str,
    model: str,
    concurrency: int = 5,
    progress_cb: Callable | None = None,
    log_cb: Callable | None = None,
    should_stop: Callable | None = None,
) -> list:
    """并行精修多段文本。失败段保留原文。

    Args:
        segments: [{start, end, text}, ...]
        progress_cb: 进度回调 fn(current, total)
        log_cb: 日志回调 fn(message)
        should_stop: 停止检查 fn() -> bool
    Returns:
        与输入等长的 segments (text 字段可能被替换)
    """
    total = len(segments)
    results: list = [None] * total
    completed = 0

    def _process(idx: int, seg: dict):
        polished = call_llm(seg["text"], api_key, base_url, model)
        return idx, polished

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_process, i, seg): i
                   for i, seg in enumerate(segments)}
        try:
            for future in as_completed(futures):
                if should_stop and should_stop():
                    if log_cb:
                        log_cb(">>> 用户停止, 取消未完成的任务")
                    for f in futures:
                        f.cancel()
                    break
                idx = futures[future]
                try:
                    _, polished_text = future.result()
                    results[idx] = {**segments[idx], "text": polished_text}
                    if log_cb:
                        log_cb(f"✅ 第 {idx+1}/{total} 段完成")
                except Exception as e:  # noqa: BLE001
                    results[idx] = segments[idx]
                    if log_cb:
                        log_cb(f"❌ 第 {idx+1}/{total} 段失败: {e}")
                completed += 1
                if progress_cb:
                    progress_cb(completed, total)
        except KeyboardInterrupt:
            for f in futures:
                f.cancel()
            raise

    # 用原文填充仍未处理的段 (用户停止后留下的)
    for i in range(total):
        if results[i] is None:
            results[i] = segments[i]
    return results


_SEG_PATTERN = re.compile(
    r"\*\*\[(\d+):(\d+):(\d+)\s*→\s*(\d+):(\d+):(\d+)\]\*\*\s*(.+)")


def read_segments_from_md(md_path: str) -> list:
    """从 MD 文件解析 [{start, end, text}, ...]。"""
    segments = []
    with open(md_path, encoding="utf-8") as f:
        for line in f:
            m = _SEG_PATTERN.match(line.strip())
            if m:
                start = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                end = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6))
                segments.append({"start": start, "end": end, "text": m.group(7)})
    return segments


def write_polished_md(original_md_path: str, polished_segments: list,
                      output_path: str) -> str:
    """保留原 MD 头部, 替换「## 文字稿」部分。"""
    with open(original_md_path, encoding="utf-8") as f:
        content = f.read()

    marker = "## 文字稿"
    idx = content.find(marker)
    if idx == -1:
        header = ""
    else:
        # 保留从文件开头到 "## 文字稿" 这一行 + 空行
        end_of_marker_line = content.find("\n", idx)
        # 跳过 ## 文字稿 后面的空行
        header_end = end_of_marker_line
        while header_end < len(content) and content[header_end] in "\n":
            header_end += 1
        header = content[:end_of_marker_line + 1] + "\n"

    lines = []
    for seg in polished_segments:
        lines.append(f"**[{fmt_ts(seg['start'])} → {fmt_ts(seg['end'])}]** "
                     f"{seg['text']}\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("\n".join(lines))
        if header and not header.endswith("\n"):
            f.write("\n")

    return output_path