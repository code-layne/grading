"""Image work for ingest: render, threshold, find the registration marks and
the page QR code, align a scan to its manifest, crop.

Two coordinate systems:
  manifest  PDF points (bp) from the lower-left corner of the page, as in
            manifest.json: a box is [x, y, w, h].
  canonical the page as a top-down pixel grid at the ingest dpi, the same
            page grading.sty laid out. A scan is warped onto it, so every
            crop is cut square to the page whatever the copier did.
"""

import re
from dataclasses import dataclass

import cv2
import numpy as np
import pymupdf
import zxingcpp

# grading.sty's default page furniture, in manifest coordinates. Used only for
# a page whose QR code can't be read: its manifest is unknown, and the strip
# still has to be blanked before the page is stored.
DEFAULT_LAYOUT = {
    "paper": [612, 792],
    "strip": [54, 756, 504, 26],
    "marks": {"tl": [21, 764, 12, 12], "tr": [579, 764, 12, 12],
              "bl": [21, 60, 12, 12], "br": [579, 60, 12, 12]},
    "qr": [566, 720, 32, 32],
}
CORNERS = ("tl", "tr", "bl", "br")
BOX_RULE_PT = 0.6       # \gradingboxrule
STRIP_BLEED_PT = 4.0    # blank this much beyond the strip on every side

QR_RE = re.compile(r"^(?P<assignment>[^|\s]+)\|(?P<page>[1-9][0-9]*)\|(?P<tid>[A-Z0-9]{6}|MASTER)$")
# A cover sheet, COVER|assignment|tracking id: the only sheet with the
# student's name. Same QR position and size as a graded page, no registration
# marks. Any code starting COVER| counts, so an odd tail (a MASTER cover)
# can't send a page with a name to the unplaced queue, stored.
COVER_RE = re.compile(r"^COVER\|")


class CoverPage(Exception):
    """A cover sheet went through the scanner. It carries a name, so nothing
    about it is kept: not the image, not the decoded text."""


class PlacementError(Exception):
    """The page can't be aligned; the reason goes to the unplaced queue."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail


@dataclass
class Code:
    text: str
    assignment: str
    page: int
    tracking_id: str
    center: tuple[float, float]  # scan pixels
    orientation: int


# ── Rendering and thresholds ──────────────────────────────────────────────────

def page_count(pdf_path) -> int:
    with pymupdf.open(pdf_path) as doc:
        return doc.page_count


def render_pages(pdf_path, dpi: int):
    """Yield each page of a scanned PDF as a grayscale array at dpi."""
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            pm = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY, alpha=False)
            yield np.frombuffer(pm.samples, np.uint8).reshape(pm.height, pm.stride)[:, :pm.width].copy()


def ink_mask(gray: np.ndarray, threshold: int) -> np.ndarray:
    return gray < threshold


def clean(gray: np.ndarray, threshold: int) -> np.ndarray:
    """Every pixel that isn't ink becomes white, so show-through, paper tone
    and faint scanner noise can't read as writing. Ink keeps its gray level,
    which keeps handwriting legible for the model."""
    out = gray.copy()
    out[out >= threshold] = 255
    return out


def binarized(gray: np.ndarray, threshold: int) -> np.ndarray:
    return np.where(gray < threshold, 0, 255).astype(np.uint8)


def ink_pixels(gray: np.ndarray, threshold: int, speck_px: int) -> int:
    """Ink pixels in gray, not counting components smaller than speck_px."""
    mask = ink_mask(gray, threshold).astype(np.uint8)
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    return int(areas[areas >= speck_px].sum())


# ── QR codes ─────────────────────────────────────────────────────────────────

def decode_codes(gray: np.ndarray, threshold: int) -> list[Code]:
    """Every grading QR code on the page, read from the thresholded image.

    Thresholding first removes show-through: a mirrored ghost of the other
    side's QR code never reaches the ink threshold, so it can't be decoded.
    Which code belongs to the page is still decided by position (place_page).

    A cover sheet's code anywhere on the page raises CoverPage, whatever else
    is on it: a page that shows a cover could show its name.
    """
    codes = []
    for b in zxingcpp.read_barcodes(binarized(gray, threshold), formats=zxingcpp.BarcodeFormat.QRCode,
                                    try_invert=False):
        if COVER_RE.match(b.text.strip()):
            raise CoverPage()
        m = QR_RE.match(b.text.strip())
        if not m:
            continue
        p = b.position
        pts = [(p.top_left.x, p.top_left.y), (p.top_right.x, p.top_right.y),
               (p.bottom_right.x, p.bottom_right.y), (p.bottom_left.x, p.bottom_left.y)]
        cx, cy = (sum(c[0] for c in pts) / 4, sum(c[1] for c in pts) / 4)
        codes.append(Code(b.text.strip(), m["assignment"], int(m["page"]), m["tid"],
                          (cx, cy), int(round(b.orientation / 90.0)) * 90 % 360))
    return codes


# ── Registration marks and the transform ─────────────────────────────────────

def _pt_to_canonical(x_pt, y_pt, paper, s):
    return x_pt * s, (paper[1] - y_pt) * s


def mark_centers_canonical(layout, s):
    paper = layout["paper"]
    out = {}
    for c in CORNERS:
        x, y, w, h = layout["marks"][c]
        out[c] = _pt_to_canonical(x + w / 2, y + h / 2, paper, s)
    return out


def find_marks(gray: np.ndarray, layout: dict, dpi: int, threshold: int) -> dict:
    """The centroid, in scan pixels, of each solid square registration mark.

    Each corner takes the solid, square ink component of about the mark's
    size nearest to where that mark sits, measured from the same corner of
    the scan (so a trimmed or offset scan still works). QR finder patterns
    are rings, not solid, and text is too small.
    """
    s = dpi / 72.0
    H, W = gray.shape
    paper = layout["paper"]
    mask = ink_mask(gray, threshold).astype(np.uint8)
    n, _, stats, cents = cv2.connectedComponentsWithStats(mask, connectivity=8)
    side = layout["marks"]["tl"][2] * s
    cands = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if not (0.7 * side <= w <= 1.4 * side and 0.7 * side <= h <= 1.4 * side):
            continue
        if area < 0.85 * w * h:
            continue
        cands.append(tuple(cents[i]))
    found = {}
    expected = mark_centers_canonical(layout, s)
    for c in CORNERS:
        ex, ey = expected[c]
        # Measure from the scan's own corner on that side.
        if c[1] == "r":
            ex = W - (paper[0] * s - ex)
        if c[0] == "b":
            ey = H - (paper[1] * s - ey)
        best = min(cands, key=lambda p: (p[0] - ex) ** 2 + (p[1] - ey) ** 2, default=None)
        if best is None or (best[0] - ex) ** 2 + (best[1] - ey) ** 2 > (1.0 * dpi) ** 2:
            raise PlacementError("marks-not-found", f"no registration mark near the {c} corner")
        found[c] = best
    if len(set(found.values())) < 4:
        raise PlacementError("marks-not-found", "two corners matched the same mark")
    return found


def fit_affine(src: dict, dst: dict) -> tuple[np.ndarray, float]:
    """Least-squares affine map src -> dst over the four corners, and its RMS
    residual in pixels. Four points over-determine an affine map, so the
    residual catches a misdetected mark."""
    A = np.array([[*src[c], 1.0] for c in CORNERS])
    B = np.array([dst[c] for c in CORNERS])
    M, *_ = np.linalg.lstsq(A, B, rcond=None)
    resid = A @ M - B
    return M.T, float(np.sqrt((resid ** 2).sum(axis=1).mean()))


def apply(M: np.ndarray, x: float, y: float) -> tuple[float, float]:
    return (M[0, 0] * x + M[0, 1] * y + M[0, 2], M[1, 0] * x + M[1, 1] * y + M[1, 2])


def warp_to_canonical(gray: np.ndarray, M: np.ndarray, layout: dict, dpi: int) -> np.ndarray:
    s = dpi / 72.0
    size = (int(round(layout["paper"][0] * s)), int(round(layout["paper"][1] * s)))
    return cv2.warpAffine(gray, M, size, flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=255)


def box_px(box, layout, dpi, margin_pt=0.0):
    """A manifest box as canonical pixel bounds (x0, y0, x1, y1), grown by margin_pt."""
    s = dpi / 72.0
    x, y, w, h = box
    paper_h = layout["paper"][1]
    x0, x1 = (x - margin_pt) * s, (x + w + margin_pt) * s
    y0, y1 = (paper_h - y - h - margin_pt) * s, (paper_h - y + margin_pt) * s
    return (max(int(np.floor(x0)), 0), max(int(np.floor(y0)), 0), int(np.ceil(x1)), int(np.ceil(y1)))


def blank_strip(canonical: np.ndarray, layout: dict, dpi: int) -> None:
    x0, y0, x1, y1 = box_px(layout["strip"], layout, dpi, STRIP_BLEED_PT)
    canonical[y0:y1, x0:x1] = 255


# ── One page ─────────────────────────────────────────────────────────────────

@dataclass
class Placement:
    code: Code
    transform: np.ndarray       # canonical px -> scan px
    residual_px: float
    canonical: np.ndarray       # aligned page, name strip blanked
    rejected_codes: list[Code]  # decoded codes not at the page's QR position


def align(gray, layout, dpi, threshold, max_residual):
    s = dpi / 72.0
    marks = find_marks(gray, layout, dpi, threshold)
    M, resid = fit_affine(mark_centers_canonical(layout, s), marks)
    if resid > max_residual:
        raise PlacementError("marks-inconsistent", f"registration marks fit to {resid:.1f}px RMS")
    return M, resid


def place_page(gray: np.ndarray, manifests, settings) -> Placement:
    """Read the page's QR code, align the page to its manifest and blank the
    name strip. Raises PlacementError for anything that belongs in the
    unplaced queue, and CoverPage for a cover sheet."""
    dpi, thr = settings.dpi, settings.ink_threshold
    s = dpi / 72.0
    codes = decode_codes(gray, thr)
    if not codes:
        raise PlacementError("qr-unreadable", "no grading QR code could be read")
    if codes[0].orientation == 180:  # fed upside down
        gray = cv2.rotate(gray, cv2.ROTATE_180)
        codes = decode_codes(gray, thr)
    elif codes[0].orientation:
        raise PlacementError("rotated", f"page is turned {codes[0].orientation} degrees")

    unknown = []
    for code in codes:
        manifest = manifests.get(code.assignment)
        if manifest is None:
            unknown.append(code.assignment)
            continue
        layout = manifest.data
        M, resid = align(gray, layout, dpi, thr, settings.max_mark_residual_px)
        qx, qy, qw, qh = layout["qr"]
        ex, ey = apply(M, *_pt_to_canonical(qx + qw / 2, qy + qh / 2, layout["paper"], s))
        # The page's own code is the one printed where the manifest puts it.
        # Anything else (a ghost through the paper, a stray sheet) is not.
        if (code.center[0] - ex) ** 2 + (code.center[1] - ey) ** 2 > (qw * s / 2) ** 2:
            continue
        if code.tracking_id == "MASTER":
            raise PlacementError("master-copy", "an unpersonalized (MASTER) copy has no student")
        if code.page > manifest.pages:
            raise PlacementError("bad-page", f"{code.text}: the manifest has {manifest.pages} pages")
        canonical = warp_to_canonical(gray, M, layout, dpi)
        blank_strip(canonical, layout, dpi)
        return Placement(code, M, resid, canonical, [c for c in codes if c is not code])
    if unknown and len(unknown) == len(codes):
        raise PlacementError("unknown-assignment", f"no manifest for {', '.join(sorted(set(unknown)))}")
    raise PlacementError("qr-misplaced", "no QR code where the manifest puts the page's code")


def unplaced_image(gray: np.ndarray, settings) -> np.ndarray:
    """A page that couldn't be placed, made safe to store: aligned with the
    default layout and strip-blanked when its marks can be found, otherwise
    the scan as is with the whole top band (where the strip prints) blanked."""
    dpi, thr = settings.dpi, settings.ink_threshold
    try:
        M, _ = align(gray, DEFAULT_LAYOUT, dpi, thr, settings.max_mark_residual_px)
        out = warp_to_canonical(gray, M, DEFAULT_LAYOUT, dpi)
        blank_strip(out, DEFAULT_LAYOUT, dpi)
    except PlacementError:
        out = gray.copy()
        out[: int(round(48 * dpi / 72.0)), :] = 255
    return clean(out, thr)


BORDER_SEARCH_PT = 4.0  # how far each printed box edge may sit from the transform's estimate
BORDER_MIN_INK = 0.6    # fraction of an edge's length that must be ink to count as the border


def find_border(canonical: np.ndarray, box, layout, dpi, threshold):
    """The printed border of a box, (x0, y0, x1, y1) in canonical pixels.

    The four registration marks fix the page as a whole, but paper feed in
    printers and scanners is not perfectly even, so a box between the marks
    can sit a point or two from where the affine transform puts it (1.7pt
    on the Brother test scan). Each edge is looked for independently, within
    BORDER_SEARCH_PT of its estimate: the row (or column) where the edge's
    span is most completely ink. An edge that isn't found (covered by
    writing, say) stays at its estimate. Show-through can't be mistaken for
    the border: it never reaches the ink threshold."""
    x0, y0, x1, y1 = box_px(box, layout, dpi)
    x1, y1 = x1 - 1, y1 - 1  # last pixel of the border, inclusive
    r = int(round(BORDER_SEARCH_PT * dpi / 72.0))
    ink = canonical < threshold
    # Fraction of ink along each row over the box's horizontal span, and along
    # each column over its vertical span, both kept clear of the corners.
    rows = ink[:, x0 + r:max(x1 - r, x0 + r + 1)].mean(axis=1)
    cols = ink[y0 + r:max(y1 - r, y0 + r + 1), :].mean(axis=0)

    def best(profile, estimate, outward):
        lo, hi = max(estimate - r, 0), min(estimate + r, len(profile) - 1)
        if hi < lo:
            return estimate
        # Most ink wins; on a tie, the line nearest the estimate.
        i = max(range(lo, hi + 1), key=lambda k: (profile[k], -abs(k - estimate)))
        if profile[i] < BORDER_MIN_INK:
            return estimate
        # The rule is a few pixels thick; boxes are measured to its outside.
        while 0 <= i + outward < len(profile) and profile[i + outward] >= BORDER_MIN_INK:
            i += outward
        return i

    left, top = best(cols, x0, -1), best(rows, y0, -1)
    right, bottom = best(cols, x1, +1), best(rows, y1, +1)
    return left, top, right + 1, bottom + 1


def crop_box(canonical: np.ndarray, box, layout, settings):
    """(crop, ink, border): the cleaned crop with margin, the ink inside the
    box (inside the border, so the printed rule never counts), and the
    border found on the page, (x0, y0, x1, y1) canonical pixels."""
    dpi, thr = settings.dpi, settings.ink_threshold
    s = dpi / 72.0
    bx0, by0, bx1, by1 = find_border(canonical, box, layout, dpi, thr)
    m = int(round(settings.crop_margin_pt * s))
    H, W = canonical.shape
    crop = clean(canonical[max(by0 - m, 0):min(by1 + m, H), max(bx0 - m, 0):min(bx1 + m, W)], thr)
    inset = int(np.ceil((BOX_RULE_PT + 3.0) * s))
    inner = canonical[by0 + inset:by1 - inset, bx0 + inset:bx1 - inset]
    return crop, ink_pixels(inner, thr, settings.speck_px), (bx0, by0, bx1, by1)
