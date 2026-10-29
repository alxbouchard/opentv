#!/usr/bin/env python3
"""Outil de développement (jamais installé sur la Pi) : régénère les images du thème Plymouth OpenTV.

    python3 scripts/pi4/tools/make-plymouth-theme.py [--chromium <chrome-headless-shell>]

Rendu par Chromium sans interface (même typographie que l'interface TV) puis réencodage PNG déterministe en Python
pur (RVB 8 bits, sans alpha, sans entrelacement, filtres None/Sub/Up seulement, zlib 9) :

  system/plymouth/opentv.png       1920×1080 : fond sombre OpenTV v23 (photo « Côte brumeuse » de l'accueil,
                                   assombrie et adoucie, voile radial de l'écran de chargement), marque « OpenTV »
                                   centrée, petit « Démarrage… ». Aucun logo Raspberry Pi/Linux, aucun texte console.
  system/plymouth/opentv-mask.png  1920×MASK_H : la même bande SANS le libellé (posée par le script Plymouth à
                                   l'arrêt/redémarrage pour masquer « Démarrage… »).

Aucun réseau : la photo vient de native/app/backgrounds/misty-coast.jpg (lu, jamais modifié).
"""
import argparse
import glob
import os
import struct
import subprocess
import sys
import tempfile
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PI4 = os.path.dirname(HERE)
REPO = os.path.dirname(os.path.dirname(PI4))
OUT = os.path.join(PI4, "system", "plymouth")
PHOTO = os.path.join(REPO, "native", "app", "backgrounds", "misty-coast.jpg")
W, H = 1920, 1080
# bande du libellé « Démarrage… » (doit rester identique aux constantes de opentv.script)
MASK_Y, MASK_H = 618, 96

PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><style>
html,body{margin:0;width:1920px;height:1080px;overflow:hidden;background:#05070b}
#bg{position:absolute;inset:-40px;background:url("%(photo)s") center/cover no-repeat;filter:blur(14px) brightness(.62)}
#veil{position:absolute;inset:0;background:radial-gradient(ellipse 70%% 75%% at 50%% 46%%,rgba(26,39,66,.62) 0%%,
 rgba(7,11,20,.86) 62%%,rgba(5,7,11,.96) 100%%)}
.mark{position:absolute;left:0;width:1920px;top:448px;height:150px;line-height:150px;text-align:center;
 font:700 132px "Inter","Helvetica Neue","Noto Sans","DejaVu Sans",Arial,sans-serif;letter-spacing:.04em;color:#f4f6fb}
.mark b{color:#6ea8ff;font-weight:700}
.label{position:absolute;left:0;width:1920px;top:640px;height:52px;line-height:52px;text-align:center;
 font:400 34px "Inter","Helvetica Neue","Noto Sans","DejaVu Sans",Arial,sans-serif;letter-spacing:.02em;
 color:rgba(244,246,251,.66)}
</style></head><body><div id="bg"></div><div id="veil"></div><div class="mark">Open<b>TV</b></div>%(label)s
</body></html>"""


def find_chromium(explicit):
    cands = [explicit, os.environ.get("OPENTV_TEST_CHROMIUM")]
    for pat in ("~/Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-*/chrome-headless-shell",
                "~/.cache/ms-playwright/chromium_headless_shell-*/chrome-*/chrome-headless-shell"):
        cands += sorted(glob.glob(os.path.expanduser(pat)))[-1:]
    for c in cands:
        if c and os.path.exists(c):
            return c
    sys.exit("chrome-headless-shell introuvable (--chromium)")


def png_decode(data):
    """Décodeur PNG minimal (8 bits, RVB/RVBA, non entrelacé, tous filtres) -> (w, h, octets RVB)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif typ == b"IDAT":
            idat += body
        pos += 12 + n
    assert ihdr is not None, "IHDR absent"
    w, h, depth, ctype, _, _, inter = ihdr
    assert depth == 8 and ctype in (2, 6) and inter == 0, ihdr
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * bpp
    prev = bytearray(stride)
    out = bytearray()
    p = 0
    for _ in range(h):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        out += line if bpp == 3 else bytes(v for j, v in enumerate(line) if j % 4 != 3)
        prev = line
    return w, h, bytes(out)


def png_encode(w, h, rgb):
    """PNG RVB 8 bits déterministe ; par ligne, le filtre None/Sub/Up de plus petite somme absolue."""
    stride = w * 3
    prev = bytes(stride)
    rows = []
    for y in range(h):
        line = rgb[y * stride:(y + 1) * stride]
        sub = bytes((line[i] - (line[i - 3] if i >= 3 else 0)) & 255 for i in range(stride))
        up = bytes((line[i] - prev[i]) & 255 for i in range(stride))
        best = min(((0, line), (1, sub), (2, up)), key=lambda t: sum(v if v < 128 else 256 - v for v in t[1]))
        rows.append(bytes((best[0],)) + best[1])
        prev = line

    def chunk(t, b):
        return struct.pack(">I", len(b)) + t + b + struct.pack(">I", zlib.crc32(t + b) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def render(chrome, label):
    d = tempfile.mkdtemp(prefix="otvply")
    html = os.path.join(d, "p.html")
    with open(html, "w", encoding="utf-8") as f:
        f.write(PAGE % {"photo": "file://" + PHOTO,
                        "label": '<div class="label">Démarrage…</div>' if label else ""})
    shot = os.path.join(d, "s.png")
    subprocess.run([chrome, "--headless", "--no-first-run", "--disable-gpu", "--hide-scrollbars",
                    "--force-device-scale-factor=1", "--window-size=%d,%d" % (W, H), "--user-data-dir=" + d,
                    "--allow-file-access-from-files", "--virtual-time-budget=2000", "--screenshot=" + shot,
                    "file://" + html], check=True, capture_output=True, timeout=120)
    with open(shot, "rb") as f:
        w, h, rgb = png_decode(f.read())
    assert (w, h) == (W, H), (w, h)
    return rgb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chromium")
    a = ap.parse_args()
    chrome = find_chromium(a.chromium)
    full = render(chrome, True)
    plain = render(chrome, False)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "opentv.png"), "wb") as f:
        f.write(png_encode(W, H, full))
    band = plain[MASK_Y * W * 3:(MASK_Y + MASK_H) * W * 3]
    with open(os.path.join(OUT, "opentv-mask.png"), "wb") as f:
        f.write(png_encode(W, MASK_H, band))
    for n in ("opentv.png", "opentv-mask.png"):
        print(n, os.path.getsize(os.path.join(OUT, n)))


if __name__ == "__main__":
    main()
