"""Recreate an image out of the characters of a font.

Every character the font supports is rendered to a small bitmap. The source
image is then cut into rows one line-height tall, and each row is filled left
to right with whichever character looks most like that patch of the image.

Example:
    python img_to_txt.py photo.jpg --font courbd.ttf --size 40 --scale 2
"""
import argparse
import hashlib
import pickle
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

APP_DIR = Path(__file__).resolve().parent
CACHE_DIR = APP_DIR / ".cache"
FONT_SEARCH_DIRS = [
    APP_DIR / "fonts",
    APP_DIR / "sandbox",
    Path(r"C:\Windows\Fonts"),
    Path.home() / r"AppData\Local\Microsoft\Windows\Fonts",
]
# Unicode categories that never draw anything useful:
# control, format, surrogate, unassigned, line/paragraph separators
SKIP_CATEGORIES = {"Cc", "Cf", "Cs", "Cn", "Zl", "Zp"}


@dataclass
class GlyphGroup:
    """All glyphs that share one advance width, stacked for fast comparison."""
    width: int
    chars: list[str]
    bitmaps: np.ndarray  # (count, line_height, width), uint8, black ink on white


@dataclass
class GlyphSet:
    font_path: Path
    font_size: int
    line_height: int
    groups: list[GlyphGroup]

    @property
    def count(self) -> int:
        return sum(len(g.chars) for g in self.groups)


def find_font(name: str) -> Path:
    """Accept a full path, or a bare file name looked up in the usual font folders."""
    path = Path(name)
    if path.is_file():
        return path
    for folder in FONT_SEARCH_DIRS:
        for candidate in (folder / name, folder / f"{name}.ttf"):
            if candidate.is_file():
                return candidate
    searched = "\n  ".join(str(d) for d in FONT_SEARCH_DIRS)
    sys.exit(f"Font '{name}' not found. Looked in:\n  {searched}")


def available_fonts() -> dict[str, Path]:
    """Every usable font in the search folders, keyed by a readable label like 'Calibri Bold'.

    Earlier folders win, so a copy in ./fonts overrides the Windows one.
    """
    fonts: dict[str, Path] = {}
    seen_files = set()
    for folder in FONT_SEARCH_DIRS:
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            if path.suffix.lower() not in {".ttf", ".otf", ".ttc"} or path.name.lower() in seen_files:
                continue
            try:
                with TTFont(path, fontNumber=0, lazy=True) as tt:
                    label = tt["name"].getBestFullName() or path.stem
            except Exception:
                continue  # damaged or unsupported font file
            seen_files.add(path.name.lower())
            if label in fonts:
                label = f"{label} ({path.name})"
            fonts[label] = path
    return dict(sorted(fonts.items(), key=lambda item: item[0].lower()))


def supported_characters(font_path: Path, first: int, last: int) -> list[str]:
    """Characters in [first, last] that the font has a glyph for (read from its cmap)."""
    with TTFont(font_path, fontNumber=0, lazy=True) as tt:
        cmap = tt.getBestCmap() or {}
    return [chr(code) for code in sorted(cmap)
            if first <= code <= last and unicodedata.category(chr(code)) not in SKIP_CATEGORIES]


def build_glyph_set(font_path: Path, font_size: int, first: int, last: int) -> GlyphSet:
    font = ImageFont.truetype(str(font_path), font_size)
    ascent, descent = font.getmetrics()
    line_height = ascent + descent

    chars = supported_characters(font_path, first, last)
    print(f"{font_path.name} has {len(chars):,} characters in range {first}-{last}")

    # Render each character into a cell as wide as its advance width (the space a
    # text editor gives it), so the .txt output lines up the same way as the image.
    # Identical-looking glyphs are dropped; the first one (lowest code point) wins.
    by_width: dict[int, dict[bytes, str]] = {}
    for ch in chars:
        width = round(font.getlength(ch))
        if width <= 0:
            continue
        cell = Image.new("L", (width, line_height), 255)
        ImageDraw.Draw(cell).text((0, 0), ch, font=font, fill=0)
        by_width.setdefault(width, {}).setdefault(cell.tobytes(), ch)
    if not by_width:
        raise ValueError(f"{font_path.name} has no drawable characters in range {first}-{last}")

    groups = []
    for width, unique in sorted(by_width.items()):
        bitmaps = np.frombuffer(b"".join(unique), dtype=np.uint8)
        groups.append(GlyphGroup(width, list(unique.values()),
                                 bitmaps.reshape(len(unique), line_height, width)))

    glyphs = GlyphSet(font_path, font_size, line_height, groups)
    print(f"{glyphs.count:,} unique glyphs, widths {[g.width for g in groups]}, "
          f"line height {line_height}")
    return glyphs


def load_glyph_set(font_path: Path, font_size: int, first: int, last: int,
                   use_cache: bool = True) -> GlyphSet:
    """Build the glyph set, or reuse a cached one made with the same settings."""
    font_stat = font_path.stat()
    key = f"{font_path.resolve()}|{font_stat.st_size}|{font_stat.st_mtime}|{font_size}|{first}|{last}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:12]
    cache_file = CACHE_DIR / f"{font_path.stem}_{font_size}pt_{digest}.pickle"

    # The cache holds plain tuples rather than GlyphSet objects, so it loads no matter
    # whether this module was run as a script or imported by the web app.
    if use_cache and cache_file.exists():
        try:
            with cache_file.open("rb") as f:
                line_height, groups = pickle.load(f)
            glyphs = GlyphSet(font_path, font_size, line_height, [GlyphGroup(*g) for g in groups])
            print(f"Loaded {glyphs.count:,} glyphs from cache {cache_file.name}")
            return glyphs
        except Exception:
            print(f"Cache {cache_file.name} is unreadable or from an older version; rebuilding")

    glyphs = build_glyph_set(font_path, font_size, first, last)
    CACHE_DIR.mkdir(exist_ok=True)
    with cache_file.open("wb") as f:
        pickle.dump((glyphs.line_height, [(g.width, g.chars, g.bitmaps) for g in glyphs.groups]), f)
    return glyphs


def prepare_image(image: Image.Image, scale: float, black_and_white: bool) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("L")
    if scale != 1:
        image = image.resize((round(image.width * scale), round(image.height * scale)),
                             Image.Resampling.LANCZOS)
    image = ImageOps.autocontrast(image)
    if black_and_white:
        # Floyd-Steinberg dithering, like the original version of this app
        image = image.convert("1").convert("L")
    return image


class Matcher:
    """Finds the glyph closest (lowest mean squared error) to a patch of the image.

    Squared error expands to |g|^2 - 2 g.s + |s|^2, so with |g|^2 precomputed,
    scoring every glyph of a width at once is a single matrix-vector product.
    """

    def __init__(self, glyphs: GlyphSet, blur: float):
        self.blur = blur
        self.groups = []
        for group in glyphs.groups:
            stack = group.bitmaps
            if blur:
                stack = np.stack([np.asarray(Image.fromarray(b).filter(ImageFilter.GaussianBlur(blur)))
                                  for b in stack])
            flat = stack.reshape(len(stack), -1).astype(np.float32)
            self.groups.append((group, flat, (flat ** 2).sum(axis=1)))
        self.min_width = min(g.width for g in glyphs.groups)

    def compare_image(self, image: Image.Image) -> np.ndarray:
        """The image as the matcher sees it (blurred the same way as the glyphs)."""
        if self.blur:
            image = image.filter(ImageFilter.GaussianBlur(self.blur))
        return np.asarray(image, dtype=np.float32)

    def best_match(self, row: np.ndarray, x: int) -> tuple[str, int]:
        best_char, best_width, best_error = " ", self.min_width, np.inf
        remaining = row.shape[1] - x
        for group, flat, norms in self.groups:
            if group.width > remaining:
                break  # groups are sorted by width
            sample = row[:, x:x + group.width].reshape(-1)
            errors = (norms - 2 * (flat @ sample) + sample @ sample) / sample.size
            i = int(errors.argmin())
            # on a tie prefer the wider glyph: it covers more of the image per character
            if errors[i] <= best_error:
                best_char, best_width, best_error = group.chars[i], group.width, errors[i]
        return best_char, best_width


def print_progress(row: int, rows: int, elapsed: float) -> None:
    print(f"\rRow {row}/{rows}  ({elapsed:.1f}s)", end="\n" if row == rows else "", flush=True)


def convert(image: Image.Image, glyphs: GlyphSet, blur: float,
            progress: Callable[[int, int, float], None] = print_progress) -> tuple[list[str], Image.Image]:
    """Return the matched text (one string per row) and the rendered result."""
    matcher = Matcher(glyphs, blur)
    pixels = matcher.compare_image(image)
    bitmaps = {ch: group.bitmaps[i] for group in glyphs.groups for i, ch in enumerate(group.chars)}

    rows = image.height // glyphs.line_height
    output = Image.new("L", (image.width, rows * glyphs.line_height), 255)
    lines = []
    start = time.perf_counter()
    for r in range(rows):
        top = r * glyphs.line_height
        row = pixels[top:top + glyphs.line_height]
        x, line = 0, []
        while row.shape[1] - x >= matcher.min_width:
            ch, width = matcher.best_match(row, x)
            output.paste(Image.fromarray(bitmaps[ch]), (x, top))
            line.append(ch)
            x += width
        lines.append("".join(line))
        progress(r + 1, rows, time.perf_counter() - start)
    return lines, output


def character_map(glyphs: GlyphSet, per_row: int = 50) -> Image.Image:
    """A sheet showing every glyph available for matching, in fixed-size cells."""
    cell_w = max(g.width for g in glyphs.groups)
    cells = [b for g in glyphs.groups for b in g.bitmaps]
    rows = -(-len(cells) // per_row)
    sheet = Image.new("L", (per_row * cell_w, rows * glyphs.line_height), 255)
    for i, bitmap in enumerate(cells):
        sheet.paste(Image.fromarray(bitmap), ((i % per_row) * cell_w, (i // per_row) * glyphs.line_height))
    return sheet


def parse_range(text: str) -> tuple[int, int]:
    """'32-126', '0x20-0x7e' or '32-' (open ended)."""
    first, _, last = text.partition("-")
    first_code = int(first, 0)
    last_code = int(last, 0) if last else sys.maxunicode
    if last_code < first_code:
        raise argparse.ArgumentTypeError("range end must not be before range start")
    return first_code, last_code


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", type=Path, help="image to convert")
    parser.add_argument("-f", "--font", default="courbd.ttf",
                        help="font file name or path (default: %(default)s)")
    parser.add_argument("-s", "--size", type=int, default=40, help="font size in points (default: %(default)s)")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="resize the image by this factor first; bigger = more characters (default: %(default)s)")
    parser.add_argument("-r", "--range", type=parse_range, default=(32, 0xFFFF), metavar="FIRST-LAST",
                        help="Unicode code points to use, e.g. 32-126 for plain ASCII (default: 32-65535)")
    parser.add_argument("--blur", type=float, default=0.0,
                        help="blur radius used when comparing; higher favours tone over shape (default: none)")
    parser.add_argument("--bw", action="store_true",
                        help="dither the image to pure black and white first (the original app's look)")
    parser.add_argument("-o", "--out-dir", type=Path, default=APP_DIR / "output",
                        help="where to write results (default: ./output)")
    parser.add_argument("--charmap", action="store_true", help="also save an image of every glyph used")
    parser.add_argument("--no-cache", action="store_true", help="rebuild the glyph set even if cached")
    parser.add_argument("--show", action="store_true", help="open the result when done")
    args = parser.parse_args()

    if not args.image.is_file():
        sys.exit(f"Image not found: {args.image}")
    font_path = find_font(args.font)
    first, last = args.range

    try:
        glyphs = load_glyph_set(font_path, args.size, first, last, use_cache=not args.no_cache)
    except ValueError as error:
        sys.exit(str(error))
    image = prepare_image(Image.open(args.image), args.scale, args.bw)
    print(f"Image {image.width}x{image.height} -> "
          f"{image.height // glyphs.line_height} rows of text")

    lines, result = convert(image, glyphs, args.blur)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    base = args.out_dir / f"{args.image.stem}_{font_path.stem}_{args.size}pt"
    result.save(base.with_suffix(".png"))
    base.with_suffix(".txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Saved {base.with_suffix('.png')}\n  and {base.with_suffix('.txt')}")

    if args.charmap:
        map_path = args.out_dir / f"charmap_{font_path.stem}_{args.size}pt.png"
        character_map(glyphs).save(map_path)
        print(f"Saved {map_path}")

    if args.show:
        result.show()


if __name__ == "__main__":
    main()
