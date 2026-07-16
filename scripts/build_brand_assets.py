"""Build deterministic Manyselves brand exports from the generated master mark."""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
SCREENSHOTS = ROOT / "assets" / "screenshots"
RESOURCES = ROOT / "manyselves" / "resources"

NAVY = "#090B1A"
PANEL = "#11152B"
PANEL_2 = "#171B36"
INK = "#F7F8FF"
MUTED = "#AEB5D4"
INDIGO = "#696CF5"
VIOLET = "#9A63F7"
CYAN = "#2FD8EF"


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        "/System/Library/Fonts/SFNS.ttf" if not bold else "/System/Library/Fonts/SFNS-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/Library/Fonts/Arial.ttf",
    )
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default(size=size)


def vertical_gradient(size: tuple[int, int], top: str, bottom: str) -> Image.Image:
    image = Image.new("RGB", size)
    draw = ImageDraw.Draw(image)
    top_rgb = tuple(int(top[i : i + 2], 16) for i in (1, 3, 5))
    bottom_rgb = tuple(int(bottom[i : i + 2], 16) for i in (1, 3, 5))
    for y in range(size[1]):
        ratio = y / max(size[1] - 1, 1)
        color = tuple(round(a + (b - a) * ratio) for a, b in zip(top_rgb, bottom_rgb, strict=True))
        draw.line((0, y, size[0], y), fill=color)
    return image


def place_mark(canvas: Image.Image, box: tuple[int, int, int, int]) -> None:
    mark = Image.open(BRAND / "manyselves-mark.png").convert("RGBA")
    width, height = box[2] - box[0], box[3] - box[1]
    mark.thumbnail((width, height), Image.Resampling.LANCZOS)
    x = box[0] + (width - mark.width) // 2
    y = box[1] + (height - mark.height) // 2
    canvas.alpha_composite(mark, (x, y))


def build_icon() -> None:
    size = 1024
    canvas = vertical_gradient((size, size), "#171A3A", "#080A18").convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((24, 24, size - 24, size - 24), radius=220, fill=255)
    canvas.putalpha(mask)
    place_mark(canvas, (158, 158, 866, 866))
    canvas.save(RESOURCES / "icon.png")
    canvas.save(BRAND / "manyselves-app-icon.png")


def build_title() -> None:
    canvas = vertical_gradient((1400, 420), "#121631", NAVY).convert("RGBA")
    draw = ImageDraw.Draw(canvas)
    draw.ellipse((1050, -280, 1570, 240), fill=(105, 108, 245, 42))
    draw.ellipse((1110, 160, 1490, 540), fill=(47, 216, 239, 24))
    place_mark(canvas, (90, 64, 370, 344))
    draw.text((415, 78), "Manyselves", font=font(76, bold=True), fill=INK)
    draw.text((420, 180), "One runtime. Many selves.", font=font(34), fill=CYAN)
    draw.text(
        (420, 244),
        "A local workspace for document-defined agent teams.",
        font=font(25),
        fill=MUTED,
    )
    canvas.convert("RGB").save(SCREENSHOTS / "title.png", quality=94)


def rounded_card(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], accent: str) -> None:
    draw.rounded_rectangle(box, radius=24, fill=PANEL_2, outline="#30365C", width=2)
    x1, y1, _, y2 = box
    draw.rounded_rectangle((x1, y1, x1 + 10, y2), radius=5, fill=accent)


def build_workflow() -> None:
    canvas = vertical_gradient((1600, 700), "#11152D", NAVY).convert("RGB")
    draw = ImageDraw.Draw(canvas)
    draw.text((90, 60), "How Manyselves becomes a team", font=font(48, bold=True), fill=INK)
    draw.text(
        (92, 124),
        "Change the documents, skills and tools. Keep the runtime.",
        font=font(25),
        fill=MUTED,
    )

    items = (
        ("01", "Identity docs", "Roles, boundaries, handoffs", INDIGO),
        ("02", "Main Agent", "Plans and coordinates", VIOLET),
        ("03", "Specialist selves", "Focused skills and review", CYAN),
        ("04", "Workspace tools", "Files, tasks, checkpoints", INDIGO),
        ("05", "Deliverables", "Documents and artifacts", VIOLET),
    )
    x_positions = (90, 395, 700, 1005, 1310)
    for index, ((number, title, detail, accent), x) in enumerate(zip(items, x_positions, strict=True)):
        box = (x, 245, x + 220, 480)
        rounded_card(draw, box, accent)
        draw.text((x + 28, 275), number, font=font(22, bold=True), fill=accent)
        draw.text((x + 28, 330), title, font=font(26, bold=True), fill=INK)
        words = detail.split(" ")
        if len(words) > 2:
            detail = " ".join(words[:2]) + "\n" + " ".join(words[2:])
        draw.multiline_text((x + 28, 382), detail, font=font(19), fill=MUTED, spacing=7)
        if index < len(items) - 1:
            arrow_x = x + 246
            draw.line((arrow_x, 362, arrow_x + 92, 362), fill="#626A96", width=4)
            draw.polygon(((arrow_x + 92, 362), (arrow_x + 76, 351), (arrow_x + 76, 373)), fill="#626A96")

    draw.rounded_rectangle((90, 555, 1510, 625), radius=22, fill="#131934", outline="#29305A", width=2)
    draw.text((120, 574), "Bundled capability", font=font(21, bold=True), fill=CYAN)
    draw.text(
        (330, 574),
        "Power-distribution reports — one team definition shipped with this repository",
        font=font(21),
        fill=INK,
    )
    canvas.save(SCREENSHOTS / "workflow.png", quality=94)


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    RESOURCES.mkdir(parents=True, exist_ok=True)
    build_icon()
    build_title()
    build_workflow()


if __name__ == "__main__":
    main()
