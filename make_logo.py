"""Erzeugt assets/logo.png und assets/icon.ico (Fadenkreuz + roter Kill-X)."""
from PIL import Image, ImageDraw, ImageFilter

S = 1024
SS = 2                      # Supersampling
N = S * SS
img = Image.new("RGBA", (N, N), (0, 0, 0, 0))

# Hintergrund: abgerundetes Quadrat mit diagonalem Farbverlauf (dunkel -> violett)
grad = Image.new("RGBA", (N, N))
px = grad.load()
c0, c1 = (22, 16, 58), (124, 92, 255)
for y in range(N):
    for x in range(N):
        t = (x * 0.35 + y * 0.65) / N
        t = min(1.0, max(0.0, t * 1.15 - 0.1))
        px[x, y] = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)) + (255,)
mask = Image.new("L", (N, N), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, N - 1, N - 1), radius=int(N * 0.23), fill=255)
img.paste(grad, (0, 0), mask)

cx = cy = N // 2


def layer():
    return Image.new("RGBA", (N, N), (0, 0, 0, 0))


# Fadenkreuz: Ring + vier Striche
ring = layer()
d = ImageDraw.Draw(ring)
r = int(N * 0.30)
d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(235, 230, 255, 235), width=int(N * 0.028))
gap, ln, w = int(N * 0.09), int(N * 0.13), int(N * 0.028)
d.rectangle((cx - w // 2, cy - r - ln // 2, cx + w // 2, cy - r + ln // 2), fill=(235, 230, 255, 235))
d.rectangle((cx - w // 2, cy + r - ln // 2, cx + w // 2, cy + r + ln // 2), fill=(235, 230, 255, 235))
d.rectangle((cx - r - ln // 2, cy - w // 2, cx - r + ln // 2, cy + w // 2), fill=(235, 230, 255, 235))
d.rectangle((cx + r - ln // 2, cy - w // 2, cx + r + ln // 2, cy + w // 2), fill=(235, 230, 255, 235))

# Roter Kill-X (vier Arme) mit Leuchten
x_layer = layer()
d = ImageDraw.Draw(x_layer)
a, b, th = int(N * 0.045), int(N * 0.17), int(N * 0.058)
for sx, sy in ((1, 1), (-1, 1), (1, -1), (-1, -1)):
    d.line((cx + sx * a, cy + sy * a, cx + sx * b, cy + sy * b), fill=(255, 61, 99, 255), width=th)
    for (px_, py_) in ((cx + sx * a, cy + sy * a), (cx + sx * b, cy + sy * b)):
        d.ellipse((px_ - th // 2, py_ - th // 2, px_ + th // 2, py_ + th // 2), fill=(255, 61, 99, 255))
glow = x_layer.filter(ImageFilter.GaussianBlur(N * 0.03))
glow2 = Image.new("RGBA", (N, N), (255, 61, 99, 0))
glow2.putalpha(glow.split()[3].point(lambda v: min(255, int(v * 1.6))))

# Leichter Glanz oben
shine = layer()
ImageDraw.Draw(shine).ellipse((-N * 0.2, -N * 0.75, N * 1.2, N * 0.45), fill=(255, 255, 255, 22))
shine_masked = Image.new("RGBA", (N, N), (0, 0, 0, 0))
shine_masked.paste(shine, (0, 0), mask)

for l in (shine_masked, ring, glow2, x_layer):
    img = Image.alpha_composite(img, l)

img = img.resize((S, S), Image.LANCZOS)
img.save("assets/logo.png")
img.save("assets/icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("ok")
