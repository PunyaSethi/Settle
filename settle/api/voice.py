"""POST /voice/extract — the voice lab's endpoint. SPEC §11, §16.

Post an audio file, get the whole chain back: transcript, every span the locator
found, what each parsed to, which validation rejected and why, the verdict, and
the action — or the explicit absence of one.

The trace is the product. A verdict on its own is an assertion; a verdict beside
the spans that were considered and set aside is a decision a reader can check.
Clip 3 is the case that matters: a hedged reply where the honest answer is that
nothing happens, and the screen has to make "nothing happened" legible rather
than looking like a failed request.

Raw body, not multipart
-----------------------
The audio arrives as the request body. FastAPI's `UploadFile` needs
`python-multipart`, which this project does not pin, and adding an undeclared
dependency would make the endpoint work here and fail for anyone cloning the
repo — the same trade CP12 made over `httpx`. The webhook already reads raw
bytes for its own reasons, so the shape is not new.

Anchored, never clocked
-----------------------
Extraction resolves relative dates against the case's `created_at`. There is no
clock in this path: the anchor arrives on a header, and absent one the endpoint
falls back to a declared constant rather than to today. A promise parsed against
wall time resolves differently on every replay.
"""

from __future__ import annotations

import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Final

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from settle.text.promise import extract
from settle.text.voice import Transcription, content_hash, detect_script, transcribe

__all__ = ["ANCHOR_HEADER", "DEMO_ANCHOR", "router"]

router = APIRouter()

ANCHOR_HEADER: Final[str] = "x-anchor-date"
FILENAME_HEADER: Final[str] = "x-filename"

# The fallback anchor. A constant rather than `date.today()`, because the whole
# extraction contract is that a relative date resolves against the case that
# caused the contact and not against whenever someone opened the page.
DEMO_ANCHOR: Final[date] = date(2026, 1, 10)

# Ogg/Opus at ~19 kbps is what WhatsApp actually delivers; a minute of it is
# well under this. The cap exists so the route cannot be used to spool a large
# file into a temporary directory.
MAX_BYTES: Final[int] = 8 * 1024 * 1024

SUFFIXES: Final[frozenset[str]] = frozenset(
    {".ogg", ".oga", ".opus", ".m4a", ".mp3", ".wav", ".webm"}
)


def _anchor_from(request: Request) -> tuple[date, str]:
    raw = request.headers.get(ANCHOR_HEADER)
    if not raw:
        return DEMO_ANCHOR, "default"
    try:
        return datetime.fromisoformat(raw).date(), "header"
    except ValueError:
        return DEMO_ANCHOR, "default (header was not an ISO date)"


def trace(audio: Path, anchor: date, *, allow_api: bool = True) -> dict[str, Any]:
    """Transcribe, extract, and return the whole chain as JSON.

    Split out from the route so the replay artefact and the tests build the
    trace through exactly the code the endpoint runs, rather than a copy of it.
    """
    transcription: Transcription = transcribe(audio, allow_api=allow_api)
    extraction = extract(transcription.text, anchor)
    return {
        "audio": {
            "filename": audio.name,
            "bytes": audio.stat().st_size,
            "sha256": transcription.audio_sha256,
        },
        "transcription": transcription.as_dict(),
        **extraction.as_dict(),
    }


@router.post("/voice/extract")
async def voice_extract(request: Request) -> JSONResponse:
    """One audio file in, one full extraction trace out."""
    audio = await request.body()
    if not audio:
        return JSONResponse(
            {"reason_code": "NO_AUDIO", "detail": "the request body is empty"},
            status_code=400,
        )
    if len(audio) > MAX_BYTES:
        return JSONResponse(
            {
                "reason_code": "AUDIO_TOO_LARGE",
                "detail": f"{len(audio):,} bytes exceeds the {MAX_BYTES:,} cap",
            },
            status_code=413,
        )

    name = Path(request.headers.get(FILENAME_HEADER, "upload.ogg")).name
    suffix = Path(name).suffix.lower()
    if suffix not in SUFFIXES:
        return JSONResponse(
            {
                "reason_code": "UNSUPPORTED_AUDIO",
                "detail": f"{suffix or '(none)'} is not one of {sorted(SUFFIXES)}",
            },
            status_code=415,
        )

    anchor, anchor_source = _anchor_from(request)

    # Written to disk because the transcription client wants a file handle, and
    # the cache is keyed on content, so a re-upload of a clip already seen costs
    # nothing beyond the write.
    directory = Path(tempfile.mkdtemp())
    path = directory / name
    try:
        path.write_bytes(audio)
        try:
            payload = trace(path, anchor)
        except LookupError as error:
            return JSONResponse(
                {
                    "reason_code": "NOT_CACHED",
                    "detail": str(error),
                    "audio_sha256": content_hash(path),
                },
                status_code=503,
            )
        except Exception as error:  # noqa: BLE001 — surfaced, never swallowed
            return JSONResponse(
                {
                    "reason_code": "TRANSCRIPTION_FAILED",
                    "detail": f"{type(error).__name__}: {error}",
                },
                status_code=502,
            )
    finally:
        path.unlink(missing_ok=True)
        directory.rmdir()

    payload["anchor_source"] = anchor_source
    payload["script_detected"] = detect_script(payload["transcription"]["text"])
    return JSONResponse(payload, status_code=200)
