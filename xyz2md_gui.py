#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小宇宙播客 → Markdown 文字稿 (CustomTkinter 现代化界面)

打包成 exe 后双击即用; 也可用命令行模式:
    xyz2md.exe --cli <单集链接> [选项]

隐藏测试参数:
    --smoke            构建窗口后 2 秒自动关闭
    --auto <url> ...   自动开始转换, 完成后自动关闭
"""
import queue
import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

import xyz2md

# ---------- 命令行模式(打包后: xyz2md.exe --cli <url> [选项]) ----------
if "--cli" in sys.argv:
    rest = [a for a in sys.argv[1:] if a != "--cli"]
    sys.exit(xyz2md.main(rest))

MODELS = [
    ("small", "small (推荐, 内存约1GB)"),
    ("tiny", "tiny (最快, 精度低)"),
    ("base", "base (较快, 精度一般)"),
    ("medium", "medium (更准, 内存约2.5GB, 慢)"),
    ("large-v3", "large-v3 (最准, 内存5GB+, 很慢)"),
]


def _parse_human_duration(s: str) -> int:
    import re
    sec = 0
    m = re.search(r"(\d+)\s*小时", s)
    if m: sec += int(m.group(1)) * 3600
    m = re.search(r"(\d+)\s*分(?:钟)?", s)
    if m: sec += int(m.group(1)) * 60
    m = re.search(r"(\d+)\s*秒", s)
    if m: sec += int(m.group(1))
    return sec


def fmt_dur_short(sec: float) -> str:
    sec = int(sec)
    if sec < 60: return f"{sec}s"
    if sec < 3600: return f"{sec//60}m{sec%60}s"
    return f"{sec//3600}h{sec%3600//60}m"


class App(ctk.CTk):
    """主窗口: 配置页 ↔ 转写页 双页切换"""

    def __init__(self) -> None:
        super().__init__()
        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("blue")

        self.title("小宇宙播客 → Markdown 文字稿")
        self.geometry("780x640")
        self.minsize(640, 540)

        self.q: "queue.Queue" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.final_code = 0

        self.container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.container.pack(fill="both", expand=True)
        self.container.grid_rowconfigure(0, weight=1)
        self.container.grid_columnconfigure(0, weight=1)

        self.config_page = ConfigPage(self.container, on_start=self._start_convert)
        self.progress_page = ProgressPage(self.container, on_back=self._go_config)

        self.config_page.grid(row=0, column=0, sticky="nsew")
        self.progress_page.grid(row=0, column=0, sticky="nsew")

        self.progress_page.bind_app(self)
        self._show(self.config_page)

    def _show(self, page) -> None:
        page.tkraise()

    def _go_config(self) -> None:
        self._show(self.config_page)

    def _start_convert(self) -> None:
        url, model, out_dir, limit, lang, no_audio = self.config_page.collect_args()
        if not url:
            messagebox.showwarning("提示", "请先粘贴小宇宙单集链接")
            return
        if not xyz2md.EPISODE_RE.search(url):
            messagebox.showwarning("提示",
                                   "链接格式不对, 应为:\n"
                                   "https://www.xiaoyuzhoufm.com/episode/<eid>")
            return

        cmd = [url, "--model", model, "--out", out_dir]
        if limit:
            cmd += ["--limit-minutes", limit]
        if lang:
            cmd += ["--lang", lang]
        if no_audio:
            cmd += ["--no-audio"]

        self.stop_event.clear()
        self.final_code = 0
        self.progress_page._reset_for_new_run()
        self.progress_page.set_status("准备中...")
        self._show(self.progress_page)

        xyz2md.LOG_HOOK = lambda line: self.q.put(("log", line + "\n"))  # noqa: E731

        self.worker = threading.Thread(target=self._run, args=(cmd,), daemon=True)
        self.worker.start()
        self.after(80, self._drain)

    def _run(self, cmd: list) -> None:
        try:
            # 1) 预取元数据(标题/封面/时长等), 立刻推到主线程更新 UI
            url = cmd[0]
            try:
                html = xyz2md.fetch(url).decode("utf-8", errors="replace")
                meta = xyz2md.parse_page(html)
                self.q.put(("meta", meta))
                self.q.put(("cover", self._load_cover(meta.get("cover", ""))))
                self.q.put(("log", f"[预取] 已加载元数据\n"))
            except Exception as e:  # noqa: BLE001
                self.q.put(("meta", {"episode_title": "（元数据获取失败）"}))
                self.q.put(("cover", None))
                self.q.put(("log", f"[预取] 失败: {e}\n"))

            # 2) 进入正式转换流程
            self.final_code = xyz2md.main(
                cmd, stop_check=self.stop_event.is_set)
        except Exception as e:  # noqa: BLE001
            self.final_code = 1
            self.q.put(("log", f"\n[ERROR] {e!r}\n"))

    def _load_cover(self, url: str):
        """后台线程: 下载封面并转成 CTkImage; 失败返回 None"""
        if not url:
            return None
        try:
            from PIL import Image
            import io
            data = xyz2md.fetch(url, timeout=15).read()
            img = Image.open(io.BytesIO(data)).convert("RGBA")
            img.thumbnail((128, 128))
            return ctk.CTkImage(light_image=img, dark_image=img, size=(72, 72))
        except Exception:
            return None

    def stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self.q.put(("log", "\n>>> 正在停止... (等待当前片段转写完成)\n"))
            self.progress_page.set_buttons(running=True, stopping=True)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.progress_page.append_log(payload)
                    self.progress_page.apply_progress_line(payload)
                elif kind == "meta":
                    self.progress_page.set_meta(payload)
                elif kind == "cover":
                    self.progress_page.set_cover(payload)
        except queue.Empty:
            pass

        if self.worker and not self.worker.is_alive():
            self.worker = None
            self.progress_page.set_status(
                "✅ 完成" if self.final_code == 0 and not self.stop_event.is_set()
                else ("⏹ 已停止" if self.stop_event.is_set() else "❌ 失败"))
            self.progress_page.set_buttons(running=False, stopping=False,
                                           finished=True)
            # --auto 模式下自动销毁窗口
            if "--auto" in sys.argv:
                self.after(500, self.destroy)
            return
        self.after(80, self._drain)


class ConfigPage(ctk.CTkFrame):
    def __init__(self, master, on_start) -> None:
        super().__init__(master, corner_radius=0, fg_color="transparent")
        self.on_start = on_start

        ctk.CTkLabel(self, text="🎙️ 小宇宙播客 → Markdown 文字稿",
                     font=ctk.CTkFont(size=22, weight="bold")).pack(
            anchor="w", padx=28, pady=(28, 18))

        card_link = ctk.CTkFrame(self, corner_radius=12)
        card_link.pack(fill="x", padx=28, pady=(0, 14))
        ctk.CTkLabel(card_link, text="单集链接", font=ctk.CTkFont(weight="bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        self.url_var = ctk.StringVar()
        self.url_entry = ctk.CTkEntry(
            card_link, textvariable=self.url_var, height=38,
            placeholder_text="https://www.xiaoyuzhoufm.com/episode/<eid>")
        self.url_entry.pack(fill="x", padx=16, pady=(0, 12))

        card_out = ctk.CTkFrame(self, corner_radius=12)
        card_out.pack(fill="x", padx=28, pady=(0, 14))
        ctk.CTkLabel(card_out, text="输出目录", font=ctk.CTkFont(weight="bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        row_out = ctk.CTkFrame(card_out, fg_color="transparent")
        row_out.pack(fill="x", padx=16, pady=(0, 12))
        self.out_var = ctk.StringVar(value=str(xyz2md.BASE_DIR / "out"))
        ctk.CTkEntry(row_out, textvariable=self.out_var, height=36).pack(
            side="left", fill="x", expand=True)
        ctk.CTkButton(row_out, text="浏览...", width=84, height=36,
                      command=self._pick_dir).pack(side="left", padx=(8, 0))

        card_model = ctk.CTkFrame(self, corner_radius=12)
        card_model.pack(fill="x", padx=28, pady=(0, 14))
        ctk.CTkLabel(card_model, text="转写模型", font=ctk.CTkFont(weight="bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        row_model = ctk.CTkFrame(card_model, fg_color="transparent")
        row_model.pack(fill="x", padx=16, pady=(0, 12))
        self.model_var = ctk.StringVar(value=MODELS[0][0])
        ctk.CTkComboBox(row_model, variable=self.model_var, width=220, height=36,
                        values=[m[0] for m in MODELS]).pack(side="left")
        self.noaudio_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(row_model, text="仅元数据(不下载音频)",
                        variable=self.noaudio_var).pack(side="left", padx=(20, 0))

        card_opt = ctk.CTkFrame(self, corner_radius=12)
        card_opt.pack(fill="x", padx=28, pady=(0, 14))
        ctk.CTkLabel(card_opt, text="可选参数", font=ctk.CTkFont(weight="bold"),
                     anchor="w").pack(fill="x", padx=16, pady=(14, 4))
        row_opt = ctk.CTkFrame(card_opt, fg_color="transparent")
        row_opt.pack(fill="x", padx=16, pady=(0, 12))
        self.limit_var = ctk.StringVar()
        self.lang_var = ctk.StringVar()
        ctk.CTkLabel(row_opt, text="只转前").pack(side="left")
        ctk.CTkEntry(row_opt, textvariable=self.limit_var, width=72,
                     placeholder_text="N").pack(side="left", padx=(6, 4))
        ctk.CTkLabel(row_opt, text="分钟").pack(side="left", padx=(0, 18))
        ctk.CTkLabel(row_opt, text="语言").pack(side="left")
        ctk.CTkEntry(row_opt, textvariable=self.lang_var, width=72,
                     placeholder_text="zh").pack(side="left", padx=(6, 0))

        ctk.CTkButton(self, text="🚀 开始转换", height=44,
                      font=ctk.CTkFont(size=15, weight="bold"),
                      command=self.on_start).pack(pady=(6, 6), ipadx=12)

        ctk.CTkLabel(
            self,
            text="首次运行会自动下载模型权重(约 460MB)到程序目录 models/; "
                 "转写在本地进行, 音频不会上传。",
            text_color=("gray40", "gray60"), wraplength=720, justify="left",
        ).pack(padx=28, pady=(0, 20), anchor="w")

    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.cwd()))
        if d:
            self.out_var.set(d)

    def collect_args(self) -> tuple:
        url = self.url_var.get().strip()
        model = self.model_var.get().strip() or "small"
        out_dir = self.out_var.get().strip() or str(xyz2md.BASE_DIR / "out")
        limit = self.limit_var.get().strip()
        lang = self.lang_var.get().strip()
        no_audio = bool(self.noaudio_var.get())
        if limit:
            try:
                float(limit)
            except ValueError:
                messagebox.showwarning("提示", "「只转前N分钟」需要填数字")
                return ("",) * 6
        return url, model, out_dir, limit, lang, no_audio


class ProgressPage(ctk.CTkFrame):
    def __init__(self, master, on_back) -> None:
        super().__init__(master, corner_radius=0, fg_color="transparent")
        self.on_back = on_back
        self._start_ts = None
        self._app = None

        topbar = ctk.CTkFrame(self, fg_color="transparent")
        topbar.pack(fill="x", padx=20, pady=(16, 0))
        ctk.CTkButton(topbar, text="← 返回", width=80, height=32,
                      command=self.on_back, fg_color="transparent",
                      border_width=1, text_color=("gray20", "gray80")).pack(
            side="left")

        header = ctk.CTkFrame(self, corner_radius=12)
        header.pack(fill="x", padx=20, pady=(14, 12))
        self.cover_label = ctk.CTkLabel(header, text="🎙", width=72, height=72,
                                        corner_radius=10,
                                        fg_color=("gray85", "gray25"),
                                        font=ctk.CTkFont(size=32),
                                        text_color=("gray40", "gray70"))
        self.cover_label.pack(side="left", padx=(14, 14), pady=14)
        info = ctk.CTkFrame(header, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, pady=14, padx=(0, 14))
        self.title_label = ctk.CTkLabel(info, text="正在获取元数据...",
                                        font=ctk.CTkFont(size=16, weight="bold"),
                                        anchor="w", justify="left",
                                        wraplength=560)
        self.title_label.pack(fill="x", anchor="w")
        self.subtitle_label = ctk.CTkLabel(info, text="",
                                           text_color=("gray40", "gray60"),
                                           anchor="w")
        self.subtitle_label.pack(fill="x", anchor="w", pady=(4, 0))

        prog = ctk.CTkFrame(self, corner_radius=12)
        prog.pack(fill="x", padx=20, pady=(0, 12))
        prog_row = ctk.CTkFrame(prog, fg_color="transparent")
        prog_row.pack(fill="x", padx=14, pady=(12, 4))
        self.progress_bar = ctk.CTkProgressBar(prog_row, height=10)
        self.progress_bar.set(0)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        self.progress_pct = ctk.CTkLabel(prog_row, text="0%", width=60,
                                         font=ctk.CTkFont(weight="bold"))
        self.progress_pct.pack(side="left", padx=(10, 0))
        self.status_label = ctk.CTkLabel(prog, text="准备中...",
                                        text_color=("gray40", "gray60"),
                                        anchor="w")
        self.status_label.pack(fill="x", padx=14, pady=(0, 12))

        log_card = ctk.CTkFrame(self, corner_radius=12)
        log_card.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        ctk.CTkLabel(log_card, text="运行日志",
                     font=ctk.CTkFont(weight="bold"), anchor="w").pack(
            fill="x", padx=14, pady=(10, 4))
        self.log_text = ctk.CTkTextbox(log_card, font=("Consolas", 11),
                                       state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=14, pady=(0, 12))

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(0, 16))
        self.stop_btn = ctk.CTkButton(bottom, text="⏹ 停止", width=120, height=38,
                                       fg_color="#c0392b", hover_color="#a93226",
                                       command=self._on_stop)
        self.stop_btn.pack(side="left")
        self.open_btn = ctk.CTkButton(bottom, text="📂 打开输出文件夹", width=160,
                                      height=38, state="disabled",
                                      command=self._open_out)
        self.open_btn.pack(side="right")

    def bind_app(self, app) -> None:
        self._app = app

    def _on_stop(self) -> None:
        if self._app:
            self._app.stop()

    def _open_out(self) -> None:
        import re
        text = self.log_text.get("1.0", "end")
        m = re.search(r"Markdown:\s+(.+)", text)
        d = None
        if m:
            d = Path(m.group(1).strip()).parent
        if d is None and self._app:
            d = Path(self._app.config_page.out_var.get())
        try:
            d.mkdir(parents=True, exist_ok=True)
            import os
            os.startfile(str(d))
        except OSError as e:  # noqa: BLE001
            messagebox.showerror("错误", f"无法打开文件夹:\n{e}")

    def _reset_for_new_run(self) -> None:
        self._start_ts = None
        self.progress_bar.set(0)
        self.progress_pct.configure(text="0%")
        self.status_label.configure(text="准备中...")
        self.title_label.configure(text="正在获取元数据...")
        self.subtitle_label.configure(text="")
        self.cover_label.configure(image="", text="🎙")
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self.set_buttons(running=True, stopping=False, finished=False)

    def set_buttons(self, *, running: bool, stopping: bool, finished: bool) -> None:
        if running:
            self.stop_btn.configure(state="disabled" if stopping else "normal",
                                    text="停止中..." if stopping else "⏹ 停止")
            self.open_btn.configure(state="disabled")
        else:
            self.stop_btn.configure(state="disabled", text="⏹ 停止")
            self.open_btn.configure(state="normal" if finished else "disabled")

    def set_status(self, text: str) -> None:
        self.status_label.configure(text=text)

    def set_meta(self, meta: dict) -> None:
        title = meta.get("episode_title") or "（未知标题）"
        sub_parts = []
        if meta.get("podcast"):
            sub_parts.append(f"播客：{meta['podcast']}")
        if meta.get("date_published"):
            sub_parts.append(f"发布：{meta['date_published'].split('T')[0]}")
        if meta.get("duration_sec"):
            sec = int(meta["duration_sec"])
            h, rem = divmod(sec, 3600)
            m, _ = divmod(rem, 60)
            if h:
                sub_parts.append(f"时长：{h} 小时 {m} 分")
            else:
                sub_parts.append(f"时长：{m} 分")
        self.title_label.configure(text=title)
        self.subtitle_label.configure(text="  ·  ".join(sub_parts) or " ")

    def set_cover(self, ctk_image) -> None:
        if ctk_image is None:
            self.cover_label.configure(
                image="", text="🎙",
                font=ctk.CTkFont(size=32),
                text_color=("gray40", "gray70"))
        else:
            self.cover_label.configure(image=ctk_image, text="")

    def append_log(self, line: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def apply_progress_line(self, line: str) -> None:
        import re
        import time as _t
        m = re.search(
            r"已转写\s+(\d+)\s+段,\s*进度到\s+(\d+):(\d+):(\d+)\s*/\s*共\s+(.+)", line)
        if m:
            if self._start_ts is None:
                self._start_ts = _t.time()
            seg_n = int(m.group(1))
            hh, mm, ss = int(m.group(2)), int(m.group(3)), int(m.group(4))
            cur_sec = hh * 3600 + mm * 60 + ss
            human = m.group(5).strip()
            total_sec = _parse_human_duration(human)
            if total_sec > 0:
                pct = min(cur_sec / total_sec, 1.0)
                self.progress_bar.set(pct)
                self.progress_pct.configure(text=f"{int(pct*100)}%")
                elapsed = _t.time() - self._start_ts
                eta = ""
                if pct > 0.01:
                    eta_sec = elapsed * (1 - pct) / pct
                    eta = f" · 剩余 {fmt_dur_short(eta_sec)}"
                self.status_label.configure(
                    text=f"已转写 {seg_n} 段 · {cur_sec/60:.1f} 分钟 / {human}{eta}")
            return

        m2 = re.search(r"检测语言:\s*(\S+).*?音频时长\s*(\d+)s", line)
        if m2:
            self.status_label.configure(
                text=f"检测到语言 {m2.group(1)}, 时长 {int(m2.group(2))//60} 分钟")

        for key, text in [
            ("抓取单集页面", "抓取单集页面..."),
            ("下载音频", "下载音频..."),
            ("加载 Whisper 模型", "加载 Whisper 模型(首次会下载)..."),
            ("使用分批转写引擎", "准备转写..."),
            ("仅转写前", "仅转写前若干分钟..."),
            ("未下载音频", "仅生成元数据..."),
        ]:
            if key in line:
                self.status_label.configure(text=text)


def main() -> int:
    app = App()

    if "--smoke" in sys.argv:
        app.after(2000, app.destroy)
    if "--auto" in sys.argv:
        i = sys.argv.index("--auto")
        rest = sys.argv[i + 1:]
        app.config_page.url_var.set(rest[0] if rest else "")
        for j in range(1, len(rest), 2):
            k, v = rest[j], rest[j + 1]
            if k == "--limit-minutes":
                app.config_page.limit_var.set(v)
            elif k == "--model":
                app.config_page.model_var.set(v)
            elif k == "--out":
                app.config_page.out_var.set(v)
        app.after(300, app._start_convert)

    app.mainloop()
    return app.final_code


if __name__ == "__main__":
    sys.exit(main())