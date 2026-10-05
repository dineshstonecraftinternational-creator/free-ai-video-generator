#!/usr/bin/env python3

import argparse
import asyncio
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import edge_tts
from PIL import Image, ImageDraw, ImageFont


DEFAULT_VOICE = "en-US-GuyNeural"
CANVAS_W = 1920
CANVAS_H = 1080
FPS = 25


def run(cmd, **kwargs):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        **kwargs
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(str(x) for x in cmd)}\n"
            f"{result.stderr[-4000:]}"
        )

    return result


def get_duration(path):
    result = run([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])

    return float(result.stdout.strip())


def parse_script(path):
    raw = Path(path).read_text(encoding="utf-8")

    blocks = raw.split("---")

    settings = {
        "voice": DEFAULT_VOICE,
        "music": None,
    }

    header = blocks[0].strip()

    for line in header.splitlines():
        line = line.strip()

        if not line:
            continue

        if line.upper().startswith("VOICE:"):
            settings["voice"] = line.split(":", 1)[1].strip()

        elif line.upper().startswith("MUSIC:"):
            value = line.split(":", 1)[1].strip()
            settings["music"] = value or None

    scenes = []

    for index, block in enumerate(blocks[1:], start=1):
        block = block.strip()

        if not block:
            continue

        lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip()
        ]

        image_name = None
        narration_lines = []

        for line in lines:

            if line.upper().startswith("IMAGE:") and image_name is None:
                image_name = line.split(":", 1)[1].strip()

            else:
                narration_lines.append(line)

        narration = " ".join(narration_lines).strip()

        if not narration:
            continue

        scenes.append({
            "image": image_name,
            "narration": narration,
        })

    if not scenes:
        raise ValueError(
            "No scenes found. Separate scenes with --- lines."
        )

    return settings, scenes


async def synthesize_async(text, voice, output):
    communicate = edge_tts.Communicate(text, voice)

    with open(output, "wb") as f:
        async for chunk in communicate.stream():

            if chunk["type"] == "audio":
                f.write(chunk["data"])


def synthesize_narration(text, voice, output):
    asyncio.run(
        synthesize_async(text, voice, output)
    )


def find_image(image_name, index, image_dir):
    extensions = [
        ".jpg",
        ".jpeg",
        ".png",
        ".webp",
    ]

    # If IMAGE: explicitly specifies a filename
    if image_name:
        possible = Path(image_name)

        candidates = [
            image_dir / possible.name,
            Path(image_name),
        ]

        for candidate in candidates:
            if candidate.exists():
                return candidate

    # Otherwise use automatic scene numbering
    number = index + 1

    for ext in extensions:

        candidate = image_dir / f"{number:03d}{ext}"

        if candidate.exists():
            return candidate

        candidate = image_dir / f"{number}{ext}"

        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"No image found for scene {number}.\n"
        f"Expected something like:\n"
        f"images/{number:03d}.jpg"
    )


def prepare_image(image_path):
    img = Image.open(image_path).convert("RGB")

    src_ratio = img.width / img.height
    dst_ratio = CANVAS_W / CANVAS_H

    if src_ratio > dst_ratio:

        new_h = CANVAS_H
        new_w = int(CANVAS_H * src_ratio)

    else:

        new_w = CANVAS_W
        new_h = int(CANVAS_W / src_ratio)

    img = img.resize(
        (new_w, new_h),
        Image.LANCZOS
    )

    left = (new_w - CANVAS_W) // 2
    top = (new_h - CANVAS_H) // 2

    return img.crop(
        (
            left,
            top,
            left + CANVAS_W,
            top + CANVAS_H
        )
    )


def load_font(size=54):

    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]

    for path in candidates:

        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def wrap_text(draw, text, font, max_width):

    words = text.split()
    lines = []
    current = ""

    for word in words:

        test = word if not current else current + " " + word

        bbox = draw.textbbox(
            (0, 0),
            test,
            font=font
        )

        width = bbox[2] - bbox[0]

        if width <= max_width:
            current = test

        else:

            if current:
                lines.append(current)

            current = word

    if current:
        lines.append(current)

    return lines


def create_caption_frame(image, text, progress):

    frame = image.copy()

    draw = ImageDraw.Draw(frame)

    font = load_font(54)

    max_width = 1500

    lines = wrap_text(
        draw,
        text,
        font,
        max_width
    )

    # Keep captions to a reasonable number of lines
    lines = lines[:3]

    line_height = 68

    total_height = len(lines) * line_height

    y = CANVAS_H - total_height - 80

    for line in lines:

        bbox = draw.textbbox(
            (0, 0),
            line,
            font=font
        )

        text_width = bbox[2] - bbox[0]

        x = (CANVAS_W - text_width) // 2

        # Black outline
        for ox in range(-3, 4):
            for oy in range(-3, 4):

                draw.text(
                    (x + ox, y + oy),
                    line,
                    font=font,
                    fill="black"
                )

        draw.text(
            (x, y),
            line,
            font=font,
            fill="white"
        )

        y += line_height

    return frame


def render_scene(
    image_path,
    narration,
    audio_path,
    output_path,
    duration
):

    base = prepare_image(image_path)

    total_frames = max(
        int(duration * FPS),
        FPS
    )

    # Random but deterministic movement
    direction = random.choice([
        "zoom_in",
        "zoom_out",
        "pan_left",
        "pan_right",
    ])

    ffmpeg_cmd = [
        "ffmpeg",
        "-y",

        "-f",
        "rawvideo",

        "-pix_fmt",
        "rgb24",

        "-s",
        f"{CANVAS_W}x{CANVAS_H}",

        "-r",
        str(FPS),

        "-i",
        "-",

        "-i",
        str(audio_path),

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "20",

        "-pix_fmt",
        "yuv420p",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-shortest",

        str(output_path),

        "-loglevel",
        "error",
    ]

    process = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE
    )

    for i in range(total_frames):

        progress = (
            i / max(total_frames - 1, 1)
        )

        frame = base

        if direction == "zoom_in":

            scale = 1.0 + 0.08 * progress

        elif direction == "zoom_out":

            scale = 1.08 - 0.08 * progress

        else:

            scale = 1.04

        new_w = int(CANVAS_W * scale)
        new_h = int(CANVAS_H * scale)

        resized = frame.resize(
            (new_w, new_h),
            Image.LANCZOS
        )

        if direction == "pan_left":

            max_x = new_w - CANVAS_W
            x = int(max_x * progress)
            y = (new_h - CANVAS_H) // 2

        elif direction == "pan_right":

            max_x = new_w - CANVAS_W
            x = int(max_x * (1 - progress))
            y = (new_h - CANVAS_H) // 2

        else:

            x = (new_w - CANVAS_W) // 2
            y = (new_h - CANVAS_H) // 2

        frame = resized.crop(
            (
                x,
                y,
                x + CANVAS_W,
                y + CANVAS_H
            )
        )

        frame = create_caption_frame(
            frame,
            narration,
            progress
        )

        process.stdin.write(
            frame.convert("RGB").tobytes()
        )

    process.stdin.close()

    process.wait()

    if process.returncode != 0:
        raise RuntimeError(
            f"FFmpeg failed rendering {output_path}"
        )


def concat_clips(clip_paths, output_path, workdir):

    list_file = workdir / "concat.txt"

    with open(list_file, "w", encoding="utf-8") as f:

        for clip in clip_paths:

            safe_path = str(
                clip.resolve()
            ).replace("'", "'\\''")

            f.write(
                f"file '{safe_path}'\n"
            )

    run([
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output_path),
        "-loglevel",
        "error",
    ])


def add_background_music(
    video_path,
    music_path,
    output_path
):

    run([
        "ffmpeg",
        "-y",

        "-i",
        str(video_path),

        "-stream_loop",
        "-1",

        "-i",
        str(music_path),

        "-filter_complex",
        "[1:a]volume=0.12[music];"
        "[0:a][music]amix="
        "inputs=2:"
        "duration=first:"
        "dropout_transition=2[aout]",

        "-map",
        "0:v",

        "-map",
        "[aout]",

        "-c:v",
        "copy",

        "-c:a",
        "aac",

        "-shortest",

        str(output_path),

        "-loglevel",
        "error",
    ])


def main():

    parser = argparse.ArgumentParser(
        description="Animal Universe Flow Image Video Generator"
    )

    parser.add_argument(
        "--script",
        required=True
    )

    parser.add_argument(
        "--out",
        default="output/final_video.mp4"
    )

    parser.add_argument(
        "--voice",
        default=None
    )

    parser.add_argument(
        "--music",
        default=None
    )

    parser.add_argument(
        "--images",
        default="images"
    )

    args = parser.parse_args()

    settings, scenes = parse_script(
        args.script
    )

    voice = (
        args.voice
        or settings["voice"]
    )

    music = (
        args.music
        or settings["music"]
    )

    image_dir = Path(args.images)

    if not image_dir.exists():

        raise FileNotFoundError(
            f"Image directory not found: {image_dir}"
        )

    out_path = Path(args.out)

    out_path.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    workdir = (
        out_path.parent / "_work"
    )

    if workdir.exists():
        shutil.rmtree(workdir)

    workdir.mkdir(
        parents=True
    )

    print(
        f"Loaded {len(scenes)} scenes."
    )

    print(
        f"Voice: {voice}"
    )

    print(
        f"Image folder: {image_dir}"
    )

    clip_paths = []

    for i, scene in enumerate(scenes):

        scene_number = i + 1

        print(
            f"\n[{scene_number}/{len(scenes)}] "
            f"{scene['narration'][:80]}..."
        )

        audio_path = (
            workdir /
            f"scene_{scene_number:03d}.mp3"
        )

        clip_path = (
            workdir /
            f"scene_{scene_number:03d}.mp4"
        )

        image_path = find_image(
            scene["image"],
            i,
            image_dir
        )

        print(
            f" -> using Flow image: "
            f"{image_path}"
        )

        print(
            " -> generating narration "
            "(edge-tts)"
        )

        synthesize_narration(
            scene["narration"],
            voice,
            audio_path
        )

        duration = (
            get_duration(audio_path)
            + 0.3
        )

        print(
            f" -> rendering scene "
            f"({duration:.1f}s)"
        )

        render_scene(
            image_path,
            scene["narration"],
            audio_path,
            clip_path,
            duration
        )

        clip_paths.append(
            clip_path
        )

    print(
        "\nConcatenating scenes..."
    )

    combined = (
        workdir /
        "combined.mp4"
    )

    concat_clips(
        clip_paths,
        combined,
        workdir
    )

    if music and Path(music).exists():

        print(
            "Adding background music..."
        )

        add_background_music(
            combined,
            Path(music),
            out_path
        )

    else:

        shutil.copy(
            combined,
            out_path
        )

    total_duration = get_duration(
        out_path
    )

    print(
        f"\nDONE!"
    )

    print(
        f"Video: {out_path}"
    )

    print(
        f"Duration: {total_duration:.1f}s"
    )

    print(
        f"Scenes: {len(scenes)}"
    )


if __name__ == "__main__":

    try:

        main()

    except Exception as e:

        print(
            f"\nERROR: {e}",
            file=sys.stderr
        )

        sys.exit(1)
