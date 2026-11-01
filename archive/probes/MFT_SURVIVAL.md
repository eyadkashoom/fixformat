# MFT survival after the reformat + reinstall - measured

Image: `F:\hard512\diskimage_PhysicalDrive0_20261028_090245.img.partial`  
512,107,741,184 bytes, read 4.54 GiB of it in 60 s (budget 8 GiB).  
Script: `probes/mft_survival.py` (stdlib only, read-only, `open(path,'rb')`).

## Verdict

**NO - the 461 GiB system volume is BitLocker (FVE 0x0802): 3884/3928 non-zero strided samples are ciphertext, 0 plaintext FILE records in the 4 GiB scanned byte-for-byte, so old names survive at 0% against a 5% threshold - not because the MFT is clean but because it cannot be read without the key.**

Threshold: **5%** of MFT records carrying an old name (the V6_PLAN section 4 kill criterion).  
Blocked by: **bitlocker** (partition 2)

## What this image actually is

GPT parsed from LBA 1 (`EFI PART`, 128 x 128-byte entries), no fallback scan needed:

| # | type | size | first LBA | VBR OEM | volume |
|---|------|------|-----------|---------|--------|
| 0 | EFI system | 0.49 GiB | 2048 | `MSDOS5.0` | EFI system partition |
| 1 | MS reserved | 0.12 GiB | 1026048 | `(none)` | Microsoft reserved partition |
| 2 | Basic data | 461.09 GiB | 1288192 | `-FVE-FS-` | Basic data partition |
| 3 | Windows recovery | 1.14 GiB | 968261632 | `NTFS····` |  |
| 4 | Windows recovery | 12.08 GiB | 970661888 | `NTFS····` | Image |
| 5 | Windows recovery | 2.00 GiB | 995999744 | `NTFS····` | DELLSUPPORT |

The 461 GiB Basic-data partition - the Windows system volume - is **not** plaintext NTFS. Its VBR OEM id is `-FVE-FS-`: it is a **BitLocker** volume. That single fact decides this probe.

The three plaintext NTFS volumes are Dell factory/recovery partitions, not user data: part 3 (`no label`) is 1.1 GiB and its largest file is `Winre.wim` (1.02 GiB); part 4 (`Image`) is 12.1 GiB and its largest file is `N3YY3_SWB_W11P_64_USB_META_1225E.wim` (8.34 GiB); part 5 (`DELLSUPPORT`) is 2.0 GiB and its largest file is `sos.wim` (0.73 GiB). That is why the numbers below describe recovery images, not the reformatted system volume.

## The blocker, measured

* FVE header: signature `-FVE-FS-`, version `0x0802`, metadata block 2,970,438 bytes (BitLocker, XTS-AES; the dummy BPB BitLocker writes into that header even claims `FAT32`, which is why the OEM id has to be checked first).
* Strided entropy map over the data-bearing region (4356 samples, 128 KiB every 16 MiB): **3884 high-entropy (>7.9 bits/byte), 428 all-zero, 44 mixed/low**. Encrypted data is indistinguishable from random; plaintext NTFS is not (a `FILE` record plus its `$FILE_NAME` sits around 3-5 bits/byte).
* The 44 mixed/low-entropy chunks were not just counted, they were searched for NTFS artifacts: **0 1024-aligned `FILE` signatures and 0 `NTFS    ` boot signatures** in them. They are boundaries between ciphertext and zeroed regions, not surviving plaintext filesystems.
* Contiguous all-zero runs of >= 64 MiB inside the data region: **24.0-30.1 GiB, 60.0-60.1 GiB, 64.2-64.3 GiB**. Nothing - encrypted or not - is stored there, so no record can survive there either.
* Sequential scan of the first 4 GiB of that volume (409 MiB/s, every byte): **0 1024-aligned `FILE` signatures, 0 of them a valid record with a $FILE_NAME, 0 `NTFS    ` boot signatures**. At 2^-32 per random position, four GiB of ciphertext should yield ~0.001 hits, so zero is the expected number for encrypted data and would also be the number for a genuinely clean MFT - which is exactly why the entropy map matters as the discriminator.
* The image stops holding data at **73,081,552,896 bytes (68.1 GiB) into the volume** - 15% of the 461 GiB partition. Everything past that is zeros, i.e. this `.partial` image captured only the first ~68 GiB of a 477 GiB disk. That is an independent limitation on top of the encryption.

## Where the MFT could still be read: the plaintext NTFS volumes

All three are freshly formatted Dell/WinRE partitions, not the system volume.

| part | size | $MFT offset | records | in use | unused-valid | unused-valid + old name | garbage |
|---|---|---|---|---|---|---|---|
| 3 | 1.15 GiB | 409,640,960 | 256 | 41 | 0 | **0** | 215 |
| 4 | 12.08 GiB | 3,221,225,472 | 256 | 66 | 0 | **0** | 190 |
| 5 | 2.00 GiB | 715,128,832 | 768 | 587 | 0 | **0** | 181 |
| **total** | | | **1,280** | **694** | **0** | **0** | **586** |

* share of the readable MFT that is unused-but-readable: **0.00%**
* UNUSED-VALID records that carry a name the current tree does not reference: **0**
* every non-in-use record in these three MFTs is a 0x00-filled slot (586 of them), with no `FILE` record failing the fixup and no valid record without a name - so `UNUSED-VALID` = 0 is a measurement, not a parser failure.

  Read that carefully: these are *newly created* volumes whose MFT free space was never written, so zero slots are the expected result and prove nothing about whether formatting an existing volume wipes pre-existing records. The only thing they prove is that this image holds no residue on the volumes that can be read at all.

## Sample names with size and timestamp

No orphan names exist to sample - there are zero `UNUSED-VALID` records carrying a name. The 20 rows below are the largest **in-use** records instead, so the reader can see that the parser produces real names, real sizes and real timestamps:

| record | name | size | modified | part |
|---|---|---|---|---|
| 44 | `N3YY3_SWB_W11P_64_USB_META_1225E.wim` | 8,950,532,331 | 2026-10-15 22:29:25 | 4 |
| 38 | `Winre.wim` | 1,090,117,188 | 2026-10-16 11:22:43 | 3 |
| 546 | `sos.wim` | 786,875,231 | 2026-10-15 22:38:52 | 5 |
| 47 | `SupportAssistx64-4.6.3.23467.zip` | 381,387,145 | 2026-10-15 22:31:56 | 4 |
| 51 | `Dell-Optimizer-Application_VPGX3_WIN64_6.0.2.0_A00.EXE` | 353,772,536 | 2026-10-15 22:32:37 | 4 |
| 406 | `installers.zip` | 76,359,072 | 2026-10-15 22:38:06 | 5 |
| 54 | `Intel-AX211-AX210-AX201-AX200-9560-9260-9462-8265-3165_RCF88_WIN_22.110.1.1_A23_Win10-x64.zip` | 34,730,135 | 2026-10-15 22:32:58 | 4 |
| 49 | `Dell-Command-Update_MFNCX_WIN_5.0.0_A00_01.EXE` | 26,875,464 | 2026-10-15 22:32:12 | 4 |
| 94 | `hdmengine_hdmsdk.dll` | 23,366,568 | 2026-10-15 22:38:03 | 5 |
| 528 | `msjh.ttc` | 21,402,152 | 2026-10-15 22:38:07 | 5 |
| 2 | `$LogFile` | 19,890,176 | 2026-10-15 22:21:03 | 4 |
| 542 | `simsun.ttc` | 18,214,472 | 2026-10-15 22:38:07 | 5 |
| 403 | `audit_util.zip` | 15,648,627 | 2026-10-15 22:38:04 | 5 |
| 156 | `SDSSmartRepairTools.dll` | 14,646,656 | 2026-10-15 22:38:03 | 5 |
| 543 | `yugothm.ttc` | 13,923,436 | 2026-10-15 22:38:07 | 5 |
| 292 | `storarc.dll` | 13,497,016 | 2026-10-15 22:38:04 | 5 |
| 527 | `malgun.ttf` | 13,457,164 | 2026-10-15 22:38:07 | 5 |
| 34 | `$TxfLogContainer00000000000000000001` | 7,340,032 | 2026-10-17 02:29:58 | 4 |
| 35 | `$TxfLogContainer00000000000000000002` | 7,340,032 | 2026-10-15 22:21:03 | 4 |
| 2 | `$LogFile` | 5,210,112 | 2026-10-16 12:17:48 | 3 |

## Sanity checks

| check | result |
|---|---|
| part 3: record 0 is `$MFT` (FILE, fixup OK, $SI + $FILE_NAME) | PASS |
| part 3: record 5 is a directory named `.` | PASS |
| part 3: `$Bitmap` size = ceil(clusters/8) (37,504 vs 37,504 bytes for 300,031 clusters) | PASS |
| part 4: record 0 is `$MFT` (FILE, fixup OK, $SI + $FILE_NAME) | PASS |
| part 4: record 5 is a directory named `.` | PASS |
| part 4: `$Bitmap` size = ceil(clusters/8) (395,904 vs 395,904 bytes for 3,167,231 clusters) | PASS |
| part 5: record 0 is `$MFT` (FILE, fixup OK, $SI + $FILE_NAME) | PASS |
| part 5: record 5 is a directory named `.` | PASS |
| part 5: `$Bitmap` size = ceil(clusters/8) (65,472 vs 65,472 bytes for 523,775 clusters) | PASS |
| in-use names look like a real NTFS volume ($MFT, $Bitmap, $UpCase, $LogFile, $Extend\$RmMetadata\TxfLog, System Volume Information, WindowsRE, osri) | PASS |
| in-use names look like a full Windows install (Windows, Program Files, Users, pagefile.sys) | **CANNOT BE EVALUATED** - those records are on the BitLocker volume |

The last row is the honest limit of this probe. The parser is proven correct on three independent plaintext NTFS volumes (record 0 = `$MFT` with a valid run list, record 5 = `.`, `$Bitmap`/`$UpCase`/`$LogFile`/TxF containers all present with plausible sizes). What it cannot do is read a volume encrypted with a key this probe does not have. So the answer to "do the OLD MFT records survive" is not "no" - it is "not measurable from this image as captured".

## What made the parse unreliable / what to watch

1. **The layout notes this probe was written against were wrong in two places**, and both would have produced nonsense numbers silently: (a) `0x0D` in an NTFS VBR is *sectors per cluster* (8 -> 4096-byte clusters), not a shift - reading `0xF6` semantics there gives a 131,072-byte cluster and a `$MFT` offset 32x too far; (b) `0xF6`/`-10` belongs to `0x40`, clusters per MFT record (-10 -> 1024-byte records). With the wrong cluster size all three `$MFT` reads land in the middle of a `.wim` and return garbage.
2. **`-FVE-FS-` instead of `NTFS    `** on the only volume that matters. A scan that just looks for `NTFS    ` finds three partitions and would happily report on them, producing numbers about recovery partitions while claiming to describe the system disk.
3. **Volume label**: the NTFS BPB has no label field; the label lives in `$Volume` (record 3, attribute `0x60`). Reading `0x47` of the BPB returns serial bytes and produces mojibake.
4. **The image is truncated and holey**: it is a 512,107,741,184-byte file, not sparse (checked with `fsutil sparse queryflag`), but everything past ~68 GiB of the data partition is zeros, and there is a ~6 GiB all-zero band at 24-30 GiB of it. Either the capture stopped and the unwritten remainder was zero-filled, or those sectors were unreadable and zero-filled by the imager; in both cases they hold no data, so any whole-disk claim from this file covers at most 15% of the disk.
5. **Zero-filled MFT slack is not damage**: unused records read as all-0x00, which the classifier calls GARBAGE. That is correct for a fresh format and must not be reported as "corrupt records".
6. BitLocker's FVE header carries a dummy BPB that advertises `FAT32` and `NO NAME`; classifying by BPB fields before the OEM id mislabels the volume.

## Consequence for V6

* MFT triage cannot be validated on this image. The kill criterion of V6_PLAN section 4 is not refuted - it is simply untested, because 0 bytes of the old `$MFT` are readable.
* If this disk's BitLocker recovery key is available (48-digit recovery password, or the key escrowed to the Microsoft account / Entra ID of the machine), the same script run against a decrypted volume - or an offline `dislocker`-style mapping - would answer the question in minutes. Without the key, no amount of MFT parsing helps.
* For the general case the encryption finding is itself a triage rule: check `-FVE-FS-` at every partition's first sector and tell the user *before* a 40-minute carve that their system volume is encrypted and a key is required. That is a one-line check with a large payoff and it belongs in v6 regardless of the MFT work.

