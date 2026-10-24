"""Offline orientation tests: synthetic pixels only, no document fixtures."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageDraw, ImageFont, ImageOps

from tools import orient_photo as orient


def document_image():
    image = Image.new("RGB", (1000, 800), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=30)
    for y in range(30, 770, 45):
        draw.text((30, y), "DOCUMENT SAMPLE Reference information TEST", font=font, fill="black")
    # Asymmetric pixels make every quarter-turn distinguishable.
    draw.rectangle((0, 0, 10, 10), fill="red")
    return image


def is_upright(image):
    """document_image() has a red corner square: it marks the upright angle."""
    return image.getpixel((0, 0)) < 150


def evidence(scores, words=4):
    return {a: orient.TextEvidence(s, words if s else 0, s, words if s else 0)
            for a, s in zip(orient.ANGLES, scores)}


@pytest.mark.parametrize("scores,expected", [
    ([0, 0, 0, 0], None), ([3, 0, 0, 0], None),
    ([0, 9, 0, 0], None), ([0, 0, 10, 0], None),
    ([14, 0, 0, 0], None), ([15, 0, 0, 0], 0),
    ([30, 16, 0, 0], None), ([30, 15, 0, 0], 0),
    ([30, 14, 0, 0], 0), ([80, 80, 0, 0], None),
    ([0, 50, 5, 0], 90), ([0, 0, 0, 80], 270),
])
def test_ocr_vote_requires_absolute_and_relative_evidence(scores, expected):
    assert orient.vote_angle(evidence(scores)) == expected


def test_one_long_word_is_not_enough():
    assert orient.vote_angle(evidence([100, 0, 0, 0], words=1)) is None


def test_tsv_score_filters_confidence_numbers_short_and_nonfinite_values():
    tsv = "conf\ttext\n71\tAlpha\n70\tBeta\n91\t12345\n99\tab\n-1\tnoise\nNaN\tnoise\nwrong\tnoise\n60\tGamma\n"
    result = orient.parse_tsv(tsv)
    assert result == orient.TextEvidence(5, 1, 14, 3)
    assert result.has_text
    assert orient.parse_tsv("conf\ttext\n95\tДокумент\n").score == 8


@pytest.mark.parametrize("confidence,margin", [
    (0.94, 0.9), (0.99, 0.79), (float("nan"), 0.9), (1.1, 0.9),
])
def test_rapid_uncertainty_keeps_original(confidence, margin):
    weak = {a: orient.TextEvidence(0, 0, 20, 4) for a in orient.ANGLES}
    assert orient.select_angle(weak, orient.RapidPrediction(90, confidence, margin)) == (None, "original")


def test_rapid_requires_text_at_its_predicted_angle_and_ocr_wins_first():
    zero = evidence([0, 0, 0, 0])
    confident = orient.RapidPrediction(90, 0.99, 0.98)
    assert orient.select_angle(zero, confident) == (None, "original")
    zero[0] = orient.TextEvidence(0, 0, 20, 4)
    assert orient.select_angle(zero, confident) == (None, "original")
    zero[90] = orient.TextEvidence(0, 0, 20, 4)
    assert orient.select_angle(zero, confident) == (90, "rapid")
    assert orient.select_angle(evidence([0, 0, 100, 0]), confident) == (180, "ocr")


@pytest.mark.parametrize("rotation", orient.ANGLES)
def test_synthetic_text_rotation_is_applied_to_color_copy(tmp_path, monkeypatch, rotation):
    upright = document_image()
    source = tmp_path / "input.png"
    upright.rotate(rotation, expand=True).save(source)
    original = source.read_bytes()
    # Upscaling does not commute with rotation byte-for-byte: rebuild the pipeline order.
    upright_gray = orient.prepare_gray(upright.rotate(rotation, expand=True)).rotate(
        (-rotation) % 360, expand=True)

    def fake_ocr(image, _languages):
        # Model OCR on actual rotated synthetic pixels, not the call order
        # (the four angles run in parallel threads).
        if image.size == upright_gray.size and image.tobytes() == upright_gray.tobytes():
            return orient.TextEvidence(80, 8, 80, 8)
        return orient.TextEvidence()

    monkeypatch.setattr(orient, "ocr_evidence", fake_ocr)
    monkeypatch.setattr(orient, "rapid_prediction", lambda _im: pytest.fail("OCR already confident"))
    result = orient.orient_photo(source, tmp_path / "output")
    assert result["angle"] == (-rotation) % 360
    assert result["method"] == "ocr"
    assert result["status"] == "ok"
    assert source.read_bytes() == original
    with Image.open(result["output"]) as output:
        assert output.size == upright.size
        assert output.tobytes() == upright.tobytes()
    assert Path(result["output"]).stat().st_mode & 0o777 == 0o600


def test_exif_is_applied_before_ocr_and_removed_from_copy(tmp_path, monkeypatch):
    source = tmp_path / "exif.jpg"
    exif = Image.Exif()
    exif[274] = 6  # EXIF: display clockwise 90.
    document_image().save(source, exif=exif)
    sizes = set()
    with Image.open(source) as opened:
        displayed = orient.prepare_gray(ImageOps.exif_transpose(opened)).tobytes()

    def fake_ocr(image, _languages):
        sizes.add(image.size)
        return orient.TextEvidence(80, 8, 80, 8) if image.tobytes() == displayed else orient.TextEvidence()

    monkeypatch.setattr(orient, "ocr_evidence", fake_ocr)
    result = orient.orient_photo(source, tmp_path / "out")
    assert sizes == {(1600, 2000), (2000, 1600)}  # EXIF first (portrait), then 2x upscale
    with Image.open(result["output"]) as output:
        assert output.size == (800, 1000)
        assert output.getexif().get(274) is None


@pytest.mark.parametrize("mode", ["blank", "noise"])
def test_nontext_and_ocr_silence_never_call_rapid(tmp_path, monkeypatch, mode):
    image = Image.new("RGB", (240, 160), "blue")
    if mode == "noise":
        image = Image.effect_noise(image.size, 80).convert("RGB")
    source = tmp_path / "nontext.png"
    image.save(source)
    original = source.read_bytes()
    monkeypatch.setattr(orient, "ocr_evidence", lambda *_: orient.TextEvidence())
    monkeypatch.setattr(orient, "rapid_prediction", lambda _: pytest.fail("no evidence of text"))
    result = orient.orient_photo(source, tmp_path / "out")
    assert result["status"] == "не определил"
    assert result["output"] == str(source)
    assert source.read_bytes() == original
    assert not (tmp_path / "out").exists()


def test_all_strong_scores_zero_can_use_weak_text_for_rapid(tmp_path, monkeypatch):
    source = tmp_path / "weak.png"
    document_image().save(source)
    monkeypatch.setattr(orient, "ocr_evidence", lambda *_: orient.TextEvidence(0, 0, 25, 5))
    monkeypatch.setattr(orient, "rapid_prediction", lambda _: orient.RapidPrediction(90, 0.99, 0.98))
    result = orient.orient_photo(source, tmp_path / "out")
    assert result["scores"] == {str(a): 0 for a in orient.ANGLES}
    assert (result["method"], result["angle"]) == ("rapid", 90)


def test_rapid_failure_preserves_original(tmp_path, monkeypatch):
    source = tmp_path / "weak.png"
    document_image().save(source)
    monkeypatch.setattr(orient, "ocr_evidence", lambda *_: orient.TextEvidence(0, 0, 25, 5))

    def broken(_image):
        raise ImportError("missing optional package")

    monkeypatch.setattr(orient, "rapid_prediction", broken)
    result = orient.orient_photo(source, tmp_path / "out")
    assert result["output"] == str(source)
    assert result["status"] == "не определил"
    assert "warning" in result


@pytest.mark.parametrize("failure", [FileNotFoundError(), subprocess.TimeoutExpired("tesseract", 30)])
def test_missing_or_timed_out_tesseract_is_explicit_error(tmp_path, monkeypatch, failure):
    source = tmp_path / "text.png"
    document_image().save(source)

    def failed(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(orient.subprocess, "run", failed)
    result = orient.orient_photo(source, tmp_path / "out")
    assert result["status"] == "error"
    assert result["output"] == str(source)
    assert "tesseract" in result["reason"]


def test_tesseract_uses_stdin_and_does_not_disclose_stderr(monkeypatch):
    def failed(args, **kwargs):
        assert args == ["tesseract", "stdin", "stdout", "-l", "rus+eng", "--psm", "11", "tsv"]
        assert kwargs["input"].startswith(b"\x89PNG")
        assert kwargs["timeout"] == 30
        return subprocess.CompletedProcess(args, 1, b"", b"PRIVATE_CONTENT")

    monkeypatch.setattr(orient.subprocess, "run", failed)
    with pytest.raises(RuntimeError, match="check OCR languages") as error:
        orient.ocr_evidence(document_image())
    assert "PRIVATE_CONTENT" not in str(error.value)


def test_cli_handles_batch_errors_and_repeated_names(tmp_path, monkeypatch, capsys):
    source = tmp_path / "text.png"
    document_image().save(source)

    def fake_ocr(image, _languages):
        return orient.TextEvidence(100, 5, 100, 5) if is_upright(image) else orient.TextEvidence()

    monkeypatch.setattr(orient, "ocr_evidence", fake_ocr)
    args = [str(tmp_path / "missing.png"), str(source), str(source), "--output-dir", str(tmp_path / "out")]
    assert orient.main(args) == 1
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(reports) == 3
    assert reports[0]["status"] == "error"
    assert reports[1]["output"] != reports[2]["output"]
    assert all(Path(r["output"]).exists() for r in reports[1:])


def test_rapid_adapter_reads_probabilities_instead_of_elapsed(monkeypatch):
    np = pytest.importorskip("numpy")

    class Engine:
        labels = ["0", "90", "180", "270"]

        def load_img(self, image):
            return image

        def preprocess(self, image):
            return image

        def session(self, image):
            return [np.tile([0.002, 0.99, 0.004, 0.004], (3, 1))]

        def __call__(self, image):
            pytest.fail("public result contains elapsed time, not confidence")

    monkeypatch.setattr(orient, "_rapid_engine", Engine)
    prediction = orient.rapid_prediction(document_image())
    assert prediction.angle == 90
    assert prediction.confidence == pytest.approx(0.99)
    assert prediction.margin == pytest.approx(0.986)


@pytest.mark.parametrize("rotation", orient.ANGLES)
def test_real_bundled_rapid_is_offline_and_uses_correct_angle(monkeypatch, rotation):
    pytest.importorskip("rapid_orientation")
    import socket

    def no_network(*_args, **_kwargs):
        pytest.fail("orientation must not access the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    prediction = orient.rapid_prediction(document_image().rotate(rotation, expand=True))
    assert prediction.angle == (-rotation) % 360
    assert 0 <= prediction.confidence <= 1


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="system tesseract not installed")
@pytest.mark.parametrize("rotation", orient.ANGLES)
def test_real_tesseract_on_synthetic_text(tmp_path, rotation):
    languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=True)
    if "eng" not in languages.stdout.split():
        pytest.skip("tesseract English data not installed")
    source = tmp_path / "text.png"
    document_image().rotate(rotation, expand=True).save(source)
    result = orient.orient_photo(source, tmp_path / "out", languages="eng")
    assert result["status"] == "ok", result
    assert result["method"] == "ocr"
    assert result["angle"] == (-rotation) % 360


@pytest.mark.parametrize("count,expected", [(1, 1), (4, 1), (5, 2), (12, 3)])
def test_sheet_layout_keeps_large_cells_order_and_source_mapping(tmp_path, count, expected):
    reports = []
    for index in range(count):
        image = Image.new("RGB", (1280, 720), (index * 17, 30, 90))
        source = tmp_path / f"image-{index}.png"
        image.save(source)
        reports.append({"source": str(source), "output": str(source), "status": "ok"})
    sheets = orient.make_sheets(reports, tmp_path / "out")
    assert len(sheets) == expected
    cells = [cell for sheet in sheets for cell in sheet["cells"]]
    assert [cell["number"] for cell in cells] == list(range(1, count + 1))
    assert [cell["source"] for cell in cells] == [r["source"] for r in reports]
    for sheet in sheets:
        assert sheet["cell_width"] == 784  # 780px thumbnail + 2px on either side.
        assert sheet["cell_height"] == 443
        assert max(sheet["width"], sheet["height"]) <= 1568
        assert len(sheet["cells"]) <= 4
        with Image.open(sheet["output"]) as image:
            for cell in sheet["cells"]:
                # Away from the badge, each cell contains its own input pixels.
                x = cell["column"] * sheet["cell_width"] + 100
                y = cell["row"] * sheet["cell_height"] + 100
                assert image.getpixel((x, y)) == ((cell["number"] - 1) * 17, 30, 90)
                # Number badge is present, while paths stay out of the image.
                assert image.getpixel((cell["column"] * sheet["cell_width"] + 3,
                                       cell["row"] * sheet["cell_height"] + 3)) == (0, 0, 0)
        assert Path(sheet["output"]).stat().st_mode & 0o777 == 0o600


def test_sheet_portraits_mixed_aspects_and_custom_cell_size(tmp_path):
    reports = []
    for index, size in enumerate([(720, 1280), (1280, 720), (300, 200), (700, 700)]):
        source = tmp_path / f"image-{index}.png"
        Image.new("RGB", size, "white").save(source)
        reports.append({"source": str(source), "output": str(source), "status": "ok"})
    sheets = orient.make_sheets(reports, tmp_path / "out", cell_size=600)
    assert sheets[0]["width"] == sheets[0]["height"] == 1208
    assert [(c["row"], c["column"]) for c in sheets[0]["cells"]] == [(0, 0), (0, 1), (1, 0), (1, 1)]
    with pytest.raises(ValueError):
        orient.make_sheets(reports, tmp_path / "out", cell_size=900)


def test_sheet_only_cli_skips_ocr_and_preserves_number_after_bad_file(tmp_path, monkeypatch, capsys):
    source = tmp_path / "text.png"
    document_image().save(source)
    monkeypatch.setattr(orient, "ocr_evidence", lambda *_: pytest.fail("already oriented"))
    assert orient.main(["--sheet-only", str(tmp_path / "bad.png"), str(source),
                        "--output-dir", str(tmp_path / "out")]) == 1
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert reports[0]["status"] == "error"
    assert reports[2]["kind"] == "sheet"
    assert reports[2]["cells"][0]["number"] == 2
    assert reports[2]["cells"][0]["image"] == str(source)


def test_sheet_cli_orients_before_building_overview(tmp_path, monkeypatch, capsys):
    source = tmp_path / "text.png"
    document_image().save(source)

    def fake_ocr(image, _languages):
        # Red corner lands bottom-left only after a 90 degree correction.
        marked = image.getpixel((0, image.height - 1)) < 150
        return orient.TextEvidence(100, 5, 100, 5) if marked else orient.TextEvidence()

    monkeypatch.setattr(orient, "ocr_evidence", fake_ocr)
    assert orient.main(["--sheet", str(source), "--output-dir", str(tmp_path / "out")]) == 0
    photo, sheet = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert photo["angle"] == 90
    assert sheet["cells"][0]["image"] == photo["output"]
    assert sheet["cells"][0]["source"] == str(source)


# Aggregates measured on 12 real document photos and 6 ordinary photos with
# 2x-upscaled OCR: per angle (0, 90, 180, 270) -> (score, strong words).
# Truth: the angle the owner verified by eye; None = leave the original.
REAL_AGGREGATES = [
    (((0, 0), (161, 23), (0, 0), (9, 2)), 90),
    (((3, 1), (0, 0), (3, 1), (0, 0)), None),
    (((3, 1), (1276, 6), (0, 0), (0, 0)), 90),
    (((0, 0), (19, 3), (0, 0), (0, 0)), 90),
    (((0, 0), (5, 1), (0, 0), (0, 0)), None),
    (((0, 0), (3, 1), (0, 0), (0, 0)), None),
    (((0, 0), (83, 15), (0, 0), (0, 0)), 90),
    (((0, 0), (66, 14), (0, 0), (7, 2)), 90),
    (((51, 9), (4, 1), (0, 0), (0, 0)), 0),
    (((225, 39), (0, 0), (0, 0), (3, 1)), 0),
    (((3, 1), (18, 5), (0, 0), (7, 1)), 90),
    (((0, 0), (18, 4), (0, 0), (3, 1)), 90),
]
ORDINARY_MAX_SCORE_3 = [((3, 1), (0, 0), (0, 0), (0, 0))] + [((0, 0),) * 4] * 5


def aggregate_evidence(cells):
    return {a: orient.TextEvidence(s, w, s, w) for a, (s, w) in zip(orient.ANGLES, cells)}


@pytest.mark.parametrize("cells,truth", REAL_AGGREGATES)
def test_vote_on_real_photo_aggregates_never_errs(cells, truth):
    result = orient.vote_angle(aggregate_evidence(cells))
    assert result in (truth, None)
    # Where the rule decides, it must decide what the owner verified.
    if result is not None:
        assert result == truth


def test_vote_completeness_on_real_aggregates():
    decided = [orient.vote_angle(aggregate_evidence(c)) is not None for c, _ in REAL_AGGREGATES]
    assert sum(decided) == 9


@pytest.mark.parametrize("cells", ORDINARY_MAX_SCORE_3)
def test_ordinary_photos_are_never_rotated(cells):
    assert orient.vote_angle(aggregate_evidence(cells)) is None


def test_small_images_are_upscaled_and_large_ones_are_not():
    assert orient.prepare_gray(Image.new("RGB", (1000, 700))).size == (2000, 1400)
    assert orient.prepare_gray(Image.new("RGB", (1999, 100))).size == (3998, 200)
    assert orient.prepare_gray(Image.new("RGB", (2000, 100))).size == (2000, 100)


def test_tesseract_runs_single_threaded_per_process(monkeypatch):
    seen = {}

    def run(args, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, b"conf\ttext\n", b"")

    monkeypatch.setattr(orient.subprocess, "run", run)
    orient.ocr_evidence(document_image())
    assert seen["OMP_THREAD_LIMIT"] == "1"


def test_four_angles_run_in_parallel(monkeypatch):
    import threading

    barrier = threading.Barrier(4, timeout=5)

    def fake_ocr(_image, _languages):
        barrier.wait()  # deadlocks (BrokenBarrierError) unless all four overlap
        return orient.TextEvidence()

    monkeypatch.setattr(orient, "ocr_evidence", fake_ocr)
    assert len(orient.collect_evidence(Image.new("L", (50, 50)), "eng", 10)) == 4


def test_overall_time_limit_reports_error_and_keeps_original(tmp_path, monkeypatch):
    import time

    source = tmp_path / "slow.png"
    document_image().save(source)
    monkeypatch.setattr(orient, "ocr_evidence", lambda *_: time.sleep(1) or orient.TextEvidence())
    started = time.monotonic()
    result = orient.orient_photo(source, tmp_path / "out", time_limit=0.1)
    assert time.monotonic() - started < 0.9
    assert result["status"] == "error"
    assert "time limit" in result["reason"]
    assert result["output"] == str(source)
