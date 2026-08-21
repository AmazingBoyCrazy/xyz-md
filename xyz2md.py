#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小宇宙播客单集链接 → Markdown 文字稿转换工具

用法:
    python xyz2md.py https://www.xiaoyuzhoufm.com/episode/<eid> [选项]

流程:
    1. 抓取单集页面, 解析元数据(标题/播客/简介/时长/封面)和音频地址
    2. 下载音频 (m4a/mp3)
    3. 用 faster-whisper 本地转写(无需联网调用第三方 ASR)
    4. 生成 Markdown 文档(元数据 + 带时间戳的完整文字稿)

常用选项:
    --model small         Whisper 模型: tiny/base/small/medium/large-v3 (默认 small)
    --out DIR             输出目录 (默认 ./out)
    --no-audio            不下载音频(仅生成元数据)
    --limit-minutes N     只转写前 N 分钟(测试/快速预览用)
    --lang zh             指定语言(默认自动检测)
    --diarize             说话人识别, 文字稿标注 A:/B: (本地声纹, 需下载约 33MB 模型)
    --speakers N          说话人数 (0=自动估计, 默认 0; 双人对谈建议 2)
"""

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

# 程序基目录: 打包成 exe 后以 exe 所在目录为准(否则指向脚本目录)
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent

# Whisper 模型权重缓存: 默认放在程序目录 models/ 下(自包含, 不依赖系统缓存目录)
if not os.environ.get("HF_HOME") and not os.environ.get("HF_HUB_CACHE"):
    os.environ["HF_HOME"] = str(BASE_DIR / "models")

# 打包为窗口程序(--windowed)时没有控制台, 防止 print/tqdm 因 stdout/stderr 为 None 崩溃
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# 说话人识别 (diarization) 模型: sherpa-onnx 本地推理, CPU 友好
SHERPA_DIR = BASE_DIR / "models" / "sherpa-onnx"
DIARIZE_SEG_URL = ("https://huggingface.co/csukuangfj/"
                   "sherpa-onnx-pyannote-segmentation-3-0/resolve/main/model.onnx")
DIARIZE_EMB_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                   "speaker-recongition-models/"
                   "3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx")

LOG_HOOK = None  # 可由 GUI 注入, 接收格式化后的日志行


class StopTranscription(Exception):
    """用户主动停止转写"""


_opencc = None  # 延迟加载: opencc.OpenCC('t2s')


def _get_opencc():
    """返回 opencc 转换器实例, 未安装时返回 None (静默跳过)"""
    global _opencc
    if _opencc is not None:
        return _opencc
    try:
        from opencc import OpenCC
        _opencc = OpenCC("t2s")
    except ImportError:
        _opencc = False
    return _opencc

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PAGE_URL = "https://www.xiaoyuzhoufm.com/episode/{eid}"
EPISODE_RE = re.compile(r"/episode/([0-9a-fA-F]{16,40})")
DUR_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
INVALID_FS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F]")


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:  # noqa: BLE001
        pass
    if LOG_HOOK:
        try:
            LOG_HOOK(line)
        except Exception:  # noqa: BLE001
            pass


def fetch(url: str, timeout: int = 60, referer: str = "https://www.xiaoyuzhoufm.com/") -> bytes:
    """带 UA/Referer 抓取, 失败重试一次。"""
    last_err = None
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": referer,
            })
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001
            last_err = e
            log(f"  抓取失败(第{attempt}次): {e}")
            time.sleep(2)
    raise RuntimeError(f"抓取失败: {last_err}")


def fetch_stream(url: str, dest: Path, chunk_size: int = 1 << 20,
                 chunk_timeout: int = 30, retries: int = 2,
                 progress_cb=None, referer: str = "https://www.xiaoyuzhoufm.com/") -> None:
    """流式下载到文件: 每个 chunk 独立超时, 校验 Content-Length, 失败重试。

    Args:
        url: 下载地址
        dest: 目标文件路径
        chunk_size: 每次读取字节数 (默认 1MB)
        chunk_timeout: 单个 chunk 读取超时 (秒)
        retries: 失败重试次数
        progress_cb: callback(received_bytes: int, total_bytes: int | None)
    """
    last_err = None
    for attempt in range(1, retries + 2):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA,
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Referer": referer,
            })
            with urllib.request.urlopen(req, timeout=chunk_timeout) as resp:
                total = resp.headers.get("Content-Length")
                total = int(total) if total else None
                received = 0
                if progress_cb:
                    progress_cb(0, total)
                with dest.open("wb") as f:
                    while True:
                        try:
                            chunk = resp.read(chunk_size)
                        except socket.timeout as e:
                            raise RuntimeError(
                                f"读取超时 (>{chunk_timeout}s 无数据, 已收 {received/1024/1024:.1f} MB)") from e
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)
                        if progress_cb and received % (5 * chunk_size) < chunk_size:
                            progress_cb(received, total)
                if progress_cb:
                    progress_cb(received, total)
                # 校验 Content-Length
                if total is not None and received != total:
                    raise RuntimeError(
                        f"下载不完整: 期望 {total/1024/1024:.1f} MB, 实际 {received/1024/1024:.1f} MB")
                return
        except Exception as e:  # noqa: BLE001
            last_err = e
            # 清理半截文件
            try:
                dest.unlink(missing_ok=True)
            except OSError:
                pass
            if attempt <= retries:
                wait = 2 ** attempt
                log(f"  下载失败(第{attempt}次, {wait}s 后重试): {e}")
                time.sleep(wait)
            else:
                break
    raise RuntimeError(f"下载失败 (重试 {retries} 次后): {last_err}")


def get_meta(html: str, prop: str) -> str | None:
    """提取 <meta property="..." content="..."/> (兼容属性顺序颠倒)。"""
    for pattern in (
        rf'<meta[^>]+property="{re.escape(prop)}"[^>]+content="([^"]*)"',
        rf'<meta[^>]+content="([^"]*)"[^>]+property="{re.escape(prop)}"',
    ):
        m = re.search(pattern, html, re.S)
        if m:
            return m.group(1)
    return None


class _SnContentImages(HTMLParser):
    """精确提取 class 含 sn-content 的 shownotes 容器内的 <img src>。

    只收集容器自身的图片, 不会把评论区头像等容器外的图混进来。
    """

    def __init__(self) -> None:
        super().__init__()
        self.in_container = False
        self.depth = 0
        self.images: list = []

    def handle_starttag(self, tag, attrs) -> None:  # noqa: D102
        d = dict(attrs)
        if tag == "div" and not self.in_container \
                and "sn-content" in d.get("class", ""):
            self.in_container = True
            self.depth = 1
            return
        if self.in_container:
            if tag == "div":
                self.depth += 1
            elif tag == "img":
                src = d.get("src")
                if src:
                    self.images.append(src)

    def handle_endtag(self, tag) -> None:  # noqa: D102
        if self.in_container and tag == "div":
            self.depth -= 1
            if self.depth <= 0:
                self.in_container = False


def parse_timeline(desc: str) -> list:
    """从简介的时间轴章节解析 [(秒, 标题), ...]。

    时间轴形如:
        🎯时间轴
        00:21 一些感慨
        04:18 出书魔咒
        01:04:48 Mapper 拆解和排序：
        02:19:06 5 组相关性：...
        📁 本期内容相关资料   <- 遇到下一个 emoji 分节标记停止
    """
    entries: list = []
    started = False
    for raw in desc.split("\n"):
        line = raw.strip()
        if not started:
            if "时间轴" in line:
                started = True
            continue
        m = re.match(r"^(\d{1,2}:\d{2}(?::\d{2})?)\s*(.*)$", line)
        if m:
            t, title = m.group(1), m.group(2).strip()
            parts = t.split(":")
            if len(parts) == 3:
                sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            else:
                sec = int(parts[0]) * 60 + int(parts[1])
            entries.append((sec, title))
        elif line and not re.match(r"^[\d\u4e00-\u9fffA-Za-z（(]", line) \
                and EMOJI_RE.match(line):
            break  # 下一个 emoji 分节标记, 时间轴结束
    # 按时间去重排序
    seen: dict = {}
    for sec, title in entries:
        seen.setdefault(sec, title)
    return sorted(seen.items())


def parse_page(html: str) -> dict:
    """从单集页面 HTML 提取元数据。"""
    # 标题: "{单集名} - {播客名} | 小宇宙 - 听播客，上小宇宙"
    m = re.search(r"<title>([^<]*)</title>", html)
    title = m.group(1).strip() if m else ""
    episode_title, podcast = title, ""
    if " | 小宇宙" in title:
        before = title.split(" | 小宇宙")[0]
        if " - " in before:
            episode_title, podcast = before.rsplit(" - ", 1)

    # JSON-LD (含完整简介/发布时间/时长)
    info: dict = {}
    m = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            info = json.loads(m.group(1))
            if not isinstance(info, dict):
                info = {}
        except json.JSONDecodeError:
            info = {}

    duration_sec = 0
    if info.get("timeRequired"):
        d = DUR_RE.match(str(info["timeRequired"]))
        if d:
            h, mi, s = (int(x) if x else 0 for x in d.groups())
            duration_sec = h * 3600 + mi * 60 + s

    description = info.get("description", "") or get_meta(html, "og:description") or ""

    # 播客 ID (从页面里的 /podcast/xxx 链接)
    pm = re.search(r'href="/podcast/([0-9a-zA-Z]+)"', html)
    podcast_id = pm.group(1) if pm else ""

    # shownotes 富文本容器 (class 含 sn-content) 内的配图, 排除头像/评论图
    img_parser = _SnContentImages()
    img_parser.feed(html)
    show_images = list(dict.fromkeys(img_parser.images))

    return {
        "episode_title": episode_title or info.get("name", ""),
        "podcast": podcast,
        "description": description,
        "date_published": info.get("datePublished", ""),
        "duration_sec": duration_sec,
        "cover": get_meta(html, "og:image") or "",
        "audio_url": get_meta(html, "og:audio") or "",
        "page_url": get_meta(html, "og:url")
        or (re.search(r'<link[^>]+rel="canonical"[^>]+href="([^"]*)"', html) or [None, ""])[1],
        "podcast_id": podcast_id,
        "show_images": show_images,
        "timeline": parse_timeline(description),
    }


def fetch_podcast_info(podcast_id: str) -> dict:
    """抓取播客页 JSON-LD, 返回 {name, author, description}。失败返回空 dict。"""
    url = f"https://www.xiaoyuzhoufm.com/podcast/{podcast_id}"
    try:
        data = fetch(url).decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        log(f"  获取播客信息失败: {e}")
        return {}
    m = re.search(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', data, re.S)
    if not m:
        return {}
    try:
        d = json.loads(m.group(1))
    except json.JSONDecodeError:
        return {}
    if not isinstance(d, dict):
        return {}
    author = d.get("author") or ""
    if isinstance(author, dict):
        author = author.get("name", "")
    return {
        "name": d.get("name") or "",
        "author": author or "",
        "description": d.get("description") or "",
    }


def fmt_duration(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h} 小时 {m} 分"
    if m:
        return f"{m} 分 {s} 秒"
    return f"{s} 秒"


def fmt_ts(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def slug(name: str, max_len: int = 60) -> str:
    name = INVALID_FS.sub("_", name).strip().strip(".")
    return name[:max_len] or "episode"


def download_audio(url: str, dest: Path) -> None:
    """下载音频 (流式 + Content-Length 校验 + 超时重试)"""
    log(f"下载音频: {url}")

    def _on_progress(received: int, total: int | None):
        if total:
            log(f"  下载中: {received/1024/1024:.1f} / {total/1024/1024:.1f} MB "
                f"({received*100/total:.0f}%)")
        else:
            log(f"  下载中: {received/1024/1024:.1f} MB")

    fetch_stream(url, dest, progress_cb=_on_progress,
                 referer="https://www.xiaoyuzhoufm.com/")
    size_mb = dest.stat().st_size / 1024 / 1024
    log(f"音频已保存: {dest} ({size_mb:.1f} MB)")


def ensure_diarize_models() -> tuple[str, str]:
    """确保说话人识别模型存在, 缺失则下载。返回 (分割模型路径, 声纹模型路径)。"""
    seg_model = SHERPA_DIR / "pyannote-segmentation" / "model.onnx"
    emb_model = SHERPA_DIR / "campplus-zh" / "model.onnx"

    if not seg_model.exists():
        seg_model.parent.mkdir(parents=True, exist_ok=True)
        url = DIARIZE_SEG_URL
        # 国内网络可通过 HF_ENDPOINT 环境变量走镜像 (同 Whisper 模型下载)
        endpoint = os.environ.get("HF_ENDPOINT")
        if endpoint:
            url = url.replace("https://huggingface.co", endpoint.rstrip("/"))
        log(f"下载说话人分割模型 (约 6MB): {url}")
        fetch_stream(url, seg_model)

    if not emb_model.exists():
        emb_model.parent.mkdir(parents=True, exist_ok=True)
        log(f"下载说话人声纹模型 (约 27MB): {DIARIZE_EMB_URL}")
        fetch_stream(DIARIZE_EMB_URL, emb_model)

    return str(seg_model), str(emb_model)


def run_speaker_diarization(samples, num_speakers: int = 0) -> list:
    """对 16kHz float32 音频跑说话人分离。

    Returns:
        [(start, end, speaker_id), ...] 按时间排序
    """
    import sherpa_onnx  # 延迟导入, 未启用说话人识别时不需要

    seg_model, emb_model = ensure_diarize_models()
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=seg_model)),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=emb_model, num_threads=max(2, (os.cpu_count() or 4) // 2)),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers, threshold=0.8),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    sd = sherpa_onnx.OfflineSpeakerDiarization(config)
    log(f"说话人分离中 ({len(samples)/16000/60:.0f} 分钟音频, 本地声纹聚类)...")
    result = sd.process(samples)
    turns = [(t.start, t.end, t.speaker)
             for t in result.sort_by_start_time()]
    # 聚类 id 可能稀疏 (如自动估计出 3 类但只有 2 类分到词), 按首次出现紧凑化为 0,1,2...
    remap: dict = {}
    for _, _, sp in turns:
        if sp not in remap:
            remap[sp] = len(remap)
    turns = [(s, e, remap[sp]) for s, e, sp in turns]
    n_speakers = len(remap)
    log(f"说话人分离完成: {n_speakers} 位说话人, {len(turns)} 个发言片段")
    return turns


def _word_speaker(mid: float, turns: list) -> int | None:
    """词中点时间 → 说话人。优先包含关系, 其次最大重叠, 最后归属最近的轮次。"""
    for ts, te, sp in turns:
        if ts <= mid <= te:
            return sp
    best, best_ov = None, 0.0
    for ts, te, sp in turns:
        ov = min(mid + 0.5, te) - max(mid - 0.5, ts)
        if ov > best_ov:
            best, best_ov = sp, ov
    if best is not None:
        return best
    # 落在轮次间隙的词: 归属时间上最近的轮次, 避免丢文本
    if not turns:
        return None
    nearest = min(turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1])))
    return nearest[2]


def merge_speaker_blocks(word_segments_iter, turns: list):
    """词级转写结果 × 说话人轮次 → 按说话人合并的块 (流式生成)。

    每个块是一个带 start/end/text 的对象, text 形如 "A: 我们从哪说起呢..."，
    与普通 whisper segment 接口兼容, write_markdown 无需感知。
    同一说话人连续发言超过 _MAX_BLOCK_CHARS 时在句末标点处软切分,
    避免后续 LLM 精修单段过长被截断。

    碎块平滑: 声纹轮次边界有 ±0.5s 抖动, 边界处的词会被误归属,
    产生 "A: 看" / "C: 起来" 这类被切断的碎块。连续碎块拼接回一个词,
    并与后续同说话人块合并。
    """
    _MAX_BLOCK_CHARS = 1000
    _TINY_CHARS = 4      # 碎块文本长度上限 (不含 "A: " 前缀)
    _TINY_DUR = 1.5      # 碎块时长上限 (秒)
    _TINY_GAP = 0.6      # 碎块与相邻块可拼接的时间间隔上限 (秒)

    def _raw():
        cur = None  # [start, end, speaker, text_parts]

        def _flush():
            if cur is not None:
                label = chr(ord("A") + cur[2])
                return SimpleNamespace(start=cur[0], end=cur[1],
                                       text=f"{label}: {cur[3]}".strip())
            return None

        for seg in word_segments_iter:
            words = getattr(seg, "words", None)
            if not words:
                # 无词级时间戳的兜底: 整段按段中点定说话人
                text = re.sub(r"\s+", " ", seg.text or "").strip()
                if not text:
                    continue
                mid = (seg.start + seg.end) / 2
                sp = _word_speaker(mid, turns)
                if sp is None:
                    continue
                if cur and sp == cur[2]:
                    cur[1] = seg.end
                    cur[3] += text
                else:
                    block = _flush()
                    if block is not None:
                        yield block
                    cur = [seg.start, seg.end, sp, text]
                continue
            for w in words:
                text = (w.word or "").strip()
                if not text:
                    continue
                sp = _word_speaker((w.start + w.end) / 2, turns)
                if sp is None:
                    continue
                if cur and sp == cur[2]:
                    cur[1] = w.end
                    cur[3] += text
                    # 超长块软切分: 句末标点处断开; 转录常无标点, 超硬阈值强制断
                    if len(cur[3]) >= _MAX_BLOCK_CHARS:
                        if text[-1] in "。！？!?，,、；;" or len(cur[3]) >= _MAX_BLOCK_CHARS * 2:
                            block = _flush()
                            if block is not None:
                                yield block
                            cur = None
                else:
                    block = _flush()
                    if block is not None:
                        yield block
                    cur = [w.start, w.end, sp, text]
        block = _flush()
        if block is not None:
            yield block

    # ---- 碎块平滑 (流式, 只需 lookahead 一个块) ----
    pending = None
    for blk in _raw():
        is_tiny = (len(blk.text) <= _TINY_CHARS + 3
                   and blk.end - blk.start < _TINY_DUR)
        if is_tiny:
            if pending is not None and blk.start - pending.end < _TINY_GAP:
                # 连续碎块: 拼回被边界抖动切断的词, 说话人取先出现的
                pending = SimpleNamespace(
                    start=pending.start, end=blk.end,
                    text=pending.text[:3] + pending.text[3:] + blk.text[3:])
            else:
                if pending is not None:
                    yield pending
                pending = blk
            continue
        if pending is not None:
            if (blk.start - pending.end < _TINY_GAP
                    and blk.text[:2] == pending.text[:2]):
                # 碎块属于后续同说话人块的开头 (被切断的词)
                blk = SimpleNamespace(
                    start=pending.start, end=blk.end,
                    text=blk.text[:3] + pending.text[3:] + blk.text[3:])
            else:
                yield pending
            pending = None
        yield blk
    if pending is not None:
        yield pending


def transcribe(audio_path: Path, model_size: str, lang: str | None,
               limit_min: float | None, condition: bool,
               diarize: bool = False, num_speakers: int = 0):
    """用 faster-whisper 转写, 逐段产出 (start, end, text)。

    优先使用 BatchedInferencePipeline: 标准 WhisperModel.transcribe 会把
    VAD 后的语音拼回一整段再算 STFT, 长音频会占数 GB 内存; 分批管道逐
    块(30s)计算特征, 内存有界。

    diarize=True 时先用 sherpa-onnx 做本地说话人分离, 再用词级时间戳把
    转写文本切成 "A: xxx" 说话人块。
    """
    from faster_whisper import WhisperModel  # 延迟导入, 元数据模式不需要

    log(f"加载 Whisper 模型 {model_size} (首次运行会自动下载模型权重)...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    try:
        from faster_whisper.transcribe import BatchedInferencePipeline
        engine = BatchedInferencePipeline(model)
        log("使用分批转写引擎 (内存友好)")
    except ImportError:
        engine = model

    kw = dict(vad_filter=True, beam_size=5,
              condition_on_previous_text=condition)
    if lang:
        kw["language"] = lang

    turns = []
    audio = None
    if diarize:
        from faster_whisper import decode_audio
        audio = decode_audio(str(audio_path), sampling_rate=16000)
        if limit_min:
            audio = audio[: int(limit_min * 60 * 16000)]
            log(f"仅转写前 {limit_min} 分钟")
        # 说话人分离失败不致命: 降级为普通转写
        try:
            turns = run_speaker_diarization(audio, num_speakers)
        except Exception as e:  # noqa: BLE001
            import traceback
            log(f"⚠️ 说话人分离失败, 降级为普通转写: {e!r}")
            log(f"  traceback: {traceback.format_exc()}")
        if not turns:
            log("⚠️ 说话人分离无结果, 已降级为普通转写")
            audio = None if limit_min is None else audio  # 无限时释放全量数组

    if turns:
        segments, info = engine.transcribe(audio, word_timestamps=True, **kw)
        segments = merge_speaker_blocks(segments, turns)
    elif limit_min:
        # 只转写前 N 分钟: 解码为 16kHz 数组后截取
        if audio is None:
            from faster_whisper import decode_audio
            audio = decode_audio(str(audio_path), sampling_rate=16000)
            audio = audio[: int(limit_min * 60 * 16000)]
            log(f"仅转写前 {limit_min} 分钟")
        segments, info = engine.transcribe(audio, **kw)
    else:
        segments, info = engine.transcribe(str(audio_path), **kw)

    log(f"检测语言: {info.language} (置信度 {info.language_probability:.2f}), "
        f"音频时长 {info.duration:.0f}s")
    return segments, bool(turns)


def write_markdown(meta: dict, segments_iter, md_path: Path,
                   audio_name: str | None, podcast_info: dict | None = None,
                   stop_check=None, diarize: bool = False) -> int:
    lines = [
        f"# {meta['episode_title']}",
        "",
    ]
    if meta.get("podcast"):
        lines.append(f"- **播客**：{meta['podcast']}")
    if meta.get("date_published"):
        dp = meta["date_published"].split("T")[0]
        lines.append(f"- **发布时间**：{dp}")
    if meta.get("duration_sec"):
        lines.append(f"- **时长**：{fmt_duration(meta['duration_sec'])}")
    if meta.get("page_url"):
        lines.append(f"- **单集链接**：{meta['page_url']}")
    if meta.get("podcast_id"):
        lines.append(f"- **播客链接**：https://www.xiaoyuzhoufm.com/podcast/{meta['podcast_id']}")
    if audio_name:
        lines.append(f"- **音频文件**：{audio_name}")
    if diarize:
        lines.append("- **说话人标注**：A/B 由本地声纹自动识别（约 90% 准确率）")
    if meta.get("cover"):
        lines.append("")
        lines.append(f"![封面]({meta['cover']})")

    # 简介 + 节目配图
    lines += ["", "---", "", "## 简介", "", meta["description"] or "（无简介）"]
    show_images = meta.get("show_images") or []
    if show_images:
        lines += ["", "### 节目配图", ""]
        for i, u in enumerate(show_images, 1):
            lines.append(f"![配图 {i}]({u})")

    # 关于播客
    if podcast_info and podcast_info.get("description"):
        lines += ["", "---", "", "## 关于播客", ""]
        if podcast_info.get("name"):
            lines.append(f"- **播客**：{podcast_info['name']}")
        if podcast_info.get("author"):
            lines.append(f"- **主播/作者**：{podcast_info['author']}")
        lines += ["", podcast_info["description"]]

    lines += ["", "---", "", "## 文字稿", ""]

    def write_seg(f, seg) -> None:
        text = re.sub(r"\s+", " ", seg.text or "").strip()
        if not text:
            return
        # 繁简转换 (opencc 未安装时静默跳过)
        cc = _get_opencc()
        if cc:
            text = cc.convert(text)
        f.write(f"**[{fmt_ts(seg.start)} → {fmt_ts(seg.end)}]** {text}\n\n")

    timeline = meta.get("timeline") or []
    ch = 0
    count = 0

    # 第一步: 立即写入 header, 即使后面转写挂了也能保留元数据
    try:
        with md_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.write("\n")
            f.flush()
        log(f"  MD header 已写入: {md_path}")
    except Exception as e:  # noqa: BLE001
        log(f"  写入 MD header 失败: {e}")
        raise

    # 第二步: 追加转写内容 (「## 文字稿」标题已在 header 中写入)
    try:
        with md_path.open("a", encoding="utf-8") as f:
            it = iter(segments_iter)
            first = next(it, None)
            if first is not None:
                if stop_check is not None and stop_check():
                    raise StopTranscription()
                # 首个分段之前的时间轴章节
                while ch < len(timeline) and timeline[ch][0] <= first.start:
                    f.write(f"### {fmt_ts(timeline[ch][0])} {timeline[ch][1]}\n\n")
                    ch += 1
                write_seg(f, first)
                count += 1
                for seg in it:
                    if stop_check is not None and stop_check():
                        raise StopTranscription()
                    while ch < len(timeline) and timeline[ch][0] <= seg.start:
                        f.write(f"### {fmt_ts(timeline[ch][0])} {timeline[ch][1]}\n\n")
                        ch += 1
                    write_seg(f, seg)
                    count += 1
                    if count % 20 == 0:
                        log(f"  已转写 {count} 段, 进度到 {fmt_ts(seg.end)} / 共 {fmt_duration(meta['duration_sec'])}")
    except StopTranscription:
        raise
    except Exception as e:  # noqa: BLE001
        import traceback
        log(f"  转写过程异常: {e!r}")
        log(f"  traceback: {traceback.format_exc()}")
        # 不抛出, 保留已写入的部分
    return count


def main(argv: list | None = None, stop_check=None) -> int:
    ap = argparse.ArgumentParser(description="小宇宙播客单集 → Markdown 文字稿")
    ap.add_argument("url", help="单集链接, 如 https://www.xiaoyuzhoufm.com/episode/<eid>")
    ap.add_argument("--model", default="small",
                    choices=["tiny", "base", "small", "medium", "large-v3"],
                    help="Whisper 模型 (默认 small; 中文质量 small 起步)")
    ap.add_argument("--out", default=str(BASE_DIR / "out"),
                    help=f"输出目录 (默认 {BASE_DIR / 'out'})")
    ap.add_argument("--no-audio", action="store_true", help="不下载音频, 仅生成元数据")
    ap.add_argument("--limit-minutes", type=float, default=None,
                    help="只转写前 N 分钟")
    ap.add_argument("--lang", default=None, help="指定音频语言, 如 zh (默认自动检测)")
    ap.add_argument("--condition", action="store_true",
                    help="允许模型参考前文(长音频可能产生重复文本, 默认关闭)")
    ap.add_argument("--diarize", action="store_true",
                    help="说话人识别: 本地声纹分离, 文字稿标注 A:/B: (首次运行下载约 33MB 模型)")
    ap.add_argument("--speakers", type=int, default=0,
                    help="说话人数 (0=自动估计, 默认 0; 双人对谈建议 2)")
    args = ap.parse_args(argv)

    m = EPISODE_RE.search(args.url)
    if not m:
        log(f"无法从链接中解析单集 ID: {args.url}")
        log("链接格式应为 https://www.xiaoyuzhoufm.com/episode/<eid>")
        return 2
    eid = m.group(1)
    page_url = PAGE_URL.format(eid=eid)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"抓取单集页面: {page_url}")
    html = fetch(page_url).decode("utf-8", errors="replace")
    meta = parse_page(html)

    if not meta["audio_url"]:
        log("未找到音频地址(og:audio)。该单集可能需要登录或已被删除。")
        return 1
    if not meta["episode_title"]:
        log("未能解析出单集标题, 页面结构可能已变化。")
        return 1

    log(f"单集: {meta['episode_title']}  |  播客: {meta['podcast'] or '未知'}")

    # 播客信息 (失败不影响主流程)
    podcast_info = {}
    if meta.get("podcast_id"):
        podcast_info = fetch_podcast_info(meta["podcast_id"])
        if podcast_info.get("name"):
            log(f"播客信息: {podcast_info['name']}")

    audio_path = None
    audio_name = None
    if not args.no_audio:
        ext = Path(meta["audio_url"].split("?")[0]).suffix or ".m4a"
        audio_path = out_dir / f"{eid}{ext}"
        download_audio(meta["audio_url"], audio_path)
        audio_name = audio_path.name

    # 元数据 JSON (失败不影响转写)
    try:
        (out_dir / f"{eid}.json").write_text(
            json.dumps({**meta, "eid": eid}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        log(f"  写入元数据 JSON 失败(忽略): {e}")

    md_path = out_dir / f"{eid}_{slug(meta['episode_title'])}.md"

    if audio_path is None:
        log(f"未下载音频, 仅生成元数据: {md_path}")
        write_markdown(meta, iter(()), md_path, None, podcast_info, stop_check)
        log("✅ 完成 (元数据模式)")
        log(f"Markdown: {md_path}")
        return 0

    try:
        segments, diarize_applied = transcribe(
            audio_path, args.model, args.lang,
            args.limit_minutes, args.condition,
            diarize=args.diarize, num_speakers=args.speakers)
        count = write_markdown(meta, segments, md_path, audio_name,
                               podcast_info, stop_check,
                               diarize=diarize_applied)
        log(f"✅ 完成! 共 {count} 段文字稿")
        log(f"Markdown: {md_path}")
        if audio_name:
            log(f"音频: {audio_path}")
        return 0
    except StopTranscription:
        log("已停止, 已转写的部分保存在文件中")
        with md_path.open("a", encoding="utf-8") as f:
            f.write("\n> ⚠️ 转写被用户停止, 以上为部分文字稿\n")
        return 130
    except KeyboardInterrupt:
        log("收到中断信号, 已转写的部分已保存在文件中")
        with md_path.open("a", encoding="utf-8") as f:
            f.write("\n> ⚠️ 转写被中断, 以上为部分文字稿\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
