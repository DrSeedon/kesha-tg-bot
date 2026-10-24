"""Conservatively orient document photos; never overwrite the source.

Run with the bot's venv Python. stdout is one JSON object per input, with no
recognized text. Angles are PIL counterclockwise corrections *after* EXIF.
Tesseract is a system dependency; all inference stays local/offline.
"""

import argparse
import csv
import io
import json
import math
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

ANGLES = (0, 90, 180, 270)
# Small phone-sized photos read far better after 2x upscaling (measured on
# real documents); larger ones are already big enough and only get slower.
UPSCALE_BELOW = 2000
DEFAULT_TIME_LIMIT = 40.0


@dataclass(frozen=True)
class TextEvidence:
    score: int = 0
    strong_words: int = 0
    weak_score: int = 0
    weak_words: int = 0

    @property
    def has_text(self) -> bool:
        return self.weak_words >= 3 and self.weak_score >= 12


@dataclass(frozen=True)
class RapidPrediction:
    angle: int
    confidence: float
    margin: float


def parse_tsv(tsv: str) -> TextEvidence:
    """Keep only aggregates: OCR words must never reach logs or stdout."""
    score = strong_words = weak_score = weak_words = 0
    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if len(text) < 3 or not any(c.isalpha() for c in text):
            continue
        try:
            confidence = float(row.get("conf", "-1"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(confidence) or not 0 <= confidence <= 100:
            continue
        if confidence > 70:
            score += len(text)
            strong_words += 1
        if confidence >= 30:
            weak_score += len(text)
            weak_words += 1
    return TextEvidence(score, strong_words, weak_score, weak_words)


def prepare_gray(image: Image.Image) -> Image.Image:
    gray = image.convert("L")
    if max(gray.size) < UPSCALE_BELOW:
        gray = gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.LANCZOS)
    return gray


def ocr_evidence(
    image: Image.Image, languages: str = "rus+eng", timeout: float = 30,
) -> TextEvidence:
    data = io.BytesIO()
    image.save(data, format="PNG")
    try:
        result = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", languages, "--psm", "11", "tsv"],
            input=data.getvalue(), capture_output=True, timeout=timeout, check=False,
            # One thread per process: the four angles run in parallel instead.
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except FileNotFoundError as exc:
        raise RuntimeError("tesseract unavailable; install tesseract-ocr and rus/eng languages") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("tesseract timed out") from exc
    if result.returncode:
        # stderr may contain private input or paths: do not forward it.
        raise RuntimeError(f"tesseract failed (exit {result.returncode}); check OCR languages")
    return parse_tsv(result.stdout.decode("utf-8", errors="replace"))


def vote_angle(evidence: dict[int, TextEvidence]) -> int | None:
    ranked = sorted(ANGLES, key=lambda a: evidence[a].score, reverse=True)
    best, second = (evidence[a] for a in ranked[:2])
    # A single short word (3/9/10 against zero) is not reliable evidence.
    # Thresholds fit aggregates of 12 real photos (9 right, 0 wrong, plain
    # photos untouched); see report.
    if best.score >= 15 and best.strong_words >= 3 and best.score >= 2 * second.score:
        return ranked[0]
    return None


@lru_cache(maxsize=1)
def _rapid_engine():
    from rapid_orientation import RapidOrientation

    return RapidOrientation()


def rapid_prediction(image: Image.Image) -> RapidPrediction:
    """Adapter for pinned rapid-orientation 0.0.11, whose API omits confidence.

    __call__ returns (label, elapsed_seconds). The bundled model itself emits
    probabilities. Its three rows are identical center crops, NOT independent
    votes; do not mistake unanimous argmax for confidence.
    """
    import numpy as np

    engine = _rapid_engine()
    batch = engine.preprocess(engine.load_img(image))
    probabilities = np.asarray(engine.session(batch)[0])
    if (
        probabilities.shape != (3, 4)
        or not np.isfinite(probabilities).all()
        or (probabilities < 0).any()
        or (probabilities > 1).any()
        or not np.allclose(probabilities.sum(axis=1), 1, atol=0.001)
    ):
        raise RuntimeError("unexpected RapidOrientation probability output")
    winners = probabilities.argmax(axis=1)
    if not (winners == winners[0]).all():
        raise RuntimeError("RapidOrientation predictions disagree")
    index = int(winners[0])
    angle = int(engine.labels[index])
    if angle not in ANGLES:
        raise RuntimeError("unexpected RapidOrientation angle")
    confidence = float(probabilities[:, index].min())
    runners_up = np.delete(probabilities, index, axis=1).max(axis=1)
    margin = float((probabilities[:, index] - runners_up).min())
    # On this model a label of 90 means a PIL.rotate(90) correction, not an
    # input rotation. Verified on synthetic text at all four rotations.
    return RapidPrediction(angle, confidence, margin)


def select_angle(
    evidence: dict[int, TextEvidence], rapid: RapidPrediction | None = None,
) -> tuple[int | None, str]:
    angle = vote_angle(evidence)
    if angle is not None:
        return angle, "ocr"
    if (
        rapid is not None
        and rapid.angle in evidence
        and math.isfinite(rapid.confidence)
        and 0.95 <= rapid.confidence <= 1
        and math.isfinite(rapid.margin)
        and 0.80 <= rapid.margin <= 1
        and evidence[rapid.angle].has_text
    ):
        return rapid.angle, "rapid"
    return None, "original"


def collect_evidence(
    gray: Image.Image, languages: str, time_limit: float,
) -> dict[int, TextEvidence]:
    """Run the four angles in parallel under one overall time budget."""
    pool = ThreadPoolExecutor(max_workers=len(ANGLES))
    try:
        futures = {
            angle: pool.submit(ocr_evidence, gray.rotate(angle, expand=True), languages)
            for angle in ANGLES
        }
        done, pending = wait(futures.values(), timeout=time_limit)
        if pending:
            raise RuntimeError("OCR time limit exceeded")
        return {angle: future.result() for angle, future in futures.items()}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def orient_photo(
    source: Path, output_dir: Path, languages: str = "rus+eng",
    time_limit: float = DEFAULT_TIME_LIMIT,
) -> dict:
    source = source.resolve()
    report = {
        "source": str(source), "output": str(source), "angle": None,
        "method": "original", "scores": {}, "status": "не определил",
    }
    try:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        evidence = collect_evidence(prepare_gray(image), languages, time_limit)
        report["scores"] = {str(a): evidence[a].score for a in ANGLES}
        report["strong_words"] = {str(a): evidence[a].strong_words for a in ANGLES}
        report["text_evidence"] = {str(a): e.has_text for a, e in evidence.items()}
        prediction = None
        if vote_angle(evidence) is None and any(e.has_text for e in evidence.values()):
            try:
                prediction = rapid_prediction(image)
                report["rapid"] = {
                    "angle": prediction.angle, "confidence": prediction.confidence,
                    "margin": prediction.margin,
                }
            except Exception:
                # Optional fallback failure must preserve the usable original.
                report["warning"] = "RapidOrientation unavailable or invalid output"
        angle, method = select_angle(evidence, prediction)
        if angle is None:
            report["reason"] = "insufficient text or orientation confidence"
            return report
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="oriented-", suffix=".png", dir=output_dir)
        output = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                image.rotate(angle, expand=True).save(stream, format="PNG")
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        report.update(output=str(output.resolve()), angle=angle, method=method, status="ok")
    except Exception as exc:
        report["status"] = "error"
        # Only our own diagnostic messages are safe to expose.
        report["reason"] = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
    return report


def make_sheets(reports: list[dict], output_dir: Path, cell_size: int = 780) -> list[dict]:
    """Make numbered overview sheets in input order, at most 2x2 per sheet.

    Each thumbnail fits a cell_size square, without upscaling small originals.
    Two pixels of padding keep a 780px long edge inside a <=1568px sheet.
    Labels overlay thumbnails: document details must be read from originals.
    """
    if not 256 <= cell_size <= 780:
        raise ValueError("cell size must be between 256 and 780 pixels")
    items = [(number, report) for number, report in enumerate(reports, 1)
             if report["status"] != "error"]
    sheets = []
    for start in range(0, len(items), 4):
        batch = items[start:start + 4]
        thumbnails = []
        cells = []
        for number, report in batch:
            with Image.open(report["output"]) as opened:
                thumbnail = ImageOps.exif_transpose(opened).convert("RGB")
            thumbnail.thumbnail((cell_size, cell_size), Image.Resampling.LANCZOS)
            thumbnails.append(thumbnail)
            cells.append({"number": number, "source": report["source"], "image": report["output"]})
        cell_width = max(image.width for image in thumbnails) + 4
        cell_height = max(image.height for image in thumbnails) + 4
        columns = min(2, len(batch))
        rows = (len(batch) + columns - 1) // columns
        sheet = Image.new("RGB", (columns * cell_width, rows * cell_height), "white")
        draw = ImageDraw.Draw(sheet)
        font = ImageFont.load_default(size=36)
        for index, (thumbnail, cell) in enumerate(zip(thumbnails, cells)):
            x = (index % columns) * cell_width
            y = (index // columns) * cell_height
            sheet.paste(thumbnail, (x + 2, y + 2))
            label = str(cell["number"])
            box = draw.textbbox((0, 0), label, font=font)
            draw.rectangle((x + 2, y + 2, x + box[2] + 18, y + 50), fill="black")
            draw.text((x + 10, y + 5), label, font=font, fill="white")
            cell.update(column=index % columns, row=index // columns)
        output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="sheet-", suffix=".png", dir=output_dir)
        output = Path(name)
        try:
            with os.fdopen(fd, "wb") as stream:
                sheet.save(stream, format="PNG")
        except BaseException:
            output.unlink(missing_ok=True)
            raise
        sheets.append({
            "kind": "sheet", "output": str(output.resolve()), "cells": cells,
            "width": sheet.width, "height": sheet.height,
            "cell_width": cell_width, "cell_height": cell_height,
            "status": "ok",
        })
    return sheets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/kesha/oriented"))
    parser.add_argument("--languages", default="rus+eng", help="Tesseract language list")
    parser.add_argument("--time-limit", type=float, default=DEFAULT_TIME_LIMIT,
                        help="Seconds of OCR per photo before reporting an error")
    sheet_mode = parser.add_mutually_exclusive_group()
    sheet_mode.add_argument("--sheet", action="store_true", help="Orient inputs, then make numbered overview sheets")
    sheet_mode.add_argument("--sheet-only", action="store_true", help="Make sheets from already oriented inputs, without OCR")
    parser.add_argument("--cell-size", type=int, default=780, choices=range(256, 781), metavar="256..780",
                        help="Maximum thumbnail long edge; default 780, at most four cells per sheet")
    args = parser.parse_args(argv)
    failed = False
    reports = []
    for path in args.paths:
        if args.sheet_only:
            source = str(path.resolve())
            report = {"source": source, "output": source, "status": "ok", "method": "preoriented"}
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception as exc:
                report.update(status="error", reason=type(exc).__name__)
        else:
            report = orient_photo(path, args.output_dir, args.languages, args.time_limit)
        reports.append(report)
        print(json.dumps(report, ensure_ascii=False), flush=True)
        failed |= report["status"] == "error"
    if args.sheet or args.sheet_only:
        try:
            for sheet in make_sheets(reports, args.output_dir, args.cell_size):
                print(json.dumps(sheet, ensure_ascii=False), flush=True)
        except Exception as exc:
            print(json.dumps({"kind": "sheet", "status": "error", "reason": type(exc).__name__}), flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
