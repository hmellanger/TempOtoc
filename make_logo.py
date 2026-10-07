import math
from PIL import Image, ImageDraw, ImageFont

# Card-only logo cropped to the frame: transparent background outside the
# rounded card, smiling clock + wordmark "TempOtoc" inside. No sky, no stars.
W, H = 860, 210   # exactly the card, cropped to its bounds

img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# --- rounded card (outline drawn inside the bounds so nothing is clipped) ---
d.rounded_rectangle([1, 1, W - 2, H - 2], radius=46,
                    fill=(36, 40, 62), outline=(90, 96, 130), width=3)

# --- smiling clock mascot ---
ccx, ccy, rad = 120, 100, 82
d.ellipse([ccx - rad, ccy - rad, ccx + rad, ccy + rad],
          fill=(52, 58, 86), outline=(255, 178, 71), width=6)
for i in range(12):
    a = math.radians(i * 30 - 90)
    d.line([ccx + math.cos(a) * (rad - 14), ccy + math.sin(a) * (rad - 14),
            ccx + math.cos(a) * (rad - 4),  ccy + math.sin(a) * (rad - 4)],
           fill=(205, 214, 240), width=3)
a_h = math.radians(300 - 90)
d.line([ccx, ccy, ccx + math.cos(a_h) * 34, ccy + math.sin(a_h) * 34],
       fill=(255, 255, 255), width=7)
a_m = math.radians(60 - 90)
d.line([ccx, ccy, ccx + math.cos(a_m) * 52, ccy + math.sin(a_m) * 52],
       fill=(255, 255, 255), width=5)
d.ellipse([ccx - 6, ccy - 6, ccx + 6, ccy + 6], fill=(255, 178, 71))
d.ellipse([ccx - 26, ccy - 30, ccx - 14, ccy - 18], fill=(255, 255, 255))
d.ellipse([ccx + 14, ccy - 30, ccx + 26, ccy - 18], fill=(255, 255, 255))
d.arc([ccx - 34, ccy - 6, ccx + 34, ccy + 40], start=20, end=160,
      fill=(255, 220, 150), width=5)

# --- wordmark: Temp + O + toc, O in accent ---
font = ImageFont.truetype("C:/Windows/Fonts/comicbd.ttf", 112)
parts = [("Temp", (242, 246, 255)), ("O", (255, 178, 71)), ("toc", (242, 246, 255))]
x = 230
y = 28
for txt, col in parts:
    d.text((x, y), txt, font=font, fill=col + (255,))
    x += d.textlength(txt, font=font) + 2

img.save(r"C:\Users\Hugues\Documents\Hermes\Tempotoc\logo.png")
print("saved logo.png", img.size, img.mode)
