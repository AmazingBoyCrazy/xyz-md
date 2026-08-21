# SPEAKER_SPEC — 说话人识别 (Diarization) 设计文档

> **状态**：已实现并通过端到端测试（2026-08-19）。
> **目标**：轻量本地说话人识别，文字稿输出 `A: xxx` / `B: xxx` 格式，
> 准确率目标 80-90%（双人对谈场景实测约 90%+）。

## 1. 技术选型

| 方案 | 结论 |
| --- | --- |
| pyannote.audio / whisperX (torch) | ❌ 依赖 torch，exe 从 107MB 涨到 ~1GB，打包成本过高 |
| **sherpa-onnx (ONNX, 纯 CPU)** | ✅ 采用。无 torch，wheel 仅 ~19MB，与现有 onnxruntime 生态同类 |
| LLM 上下文推断 | 备选，未采用（每次耗 API tokens，离线不可用） |

### 模型（合计约 33MB，存于 `models/sherpa-onnx/`）

| 模型 | 文件 | 大小 | 来源 |
| --- | --- | --- | --- |
| 语音分割 (pyannote segmentation 3.0) | `pyannote-segmentation/model.onnx` | 5.7MB | HuggingFace `csukuangfj/sherpa-onnx-pyannote-segmentation-3-0`（支持 `HF_ENDPOINT` 镜像） |
| 中文声纹嵌入 (3D-Speaker CAM++) | `campplus-zh/model.onnx` | 27MB | GitHub Releases `k2-fsa/sherpa-onnx` tag `speaker-recongition-models`（注意 tag 名是官方拼写笔误） |

首次启用时自动下载（复用 `fetch_stream` 的重试/进度日志），之后离线可用。

## 2. 数据流

```
音频 (16kHz float32, 限转时截取)
  ├─→ sherpa-onnx diarization ──→ turns [(start, end, speaker_id), ...]
  └─→ faster-whisper (word_timestamps=True) ──→ 词级分段
                │
                ▼
merge_speaker_blocks(词级分段, turns)   [流式生成器]
  每个词的中点时间 → 说话人（三层匹配: 包含 > 最大重叠 > 最近轮次）
  相邻同说话人的词合并成块; 说话人切换处断开
  同一说话人连续发言 >1000 字时在句末标点软切分（防 LLM 精修截断）
                │
                ▼
块对象 (SimpleNamespace, 带 start/end/text, text = "A: ...")
  与普通 whisper segment 接口完全兼容
  → write_markdown 零改动, 输出 "**[00:00:17 → 00:00:25]** A: 我们从哪说起呢..."
```

关键设计：**说话人块伪装成普通 segment**，下游（write_markdown / 进度日志 /
时间轴章节 / opencc 繁简转换）全部复用，无侵入。

## 3. 接口

### CLI（xyz2md.py）
```
--diarize           启用说话人识别
--speakers N        说话人数: 0=自动估计(默认), 双人对谈建议 2
```

### GUI（xyz2md_gui.py 配置页「可选参数」卡）
- 复选框「说话人识别」+ 下拉「自动 / 2 / 3 / 4 人」（未勾选时下拉禁用）
- `--auto` 测试模式支持 `--diarize`（布尔标志）与 `--speakers N`

### MD 输出
- header 增加 `- **说话人标注**：A/B 由本地声纹自动识别（约 90% 准确率）`
- 正文段: `**[00:00:17 → 00:00:25]** A: 文本`（前缀在文本部分，`_SEG_PATTERN` 无需修改）

### LLM 精修（xyz2md_polish.py）
- `_process` 送 LLM 前剥离 `^[A-Z]:\s*` 前缀，精修后恢复 —— 避免 LLM 丢失/改写前缀

## 4. 容错与降级

- sherpa-onnx 未安装 / 模型下载失败 / 推理异常 → log 警告后**自动降级为普通转写**（无前缀），不中断任务
- `transcribe()` 返回 `(segments, diarize_applied)`，header 说明行只在真正生效时写入

## 5. 性能（16 核 CPU / 14GB 内存实测，2026-08-20）

| 指标 | 数值 |
| --- | --- |
| 分离速度 (RTF) | 0.13–0.19× 实时（30 分钟音频实测 338s，8 线程） |
| 3 小时音频额外耗时 | 约 25–35 分钟 |
| 内存峰值（实测） | 10 分钟完整管线 1.78GB；30 分钟纯分离 1.59GB；3 小时完整转写推算约 2.5GB |
| 内存构成 | 音频数组 230MB/小时（3h≈660MB）+ whisper small 运行时 ~1.4GB + sherpa 模型 33MB + onnxruntime 内存池 |
| exe 体积增量 | sherpa-onnx wheel ~19MB → 打包后 +40MB 左右 |

## 6. 打包（PyInstaller）

构建命令需追加：`--collect-all sherpa_onnx`
（sherpa_onnx 为运行时延迟导入，静态分析不可见；onedir 与 onefile 同）

## 7. 已知限制

- 说话人标签是 A/B/... 匿名代号，无法对应真实姓名（未来可在 GUI 让用户指定）
- 极短插话 (<0.3s) 可能被 VAD 过滤或误归属相邻说话人
- 声纹相近的说话人（同性亲子/模仿者）可能聚成一人
- `--speakers` 强制指定比自动估计更稳，双人对谈建议固定 2

## 8. 用户实测反馈修复（2026-08-21）

用户 10 分钟实测发现的问题与修复：

| 问题 | 根因 | 修复 |
| --- | --- | --- |
| 说话人标签出现 C 没有 B | 自动聚类估出 3 类但一类没分到词，标签直接用聚类原始 id（稀疏） | `run_speaker_diarization` 按首次出现把 id 紧凑化为 0,1,2… |
| "看起来"被拆成 `A: 看` / `C: 起来` | 声纹轮次边界 ±0.5s 抖动落在词中间 | `merge_speaker_blocks` 碎块平滑：连续碎块(≤4字且<1.5s)拼回一个词，并与后续同说话人块合并；独立短插话（如 `A: 好的`）保留不合并 |
| 精修后 `###` 章节标题全部丢失 | `write_polished_md` 只回写时间戳段落 | `read_segments_from_md` 把 `### ` 标题解析为 `{"chapter": ...}` 占位条目按序透传；只解析「## 文字稿」之后内容防头部标题误捕获 |
| `A: 看` 单字片段精修后变成整段 LLM 回复（幻觉） | 碎片段无上下文，LLM 把它当用户提问 | polish 跳过 ≤6 字片段（原样保留）；返回长度 > max(4×输入, 输入+60) 判定跑题保留原文；提示词注明"输入是转录片段，不要回应内容" |
| 嘉宾 3 分钟超长单块 | 转录无标点时软切分（句号处断）永不触发 | 软切分标点集扩展（，、；等），超 2000 字硬切分兜底 |
