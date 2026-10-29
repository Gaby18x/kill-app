"""Erzeugt assets/logo.png und assets/icon.ico: helles Quadrat mit durchgeschnittenem Play-Dreieck (Logo 2)."""
from PIL import Image, ImageDraw

S = 1024
SS = 2
W = S * SS


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(len(a)))


img = Image.new("RGBA", (W, W), (0, 0, 0, 0))

# Hintergrund: weiss-lavendelgrauer Verlauf im abgerundeten Quadrat
grad = Image.new("RGBA", (W, W))
px = grad.load()
c0, c1 = (247, 246, 252), (212, 211, 230)
for y in range(W):
    for x in range(W):
        t = (x * 0.38 + y * 0.62) / W
        px[x, y] = lerp(c0, c1, t) + (255,)
mask = Image.new("L", (W, W), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, W - 1), radius=int(W * 0.235), fill=255)
img.paste(grad, (0, 0), mask)
rim = Image.new("RGBA", (W, W), (0, 0, 0, 0))
ImageDraw.Draw(rim).rounded_rectangle((3, 3, W - 4, W - 4), radius=int(W * 0.235), outline=(70, 66, 120, 70), width=6)
img = Image.alpha_composite(img, rim)

# Play-Dreieck mit abgerundeten Ecken und diagonalem Schnitt
t = Image.new("RGBA", (W, W), (0, 0, 0, 0))
d = ImageDraw.Draw(t)
poly = [(x * W, y * W) for x, y in ((0.345, 0.255), (0.345, 0.745), (0.745, 0.50))]
col = (30, 31, 46, 255)
d.polygon(poly, fill=col)
for i in range(3):
    a, b = poly[i], poly[(i + 1) % 3]
    d.line((*a, *b), fill=col, width=int(W * 0.06))
    d.ellipse((a[0] - W * 0.03, a[1] - W * 0.03, a[0] + W * 0.03, a[1] + W * 0.03), fill=col)
d.line((W * 0.30, W * 0.66, W * 0.66, W * 0.30), fill=(0, 0, 0, 0), width=int(W * 0.038))
img = Image.alpha_composite(img, t)

img = img.resize((S, S), Image.LANCZOS)
img.save("assets/logo.png")
img.save("assets/icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("ok")
