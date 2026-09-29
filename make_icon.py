from pathlib import Path
import base64
from io import BytesIO

from PIL import Image

ROOT = Path(__file__).resolve().parent
encoded_path = ROOT / "app-icon-source.b64"
source_path = ROOT / "app-icon-source.png"
if encoded_path.exists():
    source_path.write_bytes(base64.b64decode(encoded_path.read_text(encoding="ascii")))
    encoded_path.unlink()

image = Image.open(source_path).convert("RGBA")
if image.width != image.height:
    side = min(image.size)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side))
image.save(ROOT / "app-icon.png", format="PNG", optimize=True)
image.save(ROOT / "app-icon.ico", format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print(f"Created app-icon.png and app-icon.ico from {source_path.name} ({image.width}x{image.height})")
