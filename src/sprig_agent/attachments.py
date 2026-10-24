"""Explicit complete UTF-8 snapshots, independent of model tool display caps."""
import os

from .config import redact
from .tools import ToolError
from .image_inputs import IMAGE_KEY, MAX_IMAGE_BATCH_BYTES, encode_image, is_image

MAX_FILE_BYTES = 1024 * 1024
MAX_BATCH_BYTES = 2 * 1024 * 1024


def snapshots(config, tools, paths, check_cancelled):
    if not isinstance(paths, (list, tuple)) or not 1 <= len(paths) <= 16:
        raise ValueError("attach between 1 and 16 workspace files at a time")
    frames, summary, versions, image_bytes = [], [], [], 0
    # This is an I/O bound. Redaction and JSON escaping can change the wire
    # size; the existing full-request preflight decides model admission.
    remaining = MAX_BATCH_BYTES

    def ready():
        check_cancelled()
        tools._check_cancelled()
        tools.root.ensure_open()
        tools._require_tool("read_file")

    try:
        for path in paths:
            ready()
            parts = tools._parts(path)
            limit = min(MAX_FILE_BYTES, remaining)
            raw = bytearray()
            with tools._parent(path) as (parent, name), parent.open_read(name) as handle:
                before = os.fstat(handle.fileno())
                if not 0 <= before.st_size <= limit:
                    raise ValueError("attachment exceeds the 1 MiB file limit or remaining 2 MiB batch byte limit; no partial content attached")
                while True:
                    ready()
                    chunk = handle.read(min(65536, limit - len(raw) + 1))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > limit:
                        raise ValueError("attachment grew beyond its byte limit; no partial content attached")
                ready()
                revision = tools._revision(before)
                if tools._revision(os.fstat(handle.fileno())) != revision or len(raw) != before.st_size:
                    raise ValueError("attachment changed while reading; retry the selection")
            # Inspect the complete file, including credential/binary/UTF-8
            # failures past the tool's display window or 10,000-line page.
            if is_image(raw):
                image = encode_image(bytes(raw), ready)
                image_bytes += 3 * (len(image["data"]) // 4) - image["data"].count("=")
                if image_bytes > MAX_IMAGE_BATCH_BYTES:
                    raise ValueError("normalized image batch exceeds 2 MiB")
                frame = {"path": path, "image": image}
                receipt = {"path": path, "bytes": len(raw), "media_type": image["media_type"],
                           "width": image["width"], "height": image["height"]}
            else:
                frame = {"path": path, "content": tools._decode(raw)}
                receipt = {"path": path, "bytes": len(raw)}
            ready()
            remaining -= len(raw)
            frames.append(frame)
            summary.append(receipt)
            versions.append((path, parts, revision))

        # Ordinary edits, replacements and privacy revocations during another
        # selected read invalidate the batch. This is not a multi-file lock.
        for path, parts, revision in versions:
            ready()
            tools._parts(path)
            with tools._parent(path) as (parent, name), parent.open_read(name) as handle:
                if tools._revision(os.fstat(handle.fileno())) != revision:
                    raise ValueError("attachment changed while preparing the batch; retry the selection")
        ready()
        for _, parts, _ in versions:
            if config.project_instructions:
                tools.instructions.register(parts[:-1])
        frames, summary = redact(frames, config.secrets), redact(summary, config.secrets)
        ready()
    except (OSError, ToolError, ValueError) as error:
        raise ValueError("cannot attach file: " + redact(str(error), config.secrets)) from None
    return frames, summary


def snapshot_message(frames, prefix, secrets):
    import json
    data, selected = [], []
    for row in frames:
        if "image" in row:
            image = {"path": row["path"], **row["image"]}
            selected.append(image)
            data.append({key: value for key, value in image.items() if key != "data"})
        else:
            data.append(row)
    message = {"role": "user", "content": redact(prefix + "\n" + json.dumps(data, ensure_ascii=False), secrets)}
    if selected:
        message[IMAGE_KEY] = selected
    return message
