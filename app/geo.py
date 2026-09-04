"""Small geometry helpers: haversine, Douglas-Peucker, Google polyline codec."""
import math


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def rdp(points: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Iterative Douglas-Peucker. points = [(lon, lat), ...] in degrees."""
    n = len(points)
    if n < 3:
        return points
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        x1, y1 = points[i]
        x2, y2 = points[j]
        dx, dy = x2 - x1, y2 - y1
        den = math.hypot(dx, dy)
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            x0, y0 = points[k]
            d = abs(dy * x0 - dx * y0 + x2 * y1 - y2 * x1) / den if den > 0 else math.hypot(x0 - x1, y0 - y1)
            if d > best:
                best, bi = d, k
        if best > eps:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(points, keep, strict=False) if k]


def _enc(v: int) -> str:
    v = ~(v << 1) if v < 0 else (v << 1)
    out = ""
    while v >= 0x20:
        out += chr((0x20 | (v & 0x1F)) + 63)
        v >>= 5
    return out + chr(v + 63)


def encode_polyline(points: list[tuple[float, float]]) -> str:
    """points = [(lon, lat), ...] -> encoded polyline, precision 1e-5."""
    out, plat, plon = "", 0, 0
    for lon, lat in points:
        la, lo = round(lat * 1e5), round(lon * 1e5)
        out += _enc(la - plat) + _enc(lo - plon)
        plat, plon = la, lo
    return out


def decode_polyline(s: str) -> list[tuple[float, float]]:
    """encoded polyline -> [(lon, lat), ...]"""
    out, i, lat, lon = [], 0, 0, 0
    while i < len(s):
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(s[i]) - 63
                i += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if result & 1 else (result >> 1)
            if which == 0:
                lat += d
            else:
                lon += d
        out.append((lon / 1e5, lat / 1e5))
    return out
