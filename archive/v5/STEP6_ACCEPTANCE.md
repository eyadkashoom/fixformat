# Step 6 acceptance checklist - JSON signature profiles (Section 7) and SMB / network output (Section 8)

Scope: step 6 of the release-5 plan, i.e. Sections 7 and 8 of `V5_PLAN.md`, on top of
steps 0-5 which are already committed. Everything here is checked by reading what the
tool prints, what it asks, what appears on disk - never by reading `raw_scan.py`.

This checklist is written against the plan, not against the current source. Where the
build and this file disagree, this file is the requirement.

Conventions every item relies on (all of them already hold in v5):

- steps are headed `STEP n of 5 - ...` with 78 `=` characters above and below,
- numbered options are printed as `  [1] ...`,
- prompts end with `Your choice [..]: `,
- a run that refuses a configuration prints `SIGNATURE ERROR: <reason>` (or
  `CANNOT USE THAT FOLDER: <reason>` for a destination), then `Nothing was scanned.`
  with one line of advice, and leaves with exit code **2**; a run that completes
  normally exits **0**,
- `script dir` means the folder `raw_scan.py` itself sits in.

Three rules this checklist fixes for step 6, because they are user-visible and they are
what makes the section B and F cases testable:

1. **A profile that has been named and cannot be used stops the run.** It is never
   silently replaced by the built-in set: a user who asked for 3 patterns must not get
   an 81-pattern scan. `[N]`, a bare Enter and an abandoned picker are the only ways
   to end up with the built-in set, and none of them is an error.
2. **Hex is only hex when it is written as spaced bytes** (`41 43 31 30`). Anything
   else - including a bare `AC1015` or `25504446` - is searched as text, because that
   is what those bytes actually are in a DWG header. A value that is written like a
   byte list but is not whole bytes (`25 50 4G 46`, `41 43 1`) is refused instead of
   being quietly re-read as text.
3. **A same-disk output root is REFUSED unless you type YES.** Section 12 says
   "reject";
   Section 12's "refuse by default" is the imaging rule, and that one belongs to step 8.

Deliberately **not** in this checklist (step 8, Section 12 - do not implement here): the
*refusal* of a same-disk imaging destination, the 102 % free-space gate, the
NTFS/exFAT sparse-mode rule, the `(unreachable)` greyed-out volume entry, and any change
to the imaging (option 4) or fast-scan (option 1) flows. mmap and the real-media
benchmark are step 7.

---

## A. Signature profile - happy path

- [ ] Given menu option 2 reaches the signature step, the header reads `STEP 2 of 5 - CHOOSE THE SIGNATURES` and exactly two numbered choices are printed - `  [1] BUILT-IN SIGNATURES ...` and `  [2] A JSON PROFILE ...` - followed by `Your choice [1]: `.
- [ ] Given `[2]` is answered, the tool prints `PROFILE FILES FOUND (n)` with one numbered row per `.json` file next to the script (file name and size on the row, its folder on the line below), then `  [0] Type the full path of another profile` and `  [N] Keep the built-in signatures`, and asks `Profile [N]: `.
- [ ] Given `p_dwg_text.json` (one extension `dwg`, one value `AC1015`) is picked from that list, the tool prints `Loaded p_dwg_text.json`, then a summary line containing the profile's `name` and `1 signatures over .dwg(1)`, then `The extension menu is skipped: the profile IS the filter.`, and continues to `STEP 3 of 5 - CHOOSE CASE SENSITIVITY`.
- [ ] Given `p_pdf_hex.json` (`{"pdf": ["25 50 44 46 2D"]}`) is loaded, the pre-scan plan reports `signatures  : 1 patterns`, proving the spaced-hex value became exactly one byte pattern and not text.
- [ ] Given `p_dwg_text.json` (a text value) is loaded with case sensitivity kept at its default, the pre-scan plan reports `signatures  : 2 patterns`, because the marker is searched in both configured encodings (utf-8 and utf-16-le), while the summary line still says `1 signatures over .dwg(1)` - the two numbers are allowed to differ and both must appear.
- [ ] Given `p_two.json` (`.dwg` as spaced hex plus `.md` as the text `\n# `) is loaded, the plan reports `signatures  : 3 patterns` and the `file types` row reads `.dwg, .md`.
- [ ] Given the bundled `signature_profile.json` next to the script (10 values over 5 extensions) is chosen, it loads, `Default recovery profile` appears in the summary, and the plan reports `signatures  : 18 patterns` - not 81.
- [ ] Given `[0]` is answered and the full path of a profile in another folder is typed, the profile loads and that folder becomes the one offered first the next time the profile list is opened.
- [ ] Given `[N]` or a bare Enter is answered at `Profile [N]: `, the tool prints `No profile chosen - keeping the built-in signatures.`, shows the numbered `Available file extensions ...` list, and the plan reports `signatures  : 81 patterns`.
- [ ] Given one profile value is written in lowercase spaced hex and another in uppercase spaced hex, both load without an error and each produces exactly one pattern.

## B. Signature profile - refusals and error messages

Every item in this section ends the same way, so it is stated once and checked every
time: the tool prints `SIGNATURE ERROR: <plain reason>`, then the two-line
`Nothing was scanned.` advice, leaves with exit code **2**, and creates no
`recovered_*` or `organized_*` folder anywhere.

- [ ] Given `bad_json.json` (a truncated object), the reason contains the file name, the words `not valid JSON`, and the JSON parser's own position (`line 2 column 1` for the fixture), so the user can find the mistake.
- [ ] Given a profile that is valid JSON but not an object (`not_object.json` containing `[]`), the reason says a profile must be one JSON object.
- [ ] Given a profile with no `signatures` key (`no_signatures.json`), or with `"signatures": {}`, the reason says the profile has no `signatures` object or that it is empty, and it names the file.
- [ ] Given an extension key that is not a plain extension (`bad_ext.json` with `"tar.gz"`, or `"my ext"`, or `""`), the reason quotes the offending key and says what to write instead (`write .md, .dwg, .pdf ...`).
- [ ] Given an extension with an empty list (`empty_list.json` with `"dwg": []`), the reason names that extension (`profile entry .dwg has no signatures`).
- [ ] Given a value that is not a string (`number_value.json` with `"dwg": [1234]`, or `null`, or a nested list), the reason names the extension, quotes the offending value, and says a signature must be text - it must not be reported as an "empty" signature.
- [ ] Given a value written like a byte list but not in whole bytes (`bad_hex.json` with `"dwg": ["25 50 4G 46"]`), the reason quotes the value, names the extension, and says spaced values must be whole hex bytes; the run must not start with that value silently searched as text.
- [ ] Given a profile whose only value is a single character (`short_text.json` with `"md": ["#"]`), the tool refuses with `produced no searchable pattern` plus the profile's name or file - a one-character text marker matches nearly every block and must never be searched - and stops before STEP 3.
- [ ] Given a profile in which some values fall to that same one-character rule and others are usable (`mixed.json` with `"md": ["#", "\n# "]`), one note line names the dropped value, the run continues, and the plan counts only the kept ones (`signatures  : 2 patterns`).
- [ ] Given `[0]` is answered but the typed path does not exist, the tool prints `SIGNATURE ERROR: cannot read the profile <path>: ...` naming the path it tried, `Nothing was scanned.`, and exits 2 - it does not fall back to the built-in set and start scanning.
- [ ] Given a profile is refused, the message appears before `STEP 3 of 5`, so the user is never asked for case sensitivity, a destination or a confirmation they cannot use.

## C. Signature profile - user-visible consequences

- [ ] In STEP 2, a loaded profile never produces the numbered `Available file extensions ...` list, and always produces the `The extension menu is skipped: the profile IS the filter.` line.
- [ ] In STEP 4, the `extensions` line lists exactly the profile's extensions (`.dwg` for `p_dwg_text.json`), in alphabetical order, and nothing else.
- [ ] In the `PLAN FOR THIS RUN` block, the `signature set` row shows the profile's name and its per-extension counts (`AutoCAD-only recovery: 1 signatures over .dwg(1)`), and the `signatures` row shows the pattern count instead of 81.
- [ ] In the `PLAN FOR THIS RUN` block, the `output root` row always shows the resolved output root (the script directory when the default was kept), so the user sees where the data will land before confirming.
- [ ] In `provenance.json`, `"profile"` holds the path the profile was read from, `"profile_name"` holds the `name` from the JSON (empty string when the file has none), `"signatures"` is the pattern count (1 for the hex profile, 2 for the text profile), and `"signature_labels"` contains only labels of that profile.
- [ ] Every label recorded in provenance and printed in the report begins with `Profile .` (e.g. `Profile .dwg 41 43 31 30 31 35`), and no built-in label such as `PDF document (.pdf)` appears anywhere in a profile run - even though `fixture.img` carries a PDF header at offset 1024 that the profile does not select.
- [ ] In the `--- hits per pattern ---` table of `recovery_report.txt` and of the console summary, every row is a profile label with its hit count, an extension whose values produced no hit is simply absent, and the counts still add up to `hits total`.
- [ ] Given `p_dwg_hex.json` (`41 43 31 30 31 35`) is scanned against `fixture.img`, exactly one hit is reported, at offset 4096 (`0x0000001000`), and the carved file for it begins with the bytes `41 43 31 30 31 35`.
- [ ] Given the same fixture is scanned with the built-in set, the PDF header at 1024 is reported - so the two runs demonstrably differ and the profile was really used, not merely displayed.
- [ ] Given a profile run is interrupted in STEP 5 and continued with menu option 6, the tool prints `Continuing ... with 2 signatures` (not `0`), reuses the same profile path from provenance, and the continued run's report uses the same `Profile .` labels.

## D. Output root - happy path

- [ ] Inside `STEP 4 of 5`, after the `extensions` line and before `PLAN FOR THIS RUN`, the tool prints one block headed `WHERE SHOULD THE RECOVERED DATA BE WRITTEN?` with `[1]` = next to this script (showing the absolute script directory) and `[2]` = another folder or a network share, followed by the standing caution and `Your choice [1]: `.
- [ ] Given `[1]` or a bare Enter is answered, nothing else is asked, the plan's `output root` row shows the script directory, and the run creates its folders exactly there as before.
- [ ] Given `[2]` is answered, the shared numbered picker is shown - `[1]..[n]` volumes with total/free space and file system, `  [U] Network path (\\server\share)`, `  [0] Type a path manually`, `  [R] Rescan volumes`, `  [Q] Cancel`, and `Your choice [1]: ` - i.e. the same picker the imaging flow already uses, not a second one written for this question.
- [ ] Given a local folder is chosen through `[0]` and its full path typed (`C:\...\s6\out_local`), the tool proves it is writable, prints `Output root     : C:\...\s6\out_local`, and the plan's `output root` row shows that path.
- [ ] Given a folder that does not exist yet is typed at the manual entry, the tool creates it (with any missing parent levels) instead of refusing, and uses it as the output root.
- [ ] Given the scan then runs, `recovered_<label>_<size>_<stamp>/` (with `search_results.txt`, `provenance.json`, `carved\`) and `organized_<label>_<size>_<stamp>/` (with `index.html`, `summary.txt`, `by_extension\`) appear **inside the chosen root**, with the same folder names and layout as a default run.
- [ ] Given a network share is chosen through `[U]` (`\\localhost\<share>\s6out`), the UNC path is accepted, the same two folders appear on the share, and every file in them can be opened afterwards - enumerating the share's parent is never required to use it.
- [ ] Given a share was used once, the same share is offered again on the next run (the picker's `[L]`/remembered entry or the volume list) without retyping the whole path.
- [ ] Given a mapped network drive letter (`net use Z: \\localhost\<share>`) is chosen from the volume list, it is accepted like any other volume and the folders appear on the share behind it.

## E. Output root - validation and refusal

- [ ] Given an unreachable share is typed at the manual entry (`\\127.0.0.1\nosuchshare`), the tool prints `CANNOT USE THAT FOLDER: <path and the OS reason>` before `PLAN FOR THIS RUN`, then asks `Try another folder? [Y/n]: ` - it never starts scanning into a share that is not there.
- [ ] Given a path on a drive letter that does not exist (`Z:\s6out`) is typed, the same `CANNOT USE THAT FOLDER` line appears with the OS reason naming the path.
- [ ] Given `[U]` is answered with an unreachable share while picking, the picker prints `Not reachable: <path>` and shows the list again, so the user stays in the picker instead of losing their place.
- [ ] Given a path that is an existing file (`fixture.img` typed as the output root), the reason says it exists and is a file, not a folder, and nothing is written into or beside it.
- [ ] Given a folder the account cannot write to (read-only ACL, recipe in the test section), the reason contains the OS permission text and the folder is left exactly as it was - no probe file, no half-created `recovered_*` folder.
- [ ] Given a refused folder is answered with `n` at `Try another folder?`, the tool prints `Writing next to this script instead: <script dir>`, continues with the default root, and the run completes with exit code 0.
- [ ] Given a root passes validation, the probe file `.fixformat_write_test` is removed again, so the chosen folder contains no `fixformat` file before the scan starts.
- [ ] Given `[Q]` is answered in the picker, the tool keeps the default root, says so in one line and continues - cancelling a destination is a choice, not an error, and never exits non-zero.
- [ ] Given the output root becomes unusable after validation but before the first write (folder made read-only, folder deleted, or share removed), the tool prints exactly one warning naming the path and the OS reason, then one line naming the local folder it continues in, and neither crashes nor stops the recovery.
- [ ] Given that mid-run loss happened, the hits found after the loss are written into a `recovered_*` folder next to the script, `provenance.json` records both the original `"output_root"` and the local fallback, the final screen prints both paths, the status is `COMPLETE` and the exit code is 0.
- [ ] Given a write to the output root fails once and then works again on the retry, the tool keeps using the root and does not print the fallback warning (one transient failure is not a lost share).

## F. Output root - the same-physical-disk REFUSAL

**Changed after this checklist was first written.** It required a warning here,
because V5_PLAN.md Section 8 says "warn". Section 12 of the same plan says
"reject the destination on the same physical disk", and the release QA
(`v5/RELEASE_QA_v5.0.0.md`, observation O-1) flagged that the two documents
disagreed. Section 12 wins: writing recovered data onto the disk it came from
can destroy the evidence, and a second drive letter on that disk does not help.
The check compares physical disk numbers, so it also catches the case the old
drive-letter warning missed.

- [ ] Given the source is a raw disk or volume and the output root resolves to the same PHYSICAL disk (not merely the same letter), the tool prints `REFUSED - THAT FOLDER IS ON THE SAME PHYSICAL DISK AS THE SOURCE`, both disk numbers, and the words `can overwrite the very data`.
- [ ] Given that refusal, the run does **not** proceed unless the user types `YES` in capitals; anything else (including `y`) keeps the refusal and offers another folder.
- [ ] Given `YES` is typed, the run continues and writes there.
- [ ] Given the source is an image file and the root is on the same physical disk, the same refusal applies (an image of the disk is still evidence from that disk).
- [ ] Given the root is on a different physical disk, or a network share, no refusal and no warning is printed and no question is asked.
- [ ] Given the refusal cannot decide (the share or the device number cannot be resolved), the tool falls back to the drive-letter comparison and never asserts that two paths are the same disk.
- [ ] Given `OUTPUT_ROOT` is refused, the run falls back to the script folder and says so in one line instead of aborting.

## G. Regression - a plain scan must be untouched

- [ ] Given a run that presses Enter at every prompt (built-in signatures, default root), the plan reports `signatures  : 81 patterns`, the `signature set` row reads `built-in signatures`, and no profile name, path or `Profile .` label appears anywhere in the output.
- [ ] Given stdin ends right after the STEP 3 answer (every later prompt hits end-of-file), the run still completes with the defaults and exits 0 - no question added by step 6 can ever be required.
- [ ] Given that plain run, it creates exactly `recovered_<label>_<size>_<stamp>\` and `organized_<label>_<size>_<stamp>\` next to the script, with `search_results.txt`, `provenance.json`, `carved\`, `index.html`, `summary.txt` and `by_extension\`, and creates no other folder anywhere.
- [ ] Given a plain run, `provenance.json` has `"signatures": 81` and an empty (or absent) `"profile"`, and every other key it carried in v5.0 step 5 is still present with the same meaning.
- [ ] Given that plain run, the `--- hits per pattern ---` table still lists the built-in labels with their counts, unchanged.
- [ ] Given the tool is started, the main menu still offers six options with the same wording, and menu option 1 (fast scan) and option 4 (create image) ask exactly what they asked before step 6 - no profile question, no output-root question.
- [ ] Given `.\python\python.exe v5\selftest.py` is run from the repository root, it still prints PASS, and `.\python\python.exe v4\selftest.py` still prints PASS.
- [ ] Given a run that uses a profile, the log lines and the progress line of STEP 5 are unchanged in format - a profile changes which patterns are searched, never how progress is reported.

---

## How to test without a real disk

Every item above can be executed on a laptop with no raw disk and no network. The three
tricks that make that possible:

- a **1 MiB image file** with planted signatures instead of a disk (`fixture.img`),
- a **loopback share** (`\\localhost\<share>`) instead of a network share - this machine
  already publishes `\\localhost\Users`, so a genuine UNC path exists with no cable
  plugged in anywhere,
- a **piped answer file** instead of a human, so each run is repeatable and its exit
  code can be checked.

Never run these inside the repository - `recovered_*` folders would land there. Copy the
script to a scratch folder first.

### 1. Setup (cmd.exe, once)

```bat
set REPO=C:\Users\eyad\Documents\Codex\2026-09-27\fixformat
set PY=%REPO%\python\python.exe
set S6=%TEMP%\s6
mkdir %S6% 2>nul
copy /y %REPO%\v5\raw_scan.py %S6%\
copy /y %REPO%\v5\signature_profile.json %S6%\
cd /d %S6%
%PY% -V
```

`%PY% -V` must print `Python 3.12.10`. Every command below is run from `%S6%`; the
copied script writes its folders next to itself, i.e. into `%S6%`, so the repository
stays clean.

### 2. Fixtures

Save as `%S6%\make_fixtures.py`, then run `%PY% make_fixtures.py`. Planted markers:
`%PDF-1.7` at 1024, `AC1015` at 4096, `AC1018` at 8192, `\n# Title` at 10000.

```python
import json

data = bytearray(b"X" * (1024 * 1024))
data[1024:1032] = b"%PDF-1.7"
data[4096:4102] = b"AC1015"
data[8192:8198] = b"AC1018"
data[10000:10009] = b"\n# Title\n"
open("fixture.img", "wb").write(bytes(data))

def prof(name, body):
    with open(name, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)

prof("p_dwg_text.json", {"name": "AutoCAD-only recovery", "signatures": {"dwg": ["AC1015"]}})
prof("p_dwg_hex.json",  {"name": "AutoCAD hex", "signatures": {"dwg": ["41 43 31 30 31 35"]}})
prof("p_pdf_hex.json",  {"name": "PDF only", "signatures": {"pdf": ["25 50 44 46 2D"]}})
prof("p_two.json",      {"name": "DWG and MD", "signatures": {"dwg": ["41 43 31 30 31 35"], "md": ["\n# "]}})
prof("mixed.json",      {"name": "Mixed", "signatures": {"md": ["#", "\n# "]}})
prof("short_text.json", {"name": "Too short", "signatures": {"md": ["#"]}})
prof("no_signatures.json", {"name": "No signatures"})
prof("empty_list.json",    {"signatures": {"dwg": []}})
prof("bad_ext.json",       {"signatures": {"tar.gz": ["41 42"]}})
prof("number_value.json",  {"signatures": {"dwg": [1234]}})
prof("bad_hex.json",       {"signatures": {"dwg": ["25 50 4G 46"]}})
open("bad_json.json", "w", encoding="utf-8").write('{ "signatures": { ')
open("not_object.json", "w", encoding="utf-8").write("[]")
print("fixtures written")
```

### 3. Answer files

The tool reads every answer from stdin, so a run is `<answers> | %PY% raw_scan.py`.
Prompt order for a full scan: main menu, disk-or-image, image picker, signature source,
profile picker (only if `2`), case sensitivity, output root, plan confirmation, 5 %
slices, save location. A blank line is a deliberate "take the default" answer.

`<n>` in the table is the number printed for `p_dwg_text.json` in that run's list: the
list is alphabetical, so the number moves as fixtures are added - read it from the
output of the first run instead of assuming `1`. Everything else in the table is fixed.

| Scenario | Answers, one per line |
|---|---|
| plain scan, built-in, default root (A9, G) | `2` `2` `1` `1` `Y` `1` `y` `n` `n` |
| profile from the list (A, C) | `2` `2` `1` `2` `<n>` `Y` `1` `y` `n` `n` |
| profile typed by path (A8, B) | `2` `2` `1` `2` `0` `<full path of the profile>` |
| local output root (D) | `2` `2` `1` `1` `Y` `2` `0` `C:\...\s6\out_local` `y` `n` `n` |
| network output root (D) | `2` `2` `1` `1` `Y` `2` `u` `\\localhost\Users\<you>\s6out` `s` `y` `n` `n` |
| unreachable root (E) | `2` `2` `1` `1` `Y` `2` `0` `\\127.0.0.1\nosuchshare` `n` `y` `n` `n` |

Create them with Python, because `echo` mangles backslashes and newlines:

```bat
%PY% -c "open('a_plain.txt','w').write('2\n2\n1\n1\nY\n1\ny\nn\nn\n')"
rem  replace N with the number printed for p_dwg_text.json:
%PY% -c "open('a_profile.txt','w').write('2\n2\n1\n2\nN\nY\n1\ny\nn\nn\n')"
type a_plain.txt | %PY% raw_scan.py > out_plain.txt 2>&1
echo EXIT %ERRORLEVEL%
findstr /c:"signatures  : 81 patterns" out_plain.txt
findstr /c:"signature set : built-in signatures" out_plain.txt
```

Section B items are checked three ways: the `SIGNATURE ERROR:` line is in the output,
`Nothing was scanned.` follows it, and `%ERRORLEVEL%` is 2. Section C items are checked
on `out_<scenario>.txt` plus the `provenance.json` and `recovery_report.txt` the run left
in its `recovered_*` folder.

### 4. The three "can it really write there?" cases

- **Local folder**: `mkdir %S6%\out_local`, then the local-root answers above;
  `dir /s /b %S6%\out_local` must list both run folders and their files. Remove
  `out_local` before repeating, otherwise a second run leaves a second pair beside the
  first (the timestamp makes the names unique, by design). The folder is pre-created so
  the scripted runs stay deterministic; the create-on-demand rule (item D5) is checked
  separately by typing a path that does not exist yet.
- **Network share with no network**: any share this machine publishes works as a UNC
  path, e.g. `\\localhost\Users\<you>\s6out`; confirm with `net share`, and create the
  subfolder first (`mkdir \\localhost\Users\<you>\s6out`) because the picker browses
  into the share instead of creating folders. This exercises the `\\server\share`
  handling Section 8 is about, and nothing leaves the machine.
- **Write-protected folder**: the ACL form that really refuses writes, even for an
  elevated account, is `/inheritance:r` (a `/deny` ACE alone is not enough):

```bat
mkdir %S6%\out_ro
icacls %S6%\out_ro /inheritance:r /grant:r "%USERNAME%:(OI)(CI)(R)"
echo test > %S6%\out_ro\probe.txt
rem  the line above must FAIL - if it succeeds the folder is not protected and the test is invalid
```

  Undo it with `icacls %S6%\out_ro /grant:r "%USERNAME%:(OI)(CI)F"`, otherwise `rd /s`
  cannot delete it.

### 5. Mid-run loss, deterministically (1 MiB fixture, no timing race)

The tool validates the root and then waits at `Do you want to start the scan now?`. That
wait is the window in which a second window breaks the folder. Save as `run_loss.bat`
and run it; the `ping` is what holds the pipe open while you do that. `ping.exe` is
called by full path so a Unix `ping` or `timeout` earlier on the PATH cannot be used
instead.

```bat
@echo off
cd /d %S6%
mkdir out_lost 2>nul
( echo 2& echo 2& echo 1& echo 1& echo Y& echo 2& echo 0& echo %S6%\out_lost
  & "%SystemRoot%\System32\ping.exe" -n 6 127.0.0.1 >nul
  & echo y& echo n& echo n ) | "%PY%" raw_scan.py > out_loss.txt 2>&1
echo EXIT %ERRORLEVEL%
type out_loss.txt
icacls %S6%\out_lost /grant:r "%USERNAME%:(OI)(CI)F"
```

While the `ping` runs:

```bat
icacls %S6%\out_lost /inheritance:r /grant:r "%USERNAME%:(OI)(CI)(R)"
```

The run must print one warning naming `out_lost`, continue into a local `recovered_*`
folder, still finish, and exit 0. With an elevated prompt the same window can be used to
`net share <name> /delete` a share the tester created himself - never a share the
machine came with.

### 6. The same-disk warning (`F`)

- **Image-file variant, no admin needed**: scan `fixture.img` (on `C:`) with a root on
  `C:` - the same-disk line must appear. Then repeat with a `subst` letter, which gives
  a different letter for the same volume - the line must not appear:

```bat
subst Z: %S6%
subst
rem  build a_z.txt from the local-root answers, with Z:\out_local as the root
type a_z.txt | %PY% raw_scan.py > out_z.txt 2>&1
findstr /i "same disk" out_z.txt
subst Z: /d
```

  This checks the letter-level comparison the tool can honestly make; a machine with a
  real second volume should test the negative case on that volume instead.
- **Raw-disk variant, elevated prompt**: start with `\\.\C:` as the source and
  `C:\...\s6\out_local` as the output root, read the warning, then answer `n` at
  `Do you want to start the scan now?`. Nothing is read from the disk in that path, so
  it costs seconds and writes nothing to `C:`.

### 7. Cleanup

```bat
icacls %S6%\out_ro /grant:r "%USERNAME%:(OI)(CI)F" 2>nul
icacls %S6%\out_lost /grant:r "%USERNAME%:(OI)(CI)F" 2>nul
rd /s /q %S6%
```

Then check the repository: `git status` must show no `recovered_*`, `organized_*`,
`fastscan_*`, `search_results.txt` or `last_paths.json`. A test that leaves those behind
was run in the wrong folder.

### 8. What a laptop cannot check

Three items can only be verified on real hardware; record them as "not exercised", never
as "passed", and never make them a precondition for the rest:

1. a share that vanishes because the **network** drops (only the local unshare, ACL and
   delete variants can be reproduced),
2. the same-disk warning against a **second physical disk**,
3. write behaviour on a real SMB link (slow links, reconnects, partial writes).
