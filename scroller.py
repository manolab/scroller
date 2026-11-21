#!/usr/bin/env python3
# /// script
# requires-python = ">=3.9"
# dependencies = ["hidapi", "pillow"]
# ///
"""Scroller — gestione del display LED CH583 (USB 0416:5020) da terminale.

Funziona su macOS, Linux e Windows. Se mancano i pacchetti Python necessari
(hidapi, pillow) li installa da solo in un ambiente dedicato.

  scroller.py                    interfaccia interattiva
  scroller.py invia              invia al display i messaggi salvati
  scroller.py anteprima          stampa l'anteprima dei messaggi nel ciclo
  scroller.py imposta N TESTO    imposta il messaggio N (1-8) e lo mette nel ciclo
  scroller.py controlla          verifica pacchetti, display e permessi USB
  scroller.py simboli            mostra i simboli da usare nei messaggi con :nome:

Variabili d'ambiente: SCROLLER_NO_INSTALL=1 disattiva l'installazione automatica.
"""
from __future__ import annotations

import argparse
import codecs
import copy
import datetime
import functools
import hashlib
import importlib
import json
import os
import platform
import re
import select
import signal
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
if IS_WIN:
    import msvcrt
else:
    import termios
    import tty

# ─────────────────────────── dipendenze Python ──────────────────────────────

DEPS = [("hidapi", "hid", "comunicazione USB con il display"),
        ("pillow", "PIL", "disegno dei caratteri con i font")]


def missing_deps():
    """[(pacchetto pip, motivo)] dei pacchetti assenti o sbagliati."""
    out = []
    for pkg, mod, why in DEPS:
        try:
            m = importlib.import_module(mod)
        except ImportError:
            out.append((pkg, why))
            continue
        if mod == "hid" and not hasattr(m, "device"):
            out.append((pkg, why + " (c'è il pacchetto 'hid', che è un altro: serve 'hidapi')"))
    return out


def private_env():
    """Ambiente Python dedicato a Scroller, usato quando mancano i pacchetti."""
    if IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    env = base / "scroller" / "venv"
    return env, env / ("Scripts/python.exe" if IS_WIN else "bin/python")


def relaunch(python):
    env = dict(os.environ, SCROLLER_BOOTSTRAP="1")
    args = [str(python), os.path.abspath(sys.argv[0]), *sys.argv[1:]]
    sys.stdout.flush()  # execve scarta il testo ancora nel buffer
    sys.stderr.flush()
    if IS_WIN:
        sys.exit(subprocess.call(args, env=env))
    os.execve(str(python), args, env)


def ensure_deps():
    missing = missing_deps()
    if not missing:
        return
    pkgs = [p for p, _, _ in DEPS]
    if os.environ.get("SCROLLER_BOOTSTRAP"):
        sys.exit("Installazione dei pacchetti non riuscita: " + ", ".join(p for p, _ in missing))
    env, py = private_env()
    check = [str(py), "-c", "import hid, PIL; hid.device"]
    if py.exists() and subprocess.call(check, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0:
        relaunch(py)  # pacchetti già pronti nell'ambiente dedicato
    print("Scroller ha bisogno di questi pacchetti Python:")
    for p, why in missing:
        print(f"  • {p:<8} {why}")
    manual = f'"{sys.executable}" -m pip install ' + " ".join(pkgs)
    if os.environ.get("SCROLLER_NO_INSTALL"):
        sys.exit(f"\nInstallali con:\n  {manual}")
    print(f"\nLi installo in un ambiente Python dedicato: {env}")
    try:
        if not py.exists():
            import venv
            venv.EnvBuilder(with_pip=True, clear=True).create(env)
        rc = subprocess.call([str(py), "-m", "pip", "install", "--disable-pip-version-check", "-q", "--upgrade",
                              *pkgs])
    except Exception as e:  # venv/ensurepip assenti, disco pieno, ...
        rc = str(e)
    if rc != 0:
        hint = "\nSu Debian/Ubuntu può servire prima:  sudo apt install python3-venv python3-pip" if IS_LINUX else ""
        sys.exit(f"\nInstallazione automatica non riuscita ({rc}).\nInstallali a mano con:\n  {manual}{hint}")
    print("Pacchetti installati. Avvio Scroller…\n")
    relaunch(py)


ensure_deps()
import hid  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

# ─────────────────────────────── dispositivo ────────────────────────────────

VID, PID = 0x0416, 0x5020
KNOWN_DEVICES = {(0x0416, 0x5020): "badge LED 11 righe (CH583 / LS32)"}
ROWS = 11
MAX_BYTES = 8192
if IS_WIN:
    CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "scroller"
else:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "scroller"
CONFIG_PATH = CONFIG_DIR / "config.json"

MODES = [
    ("←", "scorre a sinistra"), ("→", "scorre a destra"), ("↑", "scorre in su"),
    ("↓", "scorre in giù"), ("■", "fisso"), ("✦", "animazione"),
    ("▼", "caduta"), ("▥", "tenda"), ("✱", "laser"),
]
VERTICAL = (2, 3)  # scorre in su / in giù: il testo non scorre di lato
BRIGHTNESS = [25, 50, 75, 100]
UDEV_RULE = 'SUBSYSTEM=="hidraw", ATTRS{idVendor}=="0416", ATTRS{idProduct}=="5020", MODE="0666"'


class DeviceError(Exception):
    def __init__(self, short, detail=""):
        super().__init__(short)
        self.short, self.detail = short, detail


def find_device():
    """Cerca il display tra i dispositivi HID (stesso metodo su macOS, Linux e Windows)."""
    try:
        devs = hid.enumerate()
    except Exception:
        return None
    found = [d for d in devs if (d.get("vendor_id"), d.get("product_id")) in KNOWN_DEVICES]
    if not found:
        return None
    # alcuni sistemi espongono più interfacce: preferisci quella "vendor" (usage page 0xFF00)
    found.sort(key=lambda d: (d.get("usage_page") == 0xFF00, d.get("interface_number", 0) <= 0), reverse=True)
    return found[0]


def device_label(d):
    name = " ".join(x for x in (d.get("manufacturer_string"), d.get("product_string")) if x)
    return name or KNOWN_DEVICES[(d["vendor_id"], d["product_id"])]


def open_help():
    if IS_LINUX:
        return ("Su Linux serve il permesso per accedere al display. Esegui una volta:\n"
                f"  echo '{UDEV_RULE}' | sudo tee /etc/udev/rules.d/99-scroller.rules\n"
                "  sudo udevadm control --reload-rules && sudo udevadm trigger\n"
                "poi scollega e ricollega il display.")
    if IS_WIN:
        return "Chiudi gli altri programmi che usano il display, poi scollegalo e ricollegalo."
    return "Scollega e ricollega il display."


def open_device():
    d = find_device()
    if not d:
        raise DeviceError("Display non trovato: è collegato via USB?")
    dev = hid.device()
    try:
        dev.open_path(d["path"])
    except (OSError, IOError):
        raise DeviceError("Il display è collegato ma non si riesce ad aprirlo"
                          + (" (permessi USB)" if IS_LINUX else ""), open_help())
    return dev


def send_payload(data):
    dev = open_device()
    try:
        for off in range(0, len(data), 64):
            if dev.write(b"\x00" + data[off:off + 64]) < 0:  # primo byte = report ID (nessuno)
                raise DeviceError("Scrittura sul display non riuscita", open_help())
            time.sleep(0.01)
    finally:
        dev.close()


# ──────────────────────────── font pixel 5×7 ────────────────────────────────
# Colonne da sinistra a destra, bit 0 = riga in alto (font 5×7 classico, ASCII 0x20-0x7E).
CLASSIC_5X7 = (
    "0000000000 00005f0000 0007000700 147f147f14 242a7f2a12 2313086462 3649562050 0000070000 "
    "001c224100 0041221c00 14083e0814 08083e0808 0000000000 0808080808 0060600000 2010080402 "
    "3e5149453e 00427f4000 4261514946 2141454b31 1814127f10 2745454539 3c4a494930 0171090503 "
    "3649494936 064949291e 0036360000 0000000000 0814224100 1414141414 0041221408 0201510906 "
    "324979413e 7e1111117e 7f49494936 3e41414122 7f4141221c 7f49494941 7f09090901 3e4149497a "
    "7f0808087f 00417f4100 2040413f01 7f08142241 7f40404040 7f020c027f 7f0408107f 3e4141413e "
    "7f09090906 3e4151215e 7f09192946 4649494931 01017f0101 3f4040403f 1f2040201f 3f4038403f "
    "6314081463 0708700807 6151494543 007f414100 0204081020 0041417f00 0402010204 4040404040 "
    "0001020400 2054545478 7f48444438 3844444420 384444487f 3854545418 087e090102 0000000000 "
    "7f08040478 00447d4000 0000000000 7f10284400 00417f4000 7c04180478 7c08040478 3844444438 "
    "0000000000 0000000000 7c08040408 4854545420 043f444020 3c4040207c 1c2040201c 3c4030403c "
    "4428102844 0000000000 4464544c44 0008364100 00007f0000 0041360800 0804081008"
).split()
# Glifi con discendenti (righe 7-8) o ritoccati.
GLYPH_OVERRIDES = {
    "g": [0x38, 0x144, 0x144, 0x144, 0xFC], "j": [0x80, 0x100, 0x100, 0xFD, 0x00],
    "p": [0x1FC, 0x44, 0x44, 0x44, 0x38], "q": [0x38, 0x44, 0x44, 0x44, 0x1FC],
    "y": [0x3C, 0x140, 0x140, 0x140, 0xFC], ",": [0x00, 0x80, 0x60, 0x00, 0x00],
    ";": [0x00, 0x80, 0x66, 0x00, 0x00], "°": [0x00, 0x06, 0x09, 0x09, 0x06],
    "€": [0x14, 0x3E, 0x55, 0x41, 0x22], "ı": [0x00, 0x44, 0x7C, 0x40, 0x00],
    "r": [0x7C, 0x08, 0x04, 0x04],
}
# Accenti: (riga, colonna) nella griglia interna (riga 0 = sopra le maiuscole).
ACCENTS_LOWER = {"0300": [(0, 1), (1, 2)], "0301": [(0, 3), (1, 2)],
                 "0302": [(1, 1), (0, 2), (1, 3)], "0308": [(1, 1), (1, 3)]}
ACCENTS_UPPER = {"0300": [(0, 1), (0, 2)], "0301": [(0, 2), (0, 3)],
                 "0302": [(0, 2)], "0308": [(0, 1), (0, 3)]}


# ─────────────────────────────── simboli ────────────────────────────────────
# Icone pixel alte 9 righe (riga 0 = riga degli accenti), "#" = LED acceso.
# (nomi per :nome: — il primo è quello principale —, caratteri Unicode/emoji che le richiamano, disegno)
ICONS_SRC = [
    ("cuore heart love", "♥❤💗💖💕💓🧡💛💚💙💜🖤🤍♡❣", """
        ......... .##...##. ####.#### ######### ######### .#######. ..#####.. ...###... ....#...."""),
    ("stella star", "★⭐🌟☆✩✭", """
        ....#.... ....#.... ...###... ######### .#######. ..#####.. ..##.##.. .##...##. .#.....#."""),
    ("sorriso smile", "☺🙂😀😃😄😁😊☻😆😂🤣", """
        ..#####.. .#.....#. #..#.#..# #..#.#..# #.......# #.#...#.# #..###..# .#.....#. ..#####.."""),
    ("triste sad", "☹🙁😞😢😭😟", """
        ..#####.. .#.....#. #..#.#..# #..#.#..# #.......# #..###..# #.#...#.# .#.....#. ..#####.."""),
    ("occhiolino wink", "😉", """
        ..#####.. .#.....#. #..#....# #..#.##.# #.......# #.#...#.# #..###..# .#.....#. ..#####.."""),
    ("sole sun", "☀🌞☼", """
        ....#.... .#..#..#. ..#...#.. ...###... ##.###.## ...###... ..#...#.. .#..#..#. ....#...."""),
    ("luna moon", "☾🌙🌛🌜☽", """
        ..### .##.. ##... ##... ##... ##... ##... .##.. ..###"""),
    ("nuvola cloud", "☁🌥", """
        ........... ........... ....###.... ..##...#... .#......##. #.........# #.........# .#########.
        ..........."""),
    ("ombrello pioggia umbrella rain", "☂☔🌂", """
        ....#.... ..#####.. .#######. ######### ....#.... ....#.... ....#.... .#..#.... ..##....."""),
    ("neve snow", "❄❅❆☃", """
        ....#.... .#..#..#. ..#.#.#.. ...###... ######### ...###... ..#.#.#.. .#..#..#. ....#...."""),
    ("fulmine bolt", "⚡🗲", """
        ....## ...##. ..##.. .##### ...##. ..##.. .##... .#.... #....."""),
    ("nota musica note music", "♪♫🎵🎶♩♬", """
        ...##.. ...#.#. ...#..# ...#... ...#... .###... ####... ####... .##...."""),
    ("spunta ok check", "✓✔✅☑", """
        ........ .......# ......## #....##. ##..##.. .####... ..##.... ........ ........"""),
    ("croce no cross", "✗✘❌✕✖❎", """
        ....... #.....# ##...## .##.##. ..###.. .##.##. ##...## #.....# ......."""),
    ("destra right", "→➡➔➜⇒", """
        ........ ....#... ....##.. #######. ######## #######. ....##.. ....#... ........"""),
    ("sinistra left", "←⬅⇐", """
        ........ ...#.... ..##.... .####### ######## .####### ..##.... ...#.... ........"""),
    ("su up", "↑⬆", """
        ...#... ..###.. .#####. ####### ..###.. ..###.. ..###.. ..###.. ......."""),
    ("giu giù down", "↓⬇", """
        ....... ..###.. ..###.. ..###.. ..###.. ####### .#####. ..###.. ...#..."""),
    ("telefono phone", "☎📞☏📱", """
        ......... .#######. ##.....## ##.###.## ..#####.. .##.#.##. .#######. .#######. ........."""),
    ("busta mail", "✉📧📩📨💌", """
        ........... ########### ##.......## #.#.....#.# #..#...#..# #...#.#...# #....#....# ###########
        ..........."""),
    ("caffe caffè coffee", "☕", """
        ..#.#.#... .#.#.#.... .......... ########.. #......### #......#.# #......### .#....#... ..####...."""),
    ("birra beer", "🍺🍻", """
        .#####... #######.. #.....### #.#.#.#.# #.#.#.#.# #.#.#.### #.#.#.#.. #.....#.. #######.."""),
    ("pizza", "🍕", """
        ######### #.#...#.# .#######. ..#.#.#.. ..#####.. ...#.#... ...###... ....#.... ........."""),
    ("casa home", "⌂🏠🏡", """
        ....#.... ...#.#... ..#...#.. .#.....#. ######### .#.....#. .#.##..#. .#.##..#. .#######."""),
    ("campana bell", "🔔", """
        ....#.... ...###... ..#####.. ..#####.. ..#####.. .#######. ######### ......... ....#...."""),
    ("fuoco fire", "🔥", """
        ...#..... ...##.... ..###..#. .#####.#. .######## #####.### ###...### .##...##. ..#####.."""),
    ("pollice like", "👍", """
        ....#.... ...##.... ...#..... .######.. ##.....#. ##.#####. ##.....#. ##.#####. ##.####.."""),
    ("regalo gift", "🎁", """
        .##...##. ...#.#... ######### #...#...# ######### .#..#..#. .#..#..#. .#..#..#. .#######."""),
    ("albero natale tree", "🎄🌲", """
        ....#.... ...###... ..#####.. ...###... ..#####.. .#######. ######### ....#.... ...###..."""),
    ("auto car", "🚗🚘", """
        ........... ........... ..######... .#...#..#.. #....#...## ########### ########### .##....##..
        .##....##.."""),
    ("punto dot", "●•⚫⬤", """
        ..... ..... .###. ##### ##### ##### .###. ..... ....."""),
    ("quadrato square", "■◼⬛▪", """
        ..... ..... ##### ##### ##### ##### ##### ..... ....."""),
    ("rombo diamond", "◆♦🔶🔷", """
        ..... ..... ..#.. .###. ##### .###. ..#.. ..... ....."""),
    ("play", "▶►⏵", """
        .... #... ##.. ###. #### ###. ##.. #... ...."""),
    ("pausa pause", "⏸", """
        ..... ##.## ##.## ##.## ##.## ##.## ##.## ##.## ....."""),
]
ICONS, ICON_ALIASES, ICON_NAMES, ICON_CHARS = {}, {}, {}, {}
for _names, _chars, _art in ICONS_SRC:
    _rows, _key = _art.split(), _names.split()[0]
    assert len(_rows) == 9 and len({len(r) for r in _rows}) == 1, f"icona {_key} malformata"
    ICONS[_key] = ([sum(1 << r for r in range(9) if _rows[r][x] == "#") for x in range(len(_rows[0]))], _chars[0])
    ICON_NAMES[_key] = _names.split()
    ICON_ALIASES.update(dict.fromkeys(_names.split(), _key))
    ICON_CHARS.update(dict.fromkeys(_chars, _key))
SHORTCODE_RE = re.compile(r":([a-zàèéìòù_]+):")
# selettori di variante, zero-width joiner e toni della pelle: non hanno un disegno proprio
INVISIBLE = dict.fromkeys([0xFE0E, 0xFE0F, 0x200D, *range(0x1F3FB, 0x1F400)])


def expand_text(text):
    """Sostituisce :nome: con il simbolo corrispondente e toglie i caratteri invisibili delle emoji."""
    text = SHORTCODE_RE.sub(lambda m: ICONS[ICON_ALIASES[m[1]]][1] if m[1] in ICON_ALIASES else m[0], text)
    return text.translate(INVISIBLE)


def _builtin_base(ch):
    if ch in GLYPH_OVERRIDES:
        return [c << 1 for c in GLYPH_OVERRIDES[ch]]
    o = ord(ch)
    if 0x20 <= o <= 0x7E:
        h = CLASSIC_5X7[o - 0x20]
        return [int(h[i:i + 2], 16) << 1 for i in range(0, 10, 2)]
    return None


def builtin_glyph(ch):
    """Ritorna le colonne (griglia interna a 10 righe) di un carattere del font integrato."""
    g = _builtin_base(ch)
    if g is not None:
        return g
    dec = unicodedata.decomposition(ch).split()
    if len(dec) == 2 and not dec[0].startswith("<"):
        base, accent = chr(int(dec[0], 16)), dec[1]
        if base == "i":
            base = "ı"
        g = _builtin_base(base)
        if g is None:
            return _builtin_base("?")
        table = ACCENTS_UPPER if base.isupper() else ACCENTS_LOWER
        for r, c in table.get(accent, []):
            g[c] |= 1 << r
        return g
    return _builtin_base("?")


# ─────────────────────────────── font & resa ────────────────────────────────

BUILTIN = "pixel"
FONT_EXT = (".ttf", ".otf", ".ttc")
SAMPLE = "Ciao! àèéìòù 0123"
DEFAULT_FONT = {"id": BUILTIN, "sizes": {}, "offset": 0, "spacing": 1, "bold": False}
# Font di sistema che rendono bene a 11 pixel (nomi file, minuscoli): macOS, Windows, Linux.
PREFERRED_FONTS = [
    "geneva.ttf", "monaco.ttf", "silom.ttf", "tahoma.ttf", "tahoma bold.ttf", "tahomabd.ttf",
    "verdana.ttf", "verdana bold.ttf", "verdanab.ttf", "arial.ttf", "arial bold.ttf", "arialbd.ttf",
    "arial narrow.ttf", "arial narrow bold.ttf", "arialn.ttf", "arial black.ttf", "ariblk.ttf",
    "helvetica.ttc", "segoeui.ttf", "segoeuib.ttf", "calibri.ttf", "calibrib.ttf",
    "trebuchet ms.ttf", "trebuchet ms bold.ttf", "trebuc.ttf", "trebucbd.ttf", "impact.ttf",
    "andale mono.ttf", "consola.ttf", "consolab.ttf", "lucon.ttf", "courier new.ttf", "courier new bold.ttf",
    "cour.ttf", "courbd.ttf", "menlo.ttc", "ptmono.ttc", "georgia.ttf", "georgia bold.ttf", "futura.ttc",
    "chalkboard.ttc", "sfcompact.ttf", "sfnsmono.ttf",
    "dejavusans.ttf", "dejavusans-bold.ttf", "dejavusanscondensed.ttf", "dejavusansmono.ttf",
    "liberationsans-regular.ttf", "liberationsans-bold.ttf", "liberationsansnarrow-regular.ttf",
    "liberationmono-regular.ttf", "freesans.ttf", "freesansbold.ttf", "freemono.ttf",
    "ubuntu-r.ttf", "ubuntu-b.ttf", "notosans-regular.ttf", "notosans-bold.ttf", "notosansmono-regular.ttf",
]


def font_dirs():
    """(cartelle di sistema, cartelle utente) dei font sul sistema operativo corrente."""
    home = Path.home()
    if IS_MAC:
        return [Path("/System/Library/Fonts")], [home / "Library/Fonts", Path("/Library/Fonts")]
    if IS_WIN:
        win = Path(os.environ.get("WINDIR") or "C:/Windows")
        local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
        return [win / "Fonts"], [local / "Microsoft/Windows/Fonts"]
    return ([Path("/usr/share/fonts"), Path("/usr/local/share/fonts")],
            [home / ".local/share/fonts", home / ".fonts"])


def _scan(d):
    try:
        return sorted(p for p in d.rglob("*") if p.suffix.lower() in FONT_EXT)
    except OSError:
        return []


@functools.lru_cache(maxsize=None)
def list_fonts():
    """[(id, nome)]: il font integrato, i font di sistema adatti e tutti quelli dell'utente."""
    system_dirs, user_dirs = font_dirs()
    system = {}
    for d in system_dirs:
        for p in _scan(d):
            system.setdefault(p.name.lower(), p)
    fonts, seen = [(BUILTIN, "Pixel 5×7 (integrato)")], set()
    for p in [system.get(n) for n in PREFERRED_FONTS] + [p for d in user_dirs for p in _scan(d)]:
        if p and str(p) not in seen:
            fonts.append((str(p), font_name(str(p))))
            seen.add(str(p))
    return fonts


@functools.lru_cache(maxsize=None)
def font_name(font_id):
    if font_id == BUILTIN:
        return "Pixel 5×7"
    try:
        family, style = ImageFont.truetype(font_id, 10).getname()
        return family if style in (None, "Regular", "Normal", "Book", "Roman") else f"{family} {style}"
    except Exception:
        return Path(font_id).stem


_ttf_cache, _glyph_cache = {}, {}


def load_ttf(path, size):
    key = (path, size)
    if key not in _ttf_cache:
        _ttf_cache[key] = ImageFont.truetype(path, size)
    return _ttf_cache[key]


@functools.lru_cache(maxsize=None)
def auto_size(path):
    """Dimensione più grande per cui la 'H' maiuscola resta alta al massimo 8 pixel."""
    best = 11
    for size in range(6, 30):
        try:
            img = Image.new("1", (size * 3, size * 3), 0)
            d = ImageDraw.Draw(img)
            d.fontmode = "1"
            d.text((size, size * 2), "H", font=load_ttf(path, size), fill=1, anchor="ls")
            box = img.getbbox()
        except OSError:
            return best
        if box and box[3] - box[1] <= 8:
            best = size
        elif box:
            break
    return best


def ttf_glyph(ch, path, size, baseline):
    font = load_ttf(path, size)
    img = Image.new("1", (size * 3 + 8, ROWS), 0)
    d = ImageDraw.Draw(img)
    d.fontmode = "1"  # niente antialiasing: usa l'hinting monocromatico del font
    d.text((size, baseline), ch, font=font, fill=1, anchor="ls")
    box = img.getbbox()
    if not box:
        return None
    px = img.load()
    return [sum(1 << r for r in range(ROWS) if px[x, r]) for x in range(box[0], box[2])]


def font_settings(f, font_id=None):
    """Impostazioni di resa dal font di un messaggio (opzionalmente provando un altro font)."""
    fid = font_id or f["id"]
    if fid != BUILTIN and not os.path.exists(fid):
        fid = BUILTIN  # font non presente su questo computer
    size = None if fid == BUILTIN else (f["sizes"].get(fid) or auto_size(fid))
    return {"id": fid, "size": size, "offset": f["offset"], "spacing": f["spacing"], "bold": f["bold"]}


def font_label(f):
    fs = font_settings(f)
    return font_name(fs["id"]) + (f" {fs['size']}px" if fs["size"] else "")


def glyph(ch, fs):
    key = (ch, fs["id"], fs["size"], fs["offset"], fs["bold"])
    if key in _glyph_cache:
        return _glyph_cache[key]
    if ch in ICON_CHARS or fs["id"] == BUILTIN:  # i simboli usano sempre i disegni pixel integrati
        raw = ICONS[ICON_CHARS[ch]][0] if ch in ICON_CHARS else builtin_glyph(ch)
        shift = fs["offset"]  # riga 0 = accenti, maiuscole 1-7, discendenti 8-9
        cols = [((c << shift) if shift >= 0 else (c >> -shift)) & 0x7FF for c in raw]
        while cols and not cols[0]:
            cols.pop(0)
        while cols and not cols[-1]:
            cols.pop()
        cols = cols or None
        space = 3
    else:
        try:
            cols = ttf_glyph(ch, fs["id"], fs["size"], 9 + fs["offset"])
        except OSError:
            cols = None
        space = max(2, round(fs["size"] * 0.3))
    if cols is None:
        cols = [0] * space
    elif fs["bold"]:
        cols = [(cols[i] if i < len(cols) else 0) | (cols[i - 1] if i else 0) for i in range(len(cols) + 1)]
    _glyph_cache[key] = cols
    return cols


def render(text, fs):
    """Testo → lista di colonne (int a 11 bit, bit 0 = riga in alto)."""
    cols = []
    for ch in expand_text(text):
        if cols:
            cols.extend([0] * fs["spacing"])
        cols.extend(glyph(ch, fs))
    return cols


def msg_cols(m, text=None):
    return render(m["text"] if text is None else text, font_settings(m["font"]))


def overflow(m, width, text=None):
    """Larghezza del messaggio se scorre in verticale ed è più largo del display, altrimenti 0."""
    if m["mode"] not in VERTICAL:
        return 0
    n = len(msg_cols(m, text))
    return n if n > width else 0


def active(cfg):
    return [(i, m) for i, m in enumerate(cfg["messages"]) if m["enabled"] and m["text"].strip()]


def overflowing(cfg):
    return [i + 1 for i, m in active(cfg) if overflow(m, cfg["display_width"])]


def pack(cols):
    n = (len(cols) + 7) // 8
    cols = cols + [0] * (n * 8 - len(cols))
    out = bytearray()
    for c in range(n):
        for r in range(ROWS):
            b = 0
            for bit in range(8):
                if cols[c * 8 + bit] >> r & 1:
                    b |= 0x80 >> bit
            out.append(b)
    return bytes(out), n


def assemble(entries, brightness, stamp=True):
    """entries: [(colonne, effetto, velocità, lampeggio, bordo)] → byte da inviare (header 'wang' + bitmap)."""
    h = bytearray(64)
    h[0:4] = b"wang"
    h[5] = {25: 0x40, 50: 0x20, 75: 0x10}.get(brightness, 0)
    body = bytearray()
    for i, (cols, mode, speed, blink, ants) in enumerate(entries):
        data, n = pack(cols)
        body += data
        h[6] |= int(blink) << i
        h[7] |= int(ants) << i
        h[8 + i] = ((speed - 1) << 4) | mode
        h[16 + 2 * i], h[17 + 2 * i] = n >> 8, n & 0xFF
    if stamp:
        t = datetime.datetime.now()
        h[38:44] = bytes([t.year % 100, t.month, t.day, t.hour, t.minute, t.second])
    data = bytes(h + body)
    return data + bytes((-len(data)) % 64)


def build_payload(cfg, stamp=True):
    msgs = [m for _, m in active(cfg)]
    entries = [(msg_cols(m), m["mode"], m["speed"], m["blink"], m["ants"]) for m in msgs]
    return assemble(entries, cfg["brightness"], stamp), msgs


def payload_hash(cfg):
    return hashlib.sha1(build_payload(cfg, stamp=False)[0] + bytes([cfg["brightness"]])).hexdigest()


# ─────────────────────────────── configurazione ─────────────────────────────

def default_config():
    return {
        "messages": [{"text": "", "enabled": False, "mode": 0, "speed": 5, "blink": False, "ants": False,
                      "font": copy.deepcopy(DEFAULT_FONT)} for _ in range(8)],
        "brightness": 100,
        "display_width": 44,
        "sent_hash": "",
    }


def load_config():
    cfg = default_config()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cfg
    for k in ("brightness", "display_width", "sent_hash"):
        cfg[k] = data.get(k, cfg[k])
    # versioni precedenti: un unico font globale → copialo in ogni messaggio
    legacy = {k: v for k, v in data.get("font", {}).items() if k in DEFAULT_FONT}
    for m in cfg["messages"]:
        m["font"].update(copy.deepcopy(legacy))
    for i, m in enumerate(data.get("messages", [])[:8]):
        m = dict(m)
        font = m.pop("font", None)
        cfg["messages"][i].update(m)
        if font:
            cfg["messages"][i]["font"] = {**copy.deepcopy(DEFAULT_FONT), **copy.deepcopy(font)}
    return cfg


def save_config(cfg):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


# ─────────────────────────────── terminale ──────────────────────────────────

ACC, GREEN, RED, GRAY = 173, 114, 167, 244
LED_ON, LED_OFF, LED_CUT_ON, LED_CUT_OFF = 203, 236, 243, 234
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
TOKEN_RE = re.compile(r"(\x1b\[[0-9;?]*[A-Za-z])|(.)", re.S)
MOVE_HINT = "ctrl↑↓ sposta" if IS_WIN else "⇧↑↓ sposta"


def fg(s, c):
    return f"\x1b[38;5;{c}m{s}\x1b[39m"


def bold(s):
    return f"\x1b[1m{s}\x1b[22m"


def dim(s):
    return f"\x1b[2m{s}\x1b[22m"


def inv(s):
    return f"\x1b[7m{s}\x1b[27m"


def cwidth(ch):
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def vlen(s):
    return sum(cwidth(c) for c in ANSI_RE.sub("", s))


def crop(s, w):
    out, n = [], 0
    for m in TOKEN_RE.finditer(s):
        if m.group(1):
            out.append(m.group(1))
            continue
        cw = cwidth(m.group(2))
        if n + cw > w:
            out.append("\x1b[0m")
            break
        out.append(m.group(2))
        n += cw
    return "".join(out)


def pad(s, w):
    s = crop(s, w)
    return s + " " * (w - vlen(s))


def ellipsis(s, w):
    return s if len(s) <= w else s[:max(0, w - 1)] + "…"


def box(lines, w, color=GRAY):
    inner = w - 4
    out = [fg("╭" + "─" * (w - 2) + "╮", color)]
    out += [fg("│", color) + " " + pad(l, inner) + " " + fg("│", color) for l in lines]
    out.append(fg("╰" + "─" * (w - 2) + "╯", color))
    return out


def led_lines(cols, width, limit=None):
    """Disegna la matrice LED con mezzi blocchi (2 righe di LED per riga di terminale).
    Le colonne da `limit` in poi (oltre il bordo del display) sono disegnate spente/grigie."""
    cols = cols[:width]
    out = []
    for r in range(0, ROWS, 2):
        s, last = [], None
        for x, c in enumerate(cols):
            on, off = (LED_ON, LED_OFF) if limit is None or x < limit else (LED_CUT_ON, LED_CUT_OFF)
            top = on if c >> r & 1 else off
            if r + 1 < ROWS:
                code = f"38;5;{top};48;5;{on if c >> (r + 1) & 1 else off}"
            else:
                code = f"38;5;{top};49"
            s.append(("" if code == last else f"\x1b[{code}m") + "▀")
            last = code
        out.append("".join(s) + "\x1b[0m")
    return out


def symbols_grid(width):
    """Galleria dei simboli: righe di icone LED con il loro :nome: sotto."""
    cells = []
    for key, (cols, _) in ICONS.items():
        while cols and not cols[-1]:
            cols = cols[:-1]
        label = f":{key}:"
        cells.append((cols, label, max(len(label), len(cols)) + 2))
    rows, cur, used = [], [], 0
    for cell in cells:
        if cur and used + cell[2] > width:
            rows.append(cur)
            cur, used = [], 0
        cur.append(cell)
        used += cell[2]
    rows.append(cur)
    out = []
    for row in rows:
        led = [""] * 6
        for cols, _, cw in row:
            for j, line in enumerate(led_lines(cols, len(cols))):
                led[j] += line + " " * (cw - len(cols))
        out += led + ["".join(pad(dim(label), cw) for _, label, cw in row), ""]
    return out


KEYSEQ = {
    "\x1b[A": "up", "\x1b[B": "down", "\x1b[C": "right", "\x1b[D": "left",
    "\x1bOA": "up", "\x1bOB": "down", "\x1bOC": "right", "\x1bOD": "left",
    "\x1b[H": "home", "\x1b[F": "end", "\x1bOH": "home", "\x1bOF": "end",
    "\x1b[1~": "home", "\x1b[4~": "end", "\x1b[3~": "delete", "\x1b[5~": "pgup", "\x1b[6~": "pgdn",
    "\x1b[Z": "shift-tab", "\x1bb": "word-left", "\x1bf": "word-right",
    "\x1b[1;3D": "word-left", "\x1b[1;3C": "word-right", "\x1b[1;5D": "word-left", "\x1b[1;5C": "word-right",
    "\x1b[1;2A": "move-up", "\x1b[1;2B": "move-down", "\x1b[1;3A": "move-up", "\x1b[1;3B": "move-down",
    "\x1b[1;5A": "move-up", "\x1b[1;5B": "move-down",
    "\x1b\x1b[A": "move-up", "\x1b\x1b[B": "move-down", "\x1b\x7f": "word-backspace",
}
_SEQS = sorted(KEYSEQ, key=len, reverse=True)
# Windows: tasti speciali letti da msvcrt (prefisso \x00 o \xe0) → sequenze ANSI equivalenti
WIN_KEYS = {"H": "\x1b[A", "P": "\x1b[B", "K": "\x1b[D", "M": "\x1b[C", "G": "\x1b[H", "O": "\x1b[F",
            "S": "\x1b[3~", "I": "\x1b[5~", "Q": "\x1b[6~", "s": "\x1b[1;5D", "t": "\x1b[1;5C",
            "\x8d": "\x1b[1;5A", "\x91": "\x1b[1;5B"}


def parse_keys(s):
    keys, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "\x1b":
            seq = next((q for q in _SEQS if s.startswith(q, i)), None)
            if seq:
                keys.append(KEYSEQ[seq])
                i += len(seq)
                continue
            if s.startswith("\x1b[", i):  # sequenza CSI sconosciuta: ignorala
                j = i + 2
                while j < len(s) and not ("@" <= s[j] <= "~"):
                    j += 1
                i = j + 1
                continue
            keys.append("esc")
        elif ch in "\r\n":
            keys.append("enter")
        elif ch in "\x7f\x08":
            keys.append("backspace")
        elif ch == "\t":
            keys.append("tab")
        elif ord(ch) < 32:
            keys.append("ctrl-" + chr(ord(ch) + 96))
        else:
            keys.append(ch)
        i += 1
    return keys


def enable_windows_ansi():
    """Attiva colori/sequenze ANSI e UTF-8 nella console di Windows 10+."""
    import ctypes
    k = ctypes.windll.kernel32
    h = k.GetStdHandle(-11)
    mode = ctypes.c_uint32()
    if k.GetConsoleMode(h, ctypes.byref(mode)):
        k.SetConsoleMode(h, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class Terminal:
    def __enter__(self):
        if IS_WIN:
            enable_windows_ansi()
        else:
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
            self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.write("\x1b[?1049h\x1b[?25l\x1b[?7l")
        return self

    def __exit__(self, *exc):
        self.write("\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
        if not IS_WIN:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    @staticmethod
    def write(s):
        sys.stdout.write(s)
        sys.stdout.flush()

    @staticmethod
    def size():
        sz = os.get_terminal_size()
        return sz.columns, sz.lines

    def read_keys(self, timeout):
        if IS_WIN:
            end = time.time() + timeout
            while not msvcrt.kbhit():
                if time.time() > end:
                    return []
                time.sleep(0.02)
            s = ""
            while msvcrt.kbhit():
                ch = msvcrt.getwch()
                s += WIN_KEYS.get(msvcrt.getwch(), "") if ch in ("\x00", "\xe0") else ch
            return parse_keys(s)
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return []
        s = self.dec.decode(os.read(self.fd, 4096))
        if s.endswith("\x1b") and select.select([self.fd], [], [], 0.04)[0]:
            s += self.dec.decode(os.read(self.fd, 4096))
        return parse_keys(s)


class LineEditor:
    def __init__(self, text=""):
        self.buf, self.pos = list(text), len(text)

    @property
    def text(self):
        return "".join(self.buf)

    def _word_left(self):
        p = self.pos
        while p and self.buf[p - 1] == " ":
            p -= 1
        while p and self.buf[p - 1] != " ":
            p -= 1
        return p

    def _word_right(self):
        p, n = self.pos, len(self.buf)
        while p < n and self.buf[p] == " ":
            p += 1
        while p < n and self.buf[p] != " ":
            p += 1
        return p

    def key(self, k):
        if k == "left":
            self.pos = max(0, self.pos - 1)
        elif k == "right":
            self.pos = min(len(self.buf), self.pos + 1)
        elif k in ("home", "ctrl-a"):
            self.pos = 0
        elif k in ("end", "ctrl-e"):
            self.pos = len(self.buf)
        elif k == "word-left":
            self.pos = self._word_left()
        elif k == "word-right":
            self.pos = self._word_right()
        elif k == "backspace":
            if self.pos:
                self.pos -= 1
                del self.buf[self.pos]
        elif k in ("delete", "ctrl-d"):
            if self.pos < len(self.buf):
                del self.buf[self.pos]
        elif k == "ctrl-u":
            del self.buf[:self.pos]
            self.pos = 0
        elif k == "ctrl-k":
            del self.buf[self.pos:]
        elif k in ("ctrl-w", "word-backspace"):
            p = self._word_left()
            del self.buf[p:self.pos]
            self.pos = p
        elif len(k) == 1 and k.isprintable():
            self.buf.insert(self.pos, k)
            self.pos += 1
        else:
            return False
        return True

    def render(self, width):
        start = max(0, self.pos - width + 1)
        vis = self.buf[start:start + width]
        rel = self.pos - start
        s = "".join(vis[:rel])
        s += inv(vis[rel] if rel < len(vis) else " ")
        return s + "".join(vis[rel + 1:])


# ─────────────────────────────── schermate ──────────────────────────────────

class Item:
    def __init__(self, label, value=None, action=None, left=None, right=None, toggle=False, disabled=None):
        self.label, self.value, self.action = label, value, action
        self.left, self.right, self.toggle = left, right, toggle
        self.disabled = disabled or (lambda: False)


def yesno(v):
    return fg("● sì", GREEN) if v else dim("○ no")


def speed_bar(s):
    return fg("▰" * s, ACC) + dim("▱" * (8 - s)) + f" {s}"


class Screen:
    title = ""

    def __init__(self, app):
        self.app = app

    @property
    def cfg(self):
        return self.app.cfg

    def lines(self, w):
        return []

    def key(self, k):
        if k in ("esc", "backspace"):
            self.app.pop()

    def hint(self):
        return "esc indietro"


class MenuScreen(Screen):
    def __init__(self, app):
        super().__init__(app)
        self.sel = 0

    def items(self):
        return []

    def extra(self, w):
        return []

    def lines(self, w):
        items = self.items()
        self.sel = max(0, min(self.sel, len(items) - 1))
        lw = max(vlen(it.label) for it in items) + 4
        out = []
        for i, it in enumerate(items):
            sel = i == self.sel
            label = pad(fg(it.label, ACC) if sel else it.label, lw)
            val = it.value() if it.value else ""
            if sel and (it.left or it.right) and not it.disabled():
                val = dim("‹ ") + val + dim(" ›")
            line = f"  {fg('❯', ACC) if sel else ' '} {label}{val}"
            out.append(dim(ANSI_RE.sub("", line)) if it.disabled() else line)
        return out + self.extra(w)

    def key(self, k):
        items = self.items()
        it = items[self.sel]
        if k in ("up", "ctrl-p"):
            self.sel = (self.sel - 1) % len(items)
        elif k in ("down", "ctrl-n", "tab"):
            self.sel = (self.sel + 1) % len(items)
        elif it.disabled():
            super().key(k)
        elif k == "enter" or (k == " " and it.toggle):
            if it.action:
                it.action()
            elif it.right:
                it.right()
        elif k == "left" and it.left:
            it.left()
        elif k == "right" and it.right:
            it.right()
        elif k.isdigit() and 1 <= int(k) <= len(items):
            self.sel = int(k) - 1
        else:
            super().key(k)

    def hint(self):
        return "↑↓ naviga · enter seleziona · ←→ modifica · esc indietro"


class MainScreen(MenuScreen):
    title = "Menu"

    def items(self):
        a, cfg = self.app, self.cfg
        n = len(active(cfg))
        return [
            Item("Messaggi", lambda: dim(f"{n} di 8 nel ciclo"), lambda: a.push(MessagesScreen(a))),
            Item("Luminosità", self.brightness_bar, left=lambda: a.cycle_brightness(-1),
                 right=lambda: a.cycle_brightness(1)),
            Item("Larghezza display", lambda: f"{cfg['display_width']} colonne",
                 left=lambda: a.set_width(cfg["display_width"] - 1),
                 right=lambda: a.set_width(cfg["display_width"] + 1)),
            Item("Prova larghezza", lambda: dim("mostra sul display un rettangolo largo come impostato"),
                 a.test_width),
            Item("Anteprima", lambda: dim("tutti i messaggi nel ciclo"), lambda: a.push(PreviewScreen(a))),
            Item("Simboli", lambda: dim(f"{len(ICONS)} icone da usare nei messaggi con :nome:"),
                 lambda: a.push(SymbolsScreen(a))),
            Item("Invia al display", lambda: fg("modifiche da inviare", ACC) if a.dirty else dim("già aggiornato"),
                 a.send),
            Item("Esci", None, a.quit),
        ]

    def brightness_bar(self):
        b = self.cfg["brightness"]
        k = BRIGHTNESS.index(b) + 1
        return fg("▰" * k, ACC) + dim("▱" * (4 - k)) + f" {b}%"

    def extra(self, w):
        out = ["", dim("  Nel ciclo")]
        msgs = active(self.cfg)
        if not msgs:
            out.append(dim("  nessun messaggio: apri Messaggi e premi spazio per attivarne uno"))
        width = self.cfg["display_width"]
        for i, m in msgs:
            warn = fg("  ⚠ più largo del display", RED) if overflow(m, width) else ""
            out.append(f"  {fg(MODES[m['mode']][0], ACC)} {dim(str(i + 1))}  "
                       f"{ellipsis(m['text'], max(10, w - 50))}  {dim(font_label(m['font']))}{warn}")
        used = len(build_payload(self.cfg, stamp=False)[0])
        k = min(16, round(used / MAX_BYTES * 16))
        out += ["", dim("  Memoria ") + fg("▰" * k, ACC if used <= MAX_BYTES else RED) + dim("▱" * (16 - k))
                + dim(f" {used}/{MAX_BYTES} byte")]
        return out

    def key(self, k):
        if k in ("esc", "backspace"):
            self.app.set_status("Per uscire: ctrl+c oppure /esci", GRAY)
        else:
            super().key(k)

    def hint(self):
        return "↑↓ naviga · enter seleziona · ←→ modifica · / comandi · ctrl+c esci"


class MessagesScreen(Screen):
    title = "Messaggi"

    def __init__(self, app, sel=0):
        super().__init__(app)
        self.sel = sel

    def lines(self, w):
        out = []
        tw = max(10, min(34, w - 64))
        width = self.cfg["display_width"]
        for i, m in enumerate(self.cfg["messages"]):
            sel = i == self.sel
            on = m["enabled"] and m["text"].strip()
            dot = fg("●", GREEN) if on else dim("○")
            text = ellipsis(m["text"], tw) if m["text"] else dim("(vuoto)")
            text = pad(fg(text, ACC) if sel else (text if on else dim(text)), tw)
            fx = f"{MODES[m['mode']][0]} {MODES[m['mode']][1]} · v{m['speed']} · {font_label(m['font'])}"
            fx += " · lampeggio" if m["blink"] else ""
            fx += " · bordo" if m["ants"] else ""
            warn = fg("  ⚠ troppo largo", RED) if m["text"] and overflow(m, width) else ""
            out.append(f"  {fg('❯', ACC) if sel else ' '} {dim(str(i + 1))}  {dot}  {text}  {dim(fx)}{warn}")
        m = self.cfg["messages"][self.sel]
        text = self.app.editing_text(self.sel)  # anteprima dal vivo mentre scrivi
        out += ["", dim(f"  Anteprima messaggio {self.sel + 1}")]
        out += self.app.msg_preview(m, w, text)
        out += self.app.overflow_lines(m, self.sel, text)
        return out

    def key(self, k):
        msgs = self.cfg["messages"]
        if k in ("up", "ctrl-p"):
            self.sel = (self.sel - 1) % 8
        elif k in ("down", "ctrl-n", "tab"):
            self.sel = (self.sel + 1) % 8
        elif k in "12345678" and len(k) == 1:
            self.sel = int(k) - 1
        elif k == " ":
            m = msgs[self.sel]
            if not m["text"].strip():
                self.app.set_status(f"Il messaggio {self.sel + 1} è vuoto: premi e per scriverlo", GRAY)
            else:
                m["enabled"] = not m["enabled"]
                self.app.changed()
        elif k == "enter":
            self.app.push(MessageScreen(self.app, self.sel))
        elif k == "e":
            self.app.edit_message(self.sel)
        elif k == "f":
            self.app.push(MessageScreen(self.app, self.sel))
            self.app.push(FontScreen(self.app, self.sel))
        elif k in ("move-up", "move-down"):
            j = self.sel + (-1 if k == "move-up" else 1)
            if 0 <= j < 8:
                msgs[self.sel], msgs[j] = msgs[j], msgs[self.sel]
                self.sel = j
                self.app.changed()
        else:
            super().key(k)

    def hint(self):
        return f"↑↓ naviga · spazio ciclo · e scrivi · f font · enter dettagli · {MOVE_HINT} · esc"


class MessageScreen(MenuScreen):
    def __init__(self, app, idx):
        super().__init__(app)
        self.idx = idx
        self.title = f"Messaggio {idx + 1}"

    @property
    def m(self):
        return self.cfg["messages"][self.idx]

    def set(self, key, value):
        self.m[key] = value
        self.app.changed()
        if key == "mode":
            self.app.warn_overflow(self.idx)

    def items(self):
        m, a = self.m, self.app
        width = self.cfg["display_width"]

        def effect():
            warn = fg("  ⚠ più largo del display", RED) if m["text"] and overflow(m, width) else ""
            return f"{MODES[m['mode']][0]} {MODES[m['mode']][1]}{warn}"

        return [
            Item("Testo", lambda: (ellipsis(m["text"], 60) if m["text"] else dim("(vuoto)")),
                 lambda: a.edit_message(self.idx)),
            Item("Nel ciclo", lambda: yesno(m["enabled"]), self.toggle_enabled, toggle=True,
                 left=self.toggle_enabled, right=self.toggle_enabled),
            Item("Effetto", effect,
                 left=lambda: self.set("mode", (m["mode"] - 1) % len(MODES)),
                 right=lambda: self.set("mode", (m["mode"] + 1) % len(MODES))),
            Item("Velocità", lambda: speed_bar(m["speed"]),
                 left=lambda: self.set("speed", max(1, m["speed"] - 1)),
                 right=lambda: self.set("speed", min(8, m["speed"] + 1))),
            Item("Font", lambda: font_label(m["font"]), lambda: a.push(FontScreen(a, self.idx))),
            Item("Lampeggio", lambda: yesno(m["blink"]), lambda: self.set("blink", not m["blink"]), toggle=True,
                 left=lambda: self.set("blink", not m["blink"]), right=lambda: self.set("blink", not m["blink"])),
            Item("Bordo animato", lambda: yesno(m["ants"]), lambda: self.set("ants", not m["ants"]), toggle=True,
                 left=lambda: self.set("ants", not m["ants"]), right=lambda: self.set("ants", not m["ants"])),
            Item("Svuota messaggio", None, self.clear),
        ]

    def toggle_enabled(self):
        if not self.m["text"].strip():
            self.app.set_status("Scrivi prima un testo", GRAY)
        else:
            self.set("enabled", not self.m["enabled"])

    def clear(self):
        self.m.update(text="", enabled=False)
        self.app.changed()
        self.app.set_status(f"Messaggio {self.idx + 1} svuotato", GRAY)

    def extra(self, w):
        text = self.app.editing_text(self.idx)
        if text is None:
            text = self.m["text"]
        n = len(msg_cols(self.m, text or " "))
        out = ["", dim(f"  Anteprima · {n} colonne · display {self.cfg['display_width']}")]
        return out + self.app.msg_preview(self.m, w, text) + self.app.overflow_lines(self.m, self.idx, text)


class FontScreen(MenuScreen):
    title = "Font"

    def __init__(self, app, idx):
        super().__init__(app)
        self.idx = idx
        self.fonts = list_fonts()

    @property
    def m(self):
        return self.cfg["messages"][self.idx]

    @property
    def f(self):
        return self.m["font"]

    def set(self, key, value):
        self.f[key] = value
        self.app.changed()
        self.app.warn_overflow(self.idx)

    def set_size(self, delta):
        fs = font_settings(self.f)
        self.f["sizes"][fs["id"]] = max(5, min(40, fs["size"] + delta))
        self.app.changed()
        self.app.warn_overflow(self.idx)

    def cycle_font(self, d):
        ids = [f[0] for f in self.fonts]
        cur = self.f["id"]
        i = ids.index(cur) if cur in ids else 0
        self.set("id", ids[(i + d) % len(ids)])

    def reset_size(self):
        self.f["sizes"].pop(self.f["id"], None)
        self.app.changed()

    def apply_all(self):
        for i, m in enumerate(self.cfg["messages"]):
            if i != self.idx:
                m["font"] = copy.deepcopy(self.f)
        self.app.changed()
        bad = overflowing(self.cfg)
        msg = f"Font {font_label(self.f)} applicato a tutti i messaggi"
        self.app.set_status(msg + (f" · attenzione: {', '.join(map(str, bad))} ora più larghi del display"
                                   if bad else ""), ACC if bad else GREEN)

    def items(self):
        f, a = self.f, self.app
        builtin = lambda: font_settings(f)["id"] == BUILTIN  # noqa: E731
        return [
            Item("Font", lambda: font_name(font_settings(f)["id"]),
                 lambda: a.push(FontPicker(a, self.idx, self.fonts)),
                 left=lambda: self.cycle_font(-1), right=lambda: self.cycle_font(1)),
            Item("Dimensione", lambda: "fissa (5×7)" if builtin() else f"{font_settings(f)['size']} px",
                 left=lambda: self.set_size(-1), right=lambda: self.set_size(1), disabled=builtin),
            Item("Posizione verticale", lambda: f"{f['offset']:+d}",
                 left=lambda: self.set("offset", max(-6, f["offset"] - 1)),
                 right=lambda: self.set("offset", min(6, f["offset"] + 1))),
            Item("Spaziatura", lambda: f"{f['spacing']} px",
                 left=lambda: self.set("spacing", max(0, f["spacing"] - 1)),
                 right=lambda: self.set("spacing", min(6, f["spacing"] + 1))),
            Item("Grassetto", lambda: yesno(f["bold"]), lambda: self.set("bold", not f["bold"]), toggle=True,
                 left=lambda: self.set("bold", not f["bold"]), right=lambda: self.set("bold", not f["bold"])),
            Item("Ripristina dimensione automatica", None, self.reset_size, disabled=builtin),
            Item("Usa questo font per tutti i messaggi", None, self.apply_all),
        ]

    def extra(self, w):
        text = self.m["text"] or SAMPLE
        out = ["", dim(f"  Anteprima messaggio {self.idx + 1}" + ("" if self.m["text"] else " (testo di prova)"))]
        out += self.app.msg_preview(self.m, w, text) + self.app.overflow_lines(self.m, self.idx)
        out += ["", dim(f"  Puoi aggiungere altri font .ttf/.otf in {font_dirs()[1][0]} (i font pixel rendono meglio)")]
        return out


class FontPicker(Screen):
    title = "Scegli font"

    def __init__(self, app, idx, fonts):
        super().__init__(app)
        self.idx, self.fonts = idx, fonts
        ids = [f[0] for f in fonts]
        cur = self.m["font"]["id"]
        self.sel = ids.index(cur) if cur in ids else 0

    @property
    def m(self):
        return self.cfg["messages"][self.idx]

    def lines(self, w):
        _, h = self.app.term_size
        rows = max(4, min(len(self.fonts), h - 30))
        top = max(0, min(self.sel - rows // 2, len(self.fonts) - rows))
        cur = self.m["font"]["id"]
        out = []
        for i in range(top, top + rows):
            fid, name = self.fonts[i]
            sel = i == self.sel
            mark = fg(" ✓", GREEN) if fid == cur else ""
            out.append(f"  {fg('❯', ACC) if sel else ' '} {fg(name, ACC) if sel else name}{mark}")
        out.append(dim(f"  {self.sel + 1}/{len(self.fonts)}"))
        fs = font_settings(self.m["font"], self.fonts[self.sel][0])
        out += ["", dim(f"  Anteprima messaggio {self.idx + 1}" + (f" · {fs['size']} px" if fs["size"] else ""))]
        out += self.app.msg_preview(self.m, w, self.m["text"] or SAMPLE, fs)
        return out

    def key(self, k):
        n = len(self.fonts)
        if k in ("up", "ctrl-p"):
            self.sel = (self.sel - 1) % n
        elif k in ("down", "ctrl-n", "tab"):
            self.sel = (self.sel + 1) % n
        elif k == "pgup":
            self.sel = max(0, self.sel - 10)
        elif k == "pgdn":
            self.sel = min(n - 1, self.sel + 10)
        elif k == "enter":
            self.m["font"]["id"] = self.fonts[self.sel][0]
            self.app.changed()
            self.app.set_status(f"Messaggio {self.idx + 1}: font {self.fonts[self.sel][1]}", GREEN)
            self.app.warn_overflow(self.idx)
            self.app.pop()
        else:
            super().key(k)

    def hint(self):
        return "↑↓ scorri con anteprima · enter scegli · esc annulla"


class PreviewScreen(Screen):
    title = "Anteprima"

    def __init__(self, app):
        super().__init__(app)
        self.offset = 0

    def lines(self, w):
        msgs = active(self.cfg)
        if not msgs:
            return [dim("  Nessun messaggio nel ciclo.")]
        width = self.cfg["display_width"]
        out = []
        for i, m in msgs:
            cols = msg_cols(m)
            icon, name = MODES[m["mode"]]
            info = f"{name} · v{m['speed']} · {font_label(m['font'])} · {len(cols)} colonne"
            warn = fg(f"  ⚠ più largo del display ({width})", RED) if overflow(m, width) else ""
            out.append(f"  {fg(icon, ACC)} {bold(str(i + 1))}  {dim(info)}{warn}")
            limit = width - self.offset if m["mode"] in VERTICAL else None
            out += ["  " + l for l in led_lines(cols[self.offset:], w - 4, limit)]
            out.append("")
        return out

    def key(self, k):
        if k == "right":
            self.offset += 8
        elif k == "left":
            self.offset = max(0, self.offset - 8)
        elif k == "home":
            self.offset = 0
        else:
            super().key(k)

    def hint(self):
        return "←→ scorri · esc indietro"


class SymbolsScreen(Screen):
    title = "Simboli"

    def __init__(self, app):
        super().__init__(app)
        self.top = 0

    def lines(self, w):
        intro = [dim("  Scrivi :nome: nel testo di un messaggio (mentre scrivi : compaiono i suggerimenti),"),
                 dim("  oppure incolla l'emoji: ❤️ ⭐ 😀 ☀️ 🍕 👍 … vengono convertite nel simbolo pixel."), ""]
        grid = symbols_grid(w - 4)
        _, h = self.app.term_size
        room = max(8, h - 22)
        self.top = max(0, min(self.top, len(grid) - room))
        return intro + ["  " + l for l in grid[self.top:self.top + room]]

    def key(self, k):
        if k in ("down", "pgdn"):
            self.top += 8 if k == "pgdn" else 2
        elif k in ("up", "pgup"):
            self.top = max(0, self.top - (8 if k == "pgup" else 2))
        else:
            super().key(k)

    def hint(self):
        return "↑↓ scorri · esc indietro"


# ─────────────────────────────── applicazione ───────────────────────────────

COMMANDS = [
    ("/invia", "Invia al display i messaggi nel ciclo"),
    ("/messaggi", "Modifica gli 8 messaggi"),
    ("/font", "Font di un messaggio: /font 3"),
    ("/anteprima", "Anteprima dei messaggi nel ciclo"),
    ("/luminosita", "Imposta la luminosità: /luminosita 25|50|75|100"),
    ("/larghezza", "Colonne visibili del display: /larghezza 48"),
    ("/simboli", "Galleria dei simboli da usare con :nome:"),
    ("/1", "Scrivi il messaggio 1 (da /1 a /8): /3 Buongiorno"),
    ("/esci", "Esci da Scroller"),
]


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.stack = [MainScreen(self)]
        self.status = None
        self.editor = None  # LineEditor attivo nel riquadro in basso
        self.ectx = None    # {"kind": "cmd"|"text", "label", "target", "submit"}
        self.csel = 0
        self.running = True
        self.device = find_device()
        self.usb_at = time.time()
        self.term_size = (80, 24)
        self.dirty = payload_hash(cfg) != cfg.get("sent_hash")

    # navigazione
    @property
    def screen(self):
        return self.stack[-1]

    def push(self, s):
        self.stack.append(s)

    def pop(self):
        if len(self.stack) > 1:
            self.stack.pop()

    def goto(self, s):
        self.stack = [self.stack[0], s]

    def open_font(self, idx):
        self.stack = [self.stack[0], MessagesScreen(self, idx), MessageScreen(self, idx), FontScreen(self, idx)]

    def current_idx(self):
        for s in reversed(self.stack):
            if hasattr(s, "idx"):
                return s.idx
            if isinstance(s, MessagesScreen):
                return s.sel
        return 0

    def quit(self):
        self.running = False

    # stato
    def set_status(self, text, color=GREEN):
        self.status = (text, color)

    def changed(self):
        save_config(self.cfg)
        self.dirty = payload_hash(self.cfg) != self.cfg.get("sent_hash")

    def cycle_brightness(self, d):
        i = BRIGHTNESS.index(self.cfg["brightness"])
        self.cfg["brightness"] = BRIGHTNESS[max(0, min(3, i + d))]
        self.changed()

    def set_width(self, n):
        self.cfg["display_width"] = max(8, min(128, n))
        self.changed()

    def device_error(self, e):
        self.device = find_device()
        self.set_status(e.short + ("\n" + e.detail if e.detail else ""), RED)

    def send(self):
        data, msgs = build_payload(self.cfg)
        if not msgs:
            return self.set_status("Nessun messaggio nel ciclo da inviare", RED)
        if len(data) > MAX_BYTES:
            return self.set_status(f"Testo troppo lungo: {len(data)} byte (max {MAX_BYTES})", RED)
        try:
            send_payload(data)
        except DeviceError as e:
            return self.device_error(e)
        self.cfg["sent_hash"] = payload_hash(self.cfg)
        self.changed()
        self.device = self.device or find_device()
        bad = overflowing(self.cfg)
        text = f"Inviati {len(msgs)} messaggi al display ({len(data)} byte)"
        if bad:
            text += f" · attenzione: {', '.join(map(str, bad))} più larghi del display, verranno tagliati"
        self.set_status(text, ACC if bad else GREEN)

    def test_width(self):
        w = self.cfg["display_width"]
        rect = [0x7FF] + [0x401] * (w - 2) + [0x7FF]
        try:
            send_payload(assemble([(rect, 4, 5, False, False)], self.cfg["brightness"]))
        except DeviceError as e:
            return self.device_error(e)
        self.cfg["sent_hash"] = ""  # sul display ora c'è il rettangolo, non i messaggi
        self.changed()
        self.set_status(f"Sul display c'è un rettangolo di {w} colonne. Se manca un lato verticale riduci la "
                        "larghezza,\nse restano colonne spente ai lati aumentala. Poi /invia per rimettere i messaggi.",
                        ACC)

    # anteprime e avvisi
    def preview(self, text, w, fs, limit=None):
        cols = render(text, fs)
        avail = max(8, w - 4)
        out = ["  " + l for l in led_lines(cols, avail, limit)]
        if limit is not None and limit < len(cols) and limit < avail:
            out.append("  " + " " * limit + fg("▲ qui finisce il display", RED))
        if len(cols) > avail:
            out.append(dim(f"  … altre {len(cols) - avail} colonne"))
        return out

    def msg_preview(self, m, w, text=None, fs=None):
        text = m["text"] if text is None else text
        limit = self.cfg["display_width"] if m["mode"] in VERTICAL else None
        return self.preview(text or " ", w, fs or font_settings(m["font"]), limit)

    def overflow_lines(self, m, idx, text=None):
        width = self.cfg["display_width"]
        n = overflow(m, width, text)
        if not n:
            return []
        return ["",
                fg(f"  ⚠ Il messaggio {idx + 1} è largo {n} colonne ma il display ne mostra {width}:", RED),
                fg(f"    con «{MODES[m['mode']][1]}» il testo non scorre di lato e la parte oltre il bordo "
                   "non si vedrà.", RED),
                dim("    Accorcia il testo, riduci font o spaziatura, oppure scegli uno scorrimento orizzontale.")]

    def warn_overflow(self, idx):
        m, width = self.cfg["messages"][idx], self.cfg["display_width"]
        n = overflow(m, width) if m["text"] else 0
        if n:
            self.set_status(f"Attenzione: il messaggio {idx + 1} è largo {n} colonne, il display ne mostra {width}: "
                            f"scorrendo {MODES[m['mode']][1][7:]} verrà tagliato", ACC)

    # editor di testo nel riquadro di input
    def editing_text(self, target):
        if self.editor and self.ectx["kind"] == "text" and self.ectx["target"] == target:
            return self.editor.text
        return None

    def edit_message(self, idx):
        def submit(text):
            m = self.cfg["messages"][idx]
            if text.strip() and not m["text"].strip():
                m["enabled"] = True
            m["text"] = text
            if not text.strip():
                m["enabled"] = False
            self.changed()
            self.set_status(f"Messaggio {idx + 1} salvato", GREEN)
            self.warn_overflow(idx)
        self.open_editor("text", f"Testo del messaggio {idx + 1}", self.cfg["messages"][idx]["text"], idx, submit)

    def open_editor(self, kind, label, text, target=None, submit=None):
        self.editor = LineEditor(text)
        self.ectx = {"kind": kind, "label": label, "target": target, "submit": submit}
        self.csel = 0
        self.isugg_off = False

    def icon_suggestions(self):
        """(posizione dei ':', [simboli]) se il cursore è dopo un ':nome' parziale nel testo di un messaggio."""
        if not self.editor or self.ectx["kind"] != "text" or self.isugg_off:
            return None, []
        before = "".join(self.editor.buf[:self.editor.pos])
        m = re.search(r"(?:^|\s):([a-zàèéìòù_]*)$", before)
        if not m:
            return None, []
        pre = m.group(1)
        return m.start(1) - 1, [k for k, names in ICON_NAMES.items() if any(n.startswith(pre) for n in names)]

    def close_editor(self):
        self.editor = self.ectx = None

    # comandi
    def suggestions(self):
        word = self.editor.text.split(" ")[0]
        out = []
        for name, desc in COMMANDS:
            if name == "/1":
                if word == "/" or re.fullmatch(r"/[1-8]", word):
                    out.append((word if re.fullmatch(r"/[1-8]", word) else "/1", desc))
            elif name.startswith(word):
                out.append((name, desc))
        return out

    def run_command(self, text):
        cmd, _, arg = text.strip().partition(" ")
        arg = arg.strip()
        if cmd == "/invia":
            self.send()
        elif cmd == "/messaggi":
            self.goto(MessagesScreen(self))
        elif cmd == "/font":
            self.open_font(int(arg) - 1 if re.fullmatch(r"[1-8]", arg) else self.current_idx())
        elif cmd == "/anteprima":
            self.goto(PreviewScreen(self))
        elif cmd == "/simboli":
            self.goto(SymbolsScreen(self))
        elif cmd == "/esci":
            self.quit()
        elif cmd == "/luminosita":
            if arg.rstrip("%") in ("25", "50", "75", "100"):
                self.cfg["brightness"] = int(arg.rstrip("%"))
                self.changed()
                self.set_status(f"Luminosità {arg.rstrip('%')}%", GREEN)
            else:
                self.set_status("Uso: /luminosita 25|50|75|100", RED)
        elif cmd == "/larghezza":
            if arg.isdigit():
                self.set_width(int(arg))
                self.set_status(f"Larghezza display: {self.cfg['display_width']} colonne", GREEN)
            else:
                self.set_status("Uso: /larghezza 44", RED)
        elif re.fullmatch(r"/[1-8]", cmd):
            idx = int(cmd[1]) - 1
            self.goto(MessagesScreen(self, idx))
            if arg:
                self.cfg["messages"][idx].update(text=arg, enabled=True)
                self.changed()
                self.set_status(f"Messaggio {idx + 1} salvato e messo nel ciclo", GREEN)
                self.warn_overflow(idx)
            else:
                self.edit_message(idx)
        else:
            self.set_status(f"Comando sconosciuto: {cmd}", RED)

    def handle_editor(self, k):
        ed, ctx = self.editor, self.ectx
        if ctx["kind"] == "cmd":
            sugg = self.suggestions()
            if k in ("up", "down") and sugg:
                self.csel = (self.csel + (1 if k == "down" else -1)) % len(sugg)
                return
            if k == "tab" and sugg:
                name = sugg[min(self.csel, len(sugg) - 1)][0]
                self.editor = LineEditor(name + " ")
                return
            if k == "enter":
                text = ed.text.strip()
                exact = text.split(" ")[0] in [c[0] for c in COMMANDS] or re.match(r"/[1-8]\b", text)
                if not exact and sugg:
                    text = sugg[min(self.csel, len(sugg) - 1)][0]
                self.close_editor()
                if text != "/":
                    self.run_command(text)
                return
        else:
            start, sugg = self.icon_suggestions()
            if sugg and k in ("up", "down"):
                self.csel = (self.csel + (1 if k == "down" else -1)) % len(sugg)
                return
            if sugg and k in ("tab", "enter"):
                code = f":{sugg[min(self.csel, len(sugg) - 1)]}:"
                ed.buf[start:ed.pos] = list(code)
                ed.pos = start + len(code)
                self.csel = 0
                return
            if sugg and k == "esc":
                self.isugg_off = True  # chiude i suggerimenti, non l'editor
                return
        if k in ("esc", "ctrl-c"):
            self.close_editor()
        elif k == "enter":
            submit = ctx["submit"]
            text = ed.text
            self.close_editor()
            submit(text)
        elif ed.key(k):
            self.csel = 0
            self.isugg_off = False
            if ctx["kind"] == "cmd" and not ed.text:
                self.close_editor()

    def handle(self, k):
        if self.editor:
            return self.handle_editor(k)
        if k in ("ctrl-c", "ctrl-d"):
            return self.quit()
        if k == "/":
            self.status = None
            return self.open_editor("cmd", None, "/")
        self.status = None
        self.screen.key(k)

    # disegno
    def header(self, w, compact):
        if self.device:
            usb = fg("●", GREEN) + f" {device_label(self.device)} collegato"
        else:
            usb = fg("○", RED) + " non collegato"
        state = fg("● modifiche da inviare", ACC) if self.dirty else fg("✓", GREEN) + " aggiornato"
        if compact:
            return [f" {fg('✻', ACC)} {bold('Scroller')}  {dim('·')}  {usb}  {dim('·')}  {state}"]
        home = str(Path.home())
        return box([
            f"{fg('✻', ACC)} {bold('Scroller')} {dim('— display LED CH583')}",
            "",
            dim("  / per i comandi · ctrl+c per uscire"),
            "",
            f"  {dim('display ')} {usb}",
            f"  {dim('stato   ')} {state}",
            f"  {dim('config  ')} {dim(str(CONFIG_PATH).replace(home, '~'))}",
        ], min(w, 64), ACC)

    def draw(self, term):
        w, h = term.size()
        self.term_size = (w, h)
        top = self.header(w, compact=h < 34)
        crumbs = " › ".join(s.title for s in self.stack[1:]) or "Menu"
        top += ["", " " + bold(crumbs), ""]
        bottom = [""]
        if self.status:
            text, color = self.status
            for j, line in enumerate(text.split("\n")):
                bottom.append(f" {fg('⏺', color)} {line}" if j == 0 else f"   {line}")
            bottom.append("")
        if self.editor and self.ectx["kind"] == "text":
            bottom.append(" " + fg("✎ " + self.ectx["label"], ACC))
            bottom += box([fg(">", ACC) + " " + self.editor.render(w - 7)], w)
            _, sugg = self.icon_suggestions()
            if sugg:
                self.csel = min(self.csel, len(sugg) - 1)
                first = max(0, min(self.csel - 3, len(sugg) - 7))
                for i in range(first, min(len(sugg), first + 7)):
                    key = sugg[i]
                    line = f"  {pad(':' + key + ':', 16)}{ICONS[key][1]}  {' · '.join(ICON_NAMES[key][1:])}"
                    bottom.append(fg(line, ACC) if i == self.csel else dim(line))
                bottom.append(dim(f"  tab/enter inserisce il simbolo · esc chiude i suggerimenti · {len(sugg)} simbol{'o' if len(sugg) == 1 else 'i'}"))
            else:
                bottom.append(dim("  enter salva · esc annulla · ctrl+u cancella · :nome: per i simboli"))
        elif self.editor:
            bottom += box([fg(">", ACC) + " " + self.editor.render(w - 7)], w)
            sugg = self.suggestions()
            self.csel = min(self.csel, max(0, len(sugg) - 1))
            for i, (name, desc) in enumerate(sugg):
                line = f"  {pad(name, 14)}  {desc}"
                bottom.append(fg(line, ACC) if i == self.csel else dim(line))
            if not sugg:
                bottom.append(dim("  nessun comando corrispondente"))
        else:
            bottom += box([dim("> premi / per i comandi")], w)
            bottom.append(dim("  " + self.screen.hint()))
        body = self.screen.lines(w)
        room = h - len(top) - len(bottom)
        if len(body) > room:
            body = body[:max(0, room)]
        lines = [crop(l, w) for l in top + body + bottom]
        term.write("\x1b[H" + "\x1b[K\r\n".join(lines) + "\x1b[K\x1b[J")

    def run(self):
        with Terminal() as term:
            redraw, last_size = True, None
            try:
                while self.running:
                    if time.time() - self.usb_at > 2:  # rileva collegamento/scollegamento del display
                        dev = find_device()
                        redraw |= bool(dev) != bool(self.device)
                        self.device, self.usb_at = dev, time.time()
                    size = term.size()
                    if redraw or size != last_size:
                        last_size = size
                        self.draw(term)
                    keys = term.read_keys(0.3)
                    for k in keys:
                        self.handle(k)
                        if not self.running:
                            break
                    redraw = bool(keys)
            except KeyboardInterrupt:
                pass


# ─────────────────────────────── linea di comando ───────────────────────────

def cli_preview(cfg):
    w = os.get_terminal_size().columns if sys.stdout.isatty() else 120
    msgs = active(cfg)
    if not msgs:
        print("Nessun messaggio nel ciclo.")
    width = cfg["display_width"]
    for i, m in msgs:
        print(f"{i + 1}. {m['text']}  ({MODES[m['mode']][1]}, velocità {m['speed']}, {font_label(m['font'])})")
        limit = width if m["mode"] in VERTICAL else None
        for line in led_lines(msg_cols(m), w - 2, limit):
            print(" " + line)
        n = overflow(m, width)
        if n:
            print(f" ⚠ largo {n} colonne, il display ne mostra {width}: scorrendo in verticale verrà tagliato")
        print()


def cli_check(cfg):
    from importlib.metadata import PackageNotFoundError, version
    print(f"Sistema     {platform.system()} {platform.release()} ({platform.machine()})")
    print(f"Python      {platform.python_version()}  {sys.executable}")
    for pkg, _, why in DEPS:
        try:
            v = version(pkg)
        except PackageNotFoundError:
            v = "?"
        print(f"Pacchetto   ✓ {pkg} {v}  ({why})")
    d = find_device()
    if not d:
        print(f"Display     ✗ non trovato (cerco {', '.join(f'{v:04x}:{p:04x}' for v, p in KNOWN_DEVICES)})")
    else:
        print(f"Display     ✓ {device_label(d)}  {d['vendor_id']:04x}:{d['product_id']:04x}")
        try:
            open_device().close()
            print("Accesso     ✓ il display si apre correttamente")
        except DeviceError as e:
            print(f"Accesso     ✗ {e.short}\n\n{e.detail}")
    print(f"Config      {CONFIG_PATH}")
    print(f"Font        {len(list_fonts())} disponibili · altri .ttf/.otf in {font_dirs()[1][0]}")
    print(f"Larghezza   {cfg['display_width']} colonne")


def main():
    if IS_WIN:
        enable_windows_ansi()
    p = argparse.ArgumentParser(prog="scroller", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("invia", help="invia al display i messaggi salvati")
    sub.add_parser("anteprima", help="stampa l'anteprima dei messaggi nel ciclo")
    sub.add_parser("controlla", help="verifica pacchetti Python, display e permessi USB")
    sub.add_parser("simboli", help="mostra i simboli da usare nei messaggi con :nome:")
    s = sub.add_parser("imposta", help="imposta il testo di un messaggio e lo mette nel ciclo")
    s.add_argument("n", type=int, choices=range(1, 9), metavar="N")
    s.add_argument("testo", nargs="+")
    a = p.parse_args()
    cfg = load_config()

    if a.cmd == "invia":
        data, msgs = build_payload(cfg)
        if not msgs:
            sys.exit("Nessun messaggio nel ciclo.")
        if len(data) > MAX_BYTES:
            sys.exit(f"Testo troppo lungo: {len(data)} byte (max {MAX_BYTES}).")
        try:
            send_payload(data)
        except DeviceError as e:
            sys.exit(e.short + ("\n\n" + e.detail if e.detail else ""))
        cfg["sent_hash"] = payload_hash(cfg)
        save_config(cfg)
        print(f"Inviati {len(msgs)} messaggi ({len(data)} byte).")
        for n in overflowing(cfg):
            print(f"⚠ Il messaggio {n} è più largo del display ({cfg['display_width']} colonne) "
                  "e scorrendo in verticale verrà tagliato.", file=sys.stderr)
    elif a.cmd == "anteprima":
        cli_preview(cfg)
    elif a.cmd == "controlla":
        cli_check(cfg)
    elif a.cmd == "simboli":
        w = os.get_terminal_size().columns if sys.stdout.isatty() else 100
        print("Scrivi :nome: nel testo di un messaggio, oppure incolla l'emoji corrispondente.\n")
        for line in symbols_grid(w - 2):
            print(" " + line)
    elif a.cmd == "imposta":
        cfg["messages"][a.n - 1].update(text=" ".join(a.testo), enabled=True)
        save_config(cfg)
        print(f"Messaggio {a.n} salvato. Usa 'scroller invia' per inviarlo.")
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            sys.exit("L'interfaccia interattiva richiede un terminale.")
        App(cfg).run()


if __name__ == "__main__":
    main()
