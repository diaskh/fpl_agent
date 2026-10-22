"""Shared sports editorial styling for all Pillow graphics.

Fonts are bundled (SIL Open Font License), so Linux and macOS render alike.
"""
from functools import lru_cache
from pathlib import Path

PAPER = '#F4F1EA'
INK = '#1A1A1A'
MUTED = '#6B6D69'
QUIET = '#8B8C89'
ACCENT = '#E63946'
RULE = '#D5D2CB'
CARD = '#EAE7E0'
FONTS = Path(__file__).resolve().parent / 'assets' / 'fonts'


@lru_cache(maxsize=128)
def font(size, bold=False, display=False):
    from PIL import ImageFont
    face = ImageFont.truetype(str(FONTS / ('Oswald.ttf' if display else 'Inter.ttf')), size)
    # Set axes explicitly; the default variable-font instance is regular.
    axes = face.get_variation_axes()
    face.set_variation_by_axes([
        (700 if display or bold else 400) if axis['name'] == b'Weight' else axis['default']
        for axis in axes
    ])
    return face


def fit_text(draw, value, size, max_width, bold=False, display=False, minimum=16):
    """Fit by measured width, then ellipsize without changing the source data."""
    value = str(value)
    face = font(size, bold, display)
    while draw.textlength(value, font=face) > max_width and size > minimum:
        size -= 1
        face = font(size, bold, display)
    if draw.textlength(value, font=face) > max_width:
        while value and draw.textlength(value + '…', font=face) > max_width:
            value = value[:-1]
        value += '…'
    return value, face


def tracked_text(draw, xy, value, face, fill=MUTED, spacing=2):
    """Real pixel tracking with kerning-aware advances; xy is the visible top left."""
    x, y = xy
    value = str(value)
    for i, char in enumerate(value):
        draw.text((x, y), char, font=face, fill=fill, anchor='lt')
        advance = draw.textlength(char, font=face)
        if i + 1 < len(value):
            advance = (draw.textlength(value[i:i+2], font=face)
                       - draw.textlength(value[i+1], font=face))
        x += advance + spacing


def headline(draw, xy, value, size=82, max_width=1460):
    value, face = fit_text(draw, value.upper(), size, max_width, display=True)
    draw.text(xy, value, font=face, fill=INK, anchor='lt')
