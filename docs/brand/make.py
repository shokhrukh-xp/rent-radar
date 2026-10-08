import cairosvg

SKIN, HAIR, INK = "#F3C6A0", "#2A1D1A", "#2A1D1A"

def person(top="#1E8C87", accent="#F6B53D"):
    # плечи, шея, голова, волосы с пучком, лицо
    return f'''
  <path d="M150 640 C150 520 230 470 320 470 C410 470 490 520 490 640 Z" fill="{top}"/>
  <path d="M290 420 L350 420 L350 485 C340 500 300 500 290 485 Z" fill="#E7B48B"/>
  <circle cx="320" cy="150" r="52" fill="{HAIR}"/>
  <ellipse cx="320" cy="292" rx="134" ry="140" fill="{HAIR}"/>
  <ellipse cx="320" cy="320" rx="112" ry="128" fill="{SKIN}"/>
  <path d="M208 300 C215 215 270 190 320 190 C380 190 430 215 432 300 C400 255 360 240 320 238 C290 240 245 250 208 300 Z" fill="{HAIR}"/>
  <path d="M262 318 Q280 300 298 318" stroke="{INK}" stroke-width="9" fill="none" stroke-linecap="round"/>
  <path d="M342 318 Q360 300 378 318" stroke="{INK}" stroke-width="9" fill="none" stroke-linecap="round"/>
  <ellipse cx="258" cy="355" rx="20" ry="12" fill="#EE8E86" opacity="0.55"/>
  <ellipse cx="382" cy="355" rx="20" ry="12" fill="#EE8E86" opacity="0.55"/>
  <path d="M292 378 Q320 402 348 378" stroke="#B8564D" stroke-width="9" fill="none" stroke-linecap="round"/>
  <circle cx="208" cy="345" r="9" fill="{accent}"/><circle cx="432" cy="345" r="9" fill="{accent}"/>'''

def svg(bg1, bg2, body, extra_back="", extra_front=""):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="640" height="640" viewBox="0 0 640 640">
  <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="{bg1}"/><stop offset="1" stop-color="{bg2}"/></linearGradient>
  <clipPath id="c"><circle cx="320" cy="320" r="320"/></clipPath></defs>
  <g clip-path="url(#c)"><rect width="640" height="640" fill="url(#g)"/>{extra_back}{body}{extra_front}</g></svg>'''

house = lambda x, y, s, fill, door: f'''<g transform="translate({x} {y}) scale({s})">
  <path d="M0 40 L50 0 L100 40 L100 100 L0 100 Z" fill="{fill}"/><rect x="38" y="58" width="24" height="42" rx="4" fill="{door}"/></g>'''

# A — кулон-домик на шее
A = svg("#FFE7C2", "#F7B98B", person("#1E8C87", "#F6B53D"),
        extra_front='<path d="M262 490 Q320 545 378 490" stroke="#F6B53D" stroke-width="6" fill="none"/>' + house(296, 528, 0.48, "#F6B53D", "#1E8C87"))
# B — героиня в проёме дома
B = svg("#1F7A86", "#0F4F5E", person("#F28C5A", "#FFD27A"),
        extra_back='<path d="M60 300 L320 70 L580 300 L580 640 L60 640 Z" fill="#FFF3E2"/><path d="M60 300 L320 70 L580 300" stroke="#FFD27A" stroke-width="26" fill="none" stroke-linejoin="round" stroke-linecap="round"/>')
# C — лупа с домиком: «ищет»
C = svg("#FFE7C2", "#F7B98B", person("#1E8C87", "#F6B53D"),
        extra_front='<line x1="500" y1="560" x2="548" y2="612" stroke="#2A1D1A" stroke-width="30" stroke-linecap="round"/>'
                    '<circle cx="452" cy="505" r="74" fill="#FFFFFF" stroke="#2A1D1A" stroke-width="20"/>' + house(416, 470, 0.72, "#1E8C87", "#F6B53D"))

for name, s in (("A", A), ("B", B), ("C", C)):
    open(f"rano_{name}.svg", "w").write(s)
    cairosvg.svg2png(bytestring=s.encode(), write_to=f"rano_{name}.png", output_width=640, output_height=640)
    cairosvg.svg2png(bytestring=s.encode(), write_to=f"rano_{name}_small.png", output_width=96, output_height=96)
