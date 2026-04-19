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

Safety:
  - Idempotent: script rejects an already-patched binary (wrong 5 bytes
    at the patch site).
  - Rollback: `--revert` flips the patch back. Or just restore the
    `.original` backup if you kept one.
  - No new imports, no new code sections; 43 bytes modified total
    (5 at the patch site + 38 in a pre-existing CC-padding window).

Measured impact (2026-04-19, sf-west-1 canary pod, AMD Ryzen 9 9955HX, 32-core host):
  - Baseline idle CPU  : 206.65%  (two GameThreads at ~91% each + ~5% main)
  - Patched idle CPU   :   5.08%  (mean of 10x 30s samples)
  - Improvement        : 97.5% reduction
  - Per-thread strace  : before 0 syscalls/3s; after 2818 pselect6 + 2818
                         sched_yield + 5636 getrusage / 3 s (Wine's Sleep
                         implementation)
  - Functionality      : backend registration preserved, invite code
                         unchanged, 20+ minute continuous stability

Target binary: UE5.6 R5 build `WindroseServer-Win64-Shipping.exe` shipped
in Windrose 0.10.0 — md5 8a62138c8fd19ede9ec8a5cf10579cb8 (post-2026-04-19
Steam update). The previous build (md5 61e320a6a45f4ac539f2c5d0f7b7ff2c)
had the same bug at slightly different offsets — the whole `reset()`
function moved by -0x440 but PATCH_SITE_ORIG bytes and the trampoline
window are identical across the two. A binary whose first-5-bytes-at-
patch-site differ from the original is rejected.

Usage:
  python3 patch-idle-cpu.py /path/to/WindroseServer-Win64-Shipping.exe
  python3 patch-idle-cpu.py /path/to/WindroseServer-Win64-Shipping.exe --revert
  python3 patch-idle-cpu.py /path/to/WindroseServer-Win64-Shipping.exe --dry-run
"""
import argparse
import hashlib
import os
import struct
import sys

# --- Hard-coded, build-specific patch parameters ---
# If Windrose ships a new build, re-derive these (see comments at bottom of file).
EXPECTED_MD5 = "8a62138c8fd19ede9ec8a5cf10579cb8"

# Patch site: the `jmp <loop_top>` at the tail of the hot loop in
# boost::asio::detail::socket_select_interrupter::reset().
# Previous build: file offset 0x4C98A09 / loop top 0x4C98270.
# Current build: whole function shifted by -0x440; the relative bytes
# ("jmp -0x79e") are identical, only the absolute file offset changed.
PATCH_SITE_FILE = 0x4C985C9
PATCH_SITE_ORIG = bytes.fromhex("e962f8ffff")  # jmp -0x79e -> 0x4C97E30

# Loop-top file offset (the target of the jmp we're replacing). Used to
# compute the trampoline's final `jmp back` rel32.
LOOP_TOP_FILE = 0x4C97E30

# Trampoline goes in a 655-byte CC-padding window at 0xd30371 which is
# within .text (raw) but between functions per .pdata. This window
# survived the 2026-04-19 rebuild at the same absolute offset.
TRAMPOLINE_FILE = 0xD30371
TRAMPOLINE_ORIG = b"\xcc" * 38  # we require existing bytes are all int3 padding

# Sleep IAT entry (KERNEL32.dll!Sleep). The IAT slot moved +0x20008 in
# the 2026-04-19 rebuild (0x14C282428 -> 0x14C2A2430). Anyone bumping
# this patch should verify the IAT entry didn't move again.
SLEEP_IAT_VA = 0x14C2A2430

# Image base, .text raw start, .text vaddr start.
IMAGE_BASE = 0x140000000
TEXT_RAW = 0x600
TEXT_VADDR = 0x1000


def file_to_va(file_off: int) -> int:
    return IMAGE_BASE + file_off - TEXT_RAW + TEXT_VADDR


def compile_patch() -> tuple[bytes, bytes]:
    """Return (patch_bytes, trampoline_bytes).

    patch_bytes is 5 bytes: JMP rel32 to trampoline
    trampoline_bytes is 38 bytes: save volatiles, Sleep(1), restore, JMP back to loop top
    """
    trampoline_base_va = file_to_va(TRAMPOLINE_FILE)

    tramp = bytearray()
    tramp += b"\x52"              # push rdx   — loop-carried register, MUST preserve
    tramp += b"\x51"              # push rcx   — volatile; belt-and-braces
    tramp += b"\x50"              # push rax
    tramp += b"\x41\x50"          # push r8
    tramp += b"\x41\x51"          # push r9    (5x 1-byte + 2x 2-byte = 7 bytes pushed; with +8 from call return = 16-align)
    tramp += b"\x48\x83\xec\x20"  # sub $0x20, %rsp   — shadow space for Win64 ABI
    tramp += b"\xb9\x01\x00\x00\x00"  # mov $1, %ecx  — Sleep(1ms)

    # `call qword ptr [rip + rel32]` targeting Sleep IAT.
    # Encoded as FF 15 rel32; rel32 = target_VA - (VA_of_next_instr).
    call_instr_va = trampoline_base_va + len(tramp)
    call_next_va = call_instr_va + 6
    rel_to_sleep = SLEEP_IAT_VA - call_next_va
    tramp += b"\xff\x15" + rel_to_sleep.to_bytes(4, "little", signed=True)

    tramp += b"\x48\x83\xc4\x20"  # add $0x20, %rsp
    tramp += b"\x41\x59"          # pop r9
    tramp += b"\x41\x58"          # pop r8
    tramp += b"\x58"              # pop rax
    tramp += b"\x59"              # pop rcx
    tramp += b"\x5a"              # pop rdx

    # jmp rel32 back to the loop top (file offset depends on the build).
    jmp_top_va = file_to_va(LOOP_TOP_FILE)
    jmp_instr_va = trampoline_base_va + len(tramp)
    rel_top = jmp_top_va - (jmp_instr_va + 5)
    tramp += b"\xe9" + rel_top.to_bytes(4, "little", signed=True)

    # Site patch: jmp rel32 to trampoline.
    patch_site_va = file_to_va(PATCH_SITE_FILE)
    rel_fwd = trampoline_base_va - (patch_site_va + 5)
    patch = b"\xe9" + rel_fwd.to_bytes(4, "little", signed=True)

    assert len(patch) == 5
    assert len(tramp) == 38, f"trampoline is {len(tramp)} bytes, expected 38"
    return bytes(patch), bytes(tramp)


def verify_original(data: bytes) -> None:
    got = data[PATCH_SITE_FILE:PATCH_SITE_FILE + len(PATCH_SITE_ORIG)]
    if got != PATCH_SITE_ORIG:
        raise SystemExit(
            f"error: bytes at patch site 0x{PATCH_SITE_FILE:x} are {got.hex()}; "
            f"expected {PATCH_SITE_ORIG.hex()}. "
            f"Binary is already patched, or it's a different build."
        )
    got_tramp = data[TRAMPOLINE_FILE:TRAMPOLINE_FILE + len(TRAMPOLINE_ORIG)]
    if got_tramp != TRAMPOLINE_ORIG:
        raise SystemExit(
            f"error: bytes at trampoline target 0x{TRAMPOLINE_FILE:x} are not all 0xCC "
            f"padding; first 16 bytes: {got_tramp[:16].hex()}. "
            f"This build doesn't have the padding window we expect."
        )


def verify_patched(data: bytes, patch: bytes, tramp: bytes) -> None:
    got = data[PATCH_SITE_FILE:PATCH_SITE_FILE + len(patch)]
    if got != patch:
        raise SystemExit(
            f"error: patch site bytes after write are {got.hex()}; expected {patch.hex()}."
        )
    got_tramp = data[TRAMPOLINE_FILE:TRAMPOLINE_FILE + len(tramp)]
    if got_tramp != tramp:
        raise SystemExit(
            f"error: trampoline bytes after write differ from expected."
        )


def apply_patch(path: str, revert: bool, dry_run: bool) -> None:
    with open(path, "rb") as f:
        data = f.read()

    md5 = hashlib.md5(data).hexdigest()
    print(f"MD5 of {path}: {md5}")

    patch, tramp = compile_patch()

    if revert:
        # Revert: require patched bytes at both sites; restore original at both.
        verify_patched(data, patch, tramp)
        new_data = bytearray(data)
        new_data[PATCH_SITE_FILE:PATCH_SITE_FILE + len(PATCH_SITE_ORIG)] = PATCH_SITE_ORIG
        new_data[TRAMPOLINE_FILE:TRAMPOLINE_FILE + len(TRAMPOLINE_ORIG)] = TRAMPOLINE_ORIG
        action = "reverted"
    else:
        if md5 != EXPECTED_MD5:
            print(
                f"warning: MD5 doesn't match expected {EXPECTED_MD5}. "
                "This may be a different Windrose build; the patch offsets probably need "
                "re-derivation. Proceeding only because byte verification below will catch "
                "mismatches, but review the output carefully."
            )
        verify_original(data)
        new_data = bytearray(data)
        new_data[PATCH_SITE_FILE:PATCH_SITE_FILE + len(patch)] = patch
        new_data[TRAMPOLINE_FILE:TRAMPOLINE_FILE + len(tramp)] = tramp
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
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path", help="Path to WindroseServer-Win64-Shipping.exe")
    ap.add_argument("--revert", action="store_true", help="Undo a prior patch (restore original bytes)")
    ap.add_argument("--dry-run", action="store_true", help="Print intended changes without modifying the file")
    args = ap.parse_args()
    apply_patch(args.path, revert=args.revert, dry_run=args.dry_run)


if __name__ == "__main__":
    main()


# === re-derivation notes (for the next Windrose build) ===
#
# The two offsets are tied to this specific Windrose build's code layout. If
# MD5 changes, re-derive as follows:
#
# 1. Identify the hot function by sampling GDB RIP on the two "GameThread"
#    userspace-spinning threads. Expect the hot PCs to cluster in ~5
#    instructions inside a single function.
#
# 2. The function contains string references to the Boost.Asio source file
#    "asio\detail\impl\socket_select_interrupter.ipp" — grep the binary
#    for the ASCII string and scan backwards for the function prologue
#    `48 89 5c 24 10 48 89 74 24 18 48 89 7c 24 20 55 41 54 41 55 41 56 41 57`.
#
# 3. The loop-continue tail has this distinctive pattern just before
#    jumping back to the loop top:
#       mov (%rbx), %rcx        48 8b 0b
#       mov %edi, %eax          8b c7
#       xchg %eax, 0x34(%rcx)   87 41 34
#       jmp <loop_top>          e9 xx xx xx xx
#    The file offset of that `jmp` is PATCH_SITE_FILE.
#
# 4. Find a 38-byte cc-padding window within +/- 2 GB of PATCH_SITE_FILE
#    that is NOT covered by any .pdata RUNTIME_FUNCTION entry (i.e. inter-
#    function padding, not intra-function). Put the trampoline there.
#
# 5. Find KERNEL32.dll!Sleep's IAT entry VA — parse the PE import tables,
#    locate Sleep, record its IAT RVA and VA.
#
# 6. Recompile both jmp rel32 offsets.
