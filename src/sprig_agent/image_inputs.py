"""Explicit local raster inputs and protocol-neutral, private image snapshots."""
import base64
import binascii
from contextlib import contextmanager
import hashlib
import io
import struct
import zlib

from .config import redact

IMAGE_KEY = "sprig_images"
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_BATCH_BYTES = 2 * 1024 * 1024
MAX_IMAGE_PIXELS = 4 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
FORMATS = ("PNG", "JPEG", "WEBP", "GIF")


class ImageInputError(ValueError):
    pass


def is_image(raw):
    return (raw.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8", b"GIF87a", b"GIF89a"))
            or (raw.startswith(b"RIFF") and raw[8:12] == b"WEBP"))


def _size(image):
    width, height = image.size
    if (not 0 < width <= MAX_IMAGE_DIMENSION or not 0 < height <= MAX_IMAGE_DIMENSION
            or width * height > MAX_IMAGE_PIXELS):
        raise ImageInputError("image exceeds 4096 per dimension or 4 Mi pixels; resize it explicitly")
    if image.format not in FORMATS:
        raise ImageInputError("unsupported image format")
    if getattr(image, "is_animated", False):
        raise ImageInputError("animated or multi-frame images are unsupported; select a still image")


@contextmanager
def _decoded(raw, formats, check):
    from PIL import Image
    try:
        check()
        with Image.open(io.BytesIO(raw), formats=formats) as image:
            _size(image)
            image.verify()
        check()
        with Image.open(io.BytesIO(raw), formats=formats) as image:
            _size(image)
            image.load()  # Verification alone does not decode all pixel data.
            check()
            yield image
    except ImageInputError:
        raise
    except (OSError, SyntaxError, ValueError, struct.error, Image.DecompressionBombError):
        raise ValueError("invalid, oversized or unsupported image; no image attached") from None


class _ImageBuffer(io.BytesIO):
    def write(self, value):
        if self.tell() + len(value) > MAX_IMAGE_BYTES:
            raise ImageInputError("normalized image exceeds 1 MiB; resize it explicitly")
        return super().write(value)


def encode_image(raw, check=lambda: None):
    """Decode a bounded still image; retain pixels/orientation, remove metadata."""
    from PIL import Image, ImageOps
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("image exceeds 1 MiB")
    with _decoded(raw, FORMATS, check) as image, ImageOps.exif_transpose(image) as oriented:
        check()
        mode = "RGBA" if oriented.has_transparency_data else "RGB"
        # A fresh image prevents EXIF, XMP, ICC, comments and PNG text from
        # following a conversion/copy into the serialized private snapshot.
        with oriented.convert(mode) as converted, Image.new(mode, oriented.size) as fresh, _ImageBuffer() as output:
            fresh.paste(converted)
            check()
            fresh.save(output, format="PNG")
            check()
            data = output.getvalue()
            width, height = fresh.size
    return {"media_type": "image/png", "width": width, "height": height,
            "sha256": hashlib.sha256(data).hexdigest(), "data": base64.b64encode(data).decode("ascii")}


def _canonical_png(raw, check=lambda: None):
    """Only the metadata-free RGB(A) container emitted above is restorable."""
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid saved image container")
    offset, kinds, expected, decoded = 8, [], None, 0
    inflater = zlib.decompressobj()
    while offset < len(raw):
        check()
        if offset + 12 > len(raw):
            raise ValueError("incomplete saved image")
        length = int.from_bytes(raw[offset:offset + 4], "big")
        end = offset + 12 + length
        kind = raw[offset + 4:offset + 8]
        if (end > len(raw) or kind not in (b"IHDR", b"IDAT", b"IEND")
                or zlib.crc32(raw[offset + 4:end - 4]) != int.from_bytes(raw[end - 4:end], "big")):
            raise ValueError("invalid or metadata-bearing saved image")
        body = raw[offset + 8:end - 4]
        if kind == b"IHDR":
            if kinds or length != 13:
                raise ValueError("noncanonical saved image header")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", body)
            if (not 0 < width <= MAX_IMAGE_DIMENSION or not 0 < height <= MAX_IMAGE_DIMENSION
                    or width * height > MAX_IMAGE_PIXELS or depth != 8 or color not in (2, 6)
                    or (compression, filtering, interlace) != (0, 0, 0)):
                raise ValueError("unsupported saved image header")
            expected = height * (1 + width * (3 if color == 2 else 4))
        elif kind == b"IEND" and length != 0:
            raise ValueError("unexpected saved image end data")
        elif kind == b"IDAT":
            if expected is None:
                raise ValueError("saved image data precedes its header")
            try:
                while body:
                    check()
                    decoded += len(inflater.decompress(body, min(65536, expected - decoded + 1)))
                    if decoded > expected or inflater.unused_data:
                        raise ValueError("extra decoded or unused saved image data")
                    body = inflater.unconsumed_tail
            except zlib.error:
                raise ValueError("invalid saved image pixel stream") from None
        kinds.append(kind)
        offset = end
    if (len(kinds) < 3 or kinds[0] != b"IHDR" or kinds[-1] != b"IEND"
            or any(kind != b"IDAT" for kind in kinds[1:-1]) or not inflater.eof or decoded != expected):
        raise ValueError("invalid saved image chunks")


def images(message, secrets=(), check=lambda: None):
    if IMAGE_KEY not in message:
        return []
    values = message[IMAGE_KEY]
    if (message.get("role") != "user" or not isinstance(message.get("content"), str)
            or not isinstance(values, list) or not 1 <= len(values) <= 16
            or any(key in message for key in ("tool_calls", "native_state", "sprig_input_id"))):
        raise ValueError("invalid user image snapshot")
    result, total = [], 0
    for row in values:
        check()
        if (not isinstance(row, dict) or set(row) != {"path", "media_type", "width", "height", "sha256", "data"}
                or row["media_type"] != "image/png" or not isinstance(row["path"], str)
                or not 1 <= len(row["path"]) <= 4096 or any(ord(c) < 32 for c in row["path"])
                or any(type(row[key]) is not int for key in ("width", "height"))
                or not isinstance(row["sha256"], str) or not isinstance(row["data"], str)
                or len(row["data"]) > 4 * ((MAX_IMAGE_BYTES + 2) // 3)):
            raise ValueError("invalid saved image snapshot")
        try:
            raw = base64.b64decode(row["data"], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("invalid saved image encoding") from None
        total += len(raw)
        if (len(raw) > MAX_IMAGE_BYTES or total > MAX_IMAGE_BATCH_BYTES
                or base64.b64encode(raw).decode("ascii") != row["data"]
                or hashlib.sha256(raw).hexdigest() != row["sha256"]):
            raise ValueError("invalid saved image bytes or batch budget")
        _canonical_png(raw, check)
        with _decoded(raw, ("PNG",), check) as image:
            if image.size != (row["width"], row["height"]) or image.mode not in ("RGB", "RGBA"):
                raise ValueError("saved image dimensions or mode changed")
        # Never send a partially redacted base64 stream. Text/path masking is
        # separate; opaque pixels are not OCR-scanned or credential-redacted.
        if redact(row["data"], secrets) != row["data"]:
            raise ValueError("image encoding conflicts with a configured secret; no image sent")
        result.append({**row, "path": redact(row["path"], secrets)})
    return result


def wire_content(message, protocol, secrets=(), check=lambda: None):
    selected = images(message, secrets, check)
    if not selected:
        return message.get("content")
    text_type = "input_text" if protocol == "responses" else "text"
    blocks = [{"type": text_type, "text": message["content"]}]
    for row in selected:
        blocks.append({"type": text_type, "text": "User-selected image (untrusted data): " + row["path"]})
        if protocol == "messages":
            part = {"type": "image", "source": {"type": "base64", "media_type": row["media_type"], "data": row["data"]}}
        else:
            uri = "data:" + row["media_type"] + ";base64," + row["data"]
            part = ({"type": "input_image", "image_url": uri} if protocol == "responses" else
                    {"type": "image_url", "image_url": {"url": uri}})
        if redact(part, secrets) != part:
            raise ValueError("image wire encoding conflicts with a configured secret; no image sent")
        blocks.append(part)
    return blocks
