from io import BytesIO
from os import utime
from time import time

from PIL import Image

from app.image_tools import cleanup_old_uploads, preprocess_image


def test_preprocess_resizes_and_returns_jpeg() -> None:
    source = BytesIO()
    Image.new("RGBA", (3000, 2000), (255, 0, 0, 127)).save(source, format="PNG")

    result = preprocess_image(source.getvalue(), max_edge=1000)

    with Image.open(BytesIO(result)) as image:
        assert image.format == "JPEG"
        assert image.mode == "RGB"
        assert max(image.size) == 1000


def test_cleanup_old_uploads_only_removes_stale_jpegs(tmp_path) -> None:
    old = tmp_path / "old.jpg"
    current = tmp_path / "current.jpg"
    old.write_bytes(b"old")
    current.write_bytes(b"current")
    stale_time = time() - 25 * 60 * 60
    utime(old, (stale_time, stale_time))

    removed = cleanup_old_uploads(tmp_path, older_than_hours=24)

    assert removed == 1
    assert not old.exists()
    assert current.exists()
