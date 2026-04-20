#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Patch `WindroseServer-Win64-Shipping.exe` to throttle the Boost.Asio
`socket_select_interrupter::reset()` drain loop that busy-spins under
Wine/Proton and burns ~2 CPU cores on an idle server.

Inserts a ~43-byte trampoline that calls `KERNEL32!Sleep(1)` on the
loop-continue tail; the loop exits via its normal branch as soon as
real packets arrive, so the detour is cold under player load. Revert
is byte-for-byte clean (`--revert`), state is detected from the binary
itself so there's no cache to keep in sync, and the patch refuses to
apply if the 9-byte loop-tail signature isn't uniquely present.

Modes:
  default            auto-derive offsets from a signature scan + PE
                     parse; works on any build where the interrupter
                     pattern still matches
  --use-known-offsets  skip the scan and pull from the KNOWN_OFFSETS
                     table (refuses if MD5 is unknown)

Known MD5s (patched + unpatched) populate the fast path; the scan only
runs for truly new builds. Peak memory is bounded by CHUNK (1 MiB)
regardless of binary size — safe on a memory-constrained sidecar.

DISCLAIMER: Modifies a proprietary binary. AS IS, no warranty of any
kind — may break, malfunction, corrupt saves, or become inapplicable
on a future build. You are responsible for EULA / Steam Subscriber
Agreement / local-law compliance. The authors do not distribute
modified binaries and do not authorize redistributing any binary this
script produces. Full risk rests with you.

Usage:
  python3 patch-idle-cpu.py PATH_TO_EXE
  python3 patch-idle-cpu.py PATH_TO_EXE --revert
  python3 patch-idle-cpu.py PATH_TO_EXE --dry-run
  python3 patch-idle-cpu.py PATH_TO_EXE --use-known-offsets
  python3 patch-idle-cpu.py PATH_TO_EXE --print-state   # JSON for the UI
"""
import argparse
import hashlib
import json
import os
import shutil
import struct
from dataclasses import dataclass


SIGNATURE = b"\x48\x8b\x0b\x8b\xc7\x87\x41\x34\xe9"
TRAMPOLINE_SIZE = 38
# First 7 bytes of the trampoline (push rdx; rcx; rax; r8; r9). Acts as
# the "is this binary patched?" fingerprint at the jump target.
TRAMPOLINE_PROLOGUE = b"\x52\x51\x50\x41\x50\x41\x51"
CHUNK = 1 << 20


@dataclass(frozen=True)
class Offsets:
    patch_site_file: int
    loop_top_file: int
    trampoline_file: int
    sleep_iat_va: int
    image_base: int
    text_raw: int
    text_vaddr: int

    def file_to_va(self, file_off: int) -> int:
        return self.image_base + file_off - self.text_raw + self.text_vaddr


_OLD_BUILD = Offsets(
    patch_site_file=0x4C98A09, loop_top_file=0x4C98270,
    trampoline_file=0xD30371,  sleep_iat_va=0x14C282428,
    image_base=0x140000000, text_raw=0x600, text_vaddr=0x1000,
)
_NEW_BUILD = Offsets(
    patch_site_file=0x4C985C9, loop_top_file=0x4C97E30,
    trampoline_file=0xD30371,  sleep_iat_va=0x14C2A2430,
    image_base=0x140000000, text_raw=0x600, text_vaddr=0x1000,
)
# Both unpatched and patched MD5s map to the same Offsets so
# /api/idle-cpu-patch state detection stays O(1) on known builds.
KNOWN_OFFSETS = {
    "61e320a6a45f4ac539f2c5d0f7b7ff2c": _OLD_BUILD,  # unpatched
    "b1796533f22603ad2f2da021033e3f9f": _OLD_BUILD,  # patched
    "8a62138c8fd19ede9ec8a5cf10579cb8": _NEW_BUILD,  # unpatched
    "a7f9260faf16e180d9a50959183264d0": _NEW_BUILD,  # patched
}


@dataclass
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_ptr: int
    raw_size: int


@dataclass
class PEInfo:
    image_base: int
    sections: list
    import_dir_rva: int
    import_dir_size: int
    exception_dir_rva: int
    exception_dir_size: int


# --- file helpers (all chunked, no mmap) ------------------------------------

def _file_md5(path: str) -> str:
    m = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            m.update(chunk)
    return m.hexdigest()


def _read_at(f, offset: int, n: int) -> bytes:
    f.seek(offset)
    return f.read(n)


def _scan_for_signature(f, signature: bytes, max_matches: int = 2) -> list[int]:
    """Return file offsets of `signature`, stopping at max_matches (callers
    only care about 0, 1, or ≥2). Uses bytes.find() within 1 MiB chunks
    with a (len(signature)-1)-byte tail carried forward to catch
    boundary-crossing matches."""
    overlap = len(signature) - 1
    offsets: list[int] = []
    buf = b""
    base = 0
    f.seek(0)
    while True:
        chunk = f.read(CHUNK)
        if not chunk:
            break
        combined = buf + chunk
        i = 0
        while True:
            idx = combined.find(signature, i)
            if idx < 0:
                break
            offsets.append(base + idx)
            if len(offsets) >= max_matches:
                return offsets
            i = idx + 1
        if len(combined) > overlap:
            carry = combined[-overlap:]
            base += len(combined) - overlap
            buf = carry
        else:
            buf = combined
    return offsets


def _scan_for_cc_runs(f, start: int, end: int, min_size: int) -> list[tuple[int, int]]:
    """Return (size, file_offset) for every CC-padding run ≥ min_size in
    [start, end). Uses bytes.find() to skip non-CC bytes fast, and
    carries partial runs across chunk boundaries."""
    results: list[tuple[int, int]] = []
    carry_start: int | None = None
    carry_size = 0
    f.seek(start)
    pos = start
    remaining = end - start
    while remaining > 0:
        to_read = min(CHUNK, remaining)
        chunk = f.read(to_read)
        if not chunk:
            break
        remaining -= len(chunk)
        # Extend a run carried from the previous chunk.
        i = 0
        if carry_start is not None:
            while i < len(chunk) and chunk[i] == 0xCC:
                i += 1
            carry_size += i
            if i == len(chunk):
                pos += len(chunk)
                continue
            if carry_size >= min_size:
                results.append((carry_size, carry_start))
            carry_start = None
            carry_size = 0
        # Scan the rest of the chunk for fresh runs.
        while i < len(chunk):
            idx = chunk.find(b"\xcc", i)
            if idx < 0:
                break
            j = idx
            while j < len(chunk) and chunk[j] == 0xCC:
                j += 1
            run_size = j - idx
            if j == len(chunk):
                # Runs extending to the chunk boundary might continue.
                carry_start = pos + idx
                carry_size = run_size
            elif run_size >= min_size:
                results.append((run_size, pos + idx))
            i = j
        pos += len(chunk)
    if carry_start is not None and carry_size >= min_size:
        results.append((carry_size, carry_start))
    return results


# --- PE parsing -------------------------------------------------------------

def parse_pe(f) -> PEInfo:
    head = _read_at(f, 0, 16 * 1024)
    if head[:2] != b"MZ":
        raise SystemExit("error: not a PE file (missing MZ header)")
    pe_off = struct.unpack_from("<I", head, 0x3C)[0]
    if head[pe_off:pe_off + 4] != b"PE\x00\x00":
        raise SystemExit(f"error: no PE signature at file offset {pe_off:#x}")
    coff_off = pe_off + 4
    num_sections = struct.unpack_from("<H", head, coff_off + 2)[0]
    size_opt_header = struct.unpack_from("<H", head, coff_off + 16)[0]
    opt_off = coff_off + 20
    magic = struct.unpack_from("<H", head, opt_off)[0]
    if magic != 0x20B:
        raise SystemExit(f"error: expected PE32+ (magic 0x20B), got 0x{magic:x}")
    image_base = struct.unpack_from("<Q", head, opt_off + 24)[0]
    num_dd = struct.unpack_from("<I", head, opt_off + 108)[0]
    dd_off = opt_off + 112
    if num_dd < 4:
        raise SystemExit(f"error: too few data directories ({num_dd}); need ≥4")
    import_dir_rva, import_dir_size = struct.unpack_from("<II", head, dd_off + 8)
    exception_dir_rva, exception_dir_size = struct.unpack_from("<II", head, dd_off + 24)
    section_off = opt_off + size_opt_header
    if section_off + num_sections * 40 > len(head):
        raise SystemExit("error: section table extends past the 16 KiB header window")
    sections = []
    for i in range(num_sections):
        s_off = section_off + i * 40
        name = head[s_off:s_off + 8].rstrip(b"\x00").decode("ascii", "replace")
        virt_size = struct.unpack_from("<I", head, s_off + 8)[0]
        virt_addr = struct.unpack_from("<I", head, s_off + 12)[0]
        raw_size = struct.unpack_from("<I", head, s_off + 16)[0]
        raw_ptr = struct.unpack_from("<I", head, s_off + 20)[0]
        sections.append(Section(name, virt_addr, virt_size, raw_ptr, raw_size))
    return PEInfo(
        image_base=image_base, sections=sections,
        import_dir_rva=import_dir_rva, import_dir_size=import_dir_size,
        exception_dir_rva=exception_dir_rva, exception_dir_size=exception_dir_size,
    )


def _section_for_rva(pe: PEInfo, rva: int) -> Section:
    for s in pe.sections:
        if s.virtual_address <= rva < s.virtual_address + max(s.virtual_size, s.raw_size):
            return s
    raise SystemExit(f"error: RVA {rva:#x} not in any section")


def rva_to_file(pe: PEInfo, rva: int) -> int:
    s = _section_for_rva(pe, rva)
    return rva - s.virtual_address + s.raw_ptr


def file_to_rva(pe: PEInfo, file_off: int) -> int:
    for s in pe.sections:
        if s.raw_ptr <= file_off < s.raw_ptr + s.raw_size:
            return file_off - s.raw_ptr + s.virtual_address
    raise SystemExit(f"error: file offset {file_off:#x} not in any section")


# --- anchor discovery -------------------------------------------------------

def find_signature_offset(f) -> int:
    """Return the file offset of the `e9` at SIGNATURE[8]. Enforces uniqueness."""
    offsets = _scan_for_signature(f, SIGNATURE)
    if not offsets:
        raise SystemExit(
            f"error: signature {SIGNATURE.hex()} not found. The target function "
            "was likely refactored in this build; auto-derive cannot proceed."
        )
    if len(offsets) > 1:
        raise SystemExit(
            f"error: signature {SIGNATURE.hex()} found at {', '.join(hex(o) for o in offsets)}; "
            "expected exactly 1. Refusing to guess."
        )
    return offsets[0] + 8


def find_trampoline_window(f, pe: PEInfo) -> int:
    """Return the file offset of the largest CC-padding run ≥ TRAMPOLINE_SIZE
    inside .text that doesn't overlap any RUNTIME_FUNCTION. Largest-first is
    deterministic and tends to survive linker-driven layout shuffles."""
    text = next((s for s in pe.sections if s.name == ".text"), None)
    if text is None:
        raise SystemExit("error: no .text section")
    ranges: list[tuple[int, int]] = []
    if pe.exception_dir_size > 0:
        n = pe.exception_dir_size // 12
        pdata = _read_at(f, rva_to_file(pe, pe.exception_dir_rva), n * 12)
        for i in range(n):
            b, e, _ = struct.unpack_from("<III", pdata, i * 12)
            if b == 0 and e == 0:
                break
            ranges.append((b, e))
        ranges.sort()

    def covered(rva: int, size: int) -> bool:
        lo, hi = 0, len(ranges)
        end = rva + size
        while lo < hi:
            mid = (lo + hi) // 2
            b, e = ranges[mid]
            if e <= rva:
                lo = mid + 1
            elif b >= end:
                hi = mid
            else:
                return True
        return False

    text_end = text.raw_ptr + min(text.raw_size, text.virtual_size)
    runs = _scan_for_cc_runs(f, text.raw_ptr, text_end, TRAMPOLINE_SIZE)
    candidates = [(size, off) for size, off in runs
                  if not covered(file_to_rva(pe, off), TRAMPOLINE_SIZE)]
    if not candidates:
        raise SystemExit(
            f"error: no CC-padding window of {TRAMPOLINE_SIZE}+ bytes found in "
            ".text outside .pdata. Auto-derive cannot proceed."
        )
    candidates.sort(key=lambda t: (-t[0], t[1]))
    return candidates[0][1]


def find_sleep_iat_va(f, pe: PEInfo) -> int:
    if pe.import_dir_size == 0:
        raise SystemExit("error: no import directory")
    # Import descriptors + directory region — read a bounded window.
    dir_file = rva_to_file(pe, pe.import_dir_rva)
    descriptors = _read_at(f, dir_file, max(pe.import_dir_size, 2048))
    i = 0
    while True:
        desc = descriptors[i * 20:i * 20 + 20]
        if len(desc) < 20:
            break
        ilt_rva, _, _, name_rva, iat_rva = struct.unpack_from("<IIIII", desc)
        if ilt_rva == 0 and name_rva == 0 and iat_rva == 0:
            break
        name_off = rva_to_file(pe, name_rva)
        raw_name = _read_at(f, name_off, 64)
        null = raw_name.find(b"\x00")
        dll = raw_name[:null if null >= 0 else len(raw_name)].decode("ascii", "replace")
        if dll.lower() == "kernel32.dll":
            thunk_rva = ilt_rva or iat_rva
            thunk_file = rva_to_file(pe, thunk_rva)
            j = 0
            while True:
                entry_bytes = _read_at(f, thunk_file + j * 8, 8)
                (entry,) = struct.unpack("<Q", entry_bytes)
                if entry == 0:
                    break
                if not (entry & (1 << 63)):
                    hint_bytes = _read_at(f, rva_to_file(pe, entry) + 2, 64)
                    null2 = hint_bytes.find(b"\x00")
                    name = hint_bytes[:null2 if null2 >= 0 else len(hint_bytes)].decode("ascii", "replace")
                    if name == "Sleep":
                        return pe.image_base + iat_rva + j * 8
                j += 1
            raise SystemExit("error: Sleep not found in KERNEL32.dll imports")
        i += 1
    raise SystemExit("error: KERNEL32.dll not found in imports")


# --- derivation, state, patching --------------------------------------------

def derive_offsets(f) -> Offsets:
    """Derive patch parameters by scanning the binary. Handles both
    unpatched and already-patched binaries: the patch-site signature is
    unchanged post-patch, so we follow the rel32 after its `e9`; if it
    lands on our trampoline prologue the binary's patched and we read
    loop_top from the trampoline's jmp-back instead of re-scanning CC
    windows."""
    pe = parse_pe(f)
    text = next((s for s in pe.sections if s.name == ".text"), None)
    if text is None:
        raise SystemExit("error: no .text section")

    patch_site = find_signature_offset(f)
    rel32 = struct.unpack("<i", _read_at(f, patch_site + 1, 4))[0]
    target_rva = file_to_rva(pe, patch_site + 5) + rel32
    target_file = rva_to_file(pe, target_rva)
    target_head = _read_at(f, target_file, len(TRAMPOLINE_PROLOGUE))

    if target_head == TRAMPOLINE_PROLOGUE:
        trampoline = target_file
        jmp_back = trampoline + TRAMPOLINE_SIZE - 5
        tail = _read_at(f, jmp_back, 5)
        if tail[0] != 0xE9:
            raise SystemExit(
                f"error: trampoline at 0x{trampoline:x} missing JMP-back tail "
                f"(got 0x{tail[0]:02x} at 0x{jmp_back:x})"
            )
        (jb_rel,) = struct.unpack("<i", tail[1:])
        loop_top_rva = file_to_rva(pe, jmp_back + 5) + jb_rel
        loop_top = rva_to_file(pe, loop_top_rva)
    else:
        loop_top = target_file
        trampoline = find_trampoline_window(f, pe)

    sleep_iat_va = find_sleep_iat_va(f, pe)
    return Offsets(
        patch_site_file=patch_site, loop_top_file=loop_top,
        trampoline_file=trampoline, sleep_iat_va=sleep_iat_va,
        image_base=pe.image_base,
        text_raw=text.raw_ptr, text_vaddr=text.virtual_address,
    )


def binary_state(f, off: Offsets) -> str:
    site = _read_at(f, off.patch_site_file, 5)
    tramp = _read_at(f, off.trampoline_file, TRAMPOLINE_SIZE)
    if not site or site[0] != 0xE9:
        return "corrupt"
    if tramp == b"\xcc" * TRAMPOLINE_SIZE:
        return "unpatched"
    if tramp[:len(TRAMPOLINE_PROLOGUE)] == TRAMPOLINE_PROLOGUE:
        return "patched"
    return "corrupt"


def compile_patch(off: Offsets) -> tuple[bytes, bytes]:
    """Emit (5-byte site jmp, 38-byte trampoline). Trampoline saves Win64
    volatiles across a KERNEL32!Sleep(1) call, then jmps back to loop top.
    RDX is the critical save — the loop top reloads %rbx from
    `lea 0x28(%rdx), %rbx`, so clobbering RDX across Sleep crashes."""
    tramp_va = off.file_to_va(off.trampoline_file)
    tramp = bytearray()
    tramp += b"\x52"              # push rdx (loop-carried; MUST preserve)
    tramp += b"\x51"              # push rcx
    tramp += b"\x50"              # push rax
    tramp += b"\x41\x50"          # push r8
    tramp += b"\x41\x51"          # push r9
    tramp += b"\x48\x83\xec\x20"  # sub $0x20, %rsp  (Win64 shadow space)
    tramp += b"\xb9\x01\x00\x00\x00"  # mov $1, %ecx  (Sleep arg)
    call_va = tramp_va + len(tramp)
    rel = off.sleep_iat_va - (call_va + 6)
    tramp += b"\xff\x15" + rel.to_bytes(4, "little", signed=True)
    tramp += b"\x48\x83\xc4\x20"  # add $0x20, %rsp
    tramp += b"\x41\x59"          # pop r9
    tramp += b"\x41\x58"          # pop r8
    tramp += b"\x58"              # pop rax
    tramp += b"\x59"              # pop rcx
    tramp += b"\x5a"              # pop rdx
    jmp_va = tramp_va + len(tramp)
    rel_top = off.file_to_va(off.loop_top_file) - (jmp_va + 5)
    tramp += b"\xe9" + rel_top.to_bytes(4, "little", signed=True)

    site_va = off.file_to_va(off.patch_site_file)
    rel_fwd = tramp_va - (site_va + 5)
    site = b"\xe9" + rel_fwd.to_bytes(4, "little", signed=True)

    assert len(site) == 5
    assert len(tramp) == TRAMPOLINE_SIZE
    return site, bytes(tramp)


def reconstruct_original_site_bytes(f, off: Offsets) -> bytes:
    """Reconstruct the pre-patch 5 bytes from the trampoline's jmp-back."""
    jmp_back = off.trampoline_file + TRAMPOLINE_SIZE - 5
    tail = _read_at(f, jmp_back, 5)
    if tail[0] != 0xE9:
        raise SystemExit(
            f"error: trampoline jmp-back at 0x{jmp_back:x} is not JMP "
            f"(got 0x{tail[0]:02x}); refusing to guess the original bytes."
        )
    (rel,) = struct.unpack("<i", tail[1:])
    loop_top_va = off.file_to_va(jmp_back) + 5 + rel
    orig_rel = loop_top_va - (off.file_to_va(off.patch_site_file) + 5)
    return b"\xe9" + orig_rel.to_bytes(4, "little", signed=True)


def resolve_offsets(f, md5: str, use_known: bool) -> tuple[Offsets, str]:
    """Return (offsets, mode). Known MD5 short-circuits both paths to
    an O(1) table lookup — the scan only runs for unknown builds."""
    if md5 in KNOWN_OFFSETS:
        return KNOWN_OFFSETS[md5], "known"
    if use_known:
        raise SystemExit(
            f"error: --use-known-offsets but MD5 {md5} not in table "
            f"(known: {', '.join(sorted(KNOWN_OFFSETS))})"
        )
    return derive_offsets(f), "derived"


# --- subcommands ------------------------------------------------------------

def print_state(path: str) -> None:
    if not os.path.isfile(path):
        print(json.dumps({"state": "missing", "path": path}))
        return
    md5 = _file_md5(path)
    result = {"path": path, "md5": md5}
    try:
        with open(path, "rb") as f:
            off, mode = resolve_offsets(f, md5, use_known=False)
            result["state"] = binary_state(f, off)
            result["mode"] = mode
            result["patch_site_file"] = off.patch_site_file
            result["trampoline_file"] = off.trampoline_file
    except SystemExit as e:
        result["state"] = "inapplicable"
        result["reason"] = str(e).replace("error: ", "")
    print(json.dumps(result))


def apply_patch(path: str, revert: bool, dry_run: bool, use_known: bool,
                verbose: bool, idempotent: bool) -> None:
    md5 = _file_md5(path)
    print(f"MD5 of {path}: {md5}")

    with open(path, "rb") as f:
        off, mode = resolve_offsets(f, md5, use_known)
        if verbose or mode == "derived":
            print(
                f"using {mode} offsets: "
                f"patch_site=0x{off.patch_site_file:x} "
                f"loop_top=0x{off.loop_top_file:x} "
                f"trampoline=0x{off.trampoline_file:x} "
                f"sleep_iat_va=0x{off.sleep_iat_va:x}"
            )
        state = binary_state(f, off)
        site_patch, tramp_patch = compile_patch(off)
        if revert:
            if state == "unpatched":
                _done_or_error(f"binary at {path} is already unpatched", idempotent)
                return
            if state != "patched":
                raise SystemExit(f"error: {path} is in an unexpected state ({state}); refusing to revert")
            site_bytes = reconstruct_original_site_bytes(f, off)
            tramp_bytes = b"\xcc" * TRAMPOLINE_SIZE
            action = "reverted"
        else:
            if state == "patched":
                _done_or_error(f"binary at {path} is already patched; use --revert to undo", idempotent)
                return
            if state != "unpatched":
                raise SystemExit(f"error: {path} is in an unexpected state ({state}); refusing to patch")
            site_bytes, tramp_bytes = site_patch, tramp_patch
            action = "patched"

    tmp = path + ".tmp.patch"
    shutil.copyfile(path, tmp)
    try:
        with open(tmp, "r+b") as f:
            f.seek(off.patch_site_file); f.write(site_bytes)
            f.seek(off.trampoline_file); f.write(tramp_bytes)
        new_md5 = _file_md5(tmp)
        if dry_run:
            print(f"dry-run: would have {action}; new MD5 would be {new_md5}")
            os.unlink(tmp)
            return
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except FileNotFoundError: pass
        raise
    print(f"{action} in place; new MD5 is {new_md5}")


def _done_or_error(msg: str, idempotent: bool) -> None:
    if idempotent:
        print(f"info: {msg}.")
    else:
        raise SystemExit(f"error: {msg}.")


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("path", help="Path to WindroseServer-Win64-Shipping.exe")
    ap.add_argument("--revert", action="store_true",
                    help="Undo a prior patch (reconstructs original bytes from trampoline)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print intended changes without modifying the file")
    ap.add_argument("--use-known-offsets", action="store_true",
                    help="Only use the KNOWN_OFFSETS table; refuse unknown MD5s")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print resolved offsets even on the known-builds fast path")
    ap.add_argument("--idempotent", action="store_true",
                    help="Exit 0 if already in target state; for boot scripts")
    ap.add_argument("--print-state", action="store_true",
                    help="Emit a single JSON line with md5/state/offsets; for the UI")
    args = ap.parse_args()
    if args.print_state:
        print_state(args.path)
        return
    apply_patch(args.path, revert=args.revert, dry_run=args.dry_run,
                use_known=args.use_known_offsets, verbose=args.verbose,
                idempotent=args.idempotent)


if __name__ == "__main__":
    main()
