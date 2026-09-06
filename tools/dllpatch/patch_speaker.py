# -*- coding: utf-8 -*-
"""Бинарный патч SentientSands.dll: говорит выделенный игроком персонаж.

Штатно мод везде берёт playerCharacters[0] — первого в отряде 1. Патч меняет
это умолчание на PlayerInterface::selectedCharacter в пяти местах:

  1. пузырь речи и разворот NPC            (ProcessMessageQueue)
  2. Player's sheet -> /context            (хук обновления)
  3. Player's sheet -> /context            (фоновый поток)
  4. имя в окне чата и в поле /chat        (путь по хоткею)
  5. имя в окне чата и в поле /chat        (путь по клику)

Разбор префикса ``PLAYER_SAY: <Имя>: текст`` не трогается и по-прежнему
перекрывает выбор, если имя указано явно.

Вторая, независимая правка — три строки в ``.rdata``: мод ищет класс качества
оружия по захардкоженным английским именам («Mk I», «Edge Type 1/3»), а в
русской игре эти объекты названы «Мк I» и «Тип лезвия 1/3». Из-за промаха
``createItem`` возвращает NULL, и SPAWN_ITEM не может создать ни одного
оружия. Подробности — в README.md.

    patch_speaker.py --verify     сверить состояние, ничего не писать
    patch_speaker.py --disasm     показать сгенерированный машинный код
    patch_speaker.py --apply      применить (всегда с эталона, идемпотентно)
    patch_speaker.py --revert     вернуть эталон
"""
import hashlib
import os
import struct
import sys

# ─── цели ──────────────────────────────────────────────────────────────────
GAME_DLL = (r"E:\SteamLibrary\steamapps\common\Kenshi\mods\SentientSands"
            r"\SentientSands.dll")
PROJ_DLL = r"D:\Project VSC\3771003618\SentientSands.dll"
ORIGINAL = r"D:\Project VSC\_dll_backup\SentientSands.dll.orig"
SHA256_ORIG = "f5d526dca4f6c57f70381cbabbe418f1c4c273fa5831945b3d0608420314fdac"

# ─── константы бинарника ───────────────────────────────────────────────────
TEXT_VA, TEXT_RAW = 0x1000, 0x400        # .text: смещение в файле = RVA - 0xC00
CAVE_BASE = 0x060D4C                     # нулевой паддинг в хвосте .text
CAVE_LIMIT = 0x060E00                    # дальше в файле данных нет (180 байт)

IAT_GET_CHARACTER = 0x061300             # KenshiLib!hand::getCharacter
OFF_SELECTED = 0x0F0                     # PlayerInterface::selectedCharacter
OFF_PC_STUFF = 0x2C0                     # playerCharacters.stuff
OFF_PLAYER = 0x580                       # GameWorld::player

RAX, RCX, RBX, RSP, R13 = 0, 1, 3, 4, 13

RDATA_VA, RDATA_RAW = 0x061000, 0x060200  # .rdata: файл = RVA - 0xE00

# Класс качества оружия мод ищет подстрокой в ЛОКАЛИЗОВАННОМ имени GameData,
# а сравнивает с английским литералом — в русской игре не совпадает никогда.
# Пишем русскую подстроку: она обязана влезть в слот вместе с NUL.
# Оружие и броня падают в отдельную ветку, где мод пытается сам подобрать
# «material spec» и производителя. Свои ссылки предмета он читать не умеет
# (помощник 0x023D80 не отработал ни разу за весь SDK-лог), поэтому в
# createItem уезжают первые попавшиеся объекты нужного типа, и тот возвращает
# NULL. Обычные предметы этой ветки не видят: для них mod передаёт NULL/NULL —
# и они создаются нормально. Отправляем оружие и броню туда же.
RAW = [
    dict(name="оружие и броня создаются как обычные предметы",
         where="диспетчер SPAWN_ITEM, отбор по типу предмета",
         rva=0x02298A, end=0x0229A2, jump_to=0x023014,
         orig=("83f90274" "1383f903" "740e83f9" "6b740983"
               "f96f0f85" "72060000")),
]

STRINGS = [
    dict(rva=0x065028, slot=16, was=b"Edge Type 3", now="лезвия 3",
         note="класс качества оружия, навык > 80"),
    dict(rva=0x065050, slot=16, was=b"Edge Type 1", now="лезвия 1",
         note="класс качества оружия, навык > 60"),
    dict(rva=0x06506C, slot=12, was=b"Mk I", now="Мк I",
         note="класс качества оружия, навык > 40"),
]


NOP = bytes([0x90])


def rva2off(rva):
    return rva - TEXT_VA + TEXT_RAW


def rdata2off(rva):
    return rva - RDATA_VA + RDATA_RAW


def string_slot(s):
    """Новое содержимое слота: строка, NUL, дальше нули до конца слота."""
    raw = s["now"].encode("utf-8") + b"\0"
    if len(raw) > s["slot"]:
        raise SystemExit(f"[!] '{s['now']}' не влезает в слот 0x{s['rva']:X}")
    return raw + b"\0" * (s["slot"] - len(raw))


# ─── минимальный ассемблер ─────────────────────────────────────────────────
def _rex(reg, rm):
    return 0x48 | ((reg >> 3) << 2) | (rm >> 3)


def lea(reg, rm, disp):
    return bytes([_rex(reg, rm), 0x8D, 0x80 | ((reg & 7) << 3) | (rm & 7)]) \
        + struct.pack("<I", disp)


def mov_load(reg, rm, disp):
    """mov reg, [rm + disp32]. rm=RSP требует байта SIB."""
    head = bytes([_rex(reg, rm), 0x8B, 0x80 | ((reg & 7) << 3) | (rm & 7)])
    if (rm & 7) == RSP:
        head += b"\x24"
    return head + struct.pack("<I", disp)


def mov_load0(reg, rm):
    return bytes([_rex(reg, rm), 0x8B, ((reg & 7) << 3) | (rm & 7)])


def mov_rr(dst, src):
    return bytes([_rex(src, dst), 0x89, 0xC0 | ((src & 7) << 3) | (dst & 7)])


def test_rax():
    return b"\x48\x85\xC0"


def cmovne(dst, src):
    return bytes([_rex(dst, src), 0x0F, 0x45,
                  0xC0 | ((dst & 7) << 3) | (src & 7)])


def call_rel(frm, to):
    return b"\xE8" + struct.pack("<i", to - (frm + 5))


def jmp_rel(frm, to):
    return b"\xE9" + struct.pack("<i", to - (frm + 5))


def jmp_mem(frm, to):
    return b"\xFF\x25" + struct.pack("<i", to - (frm + 6))


# ─── карта патчей ──────────────────────────────────────────────────────────
# src     — регистр с PlayerInterface* на входе
# dst     — регистр, куда исходный код клал Character* (на входе мёртв)
# recover — как получить PlayerInterface* для отката:
#           None      -> dst уже хранит копию (годится для non-volatile dst)
#           (reg,off) -> перечитать из [reg+off]
#           "none"    -> отката нет; следующая инструкция сама проверяет NULL
SITES = [
    dict(name="пузырь речи и разворот NPC", where="ProcessMessageQueue",
         rva=0x043E9B, resume=0x043EA5, orig="488b89c0020000488b09",
         src=RCX, dst=RCX, recover=(R13, OFF_PLAYER)),
    dict(name="Player's sheet -> /context, хук обновления",
         where="функция 0x04AC20",
         rva=0x04B510, resume=0x04B51A, orig="488b80c0020000488b18",
         src=RAX, dst=RBX, recover=None),
    dict(name="Player's sheet -> /context, фоновый поток",
         where="функция 0x04E500",
         rva=0x04E625, resume=0x04E62F, orig="488b80c0020000488b18",
         src=RAX, dst=RBX, recover="none"),
    dict(name="имя в окне чата, путь по хоткею", where="функция 0x04A580",
         rva=0x04A641, resume=0x04A64B, orig="488b80c0020000488b08",
         src=RAX, dst=RCX, recover=(RBX, OFF_PLAYER)),
    dict(name="имя в окне чата, путь по клику", where="функция 0x04AC20",
         rva=0x04D23A, resume=0x04D244, orig="488b81c0020000488b08",
         src=RCX, dst=RCX, recover=(RSP, 0xA8)),
]


def orig_bytes(site):
    return bytes.fromhex(site["orig"])


def build_helper(rva):
    """Общая подпрограмма: rcx = PlayerInterface* -> rax = выделенный или NULL.

    Заканчивается ХВОСТОВЫМ переходом, а не вызовом: getCharacter вернётся
    сразу в стаб. Благодаря этому стек остаётся выровненным (стаб зовёт нас
    обычным call, мы ничего не кладём), а раскрутка исключений видит наш
    кадр как лист с адресом возврата по [rsp] — что и есть правда.
    """
    out = bytearray()
    out += lea(RCX, RCX, OFF_SELECTED)
    out += jmp_mem(rva + len(out), IAT_GET_CHARACTER)
    return bytes(out)


def build_stub(site, rva, helper_rva):
    """Стаб на месте вызова: получить говорящего в dst и вернуться в код."""
    src, dst, rec = site["src"], site["dst"], site["recover"]
    out = bytearray()
    if rec is None:
        out += mov_rr(dst, src)          # копия PlayerInterface* в non-volatile
    if src != RCX:
        out += mov_rr(RCX, src)
    out += call_rel(rva + len(out), helper_rva)

    if rec == "none":
        # Следующая же инструкция исходного кода проверяет результат на NULL,
        # поэтому откат тут не нужен — и экономит 17 байт дефицитного паддинга.
        out += mov_rr(dst, RAX)
    else:
        if rec is not None:
            out += mov_load(dst, rec[0], rec[1])
        out += mov_load(dst, dst, OFF_PC_STUFF)
        out += mov_load0(dst, dst)       # dst = playerCharacters[0]
        out += test_rax()
        out += cmovne(dst, RAX)          # выделенный побеждает, если он есть
    out += jmp_rel(rva + len(out), site["resume"])
    return bytes(out)


def build_jump(site, stub_rva):
    j = jmp_rel(site["rva"], stub_rva)
    return j + b"\x90" * (len(orig_bytes(site)) - len(j))


def layout():
    helper_rva = CAVE_BASE
    helper = build_helper(helper_rva)
    plan, cur = [], helper_rva + len(helper)
    for s in SITES:
        code = build_stub(s, cur, helper_rva)
        plan.append(dict(site=s, rva=cur, code=code))
        cur += len(code)
    if cur > CAVE_LIMIT:
        raise SystemExit(f"не помещается: нужно до 0x{cur:X}, "
                         f"есть 0x{CAVE_LIMIT:X}")
    return helper_rva, helper, plan, cur


def make_patched(original):
    buf = bytearray(original)
    helper_rva, helper, plan, end = layout()
    blob = [(helper_rva, helper)] + [(p["rva"], p["code"]) for p in plan]
    for rva, code in blob:
        o = rva2off(rva)
        if any(buf[o:o + len(code)]):
            raise SystemExit(f"[!] паддинг на 0x{rva:X} не пуст — патч отменён")
        buf[o:o + len(code)] = code
    for p in plan:
        s = p["site"]
        o = rva2off(s["rva"])
        got = bytes(buf[o:o + len(orig_bytes(s))])
        if got != orig_bytes(s):
            raise SystemExit(f"[!] на 0x{s['rva']:X} лежит {got.hex(' ')}, "
                             f"ожидалось {orig_bytes(s).hex(' ')} — отменено")
        jmp = build_jump(s, p["rva"])
        buf[o:o + len(jmp)] = jmp
    for s in RAW:
        o = rva2off(s["rva"])
        want = bytes.fromhex(s["orig"])
        room = s["end"] - s["rva"]
        if len(want) != room:
            raise SystemExit(f"[!] на 0x{s['rva']:X} описано {len(want)} байт, "
                             f"а участок {room}")
        got = bytes(buf[o:o + room])
        if got != want:
            raise SystemExit(f"[!] на 0x{s['rva']:X} лежит {got.hex(' ')}, "
                             f"ожидалось {want.hex(' ')} — отменено")
        code = jmp_rel(s["rva"], s["jump_to"])
        buf[o:o + room] = code + NOP * (room - len(code))
    for s in STRINGS:
        o = rdata2off(s["rva"])
        want = s["was"] + b"\0" * (s["slot"] - len(s["was"]))
        got = bytes(buf[o:o + s["slot"]])
        if got != want:
            raise SystemExit(f"[!] на 0x{s['rva']:X} лежит {got!r}, "
                             f"ожидалось {want!r} — отменено")
        buf[o:o + s["slot"]] = string_slot(s)
    return bytes(buf)


def sha256_bytes(d):
    return hashlib.sha256(d).hexdigest()


def sha256_file(p):
    with open(p, "rb") as f:
        return sha256_bytes(f.read())


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "--verify"
    if not os.path.exists(ORIGINAL):
        print(f"[!] нет эталона {ORIGINAL}")
        return 1
    with open(ORIGINAL, "rb") as f:
        original = f.read()
    if sha256_bytes(original) != SHA256_ORIG:
        print("[!] эталон не совпадает по SHA256 — остановлено")
        return 1

    helper_rva, helper, plan, end = layout()
    used = end - CAVE_BASE
    print(f"патчей: {len(plan)};  занято {used} из "
          f"{CAVE_LIMIT - CAVE_BASE} байт паддинга "
          f"(0x{CAVE_BASE:X}..0x{end:X})")
    print(f"  подпрограмма sel  0x{helper_rva:06X}  {len(helper)} байт")
    for p in plan:
        s = p["site"]
        print(f"  [{s['name']}]  {s['where']}")
        print(f"     0x{s['rva']:06X} -> стаб 0x{p['rva']:06X} "
              f"({len(p['code'])} байт)")
    print(f"прямых правок в .text: {len(RAW)}")
    for s in RAW:
        print(f"  0x{s['rva']:06X}..0x{s['end']:06X} -> jmp 0x{s['jump_to']:06X}"
              f"   {s['name']}")
    print(f"строк в .rdata: {len(STRINGS)}")
    for s in STRINGS:
        raw = s["now"].encode("utf-8")
        print(f"  0x{s['rva']:06X}  {s['was'].decode():<12} -> "
              f"{s['now']:<10} ({len(raw) + 1} из {s['slot']} байт)"
              f"   {s['note']}")
    print()

    if mode == "--disasm":
        try:
            from capstone import CS_ARCH_X86, CS_MODE_64, Cs
        except ImportError:
            print("нужен capstone: server/python/python.exe -m pip install capstone")
            return 1
        md = Cs(CS_ARCH_X86, CS_MODE_64)
        blocks = [("подпрограмма sel", helper_rva, helper)]
        blocks += [(p["site"]["name"], p["rva"], p["code"]) for p in plan]
        for title, rva, code in blocks:
            print(f"=== {title} ===")
            for i in md.disasm(code, 0x180000000 + rva):
                cur = i.address - 0x180000000
                line = (f"  0x{cur:06X}  {i.bytes.hex(' '):<24} "
                        f"{i.mnemonic:<7} {i.op_str}")
                if "rip + " in i.op_str:
                    d = int(i.op_str.split("rip + ")[1].split("]")[0], 16)
                    line += f"   ; -> RVA 0x{cur + i.size + d:X}"
                print(line)
            print()
        return 0

    targets = [p for p in (GAME_DLL, PROJ_DLL) if os.path.exists(p)]
    if mode == "--revert":
        for p in targets:
            with open(p, "wb") as f:
                f.write(original)
            print(f"возвращён оригинал: {p}")
        return 0

    patched = make_patched(original)
    print(f"эталон sha256 {sha256_bytes(original)}")
    print(f"патч   sha256 {sha256_bytes(patched)}")
    print()
    for p in targets:
        cur = sha256_file(p)
        state = ("оригинал" if cur == SHA256_ORIG else
                 "уже пропатчен (актуальная версия)"
                 if cur == sha256_bytes(patched) else "иная версия патча")
        print(f"{p}\n   было: {state}  ({cur[:16]}…)")
        if mode == "--apply":
            bak = p + ".bak"
            if not os.path.exists(bak):
                with open(bak, "wb") as f:
                    f.write(original)
            with open(p, "wb") as f:
                f.write(patched)
            print("   применено")
    return 0


if __name__ == "__main__":
    sys.exit(main())
