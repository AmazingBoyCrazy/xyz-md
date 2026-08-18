# 现代化 GUI 改造方案（CustomTkinter）

> 本文档是给后续 AI 会话的执行蓝图。当前 `xyz2md_gui.py` 是功能完备但外观老旧的
> tkinter 版本；本方案把它替换为 CustomTkinter 双页界面，**不改动 `xyz2md.py`
> 核心逻辑**。

## 1. 技术选型：CustomTkinter

| 维度 | CustomTkinter | PySide6 | Web(pywebview) |
| --- | --- | --- | --- |
| 包体积增量 | ~5 MB | ~100 MB | ~30 MB |
| 学习/迁移成本 | 极低（API ≈ ttk） | 高 | 中 |
| 原生感 | Win11/macOS 风 | Qt 风 | 浏览器风 |
| 打包复杂度 | `--collect-all customtkinter` | 大量 hidden-import | 需 bundling web assets |
| 适合本项目 | ✅ | ❌ overkill | ❌ 多进程通信麻烦 |

**结论**：CustomTkinter。安装：`pip install customtkinter`。

## 2. 双页布局

### 2.1 配置页（ConfigPage）

```
┌──────────────────────────────────────────────┐
│  🎙️ 小宇宙播客 → Markdown 文字稿              │  ← CTkLabel 大标题
├──────────────────────────────────────────────┤
│  单集链接                                      │
│  ┌──────────────────────────────────────────┐ │
│  │ https://www.xiaoyuzhoufm.com/episode/... │ │  ← CTkEntry + placeholder
│  └──────────────────────────────────────────┘ │
│                                                │
│  输出目录                    [浏览...]          │
│  ┌─────────────────────────┐ ┌──────────────┐ │
│  │ D:\...\out              │ │   浏览        │ │  ← CTkEntry + CTkButton
│  └─────────────────────────┘ └──────────────┘ │
│                                                │
│  转写模型 ▾ small (推荐)    □ 仅元数据          │
│                                                │
│  可选：只转前 ___ 分钟     语言 ___             │
│                                                │
│           [ 🚀 开始转换 ]                       │  ← CTkButton 主色
└──────────────────────────────────────────────┘
```

要点：
- 卡片式分组（用 CTkFrame + corner_radius=10 + fg_color 区分背景）
- Entry 带 placeholder_text
- 「浏览」按钮调 `filedialog.askdirectory`
- 模型下拉用 CTkComboBox，values=["tiny","base","small","medium","large-v3"]
- 「开始转换」点击后：校验 URL → 切换到转写页 → 启动后台线程

### 2.2 转写页（ProgressPage）

```
┌──────────────────────────────────────────────┐
│  ← 返回                                       │  ← 左上角文字按钮
├──────────────────────────────────────────────┤
│  ┌──────┐                                     │
│  │ 封面 │  E169.A股的春夏秋冬：种树、种粮、种菜  │  ← CTkImage + 标题
│  │ 64px │  播客：面基  ·  时长：2h25m           │
│  └──────┘                                     │
├──────────────────────────────────────────────┤
│  ████████████░░░░░░░░░░░░░░░░░░  42%  12:34   │  ← CTkProgressBar + label
│  已转写 120 / 300 段                           │
├──────────────────────────────────────────────┤
│  [18:24:06] 加载 Whisper 模型 small ...         │
│  [18:24:10] 使用分批转写引擎 (内存友好)          │  ← CTkTextbox 滚动日志
│  [18:24:44] 仅转写前 3.0 分钟                   │
│  ...                                          │
├──────────────────────────────────────────────┤
│      [ ⏹ 停止 ]      [ 📂 打开输出文件夹 ]      │
└──────────────────────────────────────────────┘
```

要点：
- **封面**：用 `customtkinter.CTkImage` + PIL.Image 加载网络图（urllib 下载到 BytesIO → Image.open）。失败时显示占位图标。尺寸 64×64，corner_radius=8。
- **进度条**：CTkProgressBar(mode="determinate")，每收到一条含「已转写 N 段」的日志更新 value = N / total_segments（total 从 meta.duration_sec 估算或首次出现时记录）。右侧标签显示百分比 + 已用时。
- **日志区**：CTkTextbox(height=300, state="disabled")，通过 `configure(state="normal"); insert(...); see("end"); configure(state="disabled")` 追加。
- **按钮状态机**：
  - 转换中：停止=normal，打开文件夹=disabled，返回=disabled
  - 完成：停止=disabled，打开文件夹=normal，返回=normal
  - 失败/中断：停止=disabled，打开文件夹=normal（部分产物），返回=normal

## 3. 与 xyz2md.py 的集成点（不改核心）

现有 `xyz2md.py` 已暴露以下钩子，GUI 直接使用：

```python
# 日志钩子：转换线程里的 print 会同步进界面
xyz2md.LOG_HOOK = lambda line: queue.put(line + "\n")

# 停止信号：write_markdown 在每个分段间检查
stop_event = threading.Event()
xyz2md.main(cmd_argv, stop_check=stop_event.is_set)

# CLI 模式入口（打包后 xyz2md.exe --cli <url>）
if "--cli" in sys.argv:
    sys.exit(xyz2md.main([a for a in sys.argv[1:] if a != "--cli"]))
```

**新增需求**（小改 xyz2md.py，向后兼容）：
- `parse_page` 已返回 cover/podcast/title/duration/timeline —— 足够填充转写页头部。
- 若想在「开始转换」前就展示标题（用户体验更好），可在配置页失焦/回车时异步调 `fetch + parse_page` 预取 meta，缓存到 self.prefetched_meta；转写页优先用缓存，避免重复请求。**此项为可选优化，非必须**。

## 4. 关键实现细节

### 4.1 窗口与主题
```python
customtkinter.set_appearance_mode("System")   # 跟随系统深/浅色
customtkinter.set_default_color_theme("blue")
root = customtkinter.CTk()
root.title("小宇宙播客 → Markdown 文字稿")
root.geometry("780x620")
root.minsize(640, 520)
```

### 4.2 页面切换
两个 CTkFrame 作为 root 的子控件，同一时刻只 pack 一个：
```python
self.config_page.pack(fill="both", expand=True)
# 切换时：
self.config_page.pack_forget()
self.progress_page.pack(fill="both", expand=True)
```

### 4.3 后台线程 + 队列
与现有 GUI 相同模式：
- worker thread 跑 `xyz2md.main(cmd, stop_check=...)`
- LOG_HOOK → queue.Queue
- 主线程 `root.after(80, self._drain)` 消费队列、更新进度条和日志
- daemon=True，关闭窗口即终止

### 4.4 封面图加载（异步）
```python
def _load_cover(url):
    try:
        data = urllib.request.urlopen(url, timeout=15).read()
        img = Image.open(io.BytesIO(data)).resize((64, 64))
        return CTkImage(light_image=img, dark_image=img, size=(64, 64))
    except Exception:
        return None  # 转写页判断 None 则隐藏或显示占位
```
在 worker thread 开头调用，结果通过 queue 传回主线程设置 image_label。

### 4.5 进度估算
- 从 meta['duration_sec'] 得到总时长 T
- 日志行匹配 `r'已转写 (\d+) 段, 进度到 (\d+):(\d+):(\d+)'` → 当前音频位置 S
- progress = S / T；同时记录 start_time，elapsed = now - start，eta = elapsed / progress - elapsed
- 更新进度条 value 和右侧标签

### 4.6 冻结模式兼容
保留现有守卫（xyz2md.py 已有）：
```python
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
if sys.stdout is None: sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None: sys.stderr = open(os.devnull, "w", encoding="utf-8")
```

### 4.7 CLI / smoke / auto 模式
保留三个隐藏入口，方便自动化测试：
- `--cli <args>` → 纯命令行
- `--smoke` → 构建窗口 2s 后销毁，exit 0
- `--auto <url> [--limit-minutes N --model tiny --out DIR]` → 自动开始转换，完成后销毁并返回 final_code

## 5. PyInstaller 打包变更

在原命令基础上加 `--collect-all customtkinter`：

```powershell
.\.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name xyz2md `
  --workpath build --distpath dist --specpath build `
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime `
  --collect-all av --collect-all huggingface_hub --collect-all tokenizers `
  --collect-all customtkinter `       # ← 新增
  xyz2md_gui.py
```

onedir 版同理加 `--collect-all customtkinter`。预计 exe 体积增加 ~5-8 MB。

## 6. 测试清单

| 测试 | 命令 | 预期 |
| --- | --- | --- |
| 语法 | `python -m py_compile xyz2md_gui.py` | 无错 |
| 冒烟 | `python xyz2md_gui.py --smoke` | 窗口闪现 2s，exit 0 |
| 完整链路 | `python xyz2md_gui.py --auto <url> --limit-minutes 3 --model tiny --out scratch\test` | exit 0，产出 .md/.json/.m4a |
| CLI 模式 | `python xyz2md_gui.py --cli <url> --no-audio --out scratch\cli` | exit 0，产出 .md/.json |
| exe 冒烟 | `Start-Process .\dist\xyz2md.exe -ArgumentList --smoke -Wait -PassThru` | ExitCode 0 |
| exe 完整 | `Start-Process .\dist\xyz2md.exe -ArgumentList --auto,<url>,--limit-minutes,3,--model,tiny,--out,... -Wait -PassThru` | ExitCode 0 + 产物 |

> ⚠️ 在当前 DSH 沙箱环境运行 onefile exe 需要 `$env:TMP` 指向工作区目录
> （onefile 启动器要解压到 %TEMP%）；onedir 版无此限制。用户本机不受影响。

## 7. 验收标准

- [ ] 配置页可填写链接、选目录、选模型，点击开始后平滑切换到转写页
- [ ] 转写页顶部显示封面（64px 圆角）+ 单集标题 + 播客名 + 时长
- [ ] 进度条随转写推进实时更新，右侧显示百分比和已用时
- [ ] 日志区实时滚动，自动滚到底部
- [ ] 停止按钮有效（触发 StopTranscription，保留部分文字稿）
- [ ] 完成后可打开输出文件夹、可返回配置页继续下一个
- [ ] 深色/浅色模式跟随系统正常
- [ ] `--cli` / `--smoke` / `--auto` 三种隐藏模式均正常工作
- [ ] onefile + onedir 两种 exe 均通过冒烟 + 完整链路测试
- [ ] README 更新截图/使用说明，提交新版本 release

## 8. 文件改动范围

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `xyz2md_gui.py` | **重写** | 全部换成 CustomTkinter 双页 |
| `xyz2md.py` | 不动（或可选加预取 meta 接口） | 核心逻辑不变 |
| `requirements.txt` | 追加 `customtkinter>=5.2` | |
| `README.md` | 更新 GUI 截图/说明 | |
| `.gitignore` | 无需改动 | |

## 9. 给后续会话的提示

1. 先 `pip install customtkinter`，跑 `--smoke` 确认基础可用。
2. 按 §2 布局逐控件搭建，每搭完一块就跑一次 `--smoke` 验证。
3. 集成 LOG_HOOK / stop_check 后再跑 `--auto` 端到端。
4. 打包时务必 `--collect-all customtkinter`，否则 exe 启动报找不到资源。
5. 当前仓库 remote 已配好（含嵌入凭据），直接 `git push` 即可；发新版记得建 Release。
6. 本环境沙箱限制：onefile exe 需设 `$env:TMP` 到工作区；git push 用嵌入凭据的 remote URL；api.github.com 用 Python urllib（PowerShell/curl 的 TLS 栈在此环境不可靠）。
