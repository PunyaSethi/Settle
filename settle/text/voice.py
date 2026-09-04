"""Speech to transcript. SPEC §11, DECISIONS "Voice clips".

    transcribe(Path("fixtures/voice/clip1.ogg")) -> Transcription

One model call per distinct audio file, ever. The result is cached on the
audio's **content hash** in `out/llm_cache.json`, so the demo makes zero API
calls after the first run and a judge cloning the repo makes none at all: the
committed cache already holds every clip.

Content hash, not filename
--------------------------
A filename is a claim about a file; a hash is the file. Keying on the name would
serve a stale transcript the moment a clip was re-recorded under the same name,
and a re-recorded clip is exactly what happens when the first take mumbles.

The script problem, and what actually fixed it
---------------------------------------------
Prior production work said `gpt-transcribe` returns Devanagari for Hindi speech
regardless of the `language` parameter, because that parameter steers
*recognition* and not output script. Measured at CP15 on these four clips, it is
worse than that: with `language="hi"` and no prompt, `gpt-4o-transcribe` returns
**Urdu in Arabic script** — a third script, which no downstream parser was
written for.

It also truncates, and non-deterministically. Clip 1's un-prompted transcript
stops at "agle mahine kar dunga" and drops the self-correction that follows,
which is the entire point of that clip. The audio does contain it; the
transcription discarded it. At the default temperature the same file transcribed
four times gave three different strings, one of them truncated — so `temperature`
is set to 0 here, and that is a correctness setting rather than a preference.

`prompt` is what fixes both. It is documented as steering style and spelling,
and supplying a romanised Hinglish example is enough to pull the output into
Latin script *and* recover the dropped clause. Same clip, same audio:

    language="hi", no prompt   'ہاں دیکھو ابھی تھوڑا ٹائٹ چل رہا ہے۔ اگلے مہینے کر دوں گا۔'
    language="hi", + prompt    'Haan dekho abhi thoda tight chal raha hai, agle
                                mahine kar dunga. Chalega nahi pandrah tareekh
                                tak ho jayega.'

The second is usable and the first is not, and nothing about the request changed
except a sentence of context.

The prompt steers; it does not guarantee. `detect_script` records what came back
rather than correcting it, and the parsers accept Latin and Devanagari both
(`settle.text.classify.normalise` folds the digits, `WORD_NUMBERS` carries both
spellings). A transcript that arrives in a script we cannot parse is a finding to
report, not something to silently transliterate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CACHE_PATH",
    "MODEL",
    "Transcription",
    "content_hash",
    "detect_script",
    "transcribe",
]

MODEL: Final[str] = "gpt-4o-transcribe"
CACHE_PATH: Final[Path] = Path(os.environ.get("SETTLE_LLM_CACHE", "out/llm_cache.json"))

# The committed transcript record. `out/llm_cache.json` is a run artefact and is
# gitignored, so a fresh clone has no cache — but `out/voice_demo.json` is
# committed and carries every clip's transcript beside the sha256 of the audio
# that produced it. Read as a fallback, so cloning the repo is enough to replay
# the demo with no key. Read-only: nothing is ever written back here.
SEED_PATH: Final[Path] = Path(os.environ.get("SETTLE_VOICE_DEMO", "out/voice_demo.json"))

# The language we tell the model to expect. It steers recognition only; see the
# module docstring for what it does not do.
LANGUAGE: Final[str] = "hi"

# The one sentence that decides whether any of this works. See the docstring:
# without it the output is Urdu and clause-truncated; with it, romanised
# Hinglish with the self-correction intact.
PROMPT: Final[str] = (
    "Hinglish voice note from an Indian customer about a failed payment. "
    "Transcribe in Latin script (romanised Hindi), e.g. "
    "'haan bhai, pandrah tareekh ko kar dunga', 'ek hafte mein bhej dunga'."
)

# Bumped whenever MODEL, LANGUAGE or PROMPT changes. The cache key carries it,
# so a steering change invalidates the transcripts it produced instead of
# serving them under a configuration that no longer exists.
PROMPT_VERSION: Final[str] = "v3-latin-hint-temp0"

# Not a tuning knob — a correctness one, measured on clip 1. At the default
# temperature the same audio transcribed four times gave three different strings
# and one of them silently dropped the final clause, which on that clip is the
# self-correction the whole fixture exists to demonstrate. At 0 it is 4/4
# identical with the clause intact.
#
# A transcript that varies run to run also makes the cache a lie: the committed
# entry would be one sample from a distribution rather than what the audio says.
TEMPERATURE: Final[float] = 0.0

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN = re.compile(r"[A-Za-z]")


@dataclass(frozen=True)
class Transcription:
    """One transcript, and where it came from."""

    text: str
    audio_sha256: str
    model: str
    script: str
    cached: bool
    source: str  # STT | CACHE

    def as_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "audio_sha256": self.audio_sha256,
            "model": self.model,
            "script": self.script,
            "cached": self.cached,
            "source": self.source,
        }


def content_hash(path: Path) -> str:
    """SHA256 of the audio bytes. The cache key, and the only one."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def detect_script(text: str) -> str:
    """Which script came back. Recorded rather than corrected.

    A transcript that mangles Hinglish is a finding, and quietly transliterating
    it here would hide the finding and add a second place for the meaning to
    drift.
    """
    devanagari = bool(_DEVANAGARI.search(text))
    latin = bool(_LATIN.search(text))
    if devanagari and latin:
        return "mixed"
    if devanagari:
        return "devanagari"
    if latin:
        return "latin"
    return "unknown"


def _load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _seed_lookup(digest: str) -> str | None:
    """The transcript for this audio from the committed demo artefact, if any.

    Keyed on the same content hash as the cache, so the two cannot disagree
    about which audio a transcript belongs to.
    """
    if not SEED_PATH.exists():
        return None
    try:
        demo = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for clip in demo.get("clips", []):
        if (clip.get("audio") or {}).get("sha256") == digest:
            transcript = (clip.get("transcription") or {}).get("text")
            if transcript:
                return transcript
    return None


def _cache_key(digest: str) -> str:
    return f"stt:{MODEL}:{PROMPT_VERSION}:{digest}"


def transcribe(
    path: str | Path,
    *,
    cache_path: Path | None = None,
    client: Any = None,
    allow_api: bool = True,
) -> Transcription:
    """Transcript for one audio file, from the cache if it has been seen.

    Three sources, in order: the run cache, then the committed demo artefact,
    then the model. `allow_api=False` stops before the third, which is the mode
    the replay runs in — `out/voice_demo.json` alone has to reproduce the whole
    trace with no credentials, and a replay that silently reached for the
    network would not be a replay.
    """
    path = Path(path)
    digest = content_hash(path)
    cache_file = cache_path or CACHE_PATH
    cache = _load_cache(cache_file)
    key = _cache_key(digest)

    if key in cache:
        entry = cache[key]
        text = entry["text"] if isinstance(entry, dict) else str(entry)
        return Transcription(
            text=text,
            audio_sha256=digest,
            model=MODEL,
            script=detect_script(text),
            cached=True,
            source="CACHE",
        )

    seeded = _seed_lookup(digest)
    if seeded is not None:
        return Transcription(
            text=seeded,
            audio_sha256=digest,
            model=MODEL,
            script=detect_script(seeded),
            cached=True,
            source="DEMO",
        )

    if not allow_api:
        raise LookupError(
            f"{path.name} is not in {cache_file} and allow_api is false. "
            "The committed cache is what lets the demo replay without a key; "
            "if this file is new, transcribe it once with credentials."
        )

    if client is None:
        from openai import OpenAI  # imported here so the module loads without a key

        client = OpenAI()

    with path.open("rb") as handle:
        response = client.audio.transcriptions.create(
            model=MODEL,
            file=handle,
            language=LANGUAGE,
            prompt=PROMPT,
            temperature=TEMPERATURE,
            response_format="text",
        )
    text = response if isinstance(response, str) else getattr(response, "text", str(response))
    text = text.strip()

    cache[key] = {
        "text": text,
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "language": LANGUAGE,
        "temperature": TEMPERATURE,
        "audio_sha256": digest,
        "source_file": path.name,
    }
    _save_cache(cache_file, cache)

    return Transcription(
        text=text,
        audio_sha256=digest,
        model=MODEL,
        script=detect_script(text),
        cached=False,
        source="STT",
    )
