#!/usr/bin/env python3

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path

import edge_tts
from PIL import Image, ImageDraw, ImageFont


DEFAULT_VOICE = "en-US-GuyNeural"
WIDTH = 1920
HEIGHT = 1080
FPS = 30


def run(cmd):
    print(">", " ".join(str(x) for x in cmd))
    subprocess.run(cmd, check=True)


def get_duration(audio_file):
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(result.stdout.strip())


def parse_script(script_path):
    raw = Path(script_path).read_text(encoding="utf-8")

    blocks = raw.split("---")

    voice = DEFAULT_VOICE
    music = None

    header = blocks[0].strip()

    for line in header.splitlines():
        line = line.strip()

        if line.upper().startswith("VOICE:"):
            voice = line.split(":", 1)[1].strip()

        elif line.upper().startswith("MUSIC:"):
            value = line.split(":", 1)[1].strip()
            music = value if value else None

    scenes = []

    for block in blocks[1:]:
        block = block.strip()

        if not block:
            continue

        image_name = None
        narration_lines = []

        for line in block.splitlines():
            line = line.strip()

            if not line:
                continue

            if line.upper().startswith("IMAGE:"):
                image_name = line.split(":", 1)[1].strip()
            else:
                narration_lines.append(line)

        narration = " ".join(narration_lines).strip()

        if not narration:
            continue

        scenes.append(
            {
                "image": image_name,
                "narration": narration,
            }
        )

    if not scenes:
        raise ValueError("No scenes found in script.")

    return voice, music, scenes


async def synthesize_async(text, voice, output):
    communicate = edge_tts.Communicate(text, voice)

    with open(output, "wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])


def synthesize_narration(text, voice, output):
    asyncio.run(synthesize_async(text, voice, output))


def find_image(image_name, image_folder, scene_number):
    folder = Path(image_folder)

    candidates = []

    if image_name:
        candidates.append(folder / image_name)

    candidates.extend(
        [
            folder / f"{scene_number:03d}.jpg",
            folder / f"{scene_number:03d}.jpeg",
            folder / f"{scene_number:03d}.png",
            folder / f"{scene_number}.jpg",
            folder / f"{scene_number}.jpeg",
            folder / f"{scene_number}.png",
        ]
    )

    for path in candidates:
        if path.exists():
            return path

    raise FileNotFoundError(
        f"Image not found for scene {scene_number}. "
        f"Expected something like {folder}/{scene_number:03d}.jpg"
    )


def prepare_image(source, output):
    image = Image.open(source).convert("RGB")

    source_ratio = image.width / image.height
    target_ratio = WIDTH / HEIGHT

    if source_ratio > target_ratio:
        new_width = int(image.height * target_ratio)
        left = (image.width - new_width) // 2
        image = image.crop(
            (left, 0, left + new_width, image.height)
        )
    else:
        new_height = int(image.width / target_ratio)
        top = (image.height - new_height) // 2
        image = image.crop(
            (0, top, image.width, top + new_height)
        )

    image = image.resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    image.save(output, quality=95)


def get_font(size=54):
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
    ]

    for path in font_paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def wrap_text(draw, text, font, max_width):
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = word if not current else current + " " + word
        bbox = draw.textbbox((0, 0), test, font=font)

        if bbox[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    return lines


def create_caption_image(text, output):
    image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    font = get_font(54)

    max_width = int(WIDTH * 0.82)

    lines = wrap_text(draw, text, font, max_width)

    line_height = 68
    total_height = len(lines) * line_height

    y = HEIGHT - total_height - 90

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]

        x = (WIDTH - text_width) // 2

        draw.text(
            (x, y),
            line,
            font=font,
            fill="white",
            stroke_width=4,
            stroke_fill="black",
        )

        y += line_height

    image.save(output)


def render_scene(image, audio, caption, output, duration):
    caption_image = caption

    filter_complex = (
        f"[0:v]scale={WIDTH}:{HEIGHT},"
        f"zoompan="
        f"z='min(zoom+0.0007,1.12)':"
        f"x='iw/2-(iw/zoom/2)':"
        f"y='ih/2-(ih/zoom/2)':"
        f"d={int(duration * FPS)}:"
        f"s={WIDTH}x{HEIGHT}:"
        f"fps={FPS}[base];"
        f"[base][1:v]overlay=0:0:format=auto[v]"
    )

    run(
        [
            "ffmpeg",
            "-y",
            "-loop", "1",
            "-i", str(image),
            "-i", str(caption_image),
            "-i", str(audio),
            "-filter_complex", filter_complex,
            "-map", "[v]",
            "-map", "2:a",
            "-t", str(duration),
            "-r", str(FPS),
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            str(output),
        ]
    )


def concat_clips(clips, output):
    concat_file = output.parent / "concat.txt"

    with open(concat_file, "w", encoding="utf-8") as f:
        for clip in clips:
            f.write(f"file '{clip.resolve()}'\n")

    run(
        [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            str(output),
        ]
    )


def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--script",
        required=True,
        help="Path to script file",
    )

    parser.add_argument(
        "--out",
        default="output/final_video.mp4",
    )

    parser.add_argument(
        "--images",
        default="images",
    )

    parser.add_argument(
        "--voice",
        default=None,
    )

    parser.add_argument(
        "--music",
        default=None,
    )

    args = parser.parse_args()

    script_path = Path(args.script)
    output = Path(args.out)
    image_folder = Path(args.images)

    output.parent.mkdir(parents=True, exist_ok=True)

    temp = output.parent / "temp"
    temp.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("FLOW IMAGE VIDEO GENERATOR")
    print("=" * 60)
    print("Pollinations: DISABLED")
    print("Google Flow images: ENABLED")
    print("=" * 60)

    voice, script_music, scenes = parse_script(script_path)

    if args.voice:
        voice = args.voice

    music = args.music or script_music

    print(f"Voice: {voice}")
    print(f"Scenes: {len(scenes)}")
    print(f"Images folder: {image_folder}")

    clips = []

    for index, scene in enumerate(scenes, start=1):
        print()
        print(f"========== SCENE {index} ==========")

        image_path = find_image(
            scene["image"],
            image_folder,
            index,
        )

        print(f"Image: {image_path}")
        print(f"Narration: {scene['narration'][:100]}...")

        audio_path = temp / f"scene_{index:03d}.mp3"
        prepared_image = temp / f"image_{index:03d}.jpg"
        caption_path = temp / f"caption_{index:03d}.png"
        clip_path = temp / f"clip_{index:03d}.mp4"

        print("Generating Edge-TTS narration...")

        synthesize_narration(
            scene["narration"],
            voice,
            audio_path,
        )

        duration = get_duration(audio_path)

        print(f"Duration: {duration:.2f} seconds")

        print("Preparing image...")

        prepare_image(
            image_path,
            prepared_image,
        )

        print("Creating captions...")

        create_caption_image(
            scene["narration"],
            caption_path,
        )

        print("Rendering scene...")

        render_scene(
            prepared_image,
            audio_path,
            caption_path,
            clip_path,
            duration,
        )

        clips.append(clip_path)

        print(f"Scene {index} complete.")

    print()
    print("========== JOINING SCENES ==========")

    concat_clips(
        clips,
        output,
    )

    if music:
        music_path = Path(music)

        if music_path.exists():
            music_output = output.parent / "final_with_music.mp4"

            run(
                [
                    "ffmpeg",
                    "-y",
                    "-i", str(output),
                    "-stream_loop", "-1",
                    "-i", str(music_path),
                    "-filter_complex",
                    "[1:a]volume=0.08[music];"
                    "[0:a][music]amix=inputs=2:duration=first[a]",
                    "-map", "0:v",
                    "-map", "[a]",
                    "-c:v", "copy",
                    "-c:a", "aac",
                    "-shortest",
                    str(music_output),
                ]
            )

            music_output.replace(output)

    print()
    print("=" * 60)
    print("VIDEO COMPLETE")
    print("=" * 60)
    print(f"Output: {output}")


if __name__ == "__main__":
    main()
