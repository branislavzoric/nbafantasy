"""Render the "B3" logo in the chalk style of the site logo (pink B, green 3).

Output (brand/):
  - b3_logo_ball.png     B3 on the chalk basketball, transparent background
  - b3_logo_letters.png  just the B3 letters, transparent background
  - b3_avatar.png        B3 ball on the dark site background, for profile pictures
"""

import os
import subprocess

from PIL import Image

from build_player_cards import file_url, find_browser, make_dir

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
BRAND_DIR = os.path.join(PROJECT_DIR, "brand")
SIZE = 1000

PINK, GREEN, BALL, CHALK, DARK = "#ff6b9a", "#86e3a6", "#d97a2b", "#f2efe6", "#111111"

DEFS = """
<filter id="chalk" x="-10%" y="-10%" width="120%" height="120%">
  <feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="3" seed="7" result="warp"/>
  <feDisplacementMap in="SourceGraphic" in2="warp" scale="9" result="rough"/>
  <feTurbulence type="fractalNoise" baseFrequency="1.1" numOctaves="1" seed="3" result="grain"/>
  <feColorMatrix in="grain" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -1.8 1.55" result="speckle"/>
  <feComposite in="rough" in2="speckle" operator="in"/>
</filter>
<filter id="chalksolid" x="-10%" y="-10%" width="120%" height="120%">
  <feTurbulence type="fractalNoise" baseFrequency="0.035" numOctaves="3" seed="7" result="warp"/>
  <feDisplacementMap in="SourceGraphic" in2="warp" scale="9" result="rough"/>
  <feTurbulence type="fractalNoise" baseFrequency="1.1" numOctaves="1" seed="3" result="grain"/>
  <feColorMatrix in="grain" type="matrix" values="0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 -2.5 2.6" result="speckle"/>
  <feComposite in="rough" in2="speckle" operator="in"/>
</filter>
<filter id="ballgrain">
  <feTurbulence type="fractalNoise" baseFrequency="0.9" numOctaves="2" seed="11" result="g"/>
  <feColorMatrix in="g" type="matrix" values="0 0 0 0 0.45  0 0 0 0 0.2  0 0 0 0 0.05  0 0 0 -1.2 0.75" result="dots"/>
  <feComposite in="dots" in2="SourceGraphic" operator="in" result="dotsin"/>
  <feMerge><feMergeNode in="SourceGraphic"/><feMergeNode in="dotsin"/></feMerge>
</filter>
"""

BALL_SVG = f"""
<g filter="url(#chalk)" fill="none" stroke="{CHALK}" stroke-width="14" stroke-linecap="round">
  <circle cx="500" cy="500" r="462"/>
</g>
<circle cx="500" cy="500" r="405" fill="{BALL}" filter="url(#ballgrain)"/>
<g filter="url(#chalk)" fill="none" stroke="{CHALK}" stroke-width="13" stroke-linecap="round">
  <circle cx="500" cy="500" r="405"/>
  <path d="M500 95 V905"/>
  <path d="M95 500 H905"/>
  <path d="M215 215 C330 330 330 670 215 785"/>
  <path d="M785 215 C670 330 670 670 785 785"/>
</g>
"""

# Thick rounded strokes, like the hand-drawn B in the site logo.
LETTERS_SVG = f"""
<g filter="url(#chalksolid)" fill="none" stroke-linecap="round" stroke-linejoin="round">
  <g stroke="{DARK}" stroke-width="104" opacity=".55" transform="translate(6 8)">
    <path transform="translate(-20 0)" d="M270 265 V735"/>
    <path transform="translate(-20 0)" d="M270 265 H395 C520 265 520 490 395 490 H270 M270 490 H410 C550 490 550 735 410 735 H270"/>
    <path transform="translate(35 0)" d="M590 330 C615 255 760 245 760 375 C760 450 705 488 655 492 C725 492 775 540 775 615 C775 755 615 770 580 690"/>
  </g>
  <path transform="translate(-20 0)" d="M270 265 V735" stroke="{PINK}" stroke-width="86"/>
  <path transform="translate(-20 0)" d="M270 265 H395 C520 265 520 490 395 490 H270 M270 490 H410 C550 490 550 735 410 735 H270"
        stroke="{PINK}" stroke-width="86"/>
  <path transform="translate(35 0)" d="M590 330 C615 255 760 245 760 375 C760 450 705 488 655 492 C725 492 775 540 775 615 C775 755 615 770 580 690"
        stroke="{GREEN}" stroke-width="86"/>
</g>
"""


def page(body, bg="transparent"):
    return (f'<!doctype html><meta charset="utf-8"><style>html,body{{margin:0;background:{bg}}}</style>'
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" viewBox="0 0 1000 1000">'
            f'<defs>{DEFS}</defs>{body}</svg>')


def render(browser, html_text, name, work_dir):
    html_path = os.path.join(work_dir, name + ".html")
    png_path = os.path.join(BRAND_DIR, name + ".png")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_text)
    subprocess.run([browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                    f"--user-data-dir={make_dir(work_dir)}", "--default-background-color=00000000",
                    "--force-device-scale-factor=2", f"--screenshot={png_path}",
                    f"--window-size={SIZE},{SIZE}", file_url(html_path)],
                   check=True, capture_output=True, timeout=60)
    Image.open(png_path).resize((1024, 1024), Image.LANCZOS).save(png_path)
    print(png_path)


def main():
    browser = find_browser()
    work_dir = make_dir(os.path.join(PROJECT_DIR, "tmp_logo"))
    render(browser, page(BALL_SVG + LETTERS_SVG), "b3_logo_ball", work_dir)
    render(browser, page('<g transform="translate(-8 0)">' + LETTERS_SVG + "</g>"), "b3_logo_letters", work_dir)
    render(browser, page(f'<rect width="1000" height="1000" fill="{DARK}"/>'
                         '<g transform="translate(60 60) scale(.88)">' + BALL_SVG + LETTERS_SVG + "</g>",
                         DARK), "b3_avatar", work_dir)


if __name__ == "__main__":
    main()
