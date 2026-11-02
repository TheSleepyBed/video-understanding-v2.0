#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V3（3.0.0）非交互入口：路径/依赖检查 + 抽帧 + 总览图校验 + 清理脚本。

小模型或 pi 这类非交互 agent 优先调用本脚本：

    python run_video.py "C:/Videos/测试 视频.mp4"

默认输出到视频同级的同名文件夹，例如：

    C:/Videos/测试 视频/
    C:/Videos/cleanup_测试 视频.cmd

只输出 JSONL：心跳行 + 最后一行 event=result。退出码：
    0 成功
    2 路径问题或抽帧失败
    3 依赖问题（numpy / Pillow / ffmpeg / ffprobe）
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

VERSION = "3.0.0"
TOOL_NAME = "video-understanding"
MARKER_NAME = ".video-understanding.json"

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT_DIR = Path(__file__).resolve().parent
EXTRACT_SCRIPT = SCRIPT_DIR / "extract_keyframes.py"

VIDEO_EXT = {
    ".mp4", ".mov", ".mkv", ".avi", ".webm", ".flv", ".wmv", ".m4v",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv", ".rmvb", ".vob",
    ".gif",
}

# Windows 保留名
RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

# 这些字符在 Windows 文件名里合法，但在 cmd 清理脚本里容易出问题；
# 默认输出目录统一替换成 _，保证清理脚本双击可用。
BAD_STEM_CHARS = '<>:"/\\|?*%&^!'

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


class SoftTimeout(Exception):
    pass


def setup_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def emit(payload: Dict[str, Any], code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return code


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

def decode_unicode_escapes(text: str) -> str:
    """把模型抄回来的 JSON \\uXXXX 转义还原成真实字符。"""
    def repl(match: re.Match) -> str:
        code = int(match.group(1), 16)
        # 代理对先不单独解码，避免产生 lone surrogate
        if 0xD800 <= code <= 0xDFFF:
            return match.group(0)
        return chr(code)

    return re.sub(r"\\u([0-9a-fA-F]{4})", repl, text)


def normalize_path(raw: str) -> str:
    """把模型/shell 可能传进来的各种路径写法收敛成 Windows 可用的形式。"""
    s = str(raw).strip()
    if s.startswith("file://"):
        s = unquote(s[7:])
        if re.match(r"^/[A-Za-z]:", s):
            s = s[1:]

    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        s = s[1:-1]
    s = s.strip()

    # 模型经常把路径复制成 ...video.mp4\"
    while s.endswith('\\"') or s.endswith('\\\\"'):
        s = s[:-2]

    s = s.strip().strip('"').strip("'").strip()
    s = os.path.expandvars(os.path.expanduser(s))

    if os.name == "nt":
        # Git Bash / MSYS 风格：/c/Users/... -> C:/Users/...
        m = re.match(r"^/([A-Za-z])/(.*)$", s)
        if m:
            s = f"{m.group(1).upper()}:/{m.group(2)}"

    # 去掉不是盘符根的尾部反斜杠
    if len(s) > 3 and s.endswith("\\") and not re.match(r"^[A-Za-z]:\\$", s):
        s = s.rstrip("\\")

    # 模型可能把 JSON 里的 \u706b 当成路径抄进来。
    # 先按字面路径检查；不存在才解码，且只在解码后真实存在时采用。
    if re.search(r"\\u[0-9a-fA-F]{4}", s):
        literal = Path(s).expanduser()
        if not literal.exists():
            decoded = decode_unicode_escapes(s)
            if decoded != s and Path(decoded).expanduser().exists():
                s = decoded
    return s


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


def squash_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name)).casefold()


def find_near_miss(path: Path) -> List[Path]:
    """在父目录里找“去掉所有空白后同名”的真实文件。"""
    parent = Path(path).parent
    if not parent.is_dir():
        return []
    target = squash_name(Path(path).name)
    matches: List[Path] = []
    try:
        for child in parent.iterdir():
            if child.is_file() and squash_name(child.name) == target:
                matches.append(child)
    except OSError:
        return []
    return matches


def safe_stem(name: str) -> str:
    s = str(name or "")
    s = re.sub(r"[\x00-\x1f]", " ", s)
    for ch in BAD_STEM_CHARS:
        s = s.replace(ch, "_")
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    if not s:
        s = "video"
    if s.upper() in RESERVED_NAMES:
        s = "_" + s
    if len(s) > 80:
        s = s[:80].rstrip(". ")
    return s


def cmd_escape_name(name: str) -> str:
    # 顺序很重要：先处理 ^，再插入 ^& 之类的转义
    return (
        name.replace("^", "^^")
        .replace("%", "%%")
        .replace("&", "^&")
        .replace("|", "^|")
        .replace("<", "^<")
        .replace(">", "^>")
    )


def writable_dir(path: Path) -> bool:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return probe.is_dir() and os.access(probe, os.W_OK)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# 依赖
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# 输出目录 / marker
# ---------------------------------------------------------------------------

def read_marker(output_dir: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads((Path(output_dir) / MARKER_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None


def source_matches(marker: Dict[str, Any], video: Path) -> bool:
    src = marker.get("source")
    if not src:
        return False
    a, b = Path(str(src)), Path(video)
    try:
        if a.exists() and b.exists():
            if not os.path.samefile(a, b):
                return False
    except OSError:
        pass
    # 同一路径但视频内容变了（大小不同）时不要复用旧输出
    try:
        if marker.get("source_size") is not None and b.exists():
            if int(marker["source_size"]) != b.stat().st_size:
                return False
    except (TypeError, ValueError, OSError):
        pass
    return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
        os.path.abspath(str(b))
    )


def is_our_output(output_dir: Path, video: Path) -> bool:
    marker = read_marker(output_dir)
    if not marker or marker.get("tool") != TOOL_NAME:
        return False
    return source_matches(marker, video)


def choose_output_dir(
    video: Path, requested: Optional[Path], force: bool = False
) -> Path:
    if requested is not None:
        out = Path(requested).expanduser()
        if out.exists():
            if not out.is_dir():
                raise RuntimeError(f"输出路径已存在且不是目录：{out}")
            if not is_our_output(out, video) and not force:
                raise RuntimeError(
                    f"输出目录已存在，且不是 {TOOL_NAME} 创建的：{out}。"
                    "换一个路径，或加 --force 明确覆盖。"
                )
        return out

    stem = safe_stem(video.stem)
    base = video.parent / stem
    candidates = [base]
    candidates += [video.parent / f"{stem}{suffix}" for suffix in
                   ("_video-understanding", "_2", "_3", "_4", "_5")]
    for cand in candidates:
        if not cand.exists():
            return cand
        if cand.is_dir() and is_our_output(cand, video):
            return cand
    raise RuntimeError("无法选择输出目录，请用 --output 明确指定")


def write_marker(
    output_dir: Path,
    video: Path,
    args: argparse.Namespace,
    run_id: str,
    completed: bool = False,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    old = read_marker(output_dir)
    created = (
        old.get("created_at")
        if old and source_matches(old, video)
        else now_iso()
    )
    marker: Dict[str, Any] = {
        "tool": TOOL_NAME,
        "version": VERSION,
        "source": str(video.resolve()),
        "source_size": video.stat().st_size if video.exists() else None,
        "output_dir": str(output_dir.resolve()),
        "run_id": run_id,
        "created_at": created,
        "updated_at": now_iso(),
        "completed": bool(completed),
        "frame_quality": args.frame_quality,
        "sheet_quality": args.sheet_quality,
    }
    if extra:
        marker.update(extra)
    with open(output_dir / MARKER_NAME, "w", encoding="utf-8", newline="\n") as f:
        json.dump(marker, f, ensure_ascii=False, indent=2)
    return marker


def write_cleanup_script(output_dir: Path) -> Optional[Path]:
    """在输出目录旁边生成 cleanup_<文件夹名>.cmd。"""
    script_dir = output_dir.parent
    if not script_dir.is_dir() or not writable_dir(script_dir):
        return None

    script_name = f"cleanup_{safe_stem(output_dir.name)}.cmd"
    script_path = script_dir / script_name
    if script_path.exists():
        try:
            old = script_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            old = ""
        if "generated by video-understanding" not in old:
            i = 2
            while True:
                candidate = script_dir / f"cleanup_{safe_stem(output_dir.name)}_{i}.cmd"
                if not candidate.exists():
                    script_path = candidate
                    break
                i += 1

    target = cmd_escape_name(output_dir.name)
    content = f"""@echo off
chcp 65001 >nul
setlocal
REM generated by video-understanding v{VERSION}
set "TARGET=%~dp0{target}"
if not exist "%TARGET%\\.video-understanding.json" (
  echo Refusing to delete: marker not found.
  echo Target: %TARGET%
  pause
  exit /b 1
)
if /i "%~1"=="--keep-overviews" goto keep_overviews
if /i "%~1"=="--yes" goto do_delete
echo This will delete:
echo   %TARGET%
choice /m "Continue"
if errorlevel 2 exit /b 0

:do_delete
rmdir /s /q "%TARGET%"
if exist "%TARGET%" (
  echo Failed to delete "%TARGET%".
  pause
  exit /b 1
)
start "" /b cmd /c "ping 127.0.0.1 -n 2 >nul & del /f /q "%~f0""
exit /b 0

:keep_overviews
echo Deleting frames only: %TARGET%\\frames
if exist "%TARGET%\\frames" rmdir /s /q "%TARGET%\\frames"
echo Done. Overviews and keyframes.json are kept.
echo Run this script again without arguments to delete the whole folder.
exit /b 0
"""
    try:
        with open(script_path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(content)
    except OSError:
        return None
    return script_path


# ---------------------------------------------------------------------------
# 检查
# ---------------------------------------------------------------------------

def build_commands(
    video_abs: Optional[str], output_abs: Optional[str]
) -> Dict[str, str]:
    py = "py -3.12" if os.name == "nt" else "python3"
    env = "PYTHONUTF8=1 PYTHONIOENCODING=utf-8 "

    def shell_path(value: str) -> str:
        return str(value).replace("\\", "/") if os.name == "nt" else str(value)

    video = shell_path(video_abs) if video_abs else "<video_path>"
    output = shell_path(output_abs) if output_abs else "<output_dir>"
    runner = shell_path(Path(__file__).resolve())
    run_cmd = f'{env}{py} "{runner}" "{video}"'
    check_cmd = f'{env}{py} "{runner}" --check "{video}"'
    if output_abs:
        run_cmd += f' --output "{output}"'
        check_cmd += f' --output "{output}"'
    return {"check": check_cmd, "run": run_cmd}


def run_checks(
    video_raw: Optional[str],
    output_raw: Optional[str],
    force: bool = False,
    explicit_output: bool = False,
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
    video_abs: Optional[str] = None

    if video_raw:
        info = path_info(video_raw)
        info["extension_supported"] = info["suffix"] in VIDEO_EXT

        # 模型经常把空格抄错。路径不存在时，去父目录找“去掉空白后同名”的文件。
        if not info["exists"] and not info["posix_on_windows"]:
            near = find_near_miss(Path(info["absolute"]))
            if len(near) == 1:
                corrected = near[0]
                info = path_info(str(corrected))
                info["extension_supported"] = info["suffix"] in VIDEO_EXT
                info["original_raw"] = video_raw
                info["did_you_mean"] = str(corrected)
                info["corrected"] = True
                result["note"] = (
                    f"路径已自动纠正（去掉空白后同名）：{video_raw} -> {corrected}"
                )
            elif len(near) > 1:
                info["near_misses"] = [str(p) for p in near]

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
            if info.get("near_misses"):
                issues.append(
                    "视频路径不存在，但父目录有多个去掉空白后同名的文件："
                    + ", ".join(info["near_misses"])
                    + "。让用户确认用哪一个；不要自己猜。"
                )
            else:
                issues.append(
                    "视频路径不存在，父目录也没有去掉空白后同名的文件。"
                    "用 ls 看父目录并复制真实文件名；不要原样重试同一个路径。"
                )
        elif not info["is_file"]:
            path_issue = True
            issues.append("视频路径存在，但不是文件。")
        elif not info["extension_supported"]:
            path_issue = True
            issues.append(
                "不支持的扩展名。支持：" + ", ".join(sorted(VIDEO_EXT))
            )
        else:
            video_abs = info["absolute"]

    # 输出检查：显式 --output 或默认的同名文件夹
    if output_raw:
        out = Path(output_raw).expanduser()
        try:
            out_abs = str(out.resolve())
        except OSError:
            out_abs = str(out.absolute())
        out_info = {
            "raw": str(output_raw),
            "absolute": out_abs,
            "explicit": bool(explicit_output),
            "exists": out.exists(),
            "is_file": out.is_file(),
            "writable_parent": writable_dir(out),
            "will_use_suffix": False,
        }
        result["output"] = out_info
        if out.is_file():
            path_issue = True
            issues.append("输出路径已存在且是文件，请换一个目录。")
        elif (
            explicit_output
            and out.exists()
            and out.is_dir()
            and video_abs
            and not is_our_output(out, Path(video_abs))
            and not force
        ):
            path_issue = True
            issues.append(
                "显式指定的输出目录已存在，且不是本工具创建的。"
                "换一个路径，或加 --force 明确覆盖。"
            )
        elif not out_info["writable_parent"]:
            path_issue = True
            issues.append("输出目录不可写，请换一个路径。")
    elif video_abs:
        video_path = Path(video_abs)
        default_out = video_path.parent / safe_stem(video_path.stem)
        out_info = {
            "raw": str(default_out),
            "absolute": str(default_out),
            "explicit": False,
            "exists": default_out.exists(),
            "is_file": default_out.is_file(),
            "writable_parent": writable_dir(default_out),
            "will_use_suffix": False,
        }
        if default_out.exists() and default_out.is_dir():
            out_info["will_use_suffix"] = not is_our_output(default_out, video_path)
        result["output"] = out_info
        if default_out.is_file():
            path_issue = True
            issues.append("默认输出路径已存在且是文件，请用 --output 指定其他目录。")
        elif not out_info["writable_parent"]:
            path_issue = True
            issues.append("视频所在目录不可写，请用 --output 指定其他目录。")

    valid_video = video_abs
    valid_output = (
        result["output"]["absolute"]
        if result["output"]
        and result["output"].get("explicit")
        and not result["output"].get("is_file")
        and result["output"].get("writable_parent")
        and not result["output"].get("will_use_suffix")
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


# ---------------------------------------------------------------------------
# 心跳
# ---------------------------------------------------------------------------

class Heartbeat:
    def __init__(
        self,
        interval: float,
        enabled: bool = True,
        soft_timeout: float = 0.0,
        stream=None,
    ) -> None:
        self.interval = max(0.5, float(interval)) if enabled else 0.0
        self.enabled = bool(enabled and self.interval > 0)
        self.soft_timeout = float(soft_timeout or 0.0)
        self.stream = stream or sys.stdout
        self.start_time = time.monotonic()
        self.state = {"stage": "start", "current": 0, "total": None}
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

    def update(self, stage: str, current: int = 0, total=None, info=None) -> None:
        with self.lock:
            self.state.update(stage=stage, current=current, total=total)
        if (
            stage != "done"
            and self.soft_timeout > 0
            and time.monotonic() - self.start_time > self.soft_timeout
        ):
            raise SoftTimeout(
                f"soft timeout after {self.soft_timeout:g}s; "
                "use --soft-timeout 0 or a larger pi bash timeout"
            )

    def _emit(self, payload: Dict[str, Any]) -> None:
        print(json.dumps(payload, ensure_ascii=False), file=self.stream, flush=True)

    def _loop(self) -> None:
        while not self.stop_event.wait(self.interval):
            with self.lock:
                state = dict(self.state)
            payload: Dict[str, Any] = {
                "event": "heartbeat",
                "stage": state["stage"],
                "current": state["current"],
                "total": state["total"],
                "elapsed": round(time.monotonic() - self.start_time, 1),
            }
            if state["total"]:
                payload["pct"] = round(
                    100.0 * state["current"] / state["total"], 1
                )
            self._emit(payload)

    def start(self) -> None:
        if self.enabled:
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.0)

    def result(self, payload: Dict[str, Any]) -> None:
        self.stop()
        data: Dict[str, Any] = {"event": "result"}
        data.update(payload)
        self._emit(data)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    setup_stdout()
    parser = argparse.ArgumentParser(
        description="视频/GIF 关键帧抽取（V3 / 3.0.0 非交互入口）"
    )
    parser.add_argument("video", nargs="?", help="视频/GIF 路径（位置参数）")
    parser.add_argument("--video", dest="video_opt", help="视频/GIF 路径（兼容写法）")
    parser.add_argument(
        "--path-file",
        help="从 UTF-8 文本文件读取视频路径（中文路径被 shell 改写时的兜底）",
    )
    parser.add_argument("--output", help="输出目录（默认：视频同级同名文件夹）")
    parser.add_argument("--check", action="store_true", help="只检查路径和依赖")
    parser.add_argument(
        "--check-file",
        action="append",
        default=[],
        help="检查一个已存在的文件路径（例如总览图），可重复",
    )
    parser.add_argument(
        "--print-skill-dir", action="store_true", help="打印技能目录后退出"
    )
    parser.add_argument("--frame-quality", type=int, default=90,
                        help="单帧 JPEG 质量（默认 90，约等于 ffmpeg -q:v 3）")
    parser.add_argument("--sheet-quality", type=int, default=95,
                        help="总览图 JPEG 质量（默认 95，建议不要低于 92）")
    parser.add_argument("--cols", type=int, default=3, help="总览图列数（默认 3）")
    parser.add_argument("--sheet-max-cells", type=int, default=12,
                        help="每张总览图最多格数（默认 12）")
    parser.add_argument("--sheet-width", type=int, default=1600,
                        help="总览图目标宽度（默认 1600）")
    parser.add_argument("--max-frames", type=int, default=200,
                        help="最多关键帧数（默认 200）")
    parser.add_argument("--heartbeat", type=float, default=5.0,
                        help="心跳间隔秒数（默认 5）")
    parser.add_argument("--no-heartbeat", action="store_true", help="关闭心跳")
    parser.add_argument("--soft-timeout", type=float, default=570.0,
                        help="软超时秒数（默认 570；0 = 关闭）")
    parser.add_argument("--no-cleanup-script", action="store_true",
                        help="不生成清理脚本")
    parser.add_argument("--force", action="store_true",
                        help="允许写入已存在的非本工具输出目录（危险）")
    args = parser.parse_args(argv)

    if args.print_skill_dir:
        return emit({
            "ok": True,
            "skill_dir": str(SKILL_DIR),
            "runner_path": str(Path(__file__).resolve()),
            "extract_path": str(EXTRACT_SCRIPT),
        }, 0)

    if args.check_file:
        files: List[Dict[str, Any]] = []
        issues: List[str] = []
        bad = False
        for raw_file in args.check_file:
            text = normalize_path(raw_file)
            info = path_info(text)
            p = Path(text)
            try:
                info["size"] = p.stat().st_size if p.is_file() else None
            except OSError:
                info["size"] = None
            files.append(info)
            if info["posix_on_windows"]:
                bad = True
                issues.append(
                    f"POSIX-style file path on Windows: {raw_file!r} -> "
                    f"{info['absolute']!r}. Do not retry the same path."
                )
            elif not info["is_file"]:
                bad = True
                issues.append(f"File does not exist or is not a file: {raw_file!r}")
            elif not info["size"]:
                bad = True
                issues.append(f"File is empty: {raw_file!r}")
        return emit({
            "ok": not bad,
            "files": files,
            "suggestion": " ".join(issues) if issues else "检查通过。",
            "exit_code": 0 if not bad else 2,
        }, 0 if not bad else 2)

    raw: Optional[str] = None
    if args.path_file:
        try:
            lines = Path(args.path_file).expanduser().read_text(
                encoding="utf-8-sig"
            ).splitlines()
        except OSError as exc:
            return emit({
                "event": "result",
                "ok": False,
                "error": f"cannot read --path-file: {exc}",
                "hint": "Write the video path as one UTF-8 line in the file.",
                "exit_code": 2,
            }, 2)
        raw = next((line.strip() for line in lines if line.strip()), None)
    if not raw:
        raw = args.video or args.video_opt

    if not raw and not args.check:
        parser.error("video path is required unless --check/--print-skill-dir is used")

    if raw:
        raw = normalize_path(raw)

    checks, code = run_checks(
        raw,
        args.output,
        force=args.force,
        explicit_output=bool(args.output),
    )
    if args.check or code != 0:
        checks["exit_code"] = code
        return emit(checks, code)

    video = Path(checks["video"]["absolute"])
    requested = Path(args.output).expanduser() if args.output else None

    try:
        output_dir = choose_output_dir(video, requested, force=args.force)
    except Exception as exc:
        return emit({
            "event": "result",
            "ok": False,
            "error": str(exc),
            "hint": "Use a new --output path, or --force if you are sure.",
            "exit_code": 2,
        }, 2)

    run_id = uuid.uuid4().hex[:12]
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        write_marker(output_dir, video, args, run_id, completed=False)
    except Exception as exc:
        return emit({
            "event": "result",
            "ok": False,
            "error": f"cannot prepare output directory: {exc}",
            "output_dir": str(output_dir),
            "hint": "Choose a writable --output directory.",
            "exit_code": 2,
        }, 2)

    cleanup_script = None
    if not args.no_cleanup_script:
        try:
            cleanup_script = write_cleanup_script(output_dir)
        except Exception:
            cleanup_script = None

    heartbeat = Heartbeat(
        interval=args.heartbeat,
        enabled=not args.no_heartbeat,
        soft_timeout=args.soft_timeout,
    )
    heartbeat.start()
    heartbeat.update("start", 0, None)

    sys.path.insert(0, str(SCRIPT_DIR))
    try:
        from extract_keyframes import extract_keyframes
    except Exception as exc:
        heartbeat.result({
            "ok": False,
            "error": f"cannot import extract_keyframes: {exc}",
            "hint": "Use a Python with numpy/Pillow, for example py -3.12.",
            "exit_code": 3,
        })
        return 3

    try:
        meta = extract_keyframes(
            video,
            output=output_dir,
            cols=args.cols,
            sheet_width=args.sheet_width,
            sheet_max_cells=args.sheet_max_cells,
            max_frames=args.max_frames,
            max_width=0,
            quality=args.frame_quality,
            sheet_quality=args.sheet_quality,
            contact_sheet=True,
            verbose=False,
            progress_cb=heartbeat.update,
        )
    except SoftTimeout as exc:
        write_marker(
            output_dir, video, args, run_id, completed=False,
            extra={"error": "soft timeout"},
        )
        heartbeat.result({
            "ok": False,
            "error": str(exc),
            "video": str(video),
            "output_dir": str(output_dir),
            "cleanup_script": str(cleanup_script) if cleanup_script else None,
            "hint": "Increase the pi bash timeout, raise --soft-timeout, or use a shorter video.",
            "exit_code": 2,
        })
        return 2
    except Exception as exc:
        write_marker(
            output_dir, video, args, run_id, completed=False,
            extra={"error": str(exc)},
        )
        heartbeat.result({
            "ok": False,
            "error": str(exc),
            "video": str(video),
            "output_dir": str(output_dir),
            "cleanup_script": str(cleanup_script) if cleanup_script else None,
            "hint": (
                "Run --check once to diagnose. If the path starts with '/', it is "
                "a POSIX path and will resolve under C:\\ on Windows. Do not retry "
                "the same path unchanged."
            ),
            "exit_code": 2,
        })
        return 2

    overviews = [str(p) for p in (meta.get("overviews") or [])]
    if not overviews:
        write_marker(
            output_dir, video, args, run_id, completed=False,
            extra={"error": "no overview sheets"},
        )
        heartbeat.result({
            "ok": False,
            "error": "extraction produced no overview sheets",
            "video": str(video),
            "output_dir": str(output_dir),
            "cleanup_script": str(cleanup_script) if cleanup_script else None,
            "exit_code": 2,
        })
        return 2

    missing = [
        p for p in overviews
        if not Path(p).is_file() or Path(p).stat().st_size == 0
    ]
    if missing:
        write_marker(
            output_dir, video, args, run_id, completed=False,
            extra={"error": "missing overview files"},
        )
        heartbeat.result({
            "ok": False,
            "error": "overview files are missing or empty",
            "missing": missing,
            "video": str(video),
            "output_dir": str(output_dir),
            "cleanup_script": str(cleanup_script) if cleanup_script else None,
            "exit_code": 2,
        })
        return 2

    write_marker(
        output_dir, video, args, run_id, completed=True,
        extra={
            "completed_at": now_iso(),
            "keyframe_count": meta.get("keyframe_count"),
            "overview_count": len(overviews),
        },
    )

    out_dir = Path(meta.get("output_dir") or output_dir)
    keyframes_json = out_dir / "keyframes.json"
    heartbeat.result({
        "ok": True,
        "video": str(video),
        "video_corrected": bool(checks["video"].get("corrected")),
        "original_video": (
            checks["video"].get("original_raw")
            if checks["video"].get("corrected")
            else None
        ),
        "output_dir": str(out_dir),
        "cleanup_script": str(cleanup_script) if cleanup_script else None,
        "overviews": overviews,
        "overview_count": len(overviews),
        "keyframes_json": str(keyframes_json) if keyframes_json.is_file() else None,
        "keyframe_count": meta.get("keyframe_count"),
        "frame_quality": args.frame_quality,
        "sheet_quality": args.sheet_quality,
        "elapsed": round(time.monotonic() - heartbeat.start_time, 1),
        "next_step": (
            "Read every path in overviews with the read tool, in order. "
            "If text is unreadable, read the single frames listed in keyframes_json. "
            "Do not read the video or scripts with the read tool. "
            "Do not retry the same failing path unchanged."
        ),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
