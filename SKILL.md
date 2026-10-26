---
name: video-understanding
description: Extract keyframes from a video or GIF, read them with a vision model, and report the video's visual content. Trigger whenever a video or GIF is present (attachment, @reference, or local path), even if the user did not ask for analysis. Also use when a video read fails with ENOENT on Windows or a POSIX path such as /root/... is involved. Covers "what is this video", "describe/summarize this video", "what text is on screen". Audio transcription is out of scope.
---

# video-understanding（小模型 / 非交互优化版）

视频或 GIF 出现时使用本技能。按顺序执行，不要跳过检查。
遇到路径错误只诊断一次，不要原样重试。

## 0. 路径规则（先读）

- 使用用户或附件给出的原始路径，不要自己编 `/root/...`。
- Windows 上以 `/` 开头的路径是 POSIX 路径。`Path("/root/x").resolve()` 会变成
  `C:\root\x`，通常不是用户要的文件。停下来，改用真实 Windows 路径或附件本地路径。
- 不要用 read 工具读视频文件或 `.py` 脚本。用下面的 Python 命令。
- 任何工具返回 `ENOENT` / “文件不存在”时：运行一次 `--check`，看 JSON 的
  `suggestion`，然后停止。不要重复同一个调用。

## 1. 小模型 checklist

1. 记下视频路径，原样使用。
2. 运行 `--check`。`ok` 为 false 就报告 `suggestion` 并停止。
3. 运行抽帧命令。`ok` 为 false 就报告 `error` + `hint` 并停止。
4. 按顺序读取 `overviews` 里的每个路径。
5. 文字看不清时，读 `keyframes_json` 里的单帧。
6. 按下面的格式写报告。

## 2. 命令

技能目录 = 包含本文件的目录。下面用 `<skill_dir>` 表示。

检查路径和依赖（可选，失败时先跑一次）：

```bash
python "<skill_dir>/scripts/run_video.py" --check --video "<video_path>"
```

抽帧（推荐入口）：

```bash
python "<skill_dir>/scripts/run_video.py" --video "<video_path>" --output "<output_dir>"
```

- `--output` 可省略，默认在视频旁：`<video_parent>/video_understanding/<video_stem>`。
- 成功时 JSON 包含：`ok`、`overviews`、`keyframes_json`、`keyframe_count`、`next_step`。
- 失败时 JSON 包含：`ok`、`error`、`hint`、`exit_code`。
- 在 Windows 上推荐 `py -3.12`；任何装有 numpy/Pillow 的 Python 也可以。
- 一次只处理一个视频文件。

## 3. 读图

- 读 `overviews` 里的每一个文件，按数组顺序；数组顺序就是时间顺序。不要只读第一张。
- 文字或细节看不清时，读 `keyframes_json` 里的 `keyframes[].path`（单帧原图）。
- 使用 JSON 里返回的绝对路径，不要加 `/root` 前缀，也不要改写成 POSIX 路径。
- 如果 read 返回 ENOENT，不要重试；用 `--check` 检查该文件路径一次，然后报告。

## 4. 报告格式

用用户的语言回答，使用这四节：

```
## Summary
一两句话：这是什么视频，意图是什么。

## Visuals
按时间顺序：场景、主体与动作、镜头运动、光线与色彩、氛围。写具体内容，不要只给类型标签。

## On-screen text
逐字读出所有可辨认的文字，并说明含义及与画面的关系。没有就写 "No on-screen text"。

## Message
画面和文字合起来传达了什么。
```

规则：

- 报告里不要写时间戳、帧号、时间轴、分辨率、时长、帧率、关键帧数量。
- 屏幕文字逐字读，不要改写成“字幕说明了……”。
- 没有文字时也要把画面写完整，不要用“无声画面”代替描述。
- 很短的视频可以只写一段，内容少时可省略小节标题。

## 5. 停止条件

| 情况 | 动作 |
| --- | --- |
| `/root/...` 或 `posix_on_windows: true` | 要真实 Windows 路径；停止，不重试 |
| 路径不存在 / `ENOENT` | 运行一次 `--check`；报告 `suggestion`；停止 |
| `missing_dependencies` 非空 | 换有 numpy/Pillow 的 Python，或安装依赖；停止 |
| `[Image too large]` | 降低 `--sheet-width`（1600 -> 1200） |
| 总览图文字模糊 | 降低 `--sheet-max-cells`（12 -> 10），或读单帧 |
| 没有生成总览图 | 直接运行 `extract_keyframes.py --json`，报告错误 |
| 短 GIF 只有几帧 | 正常现象 |

## Scope

- 只处理视觉内容和屏幕文字；不转写音频，不从画面推断说话内容。
- 不修改源视频。
- 参数、阈值和性能说明见 `references/parameters.md`。
