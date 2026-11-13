# VHCU — Open Source Release Checklist

Release: **v1.0.1**
Date: **2026-11-13**
Scope: `dsh-vhcu/` only. Reports, temporary files, model weights, virtual
environments, and `node_modules/` are not part of the repository.

---

## 1. Repository cleanup

- [x] Scanned the project tree, excluding `node_modules/`, `.venv/`, `build/`,
      `dist/`, `models/`, and `.git/`.
- [x] Removed stale `*.bak` files before the public V1.0.0 release.
- [x] Re-scanned for `*.bak`, `*.bak-*`, `*.orig`, `*.save`, `*~`, and
      `*backup*` artifacts after the V1.0.1 packaging work.
- [x] Result: **zero backup artifacts inside `dsh-vhcu/`**.
- [x] Added backup rules to `.gitignore` so future local copies cannot be
      committed:
      `*.bak`, `*.bak-*`, `*.orig`, `*.save`, `*~`, `backup/`, `backups/`,
      `*backup*/`.

Verification command:

```powershell
find . \( -path './node_modules' -o -path './.venv' -o -path './build' `
  -o -path './dist' -o -path './models' -o -path './.git' \) -prune -o `
  \( -iname '*.bak' -o -iname '*.bak-*' -o -iname '*.orig' `
  -o -iname '*.save' -o -iname '*~' -o -iname '*backup*' \) -print
```

Expected: no output inside the repository. Backup artifacts found in the
parent workspace (`promo_cinematic.bak-*`, `dsh-ops-kit-install-backup/`) are
outside this Git repository and are not tracked by `dsh-vhcu`.

---

## 2. Open-source files

- [x] `requirements.txt` — Python runtime dependencies:
  - `mss`
  - `numpy`
  - `soundcard`
  - `webrtcvad-wheels`
  - `faster-whisper`
- [x] `.gitignore` — excludes dependencies, model weights, private macros,
      backups, logs, Python caches, virtual environments, build output, and OS
      noise.
- [x] `README.md` — contains:
  - required `dsh-better-sidebar` notice;
  - Windows-only notice;
  - DSH `0.2.1-rc.3` recommendation;
  - **Release zip installation** (extract, register, restart) with no old
    `git clone` / manual `npm install` / manual `pip install` path;
  - V1.0.1 platform-boundary notes: `DriverFactory`, HTTP interrupt, absolute
    path sandbox;
  - offline model pack and HF mirror download instructions;
  - feature sections, safety, architecture diagram, tool list, configuration,
    project structure, troubleshooting, limitations.
- [x] `RELEASE_NOTES.md` — V1.0.1 release notes and upgrade notes.
- [x] `LICENSE` — MIT.
- [x] `dsh.plugin.json` compatibility includes `0.2.1-rc.3`.
- [x] Release version set to `1.0.1` in:
  - `package.json`
  - `dsh.plugin.json`
  - `package-lock.json` (root package)
  - README badge
  - release notes and this checklist.
- [x] `THIRD_PARTY_LICENSES.md` remains up to date for the bundled runtime.

---

## 3. Path desensitization

Scanned source and docs for user-specific absolute paths:

```powershell
grep -RInE '[A-Za-z]:[\\/]+Users[\\/]+mate|/Users/mate|D:\\Users|D:/Users|Desktop-SLKN1PQ' `
  lib host vision contract scripts README.md RELEASE_NOTES.md `
  --exclude-dir=__pycache__ --exclude='*.pyc'
```

Result: **no matches** (the previous checklist command itself was removed when
this checklist was rewritten).

Path contract in V1.0.1:

- Node resolves `VHCU_MODEL_PATH`, `VHCU_MACROS_PATH`, and `VHCU_LOG_DIR` to
  absolute paths before spawning the daemon.
- Python accepts only absolute values; relative values fail with `E_CONFIG`.
- No production path is resolved against the process current directory.
- Synthetic drive paths remain only in tests and generic examples, not as
  runtime discovery values.

---

## 4. Tests

Final suites after documentation changes:

- **Node: 103/103 passing**
- **Python: 111/111 passing**
- **Contract: 14/14 passing**

```powershell
npm test
npm run test:py
```

Additional packaging verification:

- PyInstaller authoritative build: `vision/vhcu_daemon.spec`
- Build mode: **onedir**
- Output: `dist/vhcu-daemon/vhcu-daemon.exe` + `_internal/`
- Packaged READY: passed
- Packaged `POST /interrupt`: HTTP 200, `interrupt.set=true`
- Packaged `sys.info` paths: absolute and equal to injected values
- Packaged Hear dependency import: `hear.start` acknowledged successfully
- `pip check`: no broken requirements

---

## 5. Files intentionally not committed

| Path | Reason | Publish as |
|---|---|---|
| `node_modules/` | Installed Node dependencies | included in Release zip or installed by DSH |
| `.venv/` | Local Python environment (PyInstaller/full requirements) | never publish |
| `models/` | Model weights | separate Release asset / user download |
| `macros.json` | Private runtime macros | never publish |
| `build/`, `dist/` | PyInstaller output | Release zip asset, not Git history |
| `*.bak`, `*.bak-*`, `*.orig`, `*.save`, `*~`, `*backup*/` | local safety copies | never publish |
| `*.log`, `.artifacts/` | local artifacts | never publish |
| `__pycache__/`, `*.pyc` | Python cache | never publish |

Recommended release assets:

```text
dsh-vhcu-v1.0.1.zip
  dsh-vhcu/
    lib/
    host/
    contract/
    vision/
    scripts/
    dist/vhcu-daemon/
    package.json
    README.md
    RELEASE_NOTES.md
    ...

faster-whisper-base.zip
  faster-whisper-base/
    model.bin
    config.json
    tokenizer.json
    vocabulary.txt
```

Users extract the plugin zip to a permanent folder, run the included
`node scripts/install.mjs`, then restart DSH.

---

## 6. Packaging checks

- [x] `vision/vhcu_daemon.spec` switched from onefile to **onedir**:
      `EXE(..., exclude_binaries=True)` + `COLLECT(...)`.
- [x] Full Hear runtime closure installed in the release build environment:
      `faster-whisper`, `ctranslate2`, `onnxruntime`, `av`, `soundcard`,
      `webrtcvad-wheels`, `numpy`, `tokenizers`, `huggingface-hub`.
- [x] `Analysis.excludes` contains unused GUI/data-science modules:
      `tkinter`, `_tkinter`, `pandas`, `matplotlib`, `scipy`, `IPython`,
      `jupyter`, `pytest`, `notebook`, `PyQt5`, `PyQt6`, `PySide2`,
      `PySide6`, `wx`, `torch`, `tensorflow`, `sklearn`.
- [x] Verified excluded modules are absent from the release PYZ.
- [x] Kept required native runtime libraries (`av.libs`, `ctranslate2.dll`,
      `onnxruntime`, OpenBLAS/numpy). An attempt to strip optional FFmpeg video
      encoders broke `av._core` and was reverted.
- [x] onedir output is approximately **238 MB** with the full Hear stack.
- [x] No onefile TEMP extraction is required at startup.

---

## 7. Before pushing to GitHub

- [x] Repository URL set in README: `https://github.com/qiaozy-16/dsh-vhcu`.
- [ ] Create the GitHub repository (if not already created).
- [ ] Confirm the `dsh-better-sidebar` installation link/instructions for users.
- [ ] Build and upload `dsh-vhcu-v1.0.1.zip` from the onedir release artifact.
- [ ] Create a `v1.0.1` tag.
- [ ] Upload the offline `faster-whisper-base` pack as a Release asset.
- [ ] Review the MIT copyright holder if you want a different attribution.
- [ ] Optionally add screenshots/GIFs under `docs/` (not required).
- [ ] Do **not** commit `models/`, `.venv/`, `node_modules/`, `macros.json`,
      `build/`, `dist/`, or backup artifacts.

Suggested commands:

```powershell
git status --short
git add .
git status
git commit -m "release: VHCU v1.0.1"
git branch -M main
git remote add origin https://github.com/qiaozy-16/dsh-vhcu.git
git push -u origin main
git tag v1.0.1
git push origin v1.0.1
```

Before committing, verify no private artifacts are tracked:

```powershell
git ls-files | Select-String -Pattern 'models/|\.venv/|node_modules/|macros\.json|\.bak|backup'
```

The command should return no tracked private artifacts.

---

## 8. Plugin icons

- [x] `package.json` has **no** `icon` field.
- [x] `dsh.plugin.json` has **no** `icon` field.
- [x] `lib/client.js` `registerTab` has **no** icon object; the tab renders text `VHCU` only.
- [x] `lib/assets/` remains removed; no icon file is tracked.
- [x] DSH plugin management uses its default artwork.
- [x] `/vhcu/icon.png` route and its tests remain removed.

Verification after DSH restart:

1. Sidebar tab list: the VHCU tab shows **text only**.
2. Settings → Plugins: VHCU shows the DSH default artwork, with no custom icon or error.
3. No `icon exceeds 256 KiB` warning.

---

## 9. Final status

**Ready for V1.0.1 publication.** Remaining release actions are repository
creation, uploading the plugin zip and offline model asset, and creating the
`v1.0.1` tag.
