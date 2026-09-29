"""Florence-2 full precision vs int8, same 4 photos. argv[1]: fp32-auto | fp32-cpu | int8."""
import dataclasses
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image  # noqa: E402

from app.ort import hub  # noqa: E402
from app.ort.florence import OnnxFlorence  # noqa: E402



def _cache() -> Path:
    from app.core.config import load_settings

    return Path(load_settings(create_dirs=False, check_writable=False).model_cache)


PHOTOS = [r"C:\Windows\Web\Wallpaper\Spotlight\img14.jpg",
          r"C:\Windows\Web\Wallpaper\ThemeA\img20.jpg",
          r"C:\Windows\Web\Wallpaper\ThemeA\img21.jpg",
          r"C:\Windows\Web\Wallpaper\ThemeA\img23.jpg"]
mode = sys.argv[1]
spec = hub.FLORENCE if mode == "int8" else dataclasses.replace(hub.FLORENCE, suffix="")
device = "cpu" if mode.endswith("cpu") or mode == "int8" else "auto"
folder = hub.resolve(spec, _cache())
t = time.time()
f = OnnxFlorence(folder, model=spec, device=device)
print(f"[{mode}] load {time.time()-t:.1f}s vision_on_gpu={f.vision.on_gpu} decoder_on_gpu={f.on_gpu}")
for p in PHOTOS:
    with Image.open(p) as im:
        t = time.time()
        cap, tags = f.caption_and_tags(im)
    print(f"[{mode}] {Path(p).name} {time.time()-t:.1f}s | {cap} | tags={list(tags)}")
