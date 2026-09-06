"""Minimal geohash (base32) encoder/decoder. Length 7 ≈ 153 m × 153 m cells — the analytics resolution."""
_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DEC = {c: i for i, c in enumerate(_B32)}


def encode(lat: float, lon: float, length: int = 7) -> str:
    lat_i, lon_i = (-90.0, 90.0), (-180.0, 180.0)
    out, bit, ch, even = [], 0, 0, True
    while len(out) < length:
        if even:
            mid = (lon_i[0] + lon_i[1]) / 2
            if lon >= mid:
                ch |= 1 << (4 - bit)
                lon_i = (mid, lon_i[1])
            else:
                lon_i = (lon_i[0], mid)
        else:
            mid = (lat_i[0] + lat_i[1]) / 2
            if lat >= mid:
                ch |= 1 << (4 - bit)
                lat_i = (mid, lat_i[1])
            else:
                lat_i = (lat_i[0], mid)
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_B32[ch])
            bit, ch = 0, 0
    return "".join(out)


def bounds(gh: str) -> tuple[float, float, float, float]:
    """(min_lat, min_lon, max_lat, max_lon) of a cell. Raises ValueError on a bad hash."""
    lat_i, lon_i = [-90.0, 90.0], [-180.0, 180.0]
    even = True
    for c in gh:
        cd = _DEC[c]
        for mask in (16, 8, 4, 2, 1):
            if even:
                mid = (lon_i[0] + lon_i[1]) / 2
                lon_i = [mid, lon_i[1]] if cd & mask else [lon_i[0], mid]
            else:
                mid = (lat_i[0] + lat_i[1]) / 2
                lat_i = [mid, lat_i[1]] if cd & mask else [lat_i[0], mid]
            even = not even
    return lat_i[0], lon_i[0], lat_i[1], lon_i[1]


def center(gh: str) -> tuple[float, float]:
    a, b, c, d = bounds(gh)
    return round((a + c) / 2, 6), round((b + d) / 2, 6)


def polygon(gh: str) -> list[list[float]]:
    """GeoJSON ring ([lon, lat] pairs, closed) of the cell."""
    a, b, c, d = bounds(gh)
    return [[b, a], [d, a], [d, c], [b, c], [b, a]]
