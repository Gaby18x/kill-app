"""Erzeugt assets/logo.png und assets/icon.ico: schlichtes weisses "K" auf dunklem, abgerundetem Quadrat."""
from PIL import Image, ImageDraw, ImageFilter

S = 1024
SS = 2
N = S * SS

img = Image.new("RGBA", (N, N), (0, 0, 0, 0))

# Hintergrund: sehr dunkler, leicht blaeulicher Graphit-Verlauf
grad = Image.new("RGBA", (N, N))
px = grad.load()
c0, c1 = (44, 47, 66), (16, 17, 26)
for y in range(N):
    for x in range(N):
        t = (x * 0.4 + y * 0.6) / N
        px[x, y] = tuple(int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3)) + (255,)
mask = Image.new("L", (N, N), 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, N - 1, N - 1), radius=int(N * 0.24), fill=255)
img.paste(grad, (0, 0), mask)

# feiner Lichtrand oben
rim = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(rim)
d.rounded_rectangle((3, 3, N - 4, N - 4), radius=int(N * 0.24), outline=(255, 255, 255, 38), width=4)
img = Image.alpha_composite(img, rim)

# Buchstabe K aus drei runden Strichen
w = int(N * 0.118)
white = (255, 255, 255, 250)
k = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(k)


def stroke(a, b):
    ax, ay, bx, by = a[0] * N, a[1] * N, b[0] * N, b[1] * N
    d.line((ax, ay, bx, by), fill=white, width=w)
    for x, y in ((ax, ay), (bx, by)):
        d.ellipse((x - w / 2, y - w / 2, x + w / 2, y + w / 2), fill=white)


stroke((0.355, 0.27), (0.355, 0.73))        # Stamm
stroke((0.375, 0.50), (0.665, 0.27))        # oberer Arm
stroke((0.475, 0.455), (0.675, 0.73))       # Bein

shadow = k.filter(ImageFilter.GaussianBlur(N * 0.012))
sh = Image.new("RGBA", (N, N), (0, 0, 0, 0))
sh.putalpha(shadow.split()[3].point(lambda v: int(v * 0.35)))
img = Image.alpha_composite(img, sh)
img = Image.alpha_composite(img, k)

img = img.resize((S, S), Image.LANCZOS)
img.save("assets/logo.png")
img.save("assets/icon.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("ok")
