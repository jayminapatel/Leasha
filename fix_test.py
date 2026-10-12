with open("tools/grab_ui.py", "r") as f:
    content = f.read()

content = content.replace('store.upsert_file(\n        f"leasha-volume://{volume}/Holiday/beach.jpg"', 'beach = root / "pictures" / "beach.jpg"\n    beach.parent.mkdir(parents=True, exist_ok=True)\n    from PIL import Image\n    picture = Image.new("RGB", (480, 320), (200, 200, 200))\n    picture.save(beach)\n    store.upsert_file(\n        f"leasha-volume://{volume}/Holiday/beach.jpg"')

with open("tools/grab_ui.py", "w") as f:
    f.write(content)
