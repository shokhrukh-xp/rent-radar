"""Знак Ra'no «дом-сообщение» и надпись Ra'no — SVG и PNG для сайта, мини-приложения и документов.
Надпись — шрифт Outfit SemiBold (OFL), переведена в контуры: на странице шрифт не нужен.
Запуск: python3 docs/brand/make_mark.py <папка со шрифтом Outfit[wght].ttf>  (pip: fonttools uharfbuzz cairosvg pillow)"""
import base64, io, json, os, sys
import cairosvg
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from PIL import Image

E, IV, AP, INK = "#0E6B53", "#F6F1E7", "#E8945A", "#111A17"
OUT = os.path.dirname(os.path.abspath(__file__))
HOUSE = "M158 102 V94 Q158 85 151 79 L108 43 Q100 37 92 43 L49 79 Q42 85 42 94 V134 Q42 150 58 150 H84 V174"
TAIL = "M84 174 C116 170 156 156 158 126"


def mark(c1=E, c2=AP, stroke=22):
    """Знак в квадрате 200×200 (поле вокруг уже есть)."""
    return (f'<g fill="none" stroke-width="{stroke}" stroke-linecap="round" stroke-linejoin="round">'
            f'<path stroke="{c1}" d="{HOUSE}"/><path stroke="{c2}" d="{TAIL}"/></g>')


def word_paths(font_dir, size=96, x0=0.0, base=0.0):
    f = TTFont(os.path.join(font_dir, "Outfit[wght].ttf"))
    f = instancer.instantiateVariableFont(f, {"wght": 600})
    buf = io.BytesIO(); f.save(buf)
    hf = hb.Font(hb.Face(buf.getvalue()))
    b = hb.Buffer(); b.add_str("Ra’no"); b.guess_segment_properties()
    hb.shape(hf, b, {"kern": True})
    sc = size / f["head"].unitsPerEm
    gs, order = f.getGlyphSet(), f.getGlyphOrder()
    x, word, apos = x0, [], ""
    for info, pos in zip(b.glyph_infos, b.glyph_positions):
        pen = SVGPathPen(gs, lambda v: f"{v:.1f}".rstrip("0").rstrip("."))
        gs[order[info.codepoint]].draw(TransformPen(pen, (sc, 0, 0, -sc, x, base)))
        if "’" == "Ra’no"[info.cluster]:
            apos = pen.getCommands()
        else:
            word.append(pen.getCommands())
        x += pos.x_advance * sc
    return "".join(word), apos, x - x0


def lockup(font_dir, word=E, house=E, tail=AP, apos=AP):
    """Знак слева, надпись справа. viewBox подогнан под содержимое."""
    w, a, wd = word_paths(font_dir, 100, 204, 141)
    W = round(204 + wd + 16)
    body = (f'<g transform="translate(0 0)">{mark(house, tail)}</g>'
            f'<path fill="{word}" d="{w}"/><path fill="{apos}" d="{a}"/>')
    return W, body


def svg(vb, body, w=None, h=None, title="Ra'no"):
    size = f' width="{w}" height="{h}"' if w else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}"{size} role="img" aria-label="{title}">'
            f'<title>{title}</title>{body}</svg>\n')


def icon(bg=E, fg=IV, tail=AP, rx=46, pad=0, stroke=22):
    s = (200 - 2 * pad) / 200
    return (f'<rect width="200" height="200" rx="{rx}" fill="{bg}"/>'
            f'<g transform="translate({pad} {pad}) scale({s:.4f})">{mark(fg, tail, stroke)}</g>')


def main(font_dir):
    files = {}
    files["rano_mark.svg"] = svg("26 22 148 168", mark())
    files["rano_mark_on_dark.svg"] = svg("26 22 148 168", mark(IV, AP))
    W, body = lockup(font_dir)
    files["rano_logo.svg"] = svg(f"26 22 {W - 26} 168", body)
    W2, body2 = lockup(font_dir, IV, IV, AP, AP)
    files["rano_logo_on_dark.svg"] = svg(f"26 22 {W2 - 26} 168", body2)
    files["rano_icon.svg"] = svg("0 0 200 200", icon())
    files["favicon.svg"] = svg("0 0 200 200", icon(rx=44, pad=-6, stroke=28))
    for n, s in files.items():
        open(os.path.join(OUT, n), "w").write(s)
    for px in (512, 192, 180):
        cairosvg.svg2png(bytestring=files["rano_icon.svg"].encode(), write_to=os.path.join(OUT, f"rano_icon_{px}.png"),
                         output_width=px, output_height=px)
    ims = []
    for px in (16, 32, 48):
        b = cairosvg.svg2png(bytestring=files["favicon.svg"].encode(), output_width=px, output_height=px)
        ims.append(Image.open(io.BytesIO(b)).convert("RGBA"))
    ims[-1].save(os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)], append_images=ims[:-1])
    cairosvg.svg2png(bytestring=files["rano_logo.svg"].encode(), write_to=os.path.join(OUT, "rano_logo.png"), output_width=1200)
    # для воркера: пути знака и надписи — в JS (страница /about, мини-приложение, значок сайта)
    w, a, wd = word_paths(font_dir, 100, 204, 141)
    js = ("// Сгенерировано docs/brand/make_mark.py — знак «дом-сообщение» и надпись Ra'no (Outfit SemiBold в контурах)\n"
          f"export const MARK_HOUSE = {json.dumps(HOUSE)};\nexport const MARK_TAIL = {json.dumps(TAIL)};\n"
          f"export const WORD = {json.dumps(w)};\nexport const APOS = {json.dumps(a)};\n"
          f"export const LOGO_W = {round(204 + wd + 16)};\n"
          f"export const FAVICON_SVG = {json.dumps(files['favicon.svg'].strip())};\n"
          f"export const FAVICON_ICO_B64 = {json.dumps(base64.b64encode(open(os.path.join(OUT, 'favicon.ico'), 'rb').read()).decode())};\n")
    open(os.path.join(OUT, "..", "..", "worker", "src", "brand.js"), "w").write(js)
    print("ok", sorted(files))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
