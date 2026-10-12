import numpy as np
import pytest

from rt_plan_guardian.errors import CannotEvaluate
from rt_plan_guardian.geometry import (
    RoiGeometry,
    decode_dose_grid,
    dose_axes,
    percentile_dose,
    polygons_mask,
)


def square(lo, hi):
    return np.array([[lo, lo], [hi, lo], [hi, hi], [lo, hi]], dtype=float)


def test_polygon_with_hole_is_even_odd():
    xs = ys = np.arange(0.5, 10.0, 1.0)
    mask = polygons_mask([square(0, 10), square(4, 6)], xs, ys)
    assert mask.sum() == 100 - 4
    assert not mask[4:6, 4:6].any()
    assert mask[0, 0] and mask[9, 9]


def test_polygon_outside_grid_gives_empty_mask():
    mask = polygons_mask([square(100, 110)], np.arange(0.0, 10.0), np.arange(0.0, 10.0))
    assert not mask.any()


def test_roi_slice_matching_and_spacing():
    geom = RoiGeometry(
        1, "PTV", {0.0: [square(0, 10)], 5.0: [square(0, 10)], 10.0: [square(0, 10)]}
    )
    assert geom.slice_spacing == 5.0
    assert geom.contains(5, 5, 0.0)
    assert geom.contains(5, 5, 2.4)  # within half a slice thickness
    assert not geom.contains(5, 5, 12.6)  # beyond the last slice + half thickness
    assert not geom.contains(50, 5, 0.0)  # outside the polygon
    lo, hi = geom.bbox
    assert lo[2] == -2.5 and hi[2] == 12.5


def test_single_slice_roi_uses_fallback_thickness():
    geom = RoiGeometry(1, "POI", {0.0: [square(0, 10)]})
    assert geom.slice_spacing is None
    assert geom.contains(5, 5, 1.4) and not geom.contains(5, 5, 1.6)


def test_mask_on_grid_matches_contains():
    geom = RoiGeometry(1, "PTV", {0.0: [square(0, 10)], 5.0: [square(0, 10)]})
    xs = ys = np.arange(0.5, 10.0, 1.0)
    zs = np.array([0.0, 5.0, 20.0])
    mask = geom.mask_on_grid(xs, ys, zs)
    assert mask.shape == (3, 10, 10)
    assert mask[0].all() and mask[1].all() and not mask[2].any()


def test_percentile_dose_is_dvh_definition():
    values = np.arange(1, 101, dtype=float)  # 100 voxels, 1..100 Gy
    assert percentile_dose(values, 95) == pytest.approx(np.percentile(values, 5))
    assert percentile_dose(values, 100) == pytest.approx(1.0)  # D100 = minimum
    assert percentile_dose(values, 0) == pytest.approx(100.0)  # D0 = maximum


def test_dose_decoding_roundtrip(case):
    grid = decode_dose_grid(case.dose)
    assert grid.dose_gy.shape == (21, 41, 41)
    assert grid.dose_gy.max() == pytest.approx(62.0, abs=0.01)
    lo, hi = grid.extent
    assert lo.tolist() == [-60.0, -60.0, -30.0] and hi.tolist() == [60.0, 60.0, 30.0]
    centre = grid.dose_gy[10, 20, 20]  # (0, 0, 0) mm
    assert centre == pytest.approx(62.0, abs=0.01)


def test_decode_refuses_relative_dose(case):
    case.dose.DoseUnits = "RELATIVE"
    with pytest.raises(CannotEvaluate, match="not GY"):
        decode_dose_grid(case.dose)


def test_decode_refuses_truncated_pixel_data(case):
    case.dose.PixelData = case.dose.PixelData[:100]
    with pytest.raises(CannotEvaluate, match="shorter"):
        decode_dose_grid(case.dose)


def test_rotated_dose_grid_is_unsupported_not_misread(case):
    case.dose.ImageOrientationPatient = [0, 1, 0, 1, 0, 0]
    with pytest.raises(CannotEvaluate, match="orientation"):
        dose_axes(case.dose)
