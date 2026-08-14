#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小宇宙播客 → Markdown 文字稿 (图形界面)

打包成 exe 后双击即用; 也可用命令行模式:
    xyz2md.exe --cli <单集链接> [选项]

隐藏测试参数:
    --smoke            构建窗口后 2 秒自动关闭 (验证 GUI 可启动)
    --auto <url> ...   自动开始转换, 结束后自动关闭 (验证完整链路)
"""
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import xyz2md

# ---------- 命令行模式(打包后可: xyz2md.exe --cli <url> [选项]) ----------
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


class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("小宇宙播客 → Markdown 文字稿")
        root.geometry("760x560")
        root.minsize(640, 480)

        self.q: "queue.Queue[str]" = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.final_code = 0

        pad = {"padx": 10, "pady": 6}
        frm = ttk.Frame(root, padding=10)
        frm.pack(fill="both", expand=True)

        # 链接
        ttk.Label(frm, text="单集链接:").grid(row=0, column=0, sticky="w", **pad)
        self.url_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.url_var).grid(
            row=0, column=1, columnspan=3, sticky="ew", **pad)
        ttk.Label(frm, text="https://www.xiaoyuzhoufm.com/episode/<eid>",
                  foreground="#888").grid(row=1, column=1, columnspan=3, sticky="w")

        # 模型
        ttk.Label(frm, text="转写模型:").grid(row=2, column=0, sticky="w", **pad)
        self.model_var = tk.StringVar(value=MODELS[0][0])
        ttk.Combobox(frm, textvariable=self.model_var, state="readonly", width=18,
                     values=[m[0] for m in MODELS]).grid(row=2, column=1, sticky="w", **pad)

        # 输出目录
        ttk.Label(frm, text="输出目录:").grid(row=2, column=2, sticky="e", **pad)
        self.out_var = tk.StringVar(value=str(xyz2md.BASE_DIR / "out"))
        ttk.Entry(frm, textvariable=self.out_var).grid(
            row=2, column=3, sticky="ew", **pad)
        ttk.Button(frm, text="浏览...", command=self._pick_dir).grid(
            row=2, column=4, **pad)

        # 可选
        ttk.Label(frm, text="可选:").grid(row=3, column=0, sticky="w", **pad)
        ttk.Label(frm, text="只转前N分钟").grid(row=3, column=1, sticky="w")
        self.limit_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.limit_var, width=8).grid(
            row=3, column=1, sticky="e", padx=(70, 0))
        ttk.Label(frm, text="语言").grid(row=3, column=2, sticky="e")
        self.lang_var = tk.StringVar()
        ttk.Entry(frm, textvariable=self.lang_var, width=6).grid(
            row=3, column=3, sticky="w", padx=(6, 0))
        self.noaudio_var = tk.BooleanVar()
        ttk.Checkbutton(frm, text="仅元数据(不下载音频)",
                        variable=self.noaudio_var).grid(row=3, column=4, sticky="w", **pad)

        # 按钮
        btns = ttk.Frame(frm)
        btns.grid(row=4, column=0, columnspan=5, sticky="w", **pad)
        self.start_btn = ttk.Button(btns, text="开始转换", command=self.start)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(btns, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.open_btn = ttk.Button(btns, text="打开输出文件夹",
                                   command=self._open_out, state="disabled")
        self.open_btn.pack(side="left", padx=(16, 0))

        # 日志
        ttk.Label(frm, text="运行日志:").grid(row=5, column=0, sticky="w", **pad)
        self.log_text = tk.Text(frm, height=16, state="disabled", wrap="word",
                                font=("Consolas", 9))
        self.log_text.grid(row=6, column=0, columnspan=5, sticky="nsew", **pad)
        frm.rowconfigure(6, weight=1)
        frm.columnconfigure(1, weight=1)
        frm.columnconfigure(3, weight=1)

        ttk.Label(
            frm,
            text="首次运行会自动下载模型权重(约460MB)到程序目录 models/ 文件夹; "
                 "转写在本地进行, 音频不会上传。",
            foreground="#888",
        ).grid(row=7, column=0, columnspan=5, sticky="w", **pad)

        self._append("就绪。粘贴单集链接后点击「开始转换」。\n")

    # ---------- 界面动作 ----------
    def _pick_dir(self) -> None:
        d = filedialog.askdirectory(initialdir=self.out_var.get() or str(Path.cwd()))
        if d:
            self.out_var.set(d)

    def _open_out(self) -> None:
        d = Path(self.out_var.get())
        d.mkdir(parents=True, exist_ok=True)
        try:
            import os
            os.startfile(str(d))
        except OSError as e:  # noqa: BLE001
            messagebox.showerror("错误", f"无法打开文件夹:\n{e}")

    def _append(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ---------- 转换控制 ----------
    def start(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("提示", "请先粘贴小宇宙单集链接")
            return
        if not xyz2md.EPISODE_RE.search(url):
            messagebox.showwarning(
                "提示", "链接格式不对, 应为:\nhttps://www.xiaoyuzhoufm.com/episode/<eid>")
            return

        limit = self.limit_var.get().strip()
        if limit:
            try:
                float(limit)
            except ValueError:
                messagebox.showwarning("提示", "「只转前N分钟」需要填数字")
                return

        cmd = [url, "--model", self.model_var.get(), "--out", self.out_var.get().strip()]
        if limit:
            cmd += ["--limit-minutes", limit]
        if self.lang_var.get().strip():
            cmd += ["--lang", self.lang_var.get().strip()]
        if self.noaudio_var.get():
            cmd += ["--no-audio"]

        self.stop_event.clear()
        self.final_code = 0
        self.open_btn.configure(state="disabled")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._append("\n========== 开始转换 ==========\n")

        # 日志钩子: 转换线程里的 print 会同步进界面
        xyz2md.LOG_HOOK = lambda line: self.q.put(line + "\n")  # noqa: E731

        self.worker = threading.Thread(target=self._run, args=(cmd,), daemon=True)
        self.worker.start()
        self.root.after(100, self._drain)

    def stop(self) -> None:
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            self._append("\n>>> 正在停止... (等待当前片段转写完成)\n")
            self.stop_btn.configure(state="disabled")

    def _run(self, cmd: list) -> None:
        try:
            self.final_code = xyz2md.main(cmd, stop_check=self.stop_event.is_set)
        except Exception as e:  # noqa: BLE001
            self.final_code = 1
            self.q.put(f"\n[ERROR] {e!r}\n")

    def _drain(self) -> None:
        try:
            while True:
                self._append(self.q.get_nowait())
        except queue.Empty:
            pass
        if self.worker and not self.worker.is_alive():
            self.worker = None
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if not self.stop_event.is_set():
                self.open_btn.configure(state="normal")
            self._append("========== 转换结束 ==========\n")
            return
        self.root.after(100, self._drain)

    def _auto_finish(self) -> None:
        """--auto 测试模式: 转换结束后自动关闭窗口"""
        if self.worker and self.worker.is_alive():
            self.root.after(300, self._auto_finish)
        else:
            self.root.after(500, self.root.destroy)


def main() -> int:
    root = tk.Tk()
    app = App(root)
    if "--smoke" in sys.argv:
        root.after(2000, root.destroy)
    if "--auto" in sys.argv:
        i = sys.argv.index("--auto")
        rest = sys.argv[i + 1:]
        app.url_var.set(rest[0] if rest else "")
        for j in range(1, len(rest), 2):  # 简易解析 --k v
            k, v = rest[j], rest[j + 1]
            if k == "--limit-minutes":
                app.limit_var.set(v)
            elif k == "--model":
                app.model_var.set(v)
            elif k == "--out":
                app.out_var.set(v)
        root.after(300, app.start)
        root.after(600, app._auto_finish)

    root.mainloop()
    return app.final_code


if __name__ == "__main__":
    sys.exit(main())
