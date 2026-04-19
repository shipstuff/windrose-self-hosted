#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""
Patch `WindroseServer-Win64-Shipping.exe` to slash idle CPU from ~200% to ~5%.

Root cause: `boost::asio::detail::socket_select_interrupter::reset()` spins
at full rate under Wine/Proton because its socket drain loop never makes
a syscall — it's a pure userspace busy loop. Two such threads (both named
`GameThread` by Wine) burn ~91% CPU each on an idle server.

Patch: inject a `Sleep(1)` call at the loop-continue tail of `reset()`.
Registers clobbered by the call are preserved around it; the patch fires
only on the tight spin path (not during the init fall-through exits), so
the rest of the server is untouched.

Two modes:
  - --auto-derive (default): scan the binary for the stable 9-byte
    loop-tail signature, parse PE structures, and derive patch offsets
    from ground truth. Works on any build where the Asio interrupter
    pattern still matches. Refuses cleanly if the signature isn't found
    or isn't unique, or if no suitable CC-padding trampoline window
    exists outside .pdata-covered ranges.
  - --use-known-offsets: use a hard-coded table keyed on the binary's
    MD5. Fast path for known-good builds; refuses any MD5 not in the
    table. Kept as a belt-and-suspenders fallback if auto-derive ever
    produces surprising output on a known build.

Safety:
  - Idempotent: script rejects an already-patched binary (wrong 5 bytes
    at the patch site under --use-known-offsets; auto-derive separately
    detects the current bytes form an e9 jump into an occupied window).
  - Rollback: `--revert` flips the patch back.
  - No new imports, no new code sections; 43 bytes modified total
    (5 at the patch site + 38 in a pre-existing CC-padding window).
  - Auto-derive path enforces signature uniqueness: if the 9-byte
    pattern appears 0 or >1 times in the binary, the script refuses
    rather than guessing.

DISCLAIMER: This script modifies a proprietary binary. It is provided
AS IS, with no warranty of any kind, express or implied — it may break,
malfunction, corrupt saves, or become inapplicable on any future
Windrose build. You are responsible for ensuring your use complies
with the Windrose EULA, the Steam Subscriber Agreement, and any
applicable terms of service or laws in your jurisdiction; running this
against a binary you do not own a valid license to is not supported.
The authors do not distribute modified copies of the Windrose binary
and do not authorize redistribution of any binary this script produces.
The full risk as to functionality and legal compliance rests with you.

Measured impact (2026-04-19, sf-west-1 canary pod, AMD Ryzen 9 9955HX, 32-core host):
  - Baseline idle CPU  : 206.65%  (two GameThreads at ~91% each + ~5% main)
  - Patched idle CPU   :   5.08%  (mean of 10x 30s samples)
  - Improvement        : 97.5% reduction
  - Per-thread strace  : before 0 syscalls/3s; after 2818 pselect6 + 2818
                         sched_yield + 5636 getrusage / 3 s (Wine's Sleep
                         implementation)
  - Under 1-player load: Foreground/spin threads drop from 91% to ~2.2%
                         each (patched detour doesn't fire when packets flow)

Usage:
  python3 patch-idle-cpu.py /path/to/WindroseServer-Win64-Shipping.exe
  python3 patch-idle-cpu.py /path/to/WindroseServer-Win64-Shipping.exe --revert
  python3 patch-idle-cpu.py /path/to/WindroseServer-Win64-Shipping.exe --dry-run
  python3 patch-idle-cpu.py /path/to/WindroseServer-Win64-Shipping.exe --use-known-offsets
"""
import argparse
import hashlib
import os
import struct
import sys
from dataclasses import dataclass


# === Invariants the patch relies on ===
# 9-byte loop-tail signature: mov (%rbx),%rcx ; mov %edi,%eax ; xchg %eax,0x34(%rcx) ; jmp rel32
# Verified unique across two Windrose builds (pre + post 2026-04-19 Steam update).
# The byte at offset 8 is the `e9` opcode of the jmp we replace.
SIGNATURE = b"\x48\x8b\x0b\x8b\xc7\x87\x41\x34\xe9"
TRAMPOLINE_SIZE = 38


# === Offsets (same shape whether hard-coded or derived) ===

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


# Known builds — consistency check for auto-derive, and the sole source
# of truth under --use-known-offsets. Add new builds as they appear.
KNOWN_OFFSETS = {
    # Initial Windrose 0.10.0 build
    "61e320a6a45f4ac539f2c5d0f7b7ff2c": Offsets(
        patch_site_file=0x4C98A09,
        loop_top_file=0x4C98270,
        trampoline_file=0xD30371,
        sleep_iat_va=0x14C282428,
        image_base=0x140000000,
        text_raw=0x600,
        text_vaddr=0x1000,
    ),
    # Post-2026-04-19 Steam update (function shifted -0x440 as a block)
    "8a62138c8fd19ede9ec8a5cf10579cb8": Offsets(
        patch_site_file=0x4C985C9,
        loop_top_file=0x4C97E30,
        trampoline_file=0xD30371,
        sleep_iat_va=0x14C2A2430,
        image_base=0x140000000,
        text_raw=0x600,
        text_vaddr=0x1000,
    ),
}


# === PE parsing (stdlib only) ===

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


def parse_pe(data: bytes) -> PEInfo:
    if data[:2] != b"MZ":
        raise SystemExit("error: not a PE file (missing MZ header)")
    pe_off = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_off:pe_off + 4] != b"PE\x00\x00":
        raise SystemExit(f"error: no PE signature at file offset {pe_off:#x}")
    coff_off = pe_off + 4
    num_sections = struct.unpack_from("<H", data, coff_off + 2)[0]
    size_opt_header = struct.unpack_from("<H", data, coff_off + 16)[0]
    opt_off = coff_off + 20
    magic = struct.unpack_from("<H", data, opt_off)[0]
    if magic != 0x20B:
        raise SystemExit(f"error: expected PE32+ (magic 0x20B), got 0x{magic:x}")
    image_base = struct.unpack_from("<Q", data, opt_off + 24)[0]
    num_dd = struct.unpack_from("<I", data, opt_off + 108)[0]
    dd_off = opt_off + 112
    if num_dd < 4:
        raise SystemExit(f"error: too few data directories ({num_dd}); need at least 4")
    import_dir_rva, import_dir_size = struct.unpack_from("<II", data, dd_off + 1 * 8)
    exception_dir_rva, exception_dir_size = struct.unpack_from("<II", data, dd_off + 3 * 8)
    section_off = opt_off + size_opt_header
    sections = []
    for i in range(num_sections):
        s_off = section_off + i * 40
        name = data[s_off:s_off + 8].rstrip(b"\x00").decode("ascii", "replace")
        virt_size = struct.unpack_from("<I", data, s_off + 8)[0]
        virt_addr = struct.unpack_from("<I", data, s_off + 12)[0]
        raw_size = struct.unpack_from("<I", data, s_off + 16)[0]
        raw_ptr = struct.unpack_from("<I", data, s_off + 20)[0]
        sections.append(Section(name, virt_addr, virt_size, raw_ptr, raw_size))
    return PEInfo(
        image_base=image_base,
        sections=sections,
        import_dir_rva=import_dir_rva,
        import_dir_size=import_dir_size,
        exception_dir_rva=exception_dir_rva,
        exception_dir_size=exception_dir_size,
    )


def section_for_rva(pe: PEInfo, rva: int) -> Section:
    for s in pe.sections:
        if s.virtual_address <= rva < s.virtual_address + max(s.virtual_size, s.raw_size):
            return s
    raise SystemExit(f"error: RVA {rva:#x} not in any section")


def rva_to_file(pe: PEInfo, rva: int) -> int:
    s = section_for_rva(pe, rva)
    return rva - s.virtual_address + s.raw_ptr


def file_to_rva(pe: PEInfo, file_off: int) -> int:
    for s in pe.sections:
        if s.raw_ptr <= file_off < s.raw_ptr + s.raw_size:
            return file_off - s.raw_ptr + s.virtual_address
    raise SystemExit(f"error: file offset {file_off:#x} not in any section")


# === Anchor discovery ===

def find_signature(data: bytes) -> int:
    """Return the file offset of the `e9` opcode at SIGNATURE[8]. Enforces
    that the signature appears exactly once."""
    count = data.count(SIGNATURE)
    if count == 0:
        raise SystemExit(
            f"error: signature {SIGNATURE.hex()} not found in binary. "
            "The target function was likely refactored in this Windrose build; "
            "auto-derive cannot proceed. This is a refuse-cleanly outcome — the "
            "patch is inapplicable until someone re-derives against the new code shape."
        )
    if count > 1:
        offsets = []
        start = 0
        while True:
            idx = data.find(SIGNATURE, start)
            if idx < 0:
                break
            offsets.append(hex(idx))
            start = idx + 1
        raise SystemExit(
            f"error: signature {SIGNATURE.hex()} found {count} times at "
            f"{', '.join(offsets)}; expected exactly 1. Refusing to guess — "
            "fall back to --use-known-offsets with a known MD5."
        )
    return data.find(SIGNATURE) + 8


def find_cc_padding_window(data: bytes, pe: PEInfo, min_size: int = TRAMPOLINE_SIZE) -> int:
    """Return the file offset of the LARGEST CC-padding run of at least
    min_size bytes in .text that doesn't overlap any RUNTIME_FUNCTION.
    Largest-first is deterministic and most robust across rebuilds —
    a 655-byte inter-function gap survived a Steam update untouched even
    while smaller windows shuffled around."""
    text = next((s for s in pe.sections if s.name == ".text"), None)
    if text is None:
        raise SystemExit("error: no .text section found")
    if pe.exception_dir_size == 0:
        ranges = []
    else:
        pdata_off = rva_to_file(pe, pe.exception_dir_rva)
        n = pe.exception_dir_size // 12
        ranges = []
        for i in range(n):
            b, e, _ = struct.unpack_from("<III", data, pdata_off + i * 12)
            if b == 0 and e == 0:
                break
            ranges.append((b, e))
        ranges.sort()

    def covered(rva: int, size: int) -> bool:
        lo, hi = 0, len(ranges)
        target_end = rva + size
        while lo < hi:
            mid = (lo + hi) // 2
            b, e = ranges[mid]
            if e <= rva:
                lo = mid + 1
            elif b >= target_end:
                hi = mid
            else:
                return True
        return False

    text_end = text.raw_ptr + min(text.raw_size, text.virtual_size)
    candidates: list[tuple[int, int]] = []  # (size, file_offset)
    i = text.raw_ptr
    while i < text_end:
        if data[i] != 0xCC:
            i += 1
            continue
        run_start = i
        while i < text_end and data[i] == 0xCC:
            i += 1
        run_size = i - run_start
        if run_size >= min_size:
            rva = file_to_rva(pe, run_start)
            if not covered(rva, min_size):
                candidates.append((run_size, run_start))
    if not candidates:
        raise SystemExit(
            f"error: no CC-padding window of {min_size}+ bytes found in .text "
            "that is not covered by .pdata. Auto-derive cannot proceed."
        )
    # Largest run wins; tiebreak on earliest offset for determinism.
    candidates.sort(key=lambda t: (-t[0], t[1]))
    return candidates[0][1]


def find_sleep_iat_va(data: bytes, pe: PEInfo) -> int:
    """Walk the import directory to find the IAT entry VA for KERNEL32!Sleep."""
    if pe.import_dir_size == 0:
        raise SystemExit("error: no import directory in binary")
    dir_off = rva_to_file(pe, pe.import_dir_rva)
    i = 0
    while True:
        desc_off = dir_off + i * 20
        ilt_rva, _, _, name_rva, iat_rva = struct.unpack_from("<IIIII", data, desc_off)
        if ilt_rva == 0 and name_rva == 0 and iat_rva == 0:
            break
        name_off = rva_to_file(pe, name_rva)
        name_end = data.find(b"\x00", name_off)
        dll_name = data[name_off:name_end].decode("ascii", "replace")
        if dll_name.lower() == "kernel32.dll":
            thunk_rva = ilt_rva if ilt_rva != 0 else iat_rva
            t_off = rva_to_file(pe, thunk_rva)
            j = 0
            while True:
                entry = struct.unpack_from("<Q", data, t_off + j * 8)[0]
                if entry == 0:
                    break
                if not (entry & (1 << 63)):
                    # Name import
                    hint_off = rva_to_file(pe, entry)
                    name_start = hint_off + 2
                    name_end2 = data.find(b"\x00", name_start)
                    imp_name = data[name_start:name_end2].decode("ascii", "replace")
                    if imp_name == "Sleep":
                        return pe.image_base + iat_rva + j * 8
                j += 1
            raise SystemExit("error: Sleep not found in KERNEL32.dll imports")
        i += 1
    raise SystemExit("error: KERNEL32.dll not found in imports")


# 7-byte prologue of our trampoline: push rdx; push rcx; push rax; push r8; push r9.
# Used to recognize a binary that's already patched vs one that isn't — if the
# patch site's JMP lands on these bytes, the caller previously installed this
# exact trampoline and we should follow it instead of scanning CC windows.
TRAMPOLINE_PROLOGUE = b"\x52\x51\x50\x41\x50\x41\x51"


def derive_offsets(data: bytes) -> Offsets:
    """Derive patch parameters from the binary. Works on both unpatched
    and patched binaries: the patch-site signature is still present
    post-patch (only the rel32 after `e9` changes), so we follow that jump
    to locate the trampoline directly instead of re-scanning CC windows
    (which would pick a different window once the original one is partially
    consumed). Raises SystemExit with a clear message on any failure."""
    pe = parse_pe(data)
    text = next((s for s in pe.sections if s.name == ".text"), None)
    if text is None:
        raise SystemExit("error: no .text section found")

    patch_site_file = find_signature(data)
    rel32 = struct.unpack_from("<i", data, patch_site_file + 1)[0]
    target_rva = file_to_rva(pe, patch_site_file + 5) + rel32
    target_file = rva_to_file(pe, target_rva)

    target_prologue = data[target_file:target_file + len(TRAMPOLINE_PROLOGUE)]
    if target_prologue == TRAMPOLINE_PROLOGUE:
        # Binary is patched — trampoline is at the jump target; loop top lives
        # in the trampoline's final `jmp rel32` (last 5 bytes).
        trampoline_file = target_file
        jmp_back_off = trampoline_file + TRAMPOLINE_SIZE - 5
        if data[jmp_back_off] != 0xE9:
            raise SystemExit(
                f"error: trampoline at 0x{trampoline_file:x} is missing its JMP-back tail "
                f"(byte 0x{data[jmp_back_off]:02x} at 0x{jmp_back_off:x}, expected 0xe9). "
                "Trampoline appears corrupt."
            )
        jb_rel = struct.unpack_from("<i", data, jmp_back_off + 1)[0]
        loop_top_rva = file_to_rva(pe, jmp_back_off + 5) + jb_rel
        loop_top_file = rva_to_file(pe, loop_top_rva)
    else:
        # Unpatched — rel32 points to loop top; trampoline goes in a fresh CC window.
        loop_top_file = target_file
        trampoline_file = find_cc_padding_window(data, pe)

    sleep_iat_va = find_sleep_iat_va(data, pe)

    return Offsets(
        patch_site_file=patch_site_file,
        loop_top_file=loop_top_file,
        trampoline_file=trampoline_file,
        sleep_iat_va=sleep_iat_va,
        image_base=pe.image_base,
        text_raw=text.raw_ptr,
        text_vaddr=text.virtual_address,
    )


def binary_state(data: bytes, off: Offsets) -> str:
    """Return 'unpatched', 'patched', or 'corrupt' based on the bytes at the
    resolved patch-site and trampoline locations."""
    site = data[off.patch_site_file:off.patch_site_file + 5]
    tramp = data[off.trampoline_file:off.trampoline_file + TRAMPOLINE_SIZE]
    if site[0] != 0xE9:
        return "corrupt"
    if tramp == b"\xcc" * TRAMPOLINE_SIZE:
        return "unpatched"
    if tramp[:len(TRAMPOLINE_PROLOGUE)] == TRAMPOLINE_PROLOGUE:
        return "patched"
    return "corrupt"


# === Patch compilation ===

def compile_patch(off: Offsets) -> tuple[bytes, bytes]:
    """Return (patch_bytes, trampoline_bytes).
    patch_bytes is 5 bytes: JMP rel32 to trampoline.
    trampoline_bytes is 38 bytes: save volatiles, Sleep(1), restore, JMP back to loop top."""
    trampoline_base_va = off.file_to_va(off.trampoline_file)

    tramp = bytearray()
    tramp += b"\x52"              # push rdx   — loop-carried; MUST preserve
    tramp += b"\x51"              # push rcx
    tramp += b"\x50"              # push rax
    tramp += b"\x41\x50"          # push r8
    tramp += b"\x41\x51"          # push r9
    tramp += b"\x48\x83\xec\x20"  # sub $0x20, %rsp  — Win64 shadow space
    tramp += b"\xb9\x01\x00\x00\x00"  # mov $1, %ecx  — Sleep(1ms)

    call_instr_va = trampoline_base_va + len(tramp)
    call_next_va = call_instr_va + 6
    rel_to_sleep = off.sleep_iat_va - call_next_va
    tramp += b"\xff\x15" + rel_to_sleep.to_bytes(4, "little", signed=True)

    tramp += b"\x48\x83\xc4\x20"  # add $0x20, %rsp
    tramp += b"\x41\x59"          # pop r9
    tramp += b"\x41\x58"          # pop r8
    tramp += b"\x58"              # pop rax
    tramp += b"\x59"              # pop rcx
    tramp += b"\x5a"              # pop rdx

    jmp_top_va = off.file_to_va(off.loop_top_file)
    jmp_instr_va = trampoline_base_va + len(tramp)
    rel_top = jmp_top_va - (jmp_instr_va + 5)
    tramp += b"\xe9" + rel_top.to_bytes(4, "little", signed=True)

    patch_site_va = off.file_to_va(off.patch_site_file)
    rel_fwd = trampoline_base_va - (patch_site_va + 5)
    patch = b"\xe9" + rel_fwd.to_bytes(4, "little", signed=True)

    assert len(patch) == 5
    assert len(tramp) == TRAMPOLINE_SIZE, f"trampoline is {len(tramp)} bytes, expected {TRAMPOLINE_SIZE}"
    return bytes(patch), bytes(tramp)


# === Verification ===

# === Revert support ===

def reconstruct_original_site_bytes(data: bytes, off: Offsets) -> bytes:
    """Given a patched binary, reconstruct what the original 5 bytes at the
    patch site should have been, by reading the trampoline's jmp-back
    instruction. Used on revert when we have no pre-patch cache."""
    jmp_back_off = off.trampoline_file + TRAMPOLINE_SIZE - 5
    if data[jmp_back_off] != 0xE9:
        raise SystemExit(
            f"error: trampoline's final instruction at 0x{jmp_back_off:x} is not "
            f"JMP rel32 (byte 0x{data[jmp_back_off]:02x}). Trampoline may be corrupt."
        )
    rel = struct.unpack_from("<i", data, jmp_back_off + 1)[0]
    # jmp back's rel32 is relative to (jmp_back_va + 5), resolves to loop_top_va
    jmp_back_va = off.file_to_va(jmp_back_off)
    loop_top_va = jmp_back_va + 5 + rel
    # Original site jump: e9 (loop_top_va - (site_va + 5))
    site_va = off.file_to_va(off.patch_site_file)
    orig_rel = loop_top_va - (site_va + 5)
    return b"\xe9" + orig_rel.to_bytes(4, "little", signed=True)


# === Mode selection ===

def resolve_offsets(data: bytes, md5: str, use_known: bool) -> tuple[Offsets, str]:
    """Return (offsets, mode_label). mode_label is 'known' or 'derived'."""
    if use_known:
        if md5 not in KNOWN_OFFSETS:
            raise SystemExit(
                f"error: --use-known-offsets requested but MD5 {md5} is not in the "
                f"known-builds table (known: {', '.join(KNOWN_OFFSETS.keys()) or '(none)'}). "
                "Drop --use-known-offsets to let auto-derive try."
            )
        return KNOWN_OFFSETS[md5], "known"

    derived = derive_offsets(data)
    if md5 in KNOWN_OFFSETS:
        known = KNOWN_OFFSETS[md5]
        if derived != known:
            raise SystemExit(
                "error: auto-derive disagrees with known-good offsets for this MD5.\n"
                f"  known:   {known}\n"
                f"  derived: {derived}\n"
                "This is a BUG in auto-derive. Use --use-known-offsets for now and "
                "file an issue."
            )
    return derived, "derived"


# === Apply ===

def print_state(path: str) -> None:
    """Emit a single JSON line describing the binary's patch state.
    Exits 0 on any outcome that can be reported (patched, unpatched,
    corrupt, or binary-not-a-match). Meant for the UI sidecar to poll
    without parsing text output."""
    import json
    try:
        with open(path, "rb") as f:
            data = f.read()
    except FileNotFoundError:
        print(json.dumps({"state": "missing", "path": path}))
        return
    md5 = hashlib.md5(data).hexdigest()
    result = {"path": path, "md5": md5}
    try:
        off = derive_offsets(data)
        result["state"] = binary_state(data, off)
        result["mode"] = "derived"
        if md5 in KNOWN_OFFSETS:
            result["mode"] = "known"
        result["patch_site_file"] = off.patch_site_file
        result["trampoline_file"] = off.trampoline_file
    except SystemExit as e:
        result["state"] = "inapplicable"
        result["reason"] = str(e).replace("error: ", "")
    print(json.dumps(result))


def apply_patch(path: str, revert: bool, dry_run: bool, use_known: bool,
                verbose: bool, idempotent: bool) -> None:
    with open(path, "rb") as f:
        data = f.read()

    md5 = hashlib.md5(data).hexdigest()
    print(f"MD5 of {path}: {md5}")

    off, mode = resolve_offsets(data, md5, use_known)
    if verbose or mode == "derived":
        print(
            f"using {mode} offsets: "
            f"patch_site=0x{off.patch_site_file:x} "
            f"loop_top=0x{off.loop_top_file:x} "
            f"trampoline=0x{off.trampoline_file:x} "
            f"sleep_iat_va=0x{off.sleep_iat_va:x}"
        )

    state = binary_state(data, off)
    patch, tramp = compile_patch(off)

    if revert:
        if state == "unpatched":
            msg = f"binary at {path} is already unpatched; nothing to revert."
            if idempotent:
                print(f"info: {msg}")
                return
            raise SystemExit(f"error: {msg}")
        if state != "patched":
            raise SystemExit(
                f"error: binary at {path} is in an unexpected state ({state}); "
                "refusing to revert. Restore from backup if you have one."
            )
        orig_site = reconstruct_original_site_bytes(data, off)
        new_data = bytearray(data)
        new_data[off.patch_site_file:off.patch_site_file + 5] = orig_site
        new_data[off.trampoline_file:off.trampoline_file + TRAMPOLINE_SIZE] = b"\xcc" * TRAMPOLINE_SIZE
        action = "reverted"
    else:
        if state == "patched":
            msg = f"binary at {path} is already patched; use --revert to undo."
            if idempotent:
                print(f"info: {msg}")
                return
            raise SystemExit(f"error: {msg}")
        if state != "unpatched":
            raise SystemExit(
                f"error: binary at {path} is in an unexpected state ({state}); "
                "refusing to patch."
            )
        new_data = bytearray(data)
        new_data[off.patch_site_file:off.patch_site_file + 5] = patch
        new_data[off.trampoline_file:off.trampoline_file + TRAMPOLINE_SIZE] = tramp
        action = "patched"

    if dry_run:
        new_md5 = hashlib.md5(bytes(new_data)).hexdigest()
        print(f"dry-run: would have {action} binary; new MD5 would be {new_md5}")
        return

    tmp_path = path + ".tmp.patch"
    with open(tmp_path, "wb") as f:
        f.write(bytes(new_data))
    os.replace(tmp_path, path)
    new_md5 = hashlib.md5(bytes(new_data)).hexdigest()
    print(f"{action} in place; new MD5 is {new_md5}")


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
                    help="Use the hard-coded known-builds table instead of auto-derive "
                         "(falls back only — default is auto-derive)")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print resolved offsets even on the known-builds fast path")
    ap.add_argument("--idempotent", action="store_true",
                    help="Exit 0 with an info message if the binary is already in the "
                         "desired state (patched for normal mode, unpatched for --revert). "
                         "Meant for boot scripts that want a no-op on re-run.")
    ap.add_argument("--print-state", action="store_true",
                    help="Emit a single JSON line describing the binary's current patch "
                         "state (md5, state, patch-site offsets) and exit 0. For the UI.")
    args = ap.parse_args()
    if args.print_state:
        print_state(args.path)
        return
    apply_patch(
        args.path,
        revert=args.revert,
        dry_run=args.dry_run,
        use_known=args.use_known_offsets,
        verbose=args.verbose,
        idempotent=args.idempotent,
    )


if __name__ == "__main__":
    main()


# === How auto-derive works ===
#
# 1. Parse the PE headers (DOS + PE + COFF + optional + sections + data
#    directories). Extract: ImageBase, .text section VA + raw offset,
#    import directory, exception (.pdata) directory.
#
# 2. Scan the binary for the 9-byte signature
#       48 8b 0b        mov    (%rbx),%rcx
#       8b c7           mov    %edi,%eax
#       87 41 34        xchg   %eax,0x34(%rcx)
#       e9              jmp    rel32  <-- patch this
#    This signature has to be unique across the entire binary; if count
#    != 1, the script refuses rather than guess. The `e9` byte's file
#    offset is PATCH_SITE_FILE. The 4 bytes after `e9` are the rel32;
#    adding that (signed) to (PATCH_SITE_FILE+5) as RVAs gives LOOP_TOP.
#
# 3. Scan .text for CC-padding runs ≥38 bytes. For each candidate, check
#    whether its RVA range overlaps any RUNTIME_FUNCTION entry in .pdata.
#    Pick the first candidate outside all RUNTIME_FUNCTION coverage —
#    that's TRAMPOLINE_FILE.
#
# 4. Walk the import directory; for each IMAGE_IMPORT_DESCRIPTOR, read
#    the DLL name. When it matches "KERNEL32.dll", walk the INT/IAT
#    looking for the name "Sleep". The IAT entry VA for Sleep is
#    ImageBase + FirstThunk_RVA + index*8.
#
# 5. Emit the patch bytes (5-byte e9 jmp to trampoline) and trampoline
#    bytes (push volatiles, Sleep(1), pop, jmp back to loop top) with
#    the four derived values; install at the derived offsets.
