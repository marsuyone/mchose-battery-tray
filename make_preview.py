from PIL import Image, ImageDraw, ImageFont
import os
import mchose_battery as mb

os.makedirs('assets', exist_ok=True)

mb._fg_color = lambda: (255, 255, 255, 255)

states = [(97, 100, '正常'), (45, 28, '偏低'), (12, 100, '鼠标低电量'), (100, 6, '键盘低电量'), (None, None, '未连接')]

W, H = 880, 260
img = Image.new('RGBA', (W, H), (30, 31, 34, 255))
d = ImageDraw.Draw(img)

try:
    f_title = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 22)
    f_label = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 15)
    f_small = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 13)
except Exception:
    f_title = f_label = f_small = ImageFont.load_default()

d.text((30, 22), '2.4G设备电量  ·  托盘图标', font=f_title, fill=(235, 235, 235, 255))
d.text((30, 56), '外圈 = 鼠标 A7    内圈 = 键盘 G87', font=f_small, fill=(150, 150, 150, 255))

x = 46
for m, k, name in states:
    icon = mb.make_icon(m, k).resize((96, 96), Image.LANCZOS)
    img.paste(icon, (x, 96), icon)
    label = name
    d.text((x + 48 - d.textlength(label, font=f_label) / 2, 202), label, font=f_label, fill=(210, 210, 210, 255))
    sub = f'{m if m is not None else "--"}% / {k if k is not None else "--"}%'
    d.text((x + 48 - d.textlength(sub, font=f_small) / 2, 224), sub, font=f_small, fill=(120, 120, 120, 255))
    x += 160

img.convert('RGB').save('assets/preview.png')
print('assets/preview.png ok')
