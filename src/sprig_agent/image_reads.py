"""Freshly approved raster reads; opaque pixel payloads are never tool JSON."""
import json
import os

from .config import redact
from .image_inputs import images


class ImageReadResult(dict):
    def __init__(self, metadata, frame):
        super().__init__(metadata)
        self.frame = frame


def read_image(tools, path):
    # Imports are deferred: attachments itself uses Tools' shared error class.
    from .attachments import snapshots, snapshot_message

    config, version = tools.config, tools._conversation_version
    tools._require_tool("read_file")
    tools._check_cancelled()
    parts = tools._parts(path)
    path = "/".join(parts)
    with tools._open_read(path) as handle:
        revision = tools._revision(os.fstat(handle.fileno()))
    try:
        frames, receipts = snapshots(config, tools, [path], tools._check_cancelled)
    except ImportError:
        raise ValueError("image decoding requires the locked application dependencies; run uv sync --locked") from None
    if "image" not in frames[0]:
        raise ValueError("image=true requires a supported PNG, JPEG, WebP or still GIF")
    # Reject redaction collisions and corrupt assets before asking permission.
    images(snapshot_message(frames, "Prepared image snapshot", config.secrets), config.secrets, tools._check_cancelled)
    rules = tools.instructions.prepare_action(parts[:-1])

    def fresh():
        tools._require_tool("read_file")
        tools._check_cancelled()
        if tools.config is not config or tools._conversation_version != version:
            raise ValueError("image read configuration changed; request fresh approval")
        if redact(frames, config.secrets) != frames:
            raise ValueError("image snapshot conflicts with updated credential protection; request a fresh read")
        tools._parts(path)
        tools.instructions.recheck_action(parts[:-1], rules)
        with tools._open_read(path) as handle:
            if tools._revision(os.fstat(handle.fileno())) != revision:
                raise ValueError("image changed before or during approval; request a fresh read")
        tools._check_cancelled()

    metadata = redact({**receipts[0], "sha256": frames[0]["image"]["sha256"]}, config.secrets)
    def public_result(approved):
        result = {**redact(metadata, config.secrets), "approved": approved,
                  "image_prepared": approved, "network_contacted": False}
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > config.max_output:
            result = {"approved": approved, "image_prepared": approved, "network_contacted": False, "metadata_omitted": True}
        return result
    fresh()
    approved = tools.request_approval(
        "READ AND SEND IMAGE pixels to the current model?\n" + json.dumps(metadata, ensure_ascii=False)
        + "\nModel: " + config.model + " at " + config.base_url
        + "\nThis normalized snapshot may appear in subsequent model requests in this invocation, until new user guidance. "
        "Pixels are not OCR-scanned or credential-redacted. Metadata is stripped. No image is sent by this read alone. "
        "The request still must fit its byte budget; no automatic resize or retry. "
        "Pixel payloads are not saved in the transcript, grant no edit read_id or acceptance, and are not replayed on resume.")
    if approved is not True:
        return public_result(False)
    fresh()
    return ImageReadResult(public_result(True), frames[0])
