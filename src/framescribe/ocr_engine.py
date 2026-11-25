"""Apple Vision text recognition (macOS only), "accurate" level with language correction, on an upscaled image.

Vision returns one observation per text line with a normalised box (origin bottom left). The image sent is
`scale` times larger than the key frame (Lanczos), which measured 89.9% label recall against 80.6% at 1x;
rectangles are mapped back to key-frame pixels here, so callers never see the upscaled coordinates.
"""

from __future__ import annotations

import io
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from PIL import Image

from .errors import StageError
from .models import OcrLine, Rect

ENGINE = "apple-vision"
LEVEL = "accurate"


@dataclass(frozen=True)
class EngineInfo:
    name: str
    level: str
    language_correction: bool
    languages: list[str]


def require_macos() -> None:
    if sys.platform != "darwin":
        raise StageError("the ocr stage uses Apple Vision and runs on macOS only")


PREFERRED_TAG = {"en": "en-US", "zh": "zh-Hans"}  # bare codes whose first listed variant is not the one to use


def supported_languages() -> list[str]:
    """The language tags Vision's accurate text recognition offers, in its own order."""
    require_macos()
    import Vision

    request = Vision.VNRecognizeTextRequest.alloc().init()
    request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    return [str(tag) for tag in request.supportedRecognitionLanguagesAndReturnError_(None)[0]]


def vision_languages(language: str, supported: Sequence[str]) -> list[str]:
    """Vision wants locale tags. A tag it offers is passed on unchanged; a bare code ("de") becomes the preferred
    variant if it has one ("en-US", "zh-Hans"), else the first offered tag with that primary subtag ("de-DE")."""
    if language in supported:
        return [language]
    if "-" not in language:
        candidates = [t for t in supported if t.split("-")[0].lower() == language.lower()]
        preferred = PREFERRED_TAG.get(language.lower())
        if candidates:
            return [preferred if preferred in candidates else candidates[0]]
    raise StageError(f"Apple Vision does not support language {[language]}; it offers {sorted(supported)}")


def recognise(image: Image.Image, scale: int, languages: list[str]) -> list[OcrLine]:
    """Text lines of `image` with rectangles in `image`'s own pixels, in reading order (top to bottom)."""
    require_macos()
    import objc  # imported here: the module must import on any platform
    import Foundation
    import Vision

    w, h = image.size
    big = image.convert("RGB").resize((w * scale, h * scale), Image.LANCZOS) if scale != 1 else image.convert("RGB")
    buf = io.BytesIO()
    big.save(buf, "PNG")
    data = buf.getvalue()
    lines: list[OcrLine] = []
    with objc.autorelease_pool():
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setUsesLanguageCorrection_(True)
        supported = request.supportedRecognitionLanguagesAndReturnError_(None)[0]
        missing = [lang for lang in languages if lang not in supported]
        if missing:
            raise StageError(f"Apple Vision does not support language {missing}; it offers {sorted(supported)}")
        request.setRecognitionLanguages_(languages)
        handler = Vision.VNImageRequestHandler.alloc().initWithData_options_(
            Foundation.NSData.dataWithBytes_length_(data, len(data)), None
        )
        ok, err = handler.performRequests_error_([request], None)
        if not ok:
            raise StageError(f"Apple Vision failed: {err}")
        for obs in request.results() or []:
            candidate = obs.topCandidates_(1)[0]
            box = obs.boundingBox()
            x0 = box.origin.x * w
            x1 = (box.origin.x + box.size.width) * w
            y0 = (1.0 - box.origin.y - box.size.height) * h  # flip: Vision's origin is bottom left
            y1 = (1.0 - box.origin.y) * h
            lines.append(OcrLine(text=str(candidate.string()), confidence=round(float(candidate.confidence()), 3),
                                 rect=_rect(x0, y0, x1, y1, w, h)))
    lines.sort(key=lambda ln: (round(ln.rect.y / 8), ln.rect.x))
    return lines


def _rect(x0: float, y0: float, x1: float, y1: float, w: int, h: int) -> Rect:
    import math

    ix0, iy0 = max(0, math.floor(x0)), max(0, math.floor(y0))
    ix1, iy1 = min(w, math.ceil(x1)), min(h, math.ceil(y1))
    return Rect(x=ix0, y=iy0, w=max(1, ix1 - ix0), h=max(1, iy1 - iy0))
