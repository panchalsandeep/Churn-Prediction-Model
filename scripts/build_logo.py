"""Build the ChurnSight logo as clean vector SVG (mark + wordmark + tagline), with the text converted to paths.

Typefaces (both SIL Open Font License, instanced from the Google Fonts variable fonts):
  wordmark "ChurnSight"  Plus Jakarta Sans ExtraBold (800), widened 4% to the master's letter widths
  tagline                Montserrat SemiBold (600), tracked to the master's width
Geometry of the mark and the text sizes were measured from the 628x520 master PNG logo.

Usage: python scripts/build_logo.py <PlusJakartaSans[wght].ttf> <Montserrat[wght].ttf> <output dir/>
"""
import math
import sys

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

WORD_FONT, TAG_FONT, OUT = sys.argv[1], sys.argv[2], sys.argv[3]
NAVY, INDIGO, BLUE, BRIGHT, CYAN, TEAL, VIOLET = '#071426', '#10194B', '#176BFA', '#1688F8', '#09C9E8', '#13D9B4', '#6338F5'


def instance(path, weight):
    f = TTFont(path)
    instantiateVariableFont(f, {'wght': weight}, inplace=True)
    return f


BOLD, SEMIBOLD = instance(WORD_FONT, 800), instance(TAG_FONT, 600)


def _metrics(f, text):
    cmap, hmtx = f.getBestCmap(), f['hmtx']
    names = [cmap[ord(ch)] for ch in text]
    return names, sum(hmtx[n][0] for n in names), f['head'].unitsPerEm, f['OS/2'].sCapHeight


def text_path(f, text, x, baseline, cap_px=None, width_px=None, tracking=0.0, xscale=1.0):
    """Return (path_d, advance_px). Scale so caps are cap_px tall, or so the run is width_px wide.
    With both cap_px and width_px, tracking is solved so the run fills width_px exactly.
    xscale widens the glyphs horizontally (1.0 = the font's own proportions)."""
    gs, hmtx = f.getGlyphSet(), f['hmtx']
    names, adv, upm, cap = _metrics(f, text)
    if cap_px and width_px:
        s = cap_px / cap
        track_units = (width_px / (s * xscale) - adv) / (len(names) - 1)
    else:
        track_units = tracking * upm
        s = cap_px / cap if cap_px else width_px / ((adv + track_units * (len(names) - 1)) * xscale)
    pen = SVGPathPen(gs)
    cx = 0.0
    for n in names:
        gs[n].draw(TransformPen(pen, (s * xscale, 0, 0, -s, x + cx * s * xscale, baseline)))
        cx += hmtx[n][0] + track_units
    return pen.getCommands(), (cx - track_units) * s * xscale


def arc(cx, cy, r, a0, a1):
    """SVG arc from angle a0 to a1 (degrees, 0 = +x, positive = clockwise on screen), drawn clockwise (increasing angle)."""
    p0 = (cx + r * math.cos(math.radians(a0)), cy + r * math.sin(math.radians(a0)))
    p1 = (cx + r * math.cos(math.radians(a1)), cy + r * math.sin(math.radians(a1)))
    large = 1 if (a1 - a0) % 360 > 180 else 0
    return f'M{p0[0]:.1f} {p0[1]:.1f} A{r} {r} 0 {large} 1 {p1[0]:.1f} {p1[1]:.1f}'


# Mark geometry (measured on the 628x520 master).
C_CX, C_CY, C_R, C_W = 310.5, 192.5, 141, 45          # outer "C": round-capped arc
R_CX, R_CY = 313, 195                                 # inner ring: teal arc + short blue arc share this centre
mark = f'''
  <defs>
    <linearGradient id="cs-c" x1="170" y1="310" x2="410" y2="50" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{VIOLET}"/><stop offset="0.38" stop-color="#5A45F8"/><stop offset="0.66" stop-color="{BLUE}"/><stop offset="1" stop-color="{CYAN}"/>
    </linearGradient>
    <linearGradient id="cs-inner" x1="313" y1="105" x2="248" y2="262" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{TEAL}"/><stop offset="0.55" stop-color="{CYAN}"/><stop offset="1" stop-color="{BLUE}"/>
    </linearGradient>
    <linearGradient id="cs-dot" x1="358" y1="330" x2="398" y2="290" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{VIOLET}"/><stop offset="1" stop-color="{BLUE}"/>
    </linearGradient>
    <linearGradient id="cs-gem" x1="412" y1="308" x2="435" y2="285" gradientUnits="userSpaceOnUse">
      <stop offset="0" stop-color="{BRIGHT}"/><stop offset="1" stop-color="{CYAN}"/>
    </linearGradient>
  </defs>
  <g fill="none" stroke-linecap="round">
    <path d="{arc(C_CX, C_CY, C_R, 86, -64)}" stroke="url(#cs-c)" stroke-width="{C_W}"/>
    <path d="{arc(R_CX, R_CY, 90, 136, -90)}" stroke="url(#cs-inner)" stroke-width="20"/>
    <path d="{arc(R_CX, R_CY, 88, 45, 92)}" stroke="{BRIGHT}" stroke-width="21"/>
  </g>
  <circle cx="417.5" cy="116.5" r="36" fill="{CYAN}"/>
  <circle cx="378" cy="310" r="24.5" fill="url(#cs-dot)"/>
  <rect x="414" y="287" width="19" height="19" rx="3.5" transform="rotate(45 423.5 296.5)" fill="url(#cs-gem)"/>
'''
MARK_BOX = (144, 26, 456, 359)   # x0, y0, x1, y1 of the mark's ink

# Wordmark and tagline sized to the master: "ChurnSight" caps 78.5 px spanning x 22-596 on baseline 452;
# tagline caps 16 px spanning x 12-610 on baseline 501.
WORD_CAP, WORD_X0, WORD_W, WORD_XS = 78.5, 22, 574, 1.04
UPM = BOLD['head'].unitsPerEm
_, adv_word, _, cap_units = _metrics(BOLD, 'ChurnSight')
word_s = WORD_CAP / cap_units
word_track = (WORD_W / (word_s * WORD_XS) - adv_word) / (len('ChurnSight') - 1) / UPM   # em, solved to fill WORD_W


def wordmark(x, baseline, cap_px):
    """'Churn' and 'Sight' as two paths (two colours), spaced exactly as one run. Returns (churn_d, sight_d, sight_x, end_x)."""
    c_d, c_w = text_path(BOLD, 'Churn', x, baseline, cap_px=cap_px, tracking=word_track, xscale=WORD_XS)
    gap = word_track * UPM * (cap_px / cap_units) * WORD_XS
    s_d, s_w = text_path(BOLD, 'Sight', x + c_w + gap, baseline, cap_px=cap_px, tracking=word_track, xscale=WORD_XS)
    return c_d, s_d, x + c_w + gap, x + c_w + gap + s_w


churn_d, sight_d, sight_x, word_end = wordmark(WORD_X0, 452, WORD_CAP)
tag_d, tag_w = text_path(SEMIBOLD, 'AI-POWERED CUSTOMER INTELLIGENCE.', 12, 501, cap_px=16, width_px=598)

VBW, VBH = 628, 520
full = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {VBW} {VBH}" width="{VBW}" height="{VBH}" role="img" aria-labelledby="cs-title">
  <title id="cs-title">ChurnSight: AI-powered customer intelligence</title>{mark}
  <linearGradient id="cs-sight" x1="{sight_x:.0f}" y1="0" x2="596" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="{BRIGHT}"/></linearGradient>
  <path d="{churn_d}" fill="{INDIGO}"/><path d="{sight_d}" fill="url(#cs-sight)"/>
  <path d="{tag_d}" fill="{INDIGO}"/>
</svg>
'''
mx0, my0, mx1, my1 = MARK_BOX
mark_only = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="{mx0 - 4} {my0 - 4} {mx1 - mx0 + 8} {my1 - my0 + 8}" width="{mx1 - mx0 + 8}" height="{my1 - my0 + 8}" role="img" aria-label="ChurnSight">{mark}</svg>
'''

# Horizontal lockup for headers: mark scaled to the lockup height, wordmark to its right.
HH = 150
s = (HH - 12) / (my1 - my0)
mark_w = (mx1 - mx0) * s
hx_word = 6 + mark_w + 22
h_cap = 56
h_churn_d, h_sight_d, h_sight_x, h_end = wordmark(hx_word, 103, h_cap)
HW = h_end + 6
hmark = mark.replace('id="cs-', 'id="h-').replace('url(#cs-', 'url(#h-')
horizontal = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {HW:.0f} {HH}" width="{HW:.0f}" height="{HH}" role="img" aria-label="ChurnSight">
  <g transform="translate({6 - mx0 * s:.2f} {6 - my0 * s:.2f}) scale({s:.4f})">{hmark}</g>
  <defs><linearGradient id="h-sight-h" x1="{h_sight_x:.0f}" y1="0" x2="{HW:.0f}" y2="0" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{BLUE}"/><stop offset="1" stop-color="{BRIGHT}"/></linearGradient></defs>
  <path d="{h_churn_d}" fill="{INDIGO}"/><path d="{h_sight_d}" fill="url(#h-sight-h)"/>
</svg>
'''
reverse = horizontal.replace(f'fill="{INDIGO}"', 'fill="#FFFFFF"')
for name, svg in [('churnsight-logo.svg', full), ('churnsight-mark.svg', mark_only), ('churnsight-logo-horizontal.svg', horizontal),
                  ('churnsight-logo-horizontal-reverse.svg', reverse)]:
    with open(OUT + name, 'w') as fh:
        fh.write(svg)
print('ok word_track=%.3fem word_w=%.0f tag_w=%.0f horizontal=%dx%d' % (word_track, word_end - WORD_X0, tag_w, round(HW), HH))
