# -*- coding: utf-8 -*-
"""Дизассемблер участков SentientSands.dll с разрешением имён импорта.

Сам бинарник в репозиторий не входит (см. CHANGES_RU.md) — укажи путь к своей
копии через --dll или переменную окружения SS_DLL.

Зачем: нативная часть мода не пересобирается, и единственный способ узнать, что
делает тег действия, — прочитать байты. Ручное чтение hex-дампа один раз уже
привело к неверному выводу: у SPAWN_ITEM три места вызова createItem, и разбор
попал в аварийную ветку («вещь падает на землю») вместо основной («вещь в
инвентарь»). Отсюда два режима:

    disasm.py 23470 23590        участок по RVA — что делает код
    disasm.py --calls Inventory  ВСЕ вызовы импорта по имени — где ещё он есть

Второй режим обязателен перед любым выводом о поведении: линейный проход по
всей .text расходится на данных, поэтому вызовы ищутся байтовым шаблоном
`FF 15 disp32`, а ложные срабатывания отсекаются тем, что цель обязана быть
слотом таблицы импорта.

Требуется capstone:  server/python/python.exe -m pip install capstone
"""

import argparse
import os
import re
import struct
import sys

try:
    import capstone
except ImportError:                                              # pragma: no cover
    sys.exit("нужен capstone: server/python/python.exe -m pip install capstone")

try:                                  # русские комментарии в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, OSError):     # pragma: no cover
    pass

IMAGE_BASE = 0x180000000

DEFAULT_DLL = os.environ.get("SS_DLL") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "SentientSands.dll",
)


# ─── PE ──────────────────────────────────────────────────────────────────────

class Image:
    """Секции и таблица импорта разобранного PE."""

    def __init__(self, path: str):
        with open(path, "rb") as f:
            self.data = f.read()
        self.path = path
        self._parse_sections()
        self._parse_imports()

    def _parse_sections(self):
        data = self.data
        pe = struct.unpack_from("<I", data, 0x3C)[0]
        n_sections = struct.unpack_from("<H", data, pe + 6)[0]
        opt_size = struct.unpack_from("<H", data, pe + 20)[0]
        self.pe, self.opt_size = pe, opt_size
        sec_off = pe + 24 + opt_size
        self.sections = []
        for i in range(n_sections):
            o = sec_off + i * 40
            name = data[o:o + 8].rstrip(b"\0").decode("ascii", "replace")
            vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, o + 8)
            self.sections.append((name, vaddr, vsize, rawptr, rawsize))

    def _parse_imports(self):
        data = self.data
        magic = struct.unpack_from("<H", data, self.pe + 24)[0]
        dd = self.pe + 24 + (112 if magic == 0x20B else 96)
        imp_rva = struct.unpack_from("<I", data, dd + 8)[0]

        self.iat = {}
        o = self.offset(imp_rva)
        if o is None:
            return
        while True:
            oft, _, _, name_rva, first_thunk = struct.unpack_from("<IIIII", data, o)
            if oft == 0 and first_thunk == 0:
                break
            dll_name = self.cstr(name_rva, 60)
            t = self.offset(oft or first_thunk)
            slot = first_thunk
            while True:
                entry = struct.unpack_from("<Q", data, t)[0]
                if entry == 0:
                    break
                if not (entry >> 63):          # не импорт по ординалу
                    self.iat[slot] = (dll_name, self.cstr(entry + 2, 200))
                t += 8
                slot += 8
            o += 20

    def offset(self, rva):
        """Файловое смещение по RVA, либо None."""
        for _, vaddr, vsize, rawptr, rawsize in self.sections:
            if vaddr <= rva < vaddr + max(vsize, rawsize):
                return rawptr + (rva - vaddr)
        return None

    def cstr(self, rva, limit=120):
        off = self.offset(rva)
        if off is None:
            return ""
        end = self.data.find(b"\0", off, off + limit)
        return self.data[off:end if end >= 0 else off + limit].decode("utf-8", "replace")

    def section(self, name):
        for s in self.sections:
            if s[0] == name:
                return s
        raise KeyError(name)


# ─── режим 1: участок по RVA ─────────────────────────────────────────────────

def _printable(text: str) -> bool:
    return len(text) > 3 and all(32 <= ord(c) < 127 or c in "\n\t" for c in text[:40])


def show(img: Image, start_rva: int, end_rva: int):
    off = img.offset(start_rva)
    if off is None:
        sys.exit(f"RVA {start_rva:#x} вне секций")
    md = capstone.Cs(capstone.CS_ARCH_X86, capstone.CS_MODE_64)
    md.detail = True
    code = img.data[off:off + (end_rva - start_rva)]
    for ins in md.disasm(code, IMAGE_BASE + start_rva):
        rva = ins.address - IMAGE_BASE
        note = ""
        if ins.mnemonic == "call" and ins.op_str.startswith("qword ptr [rip"):
            target = ins.address + ins.size + ins.operands[0].mem.disp - IMAGE_BASE
            note = f"   ; {img.iat[target][1]}" if target in img.iat else f"   ; IAT {target:#x}"
        elif ins.mnemonic == "lea" and "rip" in ins.op_str:
            target = ins.address + ins.size + ins.operands[1].mem.disp - IMAGE_BASE
            s = img.cstr(target)
            if _printable(s):
                note = f'   ; "{s[:70]}"'
        print(f"  {rva:#08x}  {ins.mnemonic:<7} {ins.op_str}{note}")


# ─── режим 2: все вызовы импорта по имени ────────────────────────────────────

def find_calls(img: Image, pattern: str):
    """Каждый `call qword ptr [rip+disp]`, чья цель — слот импорта под шаблон."""
    rx = re.compile(pattern)
    _, vaddr, vsize, rawptr, _ = img.section(".text")
    data = img.data
    hits = []
    for off in range(rawptr, rawptr + vsize - 6):
        if data[off] != 0xFF or data[off + 1] != 0x15:
            continue
        disp = struct.unpack_from("<i", data, off + 2)[0]
        rva = vaddr + (off - rawptr)
        name = img.iat.get(rva + 6 + disp, ("", ""))[1]
        if name and rx.search(name):
            hits.append((rva, name))
    return hits


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", nargs="?", help="начальный RVA, шестнадцатеричный")
    ap.add_argument("end", nargs="?", help="конечный RVA, шестнадцатеричный")
    ap.add_argument("--dll", default=DEFAULT_DLL, help="путь к SentientSands.dll")
    ap.add_argument("--calls", metavar="RE",
                    help="показать все вызовы импорта, чьё имя подходит под шаблон")
    args = ap.parse_args()

    if not os.path.isfile(args.dll):
        sys.exit(f"не найден бинарник: {args.dll}\n"
                 f"укажи --dll или задай SS_DLL")

    img = Image(args.dll)
    print(f"{os.path.basename(args.dll)}: секций {len(img.sections)}, "
          f"импортов {len(img.iat)}\n")

    if args.calls:
        for rva, name in find_calls(img, args.calls):
            print(f"{rva:#08x}  {name[:110]}")
        return
    if not (args.start and args.end):
        ap.error("нужны start и end, либо --calls")
    show(img, int(args.start, 16), int(args.end, 16))


if __name__ == "__main__":
    main()
