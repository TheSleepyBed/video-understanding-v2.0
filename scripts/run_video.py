#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v2 非交互入口：路径/依赖检查 + 抽帧 + 总览图校验。

小模型或 pi 这类非交互 agent 优先调用本脚本，而不是直接调用
extract_keyframes.py。它把 preflight 和抽帧合并成一个入口，只输出 JSON。

用法：
  python scripts/run_video.py --video "C:\\path\\video.mp4" --output "C:\\path\\out"
  python scripts/run_video.py --check --video "C:\\path\\video.mp4"
  python scripts/run_video.py --print-skill-dir

退出码：
  0  成功
  2  路径问题或抽帧失败
  3  依赖问题（numpy / Pillow / ffmpeg / ffprobe）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
EXTRACT_SCRIPT = SCRIPT_DIR / "extract_keyframes.py"

VIDEO_EXT = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv", ".rmvb", ".vob",
    ".gif",
}

FFMPEG_FALLBACKS = [
    r"C:\ffmpeg\bin\ffmpeg.exe",
    r"D:\ffmpeg\bin\ffmpeg.exe",
    r"D:\ffmpeg-8.1.2-full_build\bin\ffmpeg.exe",
    "/usr/local/bin/ffmpeg",
    "/opt/homebrew/bin/ffmpeg",
]
FFPROBE_FALLBACKS = [
    r"C:\ffmpeg\bin\ffprobe.exe",
    r"D:\ffmpeg\bin\ffprobe.exe",
    r"D:\ffmpeg-8.1.2-full_build\bin\ffprobe.exe",
    "/usr/local/bin/ffprobe",
    "/opt/homebrew/bin/ffprobe",
]


def find_exe(name: str, fallbacks: List[str]) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found
    for candidate in fallbacks:
        if Path(candidate).is_file():
            return candidate
    return None


def package_version(module_name: str) -> Optional[str]:
    if importlib.util.find_spec(module_name) is None:
        return None
    try:
        module = __import__(module_name)
        return str(getattr(module, "__version__", "installed"))
    except Exception:
        return "installed"


def path_info(raw: str) -> Dict[str, Any]:
    text = str(raw)
    posix_on_windows = bool(
        os.name == "nt" and text.startswith("/") and not text.startswith("//")
    )
    path = Path(text).expanduser()
    try:
        absolute = str(path.resolve())
    except OSError:
        absolute = str(path.absolute())
    return {
        "raw": text,
        "absolute": absolute,
        "posix_on_windows": posix_on_windows,
        "exists": path.exists(),
        "is_file": path.is_file(),
        "is_dir": path.is_dir(),
        "suffix": path.suffix.lower(),
    }


def writable_dir(path: Path) -> bool:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return probe.is_dir() and os.access(probe, os.W_OK)
    except OSError:
        return False


def build_commands(
    video_abs: Optional[str], output_abs: Optional[str]
) -> Dict[str, str]:
    py = "py -3.12" if os.name == "nt" else "python3"
    video = video_abs or "<video_path>"
    output = output_abs or "<output_dir>"
    return {
        "check": (
            f'{py} "{Path(__file__).resolve()}" --check '
            f'--video "{video}"'
        ),
        "run": (
            f'{py} "{Path(__file__).resolve()}" '
            f'--video "{video}" --output "{output}"'
        ),
        "extract": (
            f'{py} "{EXTRACT_SCRIPT}" "{video}" -o "{output}" '
            f'--cols 3 --sheet-max-cells 12 --sheet-width 1600 -q --json'
        ),
    }


def run_checks(
    video_arg: Optional[str], output_arg: Optional[str]
) -> Tuple[Dict[str, Any], int]:
    deps = {
        "numpy": package_version("numpy"),
        "Pillow": package_version("PIL"),
        "ffmpeg": find_exe("ffmpeg", FFMPEG_FALLBACKS),
        "ffprobe": find_exe("ffprobe", FFPROBE_FALLBACKS),
    }
    missing_deps = [name for name, value in deps.items() if not value]

    result: Dict[str, Any] = {
        "ok": True,
        "platform": sys.platform,
        "os_name": os.name,
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "skill_dir": str(SKILL_DIR),
        "cwd": str(Path.cwd()),
        "dependencies": deps,
        "missing_dependencies": missing_deps,
        "video": None,
        "output": None,
        "commands": build_commands(None, None),
        "suggestion": "",
    }

    issues: List[str] = []
    path_issue = False

    if video_arg is not None:
        info = path_info(str(video_arg))
        info["extension_supported"] = info["suffix"] in VIDEO_EXT
        result["video"] = info
        if info["posix_on_windows"]:
            path_issue = True
            issues.append(
                "这是 Windows 上的 POSIX 路径，Python 会把它解析成 "
                f"{info['absolute']!r}。请改用真实 Windows 路径或附件本地路径；"
                "不要原样重试同一个路径。"
            )
        elif not info["exists"]:
            path_issue = True
            issues.append(
                "视频路径不存在。检查路径或向用户要本地文件；"
                "不要原样重试同一个路径。"
            )
        elif not info["is_file"]:
            path_issue = True
            issues.append("视频路径存在，但不是文件。")
        elif not info["extension_supported"]:
            path_issue = True
            issues.append(
                "不支持的扩展名。支持：" + ", ".join(sorted(VIDEO_EXT))
            )

    if output_arg is not None:
        out = Path(output_arg).expanduser()
        try:
            out_abs = str(out.resolve())
        except OSError:
            out_abs = str(out.absolute())
        out_info = {
            "raw": str(output_arg),
            "absolute": out_abs,
            "exists": out.exists(),
            "is_file": out.is_file(),
            "writable_parent": writable_dir(out),
        }
        result["output"] = out_info
        if out.is_file():
            path_issue = True
            issues.append("输出路径已存在且是文件，请换一个目录。")
        elif not out_info["writable_parent"]:
            path_issue = True
            issues.append("输出目录不可写，请换一个路径。")

    valid_video = (
        result["video"]["absolute"]
        if result["video"]
        and result["video"].get("exists")
        and not result["video"].get("posix_on_windows")
        else None
    )
    valid_output = (
        result["output"]["absolute"]
        if result["output"]
        and not result["output"].get("is_file")
        and result["output"].get("writable_parent")
        else None
    )
    result["commands"] = build_commands(valid_video, valid_output)

    if missing_deps:
        result["ok"] = False
        result["suggestion"] = (
            "缺少依赖：" + ", ".join(missing_deps) + "。"
            "换一个有 numpy/Pillow 的 Python（例如 py -3.12），"
            "并把 ffmpeg/ffprobe 加入 PATH。"
        )
        return result, 3

    if path_issue:
        result["ok"] = False
        result["suggestion"] = " ".join(issues)
        return result, 2

    result["suggestion"] = "检查通过。"
    return result, 0


def emit(payload: Dict[str, Any], code: int) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="路径/依赖检查 + 抽帧 + 总览图校验（v2 非交互入口）"
    )
    parser.add_argument("--video", help="视频/GIF 路径")
    parser.add_argument("--output", help="输出目录（默认在视频旁 video_understanding/<名>）")
    parser.add_argument("--check", action="store_true", help="只检查路径和依赖，不抽帧")
    parser.add_argument(
        "--print-skill-dir", action="store_true", help="打印技能目录后退出"
    )
    parser.add_argument("--cols", type=int, default=3, help="总览图列数（默认 3）")
    parser.add_argument(
        "--sheet-max-cells", type=int, default=12, help="每张总览图最多格数（默认 12）"
    )
    parser.add_argument(
        "--sheet-width", type=int, default=1600, help="总览图目标宽度（默认 1600）"
    )
    parser.add_argument("--max-frames", type=int, default=200, help="最多关键帧数（默认 200）")
    args = parser.parse_args(argv)

    if args.print_skill_dir:
        return emit({
            "ok": True,
            "skill_dir": str(SKILL_DIR),
            "runner_path": str(Path(__file__).resolve()),
            "extract_path": str(EXTRACT_SCRIPT),
        }, 0)

    if not args.video and not args.check:
        parser.error("--video is required unless --check or --print-skill-dir is used")

    video_raw = args.video
    video_path = Path(video_raw).expanduser() if video_raw else None
    if args.output:
        output_raw = args.output
    elif video_path is not None:
        output_raw = str(
            video_path.parent / "video_understanding" / (video_path.stem or "video")
        )
    else:
        output_raw = None

    checks, code = run_checks(video_raw, output_raw)
    if args.check or code != 0:
        checks.setdefault("exit_code", code)
        return emit(checks, code)

    video_abs = str(checks["video"]["absolute"])
    output_abs = str(checks["output"]["absolute"])

    # 依赖检查通过后再 import，避免 numpy 缺失时直接 traceback。
    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from extract_keyframes import extract_keyframes
    except Exception as exc:
        return emit({
            "ok": False,
            "error": f"无法导入 extract_keyframes：{exc}",
            "hint": "换一个有 numpy/Pillow 的 Python（例如 py -3.12）。",
            "exit_code": 3,
        }, 3)

    try:
        meta = extract_keyframes(
            video_abs,
            output=output_abs,
            cols=args.cols,
            sheet_width=args.sheet_width,
            sheet_max_cells=args.sheet_max_cells,
            max_frames=args.max_frames,
            max_width=0,
            quality=92,
            contact_sheet=True,
            verbose=False,
        )
    except Exception as exc:
        return emit({
            "ok": False,
            "error": str(exc),
            "video": video_abs,
            "output_dir": output_abs,
            "hint": (
                "先运行 --check 诊断一次；不要原样重试同一个路径。"
                "如果路径以 / 开头，它在 Windows 上会被解析成 C:\\...。"
            ),
            "exit_code": 2,
        }, 2)

    overviews = [str(p) for p in (meta.get("overviews") or [])]
    if not overviews:
        return emit({
            "ok": False,
            "error": "抽帧完成，但没有生成总览图。",
            "output_dir": output_abs,
            "hint": "直接用 extract_keyframes.py --json 运行一次，查看错误。",
            "exit_code": 2,
        }, 2)

    missing = [
        p for p in overviews
        if not Path(p).is_file() or Path(p).stat().st_size == 0
    ]
    if missing:
        return emit({
            "ok": False,
            "error": "总览图缺失或为空。",
            "missing": missing,
            "output_dir": output_abs,
            "exit_code": 2,
        }, 2)

    out_dir = Path(meta.get("output_dir") or output_abs)
    keyframes_json = out_dir / "keyframes.json"
    return emit({
        "ok": True,
        "video": video_abs,
        "output_dir": str(out_dir),
        "overviews": overviews,
        "overview_count": len(overviews),
        "keyframes_json": str(keyframes_json) if keyframes_json.is_file() else None,
        "keyframe_count": meta.get("keyframe_count"),
        "next_step": (
            "按顺序读取 overviews 里的每个绝对路径；文字看不清时读 "
            "keyframes_json 里的单帧。不要用 read 工具读视频或脚本；"
            "路径不存在时不要原样重试。"
        ),
    }, 0)


if __name__ == "__main__":
    sys.exit(main())
