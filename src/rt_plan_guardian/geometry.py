"""Geometry helpers: contour parsing, point-in-ROI tests and dose-grid decoding.

Deliberately small and dependency-light (numpy only). Contours are treated as planar
polygons on axial slices; overlapping polygons on one slice combine with the even-odd
rule, so an inner ring is a hole. Dose sampling uses voxel centres - this is a
consistency screen, not a replacement for the treatment planning system's DVH engine.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np
from pydicom.dataset import Dataset

from .errors import CannotEvaluate

_FALLBACK_SLICE_MM = 3.0


# --------------------------------------------------------------------------- polygons
def _even_odd(poly: np.ndarray, X: np.ndarray, Y: np.ndarray) -> np.ndarray:
    """Ray-casting point-in-polygon, vectorised over a grid of points."""
    inside = np.zeros(X.shape, dtype=bool)
    xj, yj = poly[-1]
    for xi, yi in poly:
        crosses = (yi > Y) != (yj > Y)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_int = (xj - xi) * (Y - yi) / (yj - yi) + xi
        inside ^= crosses & (x_int > X)
        xj, yj = xi, yi
    return inside


def polygons_mask(polys: list[np.ndarray], xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
    """Boolean (len(ys), len(xs)) mask of grid points inside the union of `polys` (even-odd)."""
    out = np.zeros((len(ys), len(xs)), dtype=bool)
    pts = np.concatenate(polys)
    xmin, ymin = pts.min(axis=0)
    xmax, ymax = pts.max(axis=0)
    ix = np.flatnonzero((xs >= xmin) & (xs <= xmax))
    iy = np.flatnonzero((ys >= ymin) & (ys <= ymax))
    if ix.size == 0 or iy.size == 0:
        return out
    X, Y = np.meshgrid(xs[ix], ys[iy])
    inside = np.zeros(X.shape, dtype=bool)
    for poly in polys:
        inside ^= _even_odd(poly, X, Y)
    out[np.ix_(iy, ix)] = inside
    return out


# --------------------------------------------------------------------------- ROIs
@dataclass(frozen=True)
class RoiGeometry:
    number: int
    name: str
    slices: dict[float, list[np.ndarray]]  # z (mm, rounded to 1 um) -> polygons of shape (n, 2)

    @property
    def z_levels(self) -> np.ndarray:
        return np.array(sorted(self.slices), dtype=float)

    @property
    def slice_spacing(self) -> float | None:
        z = self.z_levels
        if z.size < 2:
            return None
        return float(np.median(np.diff(z)))

    @property
    def half_thickness(self) -> float:
        sp = self.slice_spacing
        return (sp if sp else _FALLBACK_SLICE_MM) / 2.0

    @property
    def bbox(self) -> tuple[np.ndarray, np.ndarray] | None:
        if not self.slices:
            return None
        pts = np.concatenate([p for polys in self.slices.values() for p in polys])
        z = self.z_levels
        lo = np.array([pts[:, 0].min(), pts[:, 1].min(), z.min() - self.half_thickness])
        hi = np.array([pts[:, 0].max(), pts[:, 1].max(), z.max() + self.half_thickness])
        return lo, hi

    def nearest_slice(self, z: float) -> float | None:
        levels = self.z_levels
        if levels.size == 0:
            return None
        k = int(np.argmin(np.abs(levels - z)))
        if abs(levels[k] - z) <= self.half_thickness + 1e-6:
            return float(levels[k])
        return None

    def contains(self, x: float, y: float, z: float) -> bool:
        key = self.nearest_slice(z)
        if key is None:
            return False
        mask = polygons_mask(self.slices[key], np.array([x]), np.array([y]))
        return bool(mask[0, 0])

    def mask_on_grid(self, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray) -> np.ndarray:
        """Boolean (nz, ny, nx) mask of grid voxel centres inside the ROI."""
        mask = np.zeros((len(zs), len(ys), len(xs)), dtype=bool)
        for k, z in enumerate(zs):
            key = self.nearest_slice(float(z))
            if key is not None:
                mask[k] = polygons_mask(self.slices[key], xs, ys)
        return mask


def parse_structure_set(struct: Dataset) -> dict[int, RoiGeometry]:
    """Parse every ROI's closed planar contours. Malformed contours are skipped here
    (STRUCT-004 is the rule that reports them)."""
    names: dict[int, str] = {}
    for roi in struct.get("StructureSetROISequence", []):
        try:
            names[int(roi.ROINumber)] = str(roi.get("ROIName", "") or "")
        except (AttributeError, TypeError, ValueError):
            continue
    slices: dict[int, dict[float, list[np.ndarray]]] = {n: {} for n in names}
    for rc in struct.get("ROIContourSequence", []):
        try:
            num = int(rc.ReferencedROINumber)
        except (AttributeError, TypeError, ValueError):
            continue
        if num not in slices:
            continue
        for contour in rc.get("ContourSequence", []):
            data = contour.get("ContourData")
            if data is None:
                continue
            if str(contour.get("ContourGeometricType", "CLOSED_PLANAR")) != "CLOSED_PLANAR":
                continue  # catheter/applicator polylines and points are not volumes
            arr = np.asarray(data, dtype=float)
            if arr.size < 9 or arr.size % 3 or not np.isfinite(arr).all():
                continue
            pts = arr.reshape(-1, 3)
            z = round(float(pts[:, 2].mean()), 3)
            slices[num].setdefault(z, []).append(pts[:, :2])
    return {n: RoiGeometry(n, names[n], slices[n]) for n in names}


# --------------------------------------------------------------------------- dose grid
@dataclass(frozen=True)
class DoseGrid:
    dose_gy: np.ndarray  # (nz, ny, nx) float32
    xs: np.ndarray
    ys: np.ndarray
    zs: np.ndarray

    @property
    def extent(self) -> tuple[np.ndarray, np.ndarray]:
        lo = np.array([self.xs.min(), self.ys.min(), self.zs.min()])
        hi = np.array([self.xs.max(), self.ys.max(), self.zs.max()])
        return lo, hi


def dose_axes(ds: Dataset) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Voxel-centre coordinates (x, y, z in mm) from the RT Dose geometry tags only."""
    try:
        rows = int(ds.Rows)
        cols = int(ds.Columns)
        frames = int(ds.get("NumberOfFrames", 1) or 1)
        ipp = [float(v) for v in ds.ImagePositionPatient]
        dy, dx = (float(v) for v in ds.PixelSpacing)
        if len(ipp) != 3 or rows < 1 or cols < 1 or frames < 1:
            raise ValueError("bad dimensions")
    except (AttributeError, TypeError, ValueError) as exc:
        raise CannotEvaluate(f"dose grid geometry tags missing or malformed ({exc})") from exc
    iop = [float(v) for v in ds.get("ImageOrientationPatient", [1, 0, 0, 0, 1, 0])]
    if not np.allclose(iop, [1, 0, 0, 0, 1, 0], atol=1e-3):
        raise CannotEvaluate("dose grid is not in standard axial orientation (IOP 1,0,0,0,1,0)")
    offsets = ds.get("GridFrameOffsetVector")
    if offsets is None and frames == 1:
        offsets = [0.0]
    if offsets is None or len(offsets) != frames:
        raise CannotEvaluate("GridFrameOffsetVector missing or does not match NumberOfFrames")
    xs = ipp[0] + dx * np.arange(cols)
    ys = ipp[1] + dy * np.arange(rows)
    zs = ipp[2] + np.asarray([float(v) for v in offsets])
    return xs, ys, zs


def decode_dose_grid(ds: Dataset) -> DoseGrid:
    """Decode RT Dose pixel data to Gy. Raises CannotEvaluate for anything not trustworthy."""
    units = str(ds.get("DoseUnits", "")).upper()
    if units != "GY":
        raise CannotEvaluate(
            f"DoseUnits is {units or 'missing'}, not GY; absolute comparison invalid"
        )
    xs, ys, zs = dose_axes(ds)
    try:
        bits = int(ds.BitsAllocated)
        scaling = float(ds.DoseGridScaling)
    except (AttributeError, TypeError, ValueError) as exc:
        raise CannotEvaluate(f"BitsAllocated/DoseGridScaling missing or malformed ({exc})") from exc
    if bits not in (16, 32):
        raise CannotEvaluate(f"unsupported BitsAllocated={bits}")
    if not (np.isfinite(scaling) and scaling > 0):
        raise CannotEvaluate(f"DoseGridScaling must be a positive number, got {scaling}")
    meta = getattr(ds, "file_meta", None)
    ts = getattr(meta, "TransferSyntaxUID", None)
    if ts is not None and (ts.is_compressed or not ts.is_little_endian):
        raise CannotEvaluate(f"unsupported transfer syntax {ts.name}")
    n = len(xs) * len(ys) * len(zs)
    dtype = np.dtype(f"<{'i' if int(ds.get('PixelRepresentation', 0)) == 1 else 'u'}{bits // 8}")
    raw = ds.get("PixelData")
    if raw is None or len(raw) < n * dtype.itemsize:
        raise CannotEvaluate("PixelData is missing or shorter than Rows x Columns x NumberOfFrames")
    arr = np.frombuffer(raw, dtype=dtype, count=n).reshape(len(zs), len(ys), len(xs))
    return DoseGrid(arr.astype(np.float32) * np.float32(scaling), xs, ys, zs)


def percentile_dose(values: Iterable[float] | np.ndarray, volume_percent: float) -> float:
    """Dx: the minimum dose received by `volume_percent` % of voxels (D95 -> 5th percentile)."""
    arr = np.asarray(values, dtype=float)
    return float(np.percentile(arr, 100.0 - volume_percent))
