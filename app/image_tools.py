from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

MAX_EDGE = 1800
MAX_OUTPUT_BYTES = 3_500_000


def preprocess_image(raw: bytes, *, max_edge: int = MAX_EDGE) -> bytes:
    """Normalize orientation, downscale, remove metadata, and return a compact JPEG."""
    if not raw:
        raise ValueError("빈 이미지입니다.")
    with Image.open(BytesIO(raw)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        for quality in (88, 80, 70):
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            value = output.getvalue()
            if len(value) <= MAX_OUTPUT_BYTES:
                return value
        return value


def write_private_image(path: Path, image_bytes: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(image_bytes)
    path.chmod(0o600)


def remove_private_image(path_value: str | None) -> None:
    if not path_value:
        return
    path = Path(path_value)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_old_uploads(directory: Path, *, older_than_hours: int = 24) -> int:
    cutoff = datetime.now(UTC) - timedelta(hours=older_than_hours)
    removed = 0
    if not directory.exists():
        return removed
    for path in directory.glob("*.jpg"):
        try:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if modified < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
