"""Display: brightness stretches, tracker views (stacking, differences) and GIF export."""

from __future__ import annotations

import io
import warnings

import numpy as np
from PIL import Image, ImageDraw


def stretch(
    cube: np.ndarray,
    lo_pct: float = 1.0,
    hi_pct: float = 99.7,
    a: float = 0.1,
    per_frame: bool = False,
) -> np.ndarray:
    """asinh stretch to [0, 1]. Default: one global stretch so real brightness changes stay visible."""

    def one(x, lo, hi):
        z = np.clip((x - lo) / max(hi - lo, 1e-12), 0, 1)
        return np.arcsinh(z / a) / np.arcsinh(1 / a)

    out = np.empty_like(cube, dtype=np.float32)
    if per_frame:
        for i in range(len(cube)):
            f = cube[i][np.isfinite(cube[i])]
            lo, hi = np.percentile(f, [lo_pct, hi_pct]) if f.size else (0, 1)
            out[i] = one(cube[i], lo, hi)
    else:
        f = cube[np.isfinite(cube)]
        lo, hi = np.percentile(f, [lo_pct, hi_pct]) if f.size else (0, 1)
        out[:] = one(cube, lo, hi)
    return out


def limits(values: np.ndarray, lo_pct: float = 1.0, hi_pct: float = 99.7) -> tuple[float, float]:
    """Robust display range of an image or cube (percentiles of the finite pixels)."""
    good = values[np.isfinite(values)]
    if good.size == 0:
        return 0.0, 1.0
    lo, hi = np.percentile(good, [lo_pct, hi_pct])
    return float(lo), float(hi)


def mean_image(stack: np.ndarray) -> np.ndarray:
    """Per-pixel mean of a stack of images, ignoring missing pixels."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # all-NaN pixels at the edges are expected
        return np.nanmean(stack, axis=0)


def windowed(cube: np.ndarray, group: list[int], k: int, window: int = 1) -> np.ndarray:
    """Frame k averaged with its neighbours in time (`window` frames in all, centred on k).

    `group` lists the frames that may be combined with k (the ones from the same detector), in
    time order. Averaging neighbours trades time resolution for a cleaner picture.
    """
    if window <= 1 or k not in group:
        return cube[k]
    at, half = group.index(k), window // 2
    return mean_image(cube[group[max(at - half, 0) : at + half + 1]])


def static_sky(cube: np.ndarray, group: list[int]) -> np.ndarray:
    """What does not change: the per-pixel median of a group of frames."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        return np.nanmedian(cube[group], axis=0)


def enclosed_blanks(image: np.ndarray) -> np.ndarray:
    """Blank pixels completely surrounded by data. In SPHEREx images these are the cores of
    stars too bright for the detector, which the archive blanks out in every exposure."""
    from scipy.ndimage import binary_fill_holes

    have = np.isfinite(image)
    return binary_fill_holes(have) & ~have


def png_bytes(rgba: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(np.flipud(rgba), "RGBA").save(buf, format="PNG")  # flip: North up
    return buf.getvalue()


def gray_rgba(a: np.ndarray, lo: float, hi: float, soft: float = 0.1) -> np.ndarray:
    z = np.clip((a - lo) / max(hi - lo, 1e-12), 0, 1)
    g = (np.nan_to_num(np.arcsinh(z / soft) / np.arcsinh(1 / soft)) * 255).astype(np.uint8)
    return np.dstack([g, g, g, np.where(np.isfinite(a), 255, 0).astype(np.uint8)])


def diverging_rgba(a: np.ndarray, lim: float) -> np.ndarray:
    """Signed difference image: red = brighter later (arrived / brightened), blue = brighter earlier (left / faded)."""
    z = np.clip(np.nan_to_num(a) / max(lim, 1e-12), -1, 1)[..., None]
    base, pos, neg = np.array([18, 22, 38.0]), np.array([255, 90, 70.0]), np.array([80, 150, 255.0])
    rgb = base + (pos - base) * np.clip(z, 0, 1) + (neg - base) * np.clip(-z, 0, 1)
    return np.dstack([rgb.astype(np.uint8), np.where(np.isfinite(a), 255, 0).astype(np.uint8)])


def gif_bytes(
    cube01: np.ndarray, labels: list[str], fps: int = 6, zoom: int = 8, watermark: str = ""
) -> bytes:
    """Shareable GIF (North up, East left, same orientation as the interactive movie)."""
    frames = []
    for k in range(len(cube01)):
        a = np.nan_to_num(cube01[k], nan=0.0)
        im = (
            Image.fromarray((np.flipud(a) * 255).astype(np.uint8))
            .resize((a.shape[1] * zoom, a.shape[0] * zoom), Image.NEAREST)
            .convert("RGB")
        )
        d = ImageDraw.Draw(im)
        d.text((6, 6), labels[k], fill=(255, 230, 120))
        if watermark:
            d.text((6, im.height - 14), watermark, fill=(255, 120, 120))
        frames.append(im)
    buf = io.BytesIO()
    frames[0].save(
        buf, format="GIF", save_all=True, append_images=frames[1:], duration=int(1000 / fps), loop=0
    )
    return buf.getvalue()
