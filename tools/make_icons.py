"""Draws the Agent Relay mark and writes the icon files: assets/agent-relay.svg, agent-relay-512.png, agent-relay.ico (needs Pillow).

  python tools/make_icons.py

The mark: two leaning swooshes that meet at the top like an "A" (the two AIs), with a small dot between them (the baton being handed over).
The same path coordinates are used for the SVG and for the raster files, so they always match."""
import pathlib, sys
from PIL import Image, ImageDraw

OUT = pathlib.Path(__file__).resolve().parent.parent / "assets"
OUT.mkdir(exist_ok=True)

# the left swoosh, in a 256x256 box: outer edge bottom -> top, then inner edge top -> bottom (cubic Beziers). The right swoosh is its mirror.
LEFT = [((34, 224), (10, 148), (54, 58), (128, 24)), ((128, 24), (112, 100), (98, 180), (34, 224))]
BATON = (128, 172, 19)      # x, y, radius
C1, C2 = (34, 211, 238), (59, 130, 246)       # cyan -> blue


def mirror(curves): return [tuple((256 - x, y) for x, y in seg) for seg in curves]


def bez(p, t):
    a, b, c, d = p
    u = 1 - t
    return (u**3 * a[0] + 3 * u * u * t * b[0] + 3 * u * t * t * c[0] + t**3 * d[0], u**3 * a[1] + 3 * u * u * t * b[1] + 3 * u * t * t * c[1] + t**3 * d[1])


def poly(curves, n=60):
    pts = []
    for seg in curves:
        pts += [bez(seg, i / n) for i in range(n)]
    return pts


def svg_path(curves):
    d = "M%d,%d" % curves[0][0]
    for seg in curves: d += " C%d,%d %d,%d %d,%d" % (seg[1] + seg[2] + seg[3])
    return d + " Z"


def svg():
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="256" height="256">'
            '<defs><linearGradient id="g1" x1="0.2" y1="0" x2="0.8" y2="1"><stop offset="0" stop-color="#22d3ee"/><stop offset="1" stop-color="#3b82f6"/></linearGradient>'
            '<linearGradient id="g2" x1="0.8" y1="0" x2="0.2" y2="1"><stop offset="0" stop-color="#38bdf8"/><stop offset="1" stop-color="#4f6bf0"/></linearGradient>'
            '<linearGradient id="g3" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ffffff"/><stop offset="1" stop-color="#a5f3fc"/></linearGradient></defs>'
            '<path d="%s" fill="url(#g1)"/><path d="%s" fill="url(#g2)" opacity="0.92"/><circle cx="%d" cy="%d" r="%d" fill="url(#g3)"/></svg>'
            % (svg_path(LEFT), svg_path(mirror(LEFT)), BATON[0], BATON[1], BATON[2]))


def gradient(size, c1, c2, diag):
    g = Image.new("RGB", (size, size))
    px = g.load()
    for y in range(size):
        for x in range(size):
            t = ((x / size) * (0.35 if diag > 0 else -0.35) + (y / size) * 0.9) if diag else y / size
            t = max(0.0, min(1.0, t + (0.0 if diag > 0 else 0.35)))
            px[x, y] = tuple(int(c1[i] + (c2[i] - c1[i]) * t) for i in range(3))
    return g


def render(size, pad=0.0, bg=None):
    """The mark on a transparent (or coloured, rounded) square of the given size."""
    ss = 4; S = size * ss
    scale = S * (1 - 2 * pad) / 256; off = S * pad
    tf = lambda pts: [(x * scale + off, y * scale + off) for x, y in pts]
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    if bg:
        m = Image.new("L", (S, S), 0); ImageDraw.Draw(m).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
        canvas.paste(Image.new("RGBA", (S, S), bg), (0, 0), m)
    for curves, c1, c2, diag, alpha in ((LEFT, C1, C2, 1, 255), (mirror(LEFT), (56, 189, 248), (79, 107, 240), -1, 235)):
        m = Image.new("L", (S, S), 0); ImageDraw.Draw(m).polygon(tf(poly(curves)), fill=alpha)
        canvas.paste(gradient(S, c1, c2, diag).convert("RGBA"), (0, 0), m)
    x, y, r = BATON
    m = Image.new("L", (S, S), 0); ImageDraw.Draw(m).ellipse([(x - r) * scale + off, (y - r) * scale + off, (x + r) * scale + off, (y + r) * scale + off], fill=255)
    canvas.paste(gradient(S, (255, 255, 255), (165, 243, 252), 1).convert("RGBA"), (0, 0), m)
    return canvas.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    (OUT / "agent-relay.svg").write_text(svg(), encoding="utf-8", newline="\n")
    dark = (11, 15, 20, 255)
    render(512, 0.14, dark).save(OUT / "agent-relay-512.png")
    render(256, 0.0, None).save(OUT / "agent-relay-mark.png")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [render(s, 0.12, dark) for s in sizes]
    imgs[-1].save(OUT / "agent-relay.ico", sizes=[(s, s) for s in sizes], append_images=imgs[:-1])
    print("wrote", sorted(p.name for p in OUT.iterdir()))
