"""Build the ChurnSight logo as clean vector SVG (mark + wordmark + tagline), text converted to paths (Poppins)."""
import math, sys
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

OUT = sys.argv[1]
FONTS = '/usr/share/fonts/truetype/google-fonts/'
NAVY, INDIGO, BLUE, BRIGHT, CYAN, TEAL, VIOLET = '#071426', '#10194B', '#176BFA', '#1688F8', '#09C9E8', '#13D9B4', '#6338F5'


def text_path(font_file, text, x, baseline, cap_px=None, width_px=None, tracking=0.0):
    """Return (path_d, advance_px). Scale so caps are cap_px tall, or so the run is width_px wide."""
    f = TTFont(FONTS + font_file)
    gs, cmap, hmtx = f.getGlyphSet(), f.getBestCmap(), f['hmtx']
    upm = f['head'].unitsPerEm
    cap = getattr(f['OS/2'], 'sCapHeight', 0) or upm * 0.7
    names = [cmap[ord(ch)] for ch in text]
    adv_units = sum(hmtx[n][0] for n in names)
    track_units = tracking * upm
    total_units = adv_units + track_units * (len(names) - 1)
    s = cap_px / cap if cap_px else width_px / total_units
    pen = SVGPathPen(gs)
    cx = 0.0
    for n in names:
        tp = TransformPen(pen, (s, 0, 0, -s, x + cx * s, baseline))
        gs[n].draw(tp)
        cx += hmtx[n][0] + track_units
    return pen.getCommands(), (cx - track_units) * s


def arc(cx, cy, r, a0, a1, sweep=0):
    """SVG arc path from angle a0 to a1 (degrees, 0 = +x, clockwise positive in screen coords)."""
    p0 = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
    p1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
    span = (a0 - a1) % 360 if sweep == 0 else (a1 - a0) % 360
    large = 1 if span > 180 else 0
    return f'M{p0[0]:.1f} {p0[1]:.1f} A{r} {r} 0 {large} {sweep} {p1[0]:.1f} {p1[1]:.1f}'


CX, CY = 304, 189
mark = f'''
  <defs>
    <linearGradient id="cs-c" x1="170" y1="310" x2="410" y2="50" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{VIOLET}"/><stop offset="0.38" stop-color="#5A45F8"/><stop offset="0.66" stop-color="{BLUE}"/><stop offset="1" stop-color="{CYAN}"/>
    </linearGradient>
    <linearGradient id="cs-inner" x1="300" y1="100" x2="240" y2="260" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{TEAL}"/><stop offset="0.55" stop-color="{CYAN}"/><stop offset="1" stop-color="{BLUE}"/>
    </linearGradient>
    <linearGradient id="cs-dot" x1="358" y1="334" x2="402" y2="290" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{VIOLET}"/><stop offset="1" stop-color="{BLUE}"/>
    </linearGradient>
    <linearGradient id="cs-sight" x1="330" y1="0" x2="600" y2="0" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="{BRIGHT}"/>
    </linearGradient>
  </defs>
  <g fill="none" stroke-linecap="round">
    <path d="{arc(CX, CY, 137, -59, 79)}" stroke="url(#cs-c)" stroke-width="50"/>
    <path d="{arc(CX, CY, 82, -81, 128)}" stroke="url(#cs-inner)" stroke-width="22"/>
    <path d="{arc(CX, CY, 98, 84, 44, sweep=0)}" stroke="{BLUE}" stroke-width="22"/>
  </g>
  <circle cx="420" cy="120" r="34" fill="{CYAN}"/>
  <circle cx="380" cy="312" r="23" fill="url(#cs-dot)"/>
  <rect x="414" y="287" width="18" height="18" rx="3" transform="rotate(45 423 296)" fill="{CYAN}"/>
'''

churn_d, churn_w = text_path('Poppins-Bold.ttf', 'Churn', 22, 452, cap_px=80*574/669)
sight_d, sight_w = text_path('Poppins-Bold.ttf', 'Sight', 22 + churn_w + 2, 452, cap_px=80*574/669)
word_w = churn_w + 2 + sight_w
tag_d, tag_w = text_path('Poppins-Medium.ttf', 'AI-POWERED CUSTOMER INTELLIGENCE.', 0, 501, cap_px=16, tracking=0.20)

# Centre wordmark and tagline under the mark (mark centre x ~ 300).
VBW = 628
wx = (VBW - word_w) / 2 - 22
tx = (VBW - tag_w) / 2

full = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VBW} 520" width="{VBW}" height="520" role="img" aria-labelledby="cs-title">
  <title id="cs-title">ChurnSight: AI-powered customer intelligence</title>{mark}
  <g transform="translate({wx:.1f} 0)"><path d="{churn_d}" fill="{INDIGO}"/><path d="{sight_d}" fill="url(#cs-sight)"/></g>
  <path transform="translate({tx:.1f} 0)" d="{tag_d}" fill="{INDIGO}"/>
</svg>
'''
mark_only = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="130 15 340 350" width="340" height="350" role="img" aria-label="ChurnSight">{mark}</svg>
'''
# Horizontal lockup for headers: mark scaled next to the wordmark.
s = 0.42
hx_word = 340 * s + 24
h_word_d, h_word_w = text_path('Poppins-Bold.ttf', 'Churn', hx_word, 108, cap_px=56)
h_sight_d, h_sight_w = text_path('Poppins-Bold.ttf', 'Sight', hx_word + h_word_w + 2, 108, cap_px=56)
HW = hx_word + h_word_w + 2 + h_sight_w + 6
hmark = mark.replace('id="cs-', 'id="h-').replace('url(#cs-', 'url(#h-')
horizontal = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {HW:.0f} 150" width="{HW:.0f}" height="150" role="img" aria-label="ChurnSight">
  <g transform="translate({-130*s:.1f} {-15*s+2:.1f}) scale({s})">{hmark}</g>
  <defs><linearGradient id="h-sight-h" x1="{hx_word + h_word_w:.0f}" y1="0" x2="{HW:.0f}" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="{BRIGHT}"/></linearGradient></defs>
  <path d="{h_word_d}" fill="{INDIGO}"/><path d="{h_sight_d}" fill="url(#h-sight-h)"/>
</svg>
'''
reverse = horizontal.replace(f'fill="{INDIGO}"', 'fill="#FFFFFF"')
for name, svg in [('churnsight-logo.svg', full), ('churnsight-mark.svg', mark_only), ('churnsight-logo-horizontal.svg', horizontal),
                  ('churnsight-logo-horizontal-reverse.svg', reverse)]:
    open(OUT + name, 'w').write(svg)
print('ok', round(word_w), round(tag_w), round(HW))
