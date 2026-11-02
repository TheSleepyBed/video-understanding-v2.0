---
name: video-understanding
description: Extract keyframes from a video or GIF, read them with a vision model, and report the video's visual content. Trigger whenever a video or GIF is present (attachment, @reference, or local path), even if the user did not ask for analysis. Also use when a video read fails with ENOENT on Windows or a POSIX path such as /root/... is involved. Covers "what is this video", "describe/summarize this video", "what text is on screen". Audio transcription is out of scope.
metadata:
  version: "3.0.0"
---

# video-understanding-V3

视频/GIF 出现时使用。只处理画面和屏幕文字；不转写音频，不修改源视频。

## 路径规则

- 原样使用用户或附件给出的路径。Windows 上用正斜杠并加引号：
  `"C:/Users/Bob/Videos/测试 视频.mp4"`。
- 不要用 read 工具读视频文件或 `.py` 脚本；用下面的命令。
- Windows 上 `/root/...` 会被解析成 `C:\root\...`。遇到 `posix_on_windows: true`
  或 ENOENT：只跑一次 `--check`，按 `suggestion` 停止；不要原样重试。
- 工具结果里的 `\u706b` 是 JSON 转义，不是路径。看到这种形式，用 `ls` 看父目录
  并复制真实文件名；不要把它抄进命令。
- 路径不存在时，`--check` 会按“去掉空白后同名”自动纠正并返回 `did_you_mean`；
  唯一匹配直接用纠正后的 `video.absolute`。多个匹配会让用户确认。只允许这一次
  修正，不要连续改路径。
- 中文路径被 shell 改写时：
  ```bash
  mkdir -p "$TEMP"
  printf '%s\n' 'C:/Users/Bob/Videos/测试 视频.mp4' > "$TEMP/video_path.txt"
  python "<skill_dir>/scripts/run_video.py" --path-file "$TEMP/video_path.txt"
  ```
  不要用默认不存在的 `C:/tmp`。

## 调用

pi 里：

```text
/skill:video-understanding "C:/Users/Bob/Videos/测试 视频.mp4"
```

实际命令：

```bash
PYTHONUTF8=1 PYTHONIOENCODING=utf-8 python "<skill_dir>/scripts/run_video.py" "C:/Users/Bob/Videos/测试 视频.mp4"
```

- pi 的 bash 工具设置 `timeout=600`（长视频 1800）；它是工具参数，不是命令参数。
- 默认输出到 `<video_parent>/<video_stem>/`，并生成 `cleanup_<stem>.cmd`。
- 默认每 5 秒输出一条心跳；只读最后一条 `event: result`。
- 只有失败时才运行：
  ```bash
  python "<skill_dir>/scripts/run_video.py" --check "C:/.../视频.mp4"
  ```
- 完整参数、输出结构、清理脚本、心跳示例、路径兜底：见 `README.md`。

## 读图

- 按 `overviews` 数组顺序读每一张，不要只读第一张。
- 文字或细节不清时，读 `keyframes_json` 里的 `keyframes[].path`。
- 使用 JSON 返回的绝对路径；不要加 `/root` 或改写成 POSIX 路径。
- read 返回 ENOENT 时不要重试；用
  `run_video.py --check-file "<path>"` 检查一次，然后停止。

## 报告

用用户的语言，使用这四节：

```text
## Summary
一两句话：这是什么视频，意图是什么。

## Visuals
按时间顺序：场景、主体与动作、镜头运动、光线与色彩、氛围。

## On-screen text
逐字读出所有可辨认的文字，并说明含义及与画面的关系。没有就写 "No on-screen text"。

## Message
画面和文字合起来传达了什么。
```

- 不写时间戳、帧号、时间轴、分辨率、时长、帧率、关键帧数量。
- 屏幕文字逐字读；没有文字时也要把画面写完整。
- 很短的视频可以只写一段，内容少时可省略小节标题。

## 停止条件

- `event: result` 里 `ok: false`：报告 `error` + `hint`，停止。
- `missing_dependencies` 非空：换有 numpy/Pillow 的 Python，或安装依赖，停止。
- `[Image too large]`：降低 `--sheet-width`（1600 -> 1200）。
- 总览图文字模糊：降低 `--sheet-max-cells`（12 -> 10），或读单帧。

完整说明见 `README.md`；参数与算法细节见 `references/parameters.md`。
