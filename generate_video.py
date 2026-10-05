import argparse
import asyncio
import subprocess
import sys
from pathlib import Path
import math

from PIL import Image, ImageDraw, ImageFont
import edge_tts


DEFAULT_VOICE = "en-US-GuyNeural"
WIDTH = 1920
HEIGHT = 1080
FPS = 30


def run_command(command):
    print("   FFmpeg running...")
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        print("\nFFMPEG ERROR:")
        print(result.stderr[-5000:])
        raise RuntimeError("FFmpeg rendering failed.")

    return result


def parse_script(script_path):
    text = Path(script_path).read_text(encoding="utf-8")

    voice = DEFAULT_VOICE
    music = ""
    scenes = []

    current_image = None
    current_text = []

    for line in text.splitlines():
        stripped = line.strip()

        if stripped.startswith("VOICE:"):
            voice = stripped.split(":", 1)[1].strip() or DEFAULT_VOICE
            continue

        if stripped.startswith("MUSIC:"):
            music = stripped.split(":", 1)[1].strip()
            continue

        if stripped == "---":
            if current_image and current_text:
                scenes.append({
                    "image": current_image,
                    "text": "\n".join(current_text).strip()
                })

            current_image = None
            current_text = []
            continue

        if stripped.startswith("IMAGE:"):
            current_image = stripped.split(":", 1)[1].strip()
            continue

        if current_image:
            current_text.append(line)

    if current_image and current_text:
        scenes.append({
            "image": current_image,
            "text": "\n".join(current_text).strip()
        })

    return voice, music, scenes


def find_image(image_folder, filename):
    image_folder = Path(image_folder)

    exact = image_folder / filename
    if exact.exists():
        return exact

    stem = Path(filename).stem

    possible = [
        image_folder / f"{stem}.jpg",
        image_folder / f"{stem}.jpeg",
        image_folder / f"{stem}.png",
        image_folder / f"{stem}.webp",
    ]

    for item in possible:
        if item.exists():
            return item

    # Also try matching numbered files
    try:
        number = int(stem)
        for ext in ["jpg", "jpeg", "png", "webp"]:
            candidate = image_folder / f"{number:03d}.{ext}"
            if candidate.exists():
                return candidate
    except ValueError:
        pass

    return None


async def generate_voice(text, voice, output_file):
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice
    )

    await communicate.save(str(output_file))


def get_audio_duration(audio_file):
    command = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_file)
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not determine audio duration:\n" +
            result.stderr
        )

    return float(result.stdout.strip())


def create_caption_image(text, output_file):
    canvas = Image.new(
        "RGBA",
        (1800, 300),
        (0, 0, 0, 0)
    )

    draw = ImageDraw.Draw(canvas)

    font_candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"
    ]

    font_path = None

    for candidate in font_candidates:
        if Path(candidate).exists():
            font_path = candidate
            break

    if font_path:
        font = ImageFont.truetype(font_path, 48)
    else:
        font = ImageFont.load_default()

    # Limit caption length per line
    words = text.replace("\n", " ").split()

    lines = []
    current = ""

    for word in words:
        test = (current + " " + word).strip()

        bbox = draw.textbbox((0, 0), test, font=font)

        if bbox[2] - bbox[0] > 1650:
            if current:
                lines.append(current)
            current = word
        else:
            current = test

    if current:
        lines.append(current)

    # Keep only a reasonable number of caption lines
    if len(lines) > 5:
        lines = lines[:5]

    line_heights = []

    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_heights.append(bbox[3] - bbox[1])

    total_height = sum(line_heights) + max(0, len(lines) - 1) * 15
    y = (300 - total_height) // 2

    for index, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]

        x = (1800 - text_width) // 2

        # Black outline
        draw.text(
            (x, y),
            line,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=5,
            stroke_fill=(0, 0, 0, 230)
        )

        y += line_heights[index] + 15

    canvas.save(output_file)


def prepare_image(source, destination):
    img = Image.open(source).convert("RGB")

    # Make sure the image fills a 16:9 frame.
    source_ratio = img.width / img.height
    target_ratio = WIDTH / HEIGHT

    if source_ratio > target_ratio:
        new_height = HEIGHT
        new_width = int(new_height * source_ratio)
    else:
        new_width = WIDTH
        new_height = int(new_width / source_ratio)

    img = img.resize(
        (new_width, new_height),
        Image.Resampling.LANCZOS
    )

    left = max(0, (img.width - WIDTH) // 2)
    top = max(0, (img.height - HEIGHT) // 2)

    img = img.crop(
        (left, top, left + WIDTH, top + HEIGHT)
    )

    img.save(destination, quality=95)


def render_scene(
    image_file,
    audio_file,
    caption_file,
    output_file,
    duration
):
    # Create a slightly larger image.
    # FFmpeg then slowly moves the crop around the image,
    # creating a subtle documentary camera movement.
    zoom_width = 2200
    zoom_height = 1238

    filter_complex = (
        f"[0:v]"
        f"scale={zoom_width}:{zoom_height}:flags=lanczos,"
        f"crop={WIDTH}:{HEIGHT}:"
        f"x='(iw-ow)*(0.5+0.12*sin(2*PI*t/18))':"
        f"y='(ih-oh)*(0.5+0.08*cos(2*PI*t/18))'"
        f"[bg];"
        f"[bg][2:v]"
        f"overlay=(W-w)/2:H-h-55:format=auto,"
        f"format=yuv420p"
        f"[v]"
    )

    command = [
        "ffmpeg",
        "-y",

        "-loop",
        "1",
        "-i",
        str(image_file),

        "-i",
        str(audio_file),

        "-loop",
        "1",
        "-i",
        str(caption_file),

        "-filter_complex",
        filter_complex,

        "-map",
        "[v]",
        "-map",
        "1:a",

        "-t",
        f"{duration:.3f}",

        "-r",
        str(FPS),

        "-c:v",
        "libx264",

        "-preset",
        "veryfast",

        "-crf",
        "22",

        "-c:a",
        "aac",

        "-b:a",
        "192k",

        "-pix_fmt",
        "yuv420p",

        "-movflags",
        "+faststart",

        str(output_file)
    ]

    run_command(command)


def concat_clips(clips, output_file):
    list_file = output_file.parent / "concat_list.txt"

    with open(list_file, "w", encoding="utf-8") as f:
        for clip in clips:
            path = clip.resolve()
            f.write(
                "file '" +
                str(path).replace("'", "'\\''") +
                "'\n"
            )

    command = [
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
        "-movflags",
        "+faststart",
        str(output_file)
    ]

    run_command(command)


async def main():
    parser = argparse.ArgumentParser()

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
        default=""
    )

    parser.add_argument(
        "--images",
        default="images"
    )

    args = parser.parse_args()

    script_path = Path(args.script)
    output_file = Path(args.out)
    image_folder = Path(args.images)

    output_file.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    work_folder = output_file.parent / "work"
    work_folder.mkdir(
        parents=True,
        exist_ok=True
    )

    voice, music, scenes = parse_script(script_path)

    if args.voice:
        voice = args.voice

    print("")
    print("======================================")
    print("FLOW IMAGE VIDEO GENERATOR")
    print("======================================")
    print("Pollinations: DISABLED")
    print("Google Flow images: ENABLED")
    print(f"Voice: {voice}")
    print(f"Image folder: {image_folder}")
    print(f"Scenes: {len(scenes)}")
    print("")

    if not scenes:
        raise RuntimeError(
            "No scenes were found in the script."
        )

    clips = []

    for index, scene in enumerate(scenes, start=1):

        print(
            f"[{index}/{len(scenes)}] "
            f"{scene['text'][:90]}..."
        )

        image_file = find_image(
            image_folder,
            scene["image"]
        )

        if not image_file:
            raise FileNotFoundError(
                f"Could not find image: {scene['image']}"
            )

        print(
            f" -> using Flow image: {image_file}"
        )

        prepared_image = (
            work_folder /
            f"scene_{index:03d}_image.jpg"
        )

        prepare_image(
            image_file,
            prepared_image
        )

        audio_file = (
            work_folder /
            f"scene_{index:03d}.mp3"
        )

        print(
            " -> generating narration (edge-tts)"
        )

        await generate_voice(
            scene["text"],
            voice,
            audio_file
        )

        duration = get_audio_duration(
            audio_file
        )

        print(
            f" -> narration duration: {duration:.2f}s"
        )

        caption_file = (
            work_folder /
            f"scene_{index:03d}_caption.png"
        )

        create_caption_image(
            scene["text"],
            caption_file
        )

        scene_video = (
            work_folder /
            f"scene_{index:03d}.mp4"
        )

        print(
            f" -> rendering scene ({duration:.1f}s)"
        )

        render_scene(
            prepared_image,
            audio_file,
            caption_file,
            scene_video,
            duration
        )

        clips.append(scene_video)

        print(
            f" -> scene {index} complete"
        )

    print("")
    print("Joining scenes...")

    concat_clips(
        clips,
        output_file
    )

    print("")
    print("======================================")
    print("VIDEO COMPLETE")
    print("======================================")
    print(f"Output: {output_file}")


if __name__ == "__main__":
    asyncio.run(main())
