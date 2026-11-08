"""Web interface for img_to_txt.

Run with:  python app.py   (opens in your browser at http://127.0.0.1:7860)
"""
import time

import gradio as gr
from PIL import Image, ImageDraw, ImageFont

import img_to_txt as core

FONTS = core.available_fonts()
DEFAULT_FONT = next((label for label, path in FONTS.items() if path.name.lower() == "courbd.ttf"),
                    next(iter(FONTS), None))
CHARACTER_SETS = {
    "Plain ASCII (32–126)": (32, 126),
    "Latin & accents (32–591)": (32, 0x24F),
    "Everything in the basic plane (32–65535)": (32, 0xFFFF),
    "Everything (32–1114111)": (32, 0x10FFFF),
    "Custom range…": None,
}
DEFAULT_SET = "Everything in the basic plane (32–65535)"
SAMPLE_TEXT = "AaBbCc 0123 @#&% ░▒▓█ ←↑→"

CSS = """
#result-text textarea, #result-text .cm-content { white-space: pre; }
footer { display: none !important; }
"""


def font_sample(font_label: str) -> Image.Image | None:
    """A strip of sample text in the chosen font, so you can see it before converting."""
    if font_label not in FONTS:
        return None
    font = ImageFont.truetype(str(FONTS[font_label]), 36)
    left, top, right, bottom = font.getbbox(SAMPLE_TEXT)
    sample = Image.new("L", (right + 24, bottom + 24), 255)
    ImageDraw.Draw(sample).text((12, 12), SAMPLE_TEXT, font=font, fill=0)
    return sample


def run(image, font_label, size, character_set, custom_range, scale, blur, black_and_white,
        progress=gr.Progress()):
    if image is None:
        raise gr.Error("Upload an image first.")
    if font_label not in FONTS:
        raise gr.Error("Pick a font from the list.")
    if CHARACTER_SETS[character_set] is None:
        try:
            first, last = core.parse_range(custom_range.strip())
        except Exception:  # ValueError for bad numbers, ArgumentTypeError for end < start
            raise gr.Error("Custom range should look like 32-126 or 0x2500-0x259F, with the end after the start.")
    else:
        first, last = CHARACTER_SETS[character_set]

    font_path = FONTS[font_label]
    progress(0, desc=f"Preparing {font_label} glyphs (first time for a size can take a moment)…")
    try:
        glyphs = core.load_glyph_set(font_path, int(size), first, last)
    except ValueError as error:
        raise gr.Error(str(error))

    prepared = core.prepare_image(image, scale, black_and_white)
    if prepared.height < glyphs.line_height:
        raise gr.Error("The image is shorter than one line of text. Increase the scale or lower the font size.")

    start = time.perf_counter()
    lines, result = core.convert(prepared, glyphs, blur,
                                 progress=lambda row, rows, _: progress(row / rows, desc=f"Row {row} of {rows}"))
    elapsed = time.perf_counter() - start

    core.APP_DIR.joinpath("output").mkdir(exist_ok=True)
    base = core.APP_DIR / "output" / f"web_{time.strftime('%Y%m%d_%H%M%S')}_{font_path.stem}_{int(size)}pt"
    png_path, txt_path = base.with_suffix(".png"), base.with_suffix(".txt")
    result.save(png_path)
    text = "\n".join(lines) + "\n"
    txt_path.write_text(text, encoding="utf-8")

    used = len(set("".join(lines)))
    widths = [g.width for g in glyphs.groups]
    stats = (f"**{len(lines)} rows × up to {max(map(len, lines))} characters** · "
             f"{used:,} different characters used out of {glyphs.count:,} available · "
             f"{'monospace' if len(widths) == 1 else f'{len(widths)} character widths'} · "
             f"converted in {elapsed:.1f}s")
    return result, text, [str(png_path), str(txt_path)], core.character_map(glyphs), stats


def toggle_custom(character_set):
    return gr.update(visible=CHARACTER_SETS[character_set] is None)


with gr.Blocks(title="Image to Text") as demo:
    gr.Markdown("# Image to Text\nRebuild a picture out of the characters of any font on this computer.")

    with gr.Row():
        with gr.Column(scale=1, min_width=320):
            image_in = gr.Image(label="Image", type="pil", sources=["upload", "clipboard"], height=280)
            font = gr.Dropdown(list(FONTS), value=DEFAULT_FONT, label="Font", filterable=True,
                               info=f"{len(FONTS)} fonts found. Type to search.")
            sample = gr.Image(value=font_sample(DEFAULT_FONT), show_label=False, container=False,
                              interactive=False, height=60, buttons=[])
            size = gr.Slider(8, 120, value=40, step=1, label="Font size (px)",
                             info="Smaller = more characters and more detail")
            scale = gr.Slider(0.25, 4, value=1, step=0.25, label="Image scale",
                              info="Resize the image before converting")
            character_set = gr.Dropdown(list(CHARACTER_SETS), value=DEFAULT_SET, label="Characters to use")
            custom_range = gr.Textbox(label="Custom range", placeholder="e.g. 32-126 or 0x2500-0x259F",
                                      visible=False)
            with gr.Accordion("Advanced", open=False):
                blur = gr.Slider(0, 8, value=0, step=0.5, label="Blur when matching",
                                 info="0 matches shapes and edges; higher values match shading")
                black_and_white = gr.Checkbox(False, label="Dither to black & white first (the original look)")
            convert_btn = gr.Button("Convert", variant="primary", size="lg")

        with gr.Column(scale=2):
            stats = gr.Markdown()
            with gr.Tabs():
                with gr.Tab("Image"):
                    image_out = gr.Image(label="Result", type="pil", format="png", interactive=False,
                                         height=640, buttons=["download", "fullscreen"])
                with gr.Tab("Text"):
                    text_out = gr.Code(label="Text", language=None, wrap_lines=False, lines=24, max_lines=40,
                                       elem_id="result-text", buttons=["copy"])
                with gr.Tab("Character set"):
                    charmap_out = gr.Image(label="Every glyph available for matching", type="pil",
                                           interactive=False, buttons=["download", "fullscreen"])
            files_out = gr.File(label="Downloads", file_count="multiple", interactive=False)

    font.change(font_sample, font, sample)
    character_set.change(toggle_custom, character_set, custom_range)
    convert_btn.click(run, [image_in, font, size, character_set, custom_range, scale, blur, black_and_white],
                      [image_out, text_out, files_out, charmap_out, stats])

if __name__ == "__main__":
    demo.launch(inbrowser=True, theme=gr.themes.Soft(), css=CSS)
