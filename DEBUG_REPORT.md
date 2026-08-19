# xyz2md v1.3.1 失败诊断报告

> **✅ 已解决（v1.3.2）**：根因确认为本报告"猜测 C"——`xyz2md.main()` 成功路径的
> `Markdown: <path>` 等关键行用裸 `print()` 输出，绕过 `LOG_HOOK`，GUI 永远解析不到
> MD 路径，导致转写成功却判定为"失败"、不跳转美化页。
> 修复：全部改用 `log()`；GUI 增加输出目录兜底查找；进度条分母改用实际转写时长；
> 去除 MD 重复的 `## 文字稿` 标题。已通过用户实测验证。

> **目的**：记录用户（AmazingBoyCrazy）测试 v1.3.1 构建时仍出现"失败"状态的诊断信息，
> 供后续 AI 会话分析。

---

## 1. 当前构建版本

- **exe 路径**：`D:\ADP\dsh\dshwork\xyz2md\dist\xyz2md.exe` (102.3 MB)
- **构建时间**：2026/8/19 15:15:30
- **分支 commit**：`797b550`（已推送到 main）
- **GUI 版本**：三页（配置 → 转写 → 美化），CustomTkinter

构建命令：
```powershell
.\.venv\Scripts\pyinstaller --noconfirm --clean --onefile --windowed --name xyz2md `
  --workpath build --distpath dist --specpath build `
  --collect-all faster_whisper --collect-all ctranslate2 --collect-all onnxruntime `
  --collect-all av --collect-all huggingface_hub --collect-all tokenizers `
  --collect-all customtkinter --collect-all PIL --collect-data opencc `
  --add-data "D:\ADP\dsh\dshwork\xyz2md\xyz2md.py;." `
  --add-data "D:\ADP\dsh\dshwork\xyz2md\xyz2md_polish.py;." `
  --hidden-import xyz2md --hidden-import xyz2md_polish `
  xyz2md_gui.py
```

---

## 2. 用户现象

测试链接：`https://www.xiaoyuzhoufm.com/episode/6a819b2517676351c572adc0`
（"通缩的本质..."，3 小时播客）

设置：`--limit-minutes 10`（10分钟限制）

### 日志显示
```
[15:20:13] 下载中: 120.0 / 158.5 MB (79%)
[15:20:17] 音频已保存: ...158.5 MB
[15:20:18] 加载 Whisper 模型 small
[15:27:14] 使用分批转写引擎
[15:27:29] 仅转写前 10.0 分钟
[15:27:34] 检测语言: zh (置信度 0.99), 音频时长 600s
[15:27:34]   MD header 已写入: ...md
[15:30:18]   已转写 20 段, 进度到 00:09:26 / 共 2 小时 51 分
[转写] main 返回: code=0
```

### UI 状态
- 进度条：5%（错）
- 状态：**失败**
- 「停止」按钮变红高亮（变停止）
- 「打开输出文件夹」按钮变蓝可点（变 normal）
- **没跳转到美化页**

---

## 3. 已修复的相关问题（已提交 797b550）

### Bug #1: ModuleNotFoundError
- **症状**：双击 exe 弹窗 `No module named 'xyz2md'`
- **根因**：之前把 `xyz2md.py` 改成两阶段写入时，第 523 行残留死代码导致 `IndentationError`
- **修复**：删除残留代码 + 显式 `--add-data` 和 `--hidden-import` 打包本地模块

### Bug #2: 长音频下载失败
- **症状**：大文件下载截断但不报错
- **修复**：新增 `fetch_stream()` 流式下载 + Content-Length 校验 + chunk 超时重试

### Bug #3: MD 文件 0 字节
- **症状**：write_markdown 异常导致 0 字节 MD
- **修复**：MD 两阶段写入（header 立即 flush）+ write_markdown 异常捕获不抛出

---

## 4. 当前未解决的"失败"状态问题

### 关键观察
1. **worker 正常退出**：`[转写] main 返回: code=0`
2. **日志未出现** `✅ 完成! 共 N 段文字稿` 这条（main 内部 try 块的最后一行的 print）
3. **未出现** `[诊断] worker 结束, final_code=...` 这条（应该在 worker 退出后由 _drain 输出）
4. **状态显示失败**：跳到 `set_buttons(running=False, ..., finished=True)` 分支
5. **MD 文件**：
   - header 已写入（说明 write_markdown 第一阶段 OK）
   - 但**未知** 是否完整（用户未确认）

### 我的判断（待验证）

**猜测 A**：`xyz2md.main()` 在 write_markdown 调用**之前**就已 return（code=0），但 transcribe 实际返回的 segments 已被消费完（已转写 20 段 log 出现）—— 但日志"已完成 N 段"那条未出现，说明 **write_markdown 内的 for seg 循环中断了**——可能在第 21 段附近 write_seg 抛了异常。

**猜测 B**：write_markdown 内的异常被新版 except 捕获（"不抛出, 保留已写入的部分"），返回 count——main 正常 return 0——**但 main 内 try 块的 `print(f"\n✅ 完成!...")` 在异常后应该执行**——除非 write_markdown 在 print之前抛了……不对 write_markdown 在 main 内 try 块的**第一句**之前不会有异常。

**猜测 C**：`_last_md_path` 未被正确设置。看 `_drain` 代码：
```python
m = _re.search(r"Markdown:\s+(.+)", payload)
if m:
    self._last_md_path = m.group(1).strip()
```
这条解析 `Markdown:` 行——**但 xyz2md.main 改回 try/except StopTranscription 后，可能不再打印 `Markdown:` 行**——所以 `_last_md_path` 是空字符串。

### 失败的判定条件
```python
success = self.final_code == 0 and not self.stop_event.is_set()
if success and self._last_md_path and Path(self._last_md_path).exists():
    # 跳美化页
else:
    # 失败分支 (当前走这里)
```

如果 `_last_md_path` 是空（因为没匹配到 `Markdown:`），则即使 `final_code==0` 也走失败分支。

### 修复方案

**方案 1**：让 `xyz2md.main` 恢复打印 `Markdown:` 行（在 main 顶部计算 md_path 并先打印一行，或者把 `print("✅ 完成!")` 之前先 print md_path 单独一行）。

**方案 2**：在 `xyz2md_gui.py._run` 中 worker 完成后，从 `cmd` 重新构造 `md_path` 而不依赖日志正则：
```python
# worker 结束后，根据 cmd 重新计算 md_path
if not self._last_md_path:
    # cmd 格式: [url, --model, M, --out, OUT_DIR, ...]
    out_idx = cmd.index("--out") + 1
    out_dir = Path(cmd[out_idx])
    eid = ...  # 从 url 提取
    # 然后扫 out_dir 找最新的 md
```

**方案 3（最简单）**：改 `_drain` 的判定逻辑——如果 `final_code==0` 且 worker 退出，**直接跳转**美化页而不要求 md_path 存在（让 BeautifyPage 自己去 out_dir 找最新生成的 MD）。

---

## 5. 调试建议（给后续 AI）

### 推荐测试
1. 让用户跑一个**短 episode**（如 `https://www.xiaoyuzhoufm.com/episode/6a6ff6f5ab3a91c24a0ec11e`，限制 2 分钟），看：
   - 日志是否包含 `✅ 完成!` 和 `Markdown:` 行
   - 是否成功跳转到美化页

2. 如果短 episode 跳转成功，**说明 _last_md_path 解析是 bug**——采用方案 1 或方案 3 修复。

3. 如果短 episode 也失败——**说明 write_markdown 仍在中断**——查看完整日志（`[诊断]` 行）找 `final_code`。

### 关键代码位置

| 文件 | 行号 | 说明 |
| | --- | --- |
| `xyz2md_gui.py` `App._drain` | ~210-235 | 失败/成功判定逻辑 |
| `xyz2md_gui.py` `App._run` | ~140-170 | worker 线程，捕获异常 |
| `xyz2md.py` `write_markdown` | ~430-525 | 两阶段写入，异常捕获 |
| `xyz2md.py` `main` | ~595-640 | try/except StopTranscription 处理 |

### 关键诊断日志（已加，待观察）
```python
# xyz2md_gui.py._drain:
self.q.put(("log",
    f"[诊断] worker 结束, final_code={self.final_code}, "
    f"stop_event={self.stop_event.is_set()}, "
    f"md_path={self._last_md_path!r}, "
    f"exists={self._last_md_path and Path(self._last_md_path).exists()}\n"))
```

---

## 6. 提交流程

1. **不要急着改代码**：先让用户跑**短 episode 测试**（2 分钟限制），确认是 _last_md_path 解析问题还是更深的 write_markdown 中断
2. **根据诊断日志**精确定位
3. **修改后**让用户**再验证**才能发布

按之前约定：发布前需要用户手动验证。

---

## 7. 已知但用户反馈的"转写界面提示失败"对应的可能根因

按发生概率排序：

1. **`_last_md_path` 解析失败**（最可能）— GUI 判定失败但转写实际成功，MD 文件完整
2. **write_markdown 在某段异常**，被新版 except 捕获并返回 0 段 count，main 正常完成但"完成 N 段"那条 print 漏打（如果代码改过）
3. **`xyz2md.main` 没 return**（理论可能，但日志显示 `[转写] main 返回: code=0` 说明已返回）
4. **`_drain` 在 worker 结束后未执行**（理论可能，但 root.after 应该保证持续调度）

需要后续 AI 根据诊断日志精确判定。