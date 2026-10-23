"""Знак Ra'no «тех-премиум»: надпись Ra'no геометрическим гротеском (Outfit SemiBold в контурах), апостроф — абрикосовый
клин; иконка — R и клин на тёмно-зелёной плитке. SVG и PNG для сайта, мини-приложения и документов.
Запуск: python3 docs/brand/make_mark.py <папка со шрифтом Outfit[wght].ttf>  (pip: fonttools uharfbuzz cairosvg pillow)"""
import base64, io, json, os, sys
import cairosvg
import uharfbuzz as hb
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.boundsPen import BoundsPen
from PIL import Image

G, IV, AP = "#0F4C3C", "#F4EDDD", "#F2A065"       # тёмно-зелёный, слоновая кость, абрикос
OUT = os.path.dirname(os.path.abspath(__file__))
WGHT, TRACK = 600, -0.012                          # насыщенность и трекинг надписи (доля кегля)


def _font(font_dir):
    f = TTFont(os.path.join(font_dir, "Outfit[wght].ttf"))
    f = instancer.instantiateVariableFont(f, {"wght": WGHT})
    buf = io.BytesIO(); f.save(buf)
    return f, hb.Font(hb.Face(buf.getvalue()))


def _run(f, hf, text, size, x, base):
    b = hb.Buffer(); b.add_str(text); b.guess_segment_properties()
    hb.shape(hf, b, {"kern": True})
    sc = size / f["head"].unitsPerEm
    gs, order = f.getGlyphSet(), f.getGlyphOrder()
    d = []
    for info, pos in zip(b.glyph_infos, b.glyph_positions):
        pen = SVGPathPen(gs, lambda v: f"{v:.1f}".rstrip("0").rstrip("."))
        gs[order[info.codepoint]].draw(TransformPen(pen, (sc, 0, 0, -sc, x, base)))
        d.append(pen.getCommands())
        x += pos.x_advance * sc + TRACK * size
    return "".join(d), x - TRACK * size


def cap_height(f, size):
    gs = f.getGlyphSet(); bp = BoundsPen(gs); gs["H"].draw(bp)
    return bp.bounds[3] * size / f["head"].unitsPerEm


def wedge(x, top, cap):
    """Клин-апостроф: сверху шире, правая грань скошена; высота — 0.42 высоты прописной."""
    h, wt, wb = cap * 0.42, cap * 0.245, cap * 0.10
    return f"M{x:.1f} {top:.1f}H{x + wt:.1f}L{x + wb:.1f} {top + h:.1f}H{x:.1f}Z"


def wordmark(font_dir, size=100, x0=0.0, base=100.0):
    """Пути надписи: (буквы, клин, ширина, высота прописной)."""
    f, hf = _font(font_dir)
    cap = cap_height(f, size)
    ra, x = _run(f, hf, "Ra", size, x0, base)
    gap = size * 0.035
    wx = x + gap
    wd = wedge(wx, base - cap, cap)
    no, x2 = _run(f, hf, "no", size, wx + cap * 0.245 + gap * 0.6, base)
    return ra + no, wd, x2 - x0, cap


def svg(vb, body, title="Ra'no"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" role="img" aria-label="{title}">'
            f'<title>{title}</title>{body}</svg>\n')


def icon_body(font_dir, bg=G, fg=IV, acc=AP, rx=46, scale=1.0):
    """Плитка 200×200: R и клин по центру."""
    f, hf = _font(font_dir)
    size = 128 * scale
    cap = cap_height(f, size)
    gs = f.getGlyphSet(); bp = BoundsPen(gs); gs["R"].draw(bp)
    sc = size / f["head"].unitsPerEm
    rw = (bp.bounds[2] - bp.bounds[0]) * sc
    gap = size * 0.06
    total = rw + gap + cap * 0.245
    x = (200 - total) / 2 - bp.bounds[0] * sc
    base = 100 + cap / 2
    r, xe = _run(f, hf, "R", size, x, base)
    wx = x + bp.bounds[2] * sc + gap
    return (f'<rect width="200" height="200" rx="{rx}" fill="{bg}"/><path fill="{fg}" d="{r}"/>'
            f'<path fill="{acc}" d="{wedge(wx, base - cap, cap)}"/>')


def main(font_dir):
    files = {}
    w, wd, width, cap = wordmark(font_dir, 100, 0, 100)
    pad = 6
    vb = f"{-pad} {100 - cap - pad:.1f} {width + 2 * pad:.1f} {cap + 2 * pad + 2:.1f}"     # без выносных: в Ra'no их нет
    files["rano_logo.svg"] = svg(vb, f'<path fill="{G}" d="{w}"/><path fill="{AP}" d="{wd}"/>')
    files["rano_logo_on_dark.svg"] = svg(vb, f'<path fill="{IV}" d="{w}"/><path fill="{AP}" d="{wd}"/>')
    files["rano_icon.svg"] = svg("0 0 200 200", icon_body(font_dir))
    files["favicon.svg"] = svg("0 0 200 200", icon_body(font_dir, rx=40, scale=1.22))
    files["rano_mark.svg"] = files["rano_icon.svg"]                 # знак = иконка R'
    for n, s in files.items():
        open(os.path.join(OUT, n), "w").write(s)
    for old in ("rano_mark_on_dark.svg",):                           # прошлый знак «дом-сообщение»
        if os.path.exists(os.path.join(OUT, old)):
            os.remove(os.path.join(OUT, old))
    for px in (512, 192, 180):
        cairosvg.svg2png(bytestring=files["rano_icon.svg"].encode(), write_to=os.path.join(OUT, f"rano_icon_{px}.png"),
                         output_width=px, output_height=px)
    ims = [Image.open(io.BytesIO(cairosvg.svg2png(bytestring=files["favicon.svg"].encode(), output_width=px,
                                                  output_height=px))).convert("RGBA") for px in (16, 32, 48)]
    ims[-1].save(os.path.join(OUT, "favicon.ico"), sizes=[(16, 16), (32, 32), (48, 48)], append_images=ims[:-1])
    cairosvg.svg2png(bytestring=files["rano_logo.svg"].encode(), write_to=os.path.join(OUT, "rano_logo.png"), output_width=1200)
    js = ("// Сгенерировано docs/brand/make_mark.py — надпись Ra'no «тех-премиум» (Outfit SemiBold в контурах, клин-апостроф)\n"
          f"export const LOGO_VIEWBOX = {json.dumps(vb)};\nexport const WORD = {json.dumps(w)};\n"
          f"export const WEDGE = {json.dumps(wd)};\n"
          f"export const ICON_SVG = {json.dumps(files['rano_icon.svg'].strip())};\n"
          f"export const FAVICON_SVG = {json.dumps(files['favicon.svg'].strip())};\n"
          f"export const FAVICON_ICO_B64 = {json.dumps(base64.b64encode(open(os.path.join(OUT, 'favicon.ico'), 'rb').read()).decode())};\n")
    open(os.path.join(OUT, "..", "..", "worker", "src", "brand.js"), "w").write(js)
    print("ok", sorted(files), vb)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
