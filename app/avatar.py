"""Generate a deterministic initials avatar as an SVG data-URI.

Stored in users.avatar_url at register time so the client can always render
<img src={avatar_url}>. Replaced by a real URL when the user uploads a photo.
"""
import base64
import hashlib

_PALETTE = [
    "#2563eb", "#7c3aed", "#db2777", "#dc2626", "#ea580c",
    "#16a34a", "#0891b2", "#4f46e5", "#9333ea", "#0d9488",
]


def _initials(name: str) -> str:
    parts = [p for p in name.strip().split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][0].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def initials_avatar_data_uri(name: str, size: int = 128) -> str:
    initials = _initials(name)
    idx = int(hashlib.sha256(name.encode()).hexdigest(), 16) % len(_PALETTE)
    bg = _PALETTE[idx]
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" '
        f'viewBox="0 0 {size} {size}">'
        f'<rect width="{size}" height="{size}" rx="{size // 8}" fill="{bg}"/>'
        f'<text x="50%" y="50%" dy=".08em" text-anchor="middle" '
        f'font-family="system-ui,-apple-system,Segoe UI,Roboto,sans-serif" '
        f'font-size="{int(size * 0.42)}" font-weight="600" fill="#ffffff">{initials}</text>'
        f'</svg>'
    )
    b64 = base64.b64encode(svg.encode()).decode()
    return f"data:image/svg+xml;base64,{b64}"
