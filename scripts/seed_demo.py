"""Load the fictional sample factory so the app can be tried before real data exists.

    python -m scripts.seed_demo           # use the models configured in .env / Settings
    python -m scripts.seed_demo --demo    # no models needed (demo provider)
"""
import asyncio
import os
import sys
from pathlib import Path

if "--demo" in sys.argv:
    for role in ("CHAT", "VISION", "EMBEDDING"):
        os.environ[f"{role}_PROVIDER"] = "demo"
        os.environ[f"{role}_MODEL"] = "demo"

from app import db, ics, ingest  # noqa: E402  (after the environment is set)

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data"


def nameplate() -> bytes:
    """A generated equipment nameplate, to exercise the image pipeline."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (640, 360), "#c9ccd1")
    draw = ImageDraw.Draw(img)
    draw.rectangle((16, 16, 624, 344), outline="#3a3d42", width=4)
    lines = ["DEMO PUMPS GmbH", "CENTRIFUGAL PUMP", "TAG: 20-PMP-001",
             "Q 44 m3/h   H 34 m", "P 7.5 kW   2900 rpm", "IP55   SN 0001-DEMO"]
    for i, line in enumerate(lines):
        draw.text((48, 44 + i * 46), line, fill="#1b1d20", font_size=30)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


async def main() -> None:
    db.init()
    print("Parts imported:", ics.import_csv((SAMPLE / "ics_codes.csv").read_bytes()))
    existing = {r["filename"] for r in db.query("SELECT filename FROM documents")}
    files = [(p.name, p.read_bytes()) for p in sorted((SAMPLE / "docs").iterdir())]
    files.append(("20-PMP-001_nameplate.png", nameplate()))
    for name, data in files:
        if name in existing:
            continue
        doc_id = ingest.create_document(name, data)
        await ingest.process_document(doc_id)
        row = db.query("SELECT status, error, n_chunks FROM documents WHERE id=?", (doc_id,))[0]
        print(f"  {name}: {row['status']} ({row['n_chunks']} passages) {row['error'] or ''}")


if __name__ == "__main__":
    asyncio.run(main())
