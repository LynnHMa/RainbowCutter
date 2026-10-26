# RainbowCutter

RainbowCutter 是一个面向播客/访谈后期的 Windows 桌面工作台：

`本地音频 → whisper.cpp SRT → 繁体转简体 → OpenAI 兼容 Agent → Markdown 剪辑指南 → 可选音频剪辑副本`

## 启动

双击 `启动 RainbowCutter.bat`，或执行：

```powershell
python rainbowcutter.py
```

## 首次设置

1. 在“设置”中确认 `whisper.cpp 目录`，默认指向项目内的 `whiper_cpp`。
2. 确认“字幕模型目录”中存在 `.bin` 文件；程序启动时会自动扫描全部模型。
3. 填写 API Base URL、API Key、模型名称。支持官方 OpenAI API 以及返回 OpenAI Chat Completions 格式的中转站。
4. 选择输出目录并保存设置。
5. 在工作台选择“播客剪辑”，添加音频或目录，再选择输出模式并点击“开始处理”。

## 输出

每个音频至少生成：

- 同目录的 `.srt` 字幕
- 输出目录中的 `.md` 剪辑指南，包含摘要、高光时间段、删除/保留建议、标题与切片方向、风险提示
- 选择“仅进行 SRT 识别”时，只生成 `.srt`，不生成 `.md` 或 `_edited` 音频
- 选择“总结并根据 MD 剪辑”时，额外生成同输入格式的 `_edited` 音频副本
- 源音频始终只读，不会被覆盖或修改

## 说明

- 应用会复用已有同名 SRT，方便中断后继续。
- 日志区会实时展示 whisper.cpp 子进程输出、API 请求、失败原因与最终文件路径。
- 原生拖放需安装可选依赖 `tkinterdnd2`：`python -m pip install tkinterdnd2`；未安装时仍可用文件选择和目录选择。
- API Key 保存在项目目录 `rainbowcutter.config.json`，请妥善保护该文件。

## 字幕总结模式

工作台顶部可切换两种模式：

- `播客剪辑`：音频 → whisper.cpp → SRT → Markdown 剪辑指南；可继续执行 Agent 剪辑流程
- `播客剪辑` 输出模式还支持“仅进行 SRT 识别”：只调用 whisper.cpp 生成 `.srt`，跳过 Agent、API Key 校验、Markdown 和音频剪辑。
- `字幕总结`：SRT → Agent → Markdown，不执行音频识别，仅生成总结

字幕总结模式支持多选 `.srt` 文件和目录批量导入，输出仍保存到当前输出目录。

设置页提供两条独立的 System Prompt：

- `字幕总结 System Prompt`：用于“字幕总结”模式。
- `播客剪辑 System Prompt`：用于“播客剪辑”模式；剪辑 Agent 会完整读取对应的 SRT 字幕，并按该提示词生成剪辑清单。

保存 Agent 预设时，两条 System Prompt 会一起保存。

## Agent 剪辑流程

剪辑流程只读取 Agent Markdown 中固定的“剪辑清单”章节，使用内置音频工具完成：

- `AudioTool.inspect_audio`：通过 `ffprobe` 获取源文件时长并校验时间范围
- `AudioTool.cut_audio_segments`：将“删”和“⚠️高风险”片段移除，拼接其余片段
- 输出使用与输入相同的扩展名，保存为 `<原文件名>_edited.<原扩展名>`

如果 Markdown 缺少固定章节、时间段非法、超出源文件时长或会删除整段音频，流程会停止，不会生成剪辑结果。执行剪辑前请确保 `ffmpeg` 和 `ffprobe` 已加入系统 PATH。

## 界面说明

当前桌面端采用产品化应用壳布局：顶部品牌栏、左侧工作区导航、中央主工作台、右侧处理配置和底部实时执行记录。工作台支持 `播客剪辑` 与 `字幕总结` 两种模式。
