"""Where generated images live. Supabase Storage (public bucket, free tier) when SUPABASE_URL and
SUPABASE_SERVICE_KEY are set, otherwise ./out/img/ for local runs. Images are re-encoded as JPEG first:
a 1K PNG from the model is ~1.5 MB, the JPEG ~200 KB, and the re-encode strips any metadata."""
import io
import os

import httpx

from core.config import ROOT

BUCKET = "substack-images"
LOCAL = ROOT / "out" / "img"


def to_jpeg(raw: bytes, max_width: int = 1600) -> bytes:
    from PIL import Image
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    if img.width > max_width:
        img = img.resize((max_width, round(img.height * max_width / img.width)))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85, optimize=True, progressive=True)
    return out.getvalue()


def put(path: str, data: bytes, content_type: str) -> str:
    """Store a file and return a URL the dashboard and Substack can load (a file:// path when local)."""
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY")
    if url and key:
        headers = {"apikey": key, "Content-Type": content_type, "x-upsert": "true"}
        if key.startswith("eyJ"):  # legacy JWT service_role key; the newer sb_secret_... keys go in `apikey` only
            headers["Authorization"] = f"Bearer {key}"
        r = httpx.post(f"{url}/storage/v1/object/{BUCKET}/{path}", content=data, timeout=60, headers=headers)
        r.raise_for_status()
        return f"{url}/storage/v1/object/public/{BUCKET}/{path}"
    dest = LOCAL / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest.as_uri()
