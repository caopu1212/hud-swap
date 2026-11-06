"""生成 icon.ico：深色圆角底 + 菱形四个技能圆，左/上一对、下/右一对，带交换箭头。"""
import os
from PIL import Image, ImageDraw

S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, S - 8, S - 8), radius=48, fill=(28, 34, 44, 255))

c, off, r = S // 2, 76, 30
pos = {"上": (c, c - off), "左": (c - off, c), "右": (c + off, c), "下": (c, c + off)}
col = {"上": (245, 176, 40), "左": (245, 176, 40), "右": (40, 200, 220), "下": (40, 200, 220)}
for k, (x, y) in pos.items():
    d.ellipse((x - r, y - r, x + r, y + r), fill=col[k] + (255,), outline=(255, 255, 255, 255), width=6)


def arrow(p, q, color):
    (x1, y1), (x2, y2) = p, q
    d.line((x1, y1, x2, y2), fill=color, width=10)
    for (ax, ay), (bx, by) in (((x1, y1), (x2, y2)), ((x2, y2), (x1, y1))):
        vx, vy = ax - bx, ay - by
        n = (vx * vx + vy * vy) ** 0.5
        vx, vy = vx / n, vy / n
        px, py = -vy, vx
        d.polygon([(ax, ay), (ax - vx * 22 + px * 14, ay - vy * 22 + py * 14),
                   (ax - vx * 22 - px * 14, ay - vy * 22 - py * 14)], fill=color)


def between(a, b):
    (x1, y1), (x2, y2) = pos[a], pos[b]
    vx, vy = x2 - x1, y2 - y1
    n = (vx * vx + vy * vy) ** 0.5
    k = (r + 6) / n
    return (x1 + vx * k, y1 + vy * k), (x2 - vx * k, y2 - vy * k)


arrow(*between("左", "上"), (255, 255, 255, 255))
arrow(*between("下", "右"), (255, 255, 255, 255))

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(out.replace(".ico", "_preview.png"))
print(out)
