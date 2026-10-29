# Directory artefacts: which whole-file recovery sources exist in this image

Probe for V6 plan section 9c, run read-only against the raw image. Script: `probes/dir_artifacts.py`, which rewrites this file. 64.82 MiB read in 869 reads (64.8 MiB of the 6 GiB budget).

## 0. Headline

**The 495 GB Windows volume in this image is BitLocker-encrypted** - its VBR carries the `-FVE-FS-` OEM id instead of `NTFS    ` and its payload measures 7.99-8.00 bits/byte. Its MFT, `$Recycle.Bin`, `System Volume Information` and `$Extend` therefore cannot be reached at all: everything the user ever stored there is behind FVE, not behind a parsing problem.

The only readable NTFS volumes are three small OEM/recovery partitions (1.14 GiB, 12.08 GiB, 2.00 GiB - WinRE, a Dell factory image and a vendor rescue image). On those the MFT works and hands back **417 files / 11.14 GiB**, none of it user data; the Recycle Bin is present but empty, and **there is no VSS catalog, no snapshot store and no `$UsnJrnl` anywhere in the image**.

| source | status | files it could hand back |
|---|---|---|
| MFT direct | PRESENT on 3 of 6 partitions, UNREADABLE on the data volume | 417 real files (462 counting `$`-metafiles) |
| `$Recycle.Bin` | present, empty | 0 |
| VSS | ABSENT | 0 |
| `$UsnJrnl:$J` | ABSENT | 0 |

## 1. Image and partition table

| item | value |
|---|---|
| image | `F:\hard512\diskimage_PhysicalDrive0_20261028_090245.img.partial` |
| size | 476.94 GiB (512107741184 bytes, 1000210432 sectors) |
| sector 0 | MBR stub, boot flag `0x00`, signature `55aa`, one entry type `0xee` covering LBA 1..+0xffffffff |
| GPT | `EFI PART` rev 0.1 at LBA 1, header CRC `da77c761`, 128 entries x 128 B at LBA 2 |
| usable LBA range | 34..1000215182 |
| backup GPT | declared at LBA 1000215215, image ends at LBA 1000210431: the last 4816 sectors (~2.35 MiB) of the disk were never captured - this is the interrupted image |
| disk GUID | `{a03dadc0-22ad-4c2c-8243-6e30e9b2037e}` |

| # | type | first LBA | last LBA | size | OEM id at 0x03 | kind | label |
|---|---|---|---|---|---|---|---|
| 0 | EFI system partition | 2048 | 1026047 | 500.00 MiB | `MSDOS5.0` | FAT | EFI system partition |
| 1 | Microsoft reserved (MSR) | 1026048 | 1288191 | 128.00 MiB | `........` | empty (MSR) | Microsoft reserved partition |
| 2 | Microsoft basic data | 1288192 | 968261631 | 461.09 GiB | `-FVE-FS-` | BitLocker (FVE) | Basic data partition |
| 3 | Windows recovery (WinRE) | 968261632 | 970661887 | 1.14 GiB | `NTFS    ` | NTFS | - |
| 4 | Windows recovery (WinRE) | 970661888 | 995999743 | 12.08 GiB | `NTFS    ` | NTFS | - |
| 5 | Windows recovery (WinRE) | 995999744 | 1000189951 | 2.00 GiB | `NTFS    ` | NTFS | - |

Six partitions, not the four the earlier layout notes assumed: all 128 GPT entries have to be read to see the 12.1 GiB and 2.0 GiB recovery volumes at the end of the disk.

## 2. NTFS volumes: VBR and MFT

The MFT is located by reading record 0 at the `$MFT` LCN from the VBR and following the run list of its unnamed `$DATA`; a fragmented `$MFT` is handled, though none of these three is fragmented. Records are 1024 bytes and the USA fixup is applied before any attribute is read.

| # | first LBA | size | cluster | MFT LCN | MFT runs | $MFT size | records | in use | dirs | files | fixup fails |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 3 | 968261632 | 1.14 GiB | 4096 B (spc 8) | 100010 | vcn 0 -> lcn 100010, 64 cl | 256.00 KiB | 256 | 41 | 13 | 24 | 0 |
| 4 | 970661888 | 12.08 GiB | 4096 B (spc 8) | 786432 | vcn 0 -> lcn 786432, 64 cl | 256.00 KiB | 256 | 66 | 28 | 34 | 0 |
| 5 | 995999744 | 2.00 GiB | 4096 B (spc 8) | 174592 | vcn 0 -> lcn 174592, 192 cl | 768.00 KiB | 768 | 587 | 170 | 413 | 0 |

Volume serials: `00225b97225b9110` (partition 3), `a66277166276ea87` (partition 4), `b69278cf92789599` (partition 5).

## 3. Source by source

### 3.1 MFT direct

**Enumeration method, stated explicitly:** one pass over the MFT, grouping in-use records by the parent reference in their `$FILE_NAME` attribute - the shortcut section 9c sanctions. Directory `$I30` B-trees were *not* walked for the child lists; instead record 5's own `$I30` (`INDEX_ROOT` plus every `INDX` block of `$INDEX_ALLOCATION`, fixups applied) was parsed as an independent check of each root listing:

| # | names in $I30 | names from the walk | missing from walk | missing from index | verdict |
|---|---|---|---|---|---|
| 3 | 13 | 13 | none | none | match |
| 4 | 14 | 14 | none | none | match |
| 5 | 18 | 18 | none | none | match |

| # | files with $DATA | real files (not `$`-metafiles) | bytes in real files | newest mtime | largest entry |
|---|---|---|---|---|---|
| 3 | 21 | 6 | 1.02 GiB | 2026-10-16 12:27:07Z | `Winre.wim` (1.02 GiB) |
| 4 | 31 | 16 | 9.08 GiB | 2026-10-16 12:16:47Z | `N3YY3_SWB_W11P_64_USB_META_1225E.wim` (8.34 GiB) |
| 5 | 410 | 395 | 1.04 GiB | 2026-10-16 13:21:08Z | `sos.wim` (750.42 MiB) |

Largest real files, i.e. what these volumes would hand back whole:

- **partition 3** (1.14 GiB volume, 6 real files):
  - `Winre.wim` - 1.02 GiB, mtime 2026-10-16 12:17:48Z
  - `boot.sdi` - 3.02 MiB, mtime 2026-10-16 12:17:48Z
  - `tracking.log` - 20.00 KiB, mtime 2026-10-16 12:19:27Z
  - `ReCustomization.xml` - 15.92 KiB, mtime 2026-10-16 12:27:07Z
  - `ReAgent.xml` - 1.08 KiB, mtime 2026-10-16 12:27:07Z
  - `SrSettings.ini` - 0 B, mtime 2026-10-16 12:17:48Z
- **partition 4** (12.08 GiB volume, 16 real files):
  - `N3YY3_SWB_W11P_64_USB_META_1225E.wim` - 8.34 GiB, mtime 2026-10-15 22:29:25Z
  - `SupportAssistx64-4.6.3.23467.zip` - 363.72 MiB, mtime 2026-10-15 22:31:56Z
  - `Dell-Optimizer-Application_VPGX3_WIN64_6.0.2.0_A00.EXE` - 337.38 MiB, mtime 2026-10-15 22:32:37Z
  - `Intel-AX211-AX210-AX201-AX200-9560-9260-9462-8265-3165_RCF88_WIN_22.110.1.1_A23_Win10-x64.zip` - 33.12 MiB, mtime 2026-10-15 22:32:58Z
  - `Dell-Command-Update_MFNCX_WIN_5.0.0_A00_01.EXE` - 25.63 MiB, mtime 2026-10-15 22:32:12Z
  - `Network-Ethernet_3K7FF_WT4_A27.zip` - 1.94 MiB, mtime 2026-10-15 22:33:07Z
  - `IRST_0F5G4_WT64.zip` - 1.57 MiB, mtime 2026-10-15 22:33:01Z
  - `Intel-Serial-IO-Driver_HTFCM_WIN_30.100.2148.1_A01_Win10-x64.zip` - 331.94 KiB, mtime 2026-10-15 22:33:10Z
- **partition 5** (2.00 GiB volume, 395 real files):
  - `sos.wim` - 750.42 MiB, mtime 2026-10-15 22:38:53Z
  - `installers.zip` - 72.82 MiB, mtime 2026-10-15 22:38:05Z
  - `hdmengine_hdmsdk.dll` - 22.28 MiB, mtime 2026-10-15 22:38:03Z
  - `msjh.ttc` - 20.41 MiB, mtime 2026-10-15 22:38:07Z
  - `simsun.ttc` - 17.37 MiB, mtime 2026-10-15 22:38:07Z
  - `audit_util.zip` - 14.92 MiB, mtime 2026-10-15 22:38:04Z
  - `SDSSmartRepairTools.dll` - 13.97 MiB, mtime 2026-10-15 22:38:03Z
  - `yugothm.ttc` - 13.28 MiB, mtime 2026-10-15 22:38:07Z

### 3.2 `$Recycle.Bin`

| # | `$Recycle.Bin` | SIDs present | `$I` files | `$R` files | complete pairs | orphan `$I` | orphan `$R` |
|---|---|---|---|---|---|---|---|
| 3 | **absent** | - | 0 | 0 | 0 | 0 | 0 |
| 4 | present | `S-1-5-18` | 0 | 0 | 0 | 0 | 0 |
| 5 | present | `S-1-5-18` | 0 | 0 | 0 | 0 | 0 |

No `$I` file exists on any readable volume, so there is **no** original path, size or deletion time to report: **0 pairs**. Two volumes do have a `$Recycle.Bin` directory, holding only the SID `S-1-5-18` (SYSTEM) and nothing inside - exactly what Windows creates on a volume that no interactive user has ever deleted from. The user's own bin (per-user SID subfolders with `$I`/`$R` pairs) is on the encrypted volume.

The `$I` parser had nothing real to chew on, so the run exercises it against synthetic blobs: v1 (544 bytes, path `C:\Users\eyad\Documents\report final.docx`, deleted 2023-09-24 03:33:20Z) and v2 (74 bytes, path `D:\Music\track 01.flac`, deleted 2023-11-21 00:26:40Z) - parser produces the right path, size and FILETIME for both.

### 3.3 VSS / shadow copies

`System Volume Information` exists on all three readable volumes and on none of them holds the catalog `{3808876b-c176-4e48-b7ae-04046e6cc752}` or any `{<guid>}{3808876b-c176-4e48-b7ae-04046e6cc752}` store:

- partition 3: `AadRecoveryPasswordDelete`/, `ClientRecoveryPasswordRotation`/, `FveDecryptedVolumeFolder`/, `ResumeProtectionFolder`/, `tracking.log`
- partition 4: `AadRecoveryPasswordDelete`/, `ClientRecoveryPasswordRotation`/, `FveDecryptedVolumeFolder`/, `ResumeProtectionFolder`/, `tracking.log`
- partition 5: `AadRecoveryPasswordDelete`/, `ClientRecoveryPasswordRotation`/, `FveDecryptedVolumeFolder`/, `ResumeProtectionFolder`/, `tracking.log`

Those four subdirectories (`AadRecoveryPasswordDelete`, `ClientRecoveryPasswordRotation`, `FveDecryptedVolumeFolder`, `ResumeProtectionFolder`) plus `tracking.log` are the FVE/BitLocker and volume-tracking scaffolding Windows writes on a volume it manages - not shadow copies. Totals: **0 catalog files, 0 snapshot stores, 0 bytes of shadow storage**. The 16 KiB store-header probe at offset 0x1E00 (GUID signature, entropy, first 64 bytes) is implemented but had no store to run on, so it is unproven against real VSS data.

### 3.4 `$Extend\$UsnJrnl:$J`

| # | `$Extend` | children | `$UsnJrnl` record | `$J` allocated | `$J` real |
|---|---|---|---|---|---|
| 3 | present | `$Deleted`, `$ObjId`, `$Quota`, `$Reparse`, `$RmMetadata` | **absent** | - | - |
| 4 | present | `$Deleted`, `$ObjId`, `$Quota`, `$Reparse`, `$RmMetadata` | **absent** | - | - |
| 5 | present | `$Deleted`, `$ObjId`, `$Quota`, `$Reparse`, `$RmMetadata` | **absent** | - | - |

No volume in this image has a USN journal at all, so there are no create/delete/rename records to harvest - not a parser limit, the stream does not exist. `$Extend` itself is healthy (`$Deleted`, `$ObjId`, `$Quota`, `$Reparse`, `$RmMetadata`); Windows only creates `$UsnJrnl` on volumes where journaling was switched on, and OEM recovery volumes never have it.

## 4. Why the volume that matters is dark: partition 2 is BitLocker

Partition 2: 461.09 GiB, LBA 1288192..968261631, OEM id `-FVE-FS-`.

| evidence | reading |
|---|---|
| OEM id at offset 3 | `-FVE-FS-` - an FVE stub, not `NTFS    ` |
| stub text | `NO NAME    FAT32` - the fake label a BitLocker boot sector carries |
| payload entropy | 7.99-8.00 bits/byte over the 62 non-zero 1 GiB bands |
| content map | non-zero ciphertext in bands 0..70 GiB, zeros from 71 GiB to the end |
| zero bands | 399 of 461 sampled 1 GiB bands are all-zero |
| last sector of the volume | not zero, entropy 7.57 - an encrypted backup boot sector |
| `-FVE-FS-` signatures in first 16 MiB / mid +-1 MiB / last 16 MiB | 1 / 0 / 0 |

Reading: a BitLocker volume whose used space is encrypted while untouched free space is still zero. Its NTFS - `$MFT` included - is inside the ciphertext. Only **one** `-FVE-FS-` signature exists (the stub itself) and **no FVE metadata block** turned up in the first 16 MiB, around the midpoint or in the last 16 MiB, where the key-wrapped VMK copies normally live; this image may not even contain the material a recovery password would need. Nothing was decrypted, no key was guessed, no password was tried.

Consequence: without the BitLocker key this image yields **no** whole file from the 495 GB volume - no names, no sizes, no paths, no shadow copies. Carving it is worse than useless: 71 GiB of ciphertext at ~8 bits/byte throws signature hits at the noise rate and every one is a false positive.

## 5. What could not be read, and why

1. **Partition 2, the 495 GB data volume** - BitLocker. No MFT, no directories, no `$Recycle.Bin`, no `$UsnJrnl`, no VSS. Needs the FVE key.
2. **Backup GPT** - declared at LBA 1000215215 while the image ends at LBA 1000210431; the last 4816 sectors (~2.35 MiB) were never captured. Every partition still lies inside the image, so the primary table is complete and usable.
3. **VSS store internals** - no store file exists to read.
4. **`$I` contents** - no `$I` file exists to read; parser proven on synthetic v1/v2 only.
5. **`$I30` B-trees below the root** - deliberately not walked; child lists come from the parent-reference walk, cross-checked at each root.

The ESP is FAT32, not NTFS (no MFT), and was listed separately: 229 entries, 5 of them top level (`\Boot`/, `\BOOTMGR`, `\BOOTNXT`, `\EFI`/, `\System Volume Information`/). BitLocker `.bek` startup-key files: **none**, so no recovery key is lying in the clear on this disk.

## 6. Sanity checks against what a real Windows volume looks like

| check | result |
|---|---|
| partition 3 root listing | `$AttrDef`, `$BadClus`, `$Bitmap`, `$Boot`, `$Extend`, `$LogFile`, `$MFT`, `$MFTMirr`, `$Secure`, `$UpCase`, `$Volume`, `.`, `Recovery`, `System Volume Information` |
| partition 4 root listing | `$AttrDef`, `$BadClus`, `$Bitmap`, `$Boot`, `$Extend`, `$LogFile`, `$MFT`, `$MFTMirr`, `$RECYCLE.BIN`, `$Secure`, `$UpCase`, `$Volume`, `.`, `System Volume Information`, `osri` |
| partition 5 root listing | `$AttrDef`, `$BadClus`, `$Bitmap`, `$Boot`, `$Extend`, `$LogFile`, `$MFT`, `$MFTMirr`, `$RECYCLE.BIN`, `$Secure`, `$UpCase`, `$Volume`, `.`, `System Volume Information`, `apps`, `data`, `logs`, `sources`, `telemetry` |
| `$I30` vs parent-reference walk | p3 match; p4 match; p5 match |
| system metafiles ($MFT $MFTMirr $LogFile $Bitmap $Secure $UpCase $Volume $AttrDef $BadClus $Boot $Extend) | p3 11/11; p4 11/11; p5 11/11 |
| `FILE` magic at the 1024-byte slots of `$MFT` | p3 41/256 slots, p4 66/256 slots, p5 587/768 slots |
| `Windows\`, `Users\`, `Program Files\`, `pagefile.sys` | **absent everywhere** - none of the readable volumes is the OS volume, the OS volume is the encrypted one; their absence is the expected shape here, not a parser fault |
| OEM artefacts that must be there if the walk is right | `Recovery\WindowsRE\Winre.wim` (1.02 GiB), Dell `osri\` (apps/config/drivers/manifest/os) with the 8.34 GiB `N3YY3_SWB_W11P_64_USB_META_1225E.wim`, vendor `sos.wim` (750.42 MiB) + `File_Restore.exe` |

Each root listing is a clean Windows-shape list: `$`-metafiles plus the volume's own payload directory, every `$FILE_NAME` parent reference resolving to a record that exists, and the `$I30` set agreeing with the walk on all three volumes.

## 7. Verdict and what it means for release 6

| source | status on this image | files it could hand back | what that implies |
|---|---|---|---|
| **MFT direct** | PRESENT on 3 of 6 partitions (WinRE, Dell factory, vendor rescue); UNREADABLE on the 495 GB data volume | 417 real files, 11.14 GiB (462 in-use files counting `$`-metafiles) | the only source that works here, and it returns OEM payload only - no user file |
| **`$Recycle.Bin`** | PRESENT as a directory on 2 volumes, EMPTY (S-1-5-18 only) | 0 | no `$I`, no `$R`, no deletion metadata; the user's bin is inside the ciphertext |
| **VSS** | ABSENT | 0 | no catalog, no store; `System Volume Information` holds only FVE/tracking scaffolding |
| **`$UsnJrnl:$J`** | ABSENT | 0 | `$Extend` intact, `$UsnJrnl` never created on these volumes |

**Implications, most important first:**

No readable structure in this image can hand back a pre-reformat file, and that is a property of the image, not of these parsers: the three readable volumes are OEM install content dated 2026-10-15/16 (after the reinstall), and the data volume is 71 GiB of ciphertext over 390 GiB of untouched zeros.

1. **Add a BitLocker/FVE gate before anything else.** One 512-byte read per partition is enough: if offset 3 is `-FVE-FS-`, mark the volume ENCRYPTED, stop, and tell the user which key is needed. Without that gate release 6 would spend hours "scanning" 71 GiB of ciphertext and emit thousands of false positives - the exact failure 9c exists to prevent.
2. **Read all 128 GPT entries.** Two recovery volumes (12.1 GiB, 2.0 GiB) were invisible to the earlier four-entry layout notes; a factory image and a rescue partition are whole-file sources in their own right.
3. **Keep the MFT pass first, as 9c says.** It is the only source that worked, it costs ~1.3 MiB of MFT reads for 694 in-use records across three volumes, and the `$I30` cross-check comes almost free.
4. **Keep the `$I`/`$R` and `$UsnJrnl` code but promise nothing for this image.** On a normal user volume they are cheap wins; here both are empty by construction and stay unvalidated against real data until an unencrypted Windows volume is run through the probe.
5. **Say it plainly in the release notes:** on a BitLocker disk the honest answer is "give me the key, or this image is 495 GB of noise", not "0 files recovered".

---

Reproduce: `./python/python.exe probes/dir_artifacts.py` - read-only, prints a JSON digest to stdout and rewrites this report.
