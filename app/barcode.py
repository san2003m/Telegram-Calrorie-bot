from __future__ import annotations

from io import BytesIO

from PIL import Image


def normalize_barcode(value: str) -> str:
    normalized = "".join(character for character in value if character.isdigit())
    if not 8 <= len(normalized) <= 14:
        raise ValueError("바코드는 8~14자리 숫자여야 합니다.")
    return normalized


def decode_barcodes(raw: bytes) -> list[str]:
    try:
        import zxingcpp
    except ImportError:
        return []

    with Image.open(BytesIO(raw)) as image:
        results = zxingcpp.read_barcodes(image)
    found: list[str] = []
    for result in results:
        try:
            barcode = normalize_barcode(result.text)
        except ValueError:
            continue
        if barcode not in found:
            found.append(barcode)
    return found
