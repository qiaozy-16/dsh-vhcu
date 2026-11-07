# VHCU — Open Source Release Checklist

Release: **v1.0.0**  
Date: **2026-11-07**  
Scope: `dsh-vhcu/` only. Reports, temporary files, model weights, and `node_modules/` are not part of the repository.

---

## 1. Repository cleanup

- [x] Scanned the project tree excluding `node_modules/` and `models/`.
- [x] Deleted **33** `*.bak` files.
- [x] Re-scanned after deletion: **0** `*.bak` files remain outside excluded directories.
- [x] No source, schema, test, or configuration file was deleted.

Verification command:

```powershell
find . -path './node_modules' -prune -o -path './models' -prune -o -type f -name '*.bak' -print
```

Expected: no output.

---

## 2. Open-source files

- [x] `requirements.txt` — Python runtime dependencies:
  - `mss`
  - `numpy`
  - `soundcard`
  - `webrtcvad-wheels`
  - `faster-whisper`
- [x] `.gitignore` — excludes:
  - `node_modules/`
  - `models/`
  - `macros.json`
  - `*.bak`
  - `*.log`
  - Python caches / venvs / build output / OS noise
- [x] `README.md` — contains:
  - required `dsh-better-sidebar` notice;
  - Windows-only notice;
  - DSH `0.2.1-rc.3` recommendation;
  - `npm install` + `pip install -r requirements.txt`;
  - offline model pack and HF mirror download instructions;
  - multimodal / macro / safety feature sections;
  - Mermaid architecture diagram;
  - tool reference, project structure, troubleshooting, limitations.
- [x] `LICENSE` — MIT.
- [x] `dsh.plugin.json` compatibility includes `0.2.1-rc.3`.
- [x] Release version set to `1.0.0` in `package.json`, `dsh.plugin.json`, `package-lock.json`, README badge.

---

## 3. Path desensitization

Scanned `lib/` and `vision/` for user-specific absolute paths:

```powershell
grep -RInE '[A-Za-z]:[\\/]+Users[\\/]+mate|/Users/mate|D:\\Users|D:/Users' lib vision --exclude-dir=__pycache__
```

Result: **no matches**.

Notes:

- Production code obtains the plugin root from `import.meta.url` / `Path(__file__)` and the model root from `VHCU_MODEL_PATH` or `<plugin>/models`.
- Synthetic drive paths remain only in unit-test fixtures and one generic tool-description example (`D:\`); they are not user-specific and are not used for runtime discovery.
- The old `hear.py` doc comment containing a local path was replaced with `dsh-vhcu/models`.

---

## 4. Tests

Final suites after cleanup and documentation changes:

- **Node: 97/97 passing**
- **Python: 100/100 passing**

```powershell
npm test
npm run test:py
```

---

## 5. Files intentionally not committed

| Path | Reason | Publish as |
|---|---|---|
| `node_modules/` | Installed dependencies | `npm install` |
| `models/` | ~142 MB model weights | GitHub Release asset / user download |
| `macros.json` | Private runtime macros | never publish |
| `*.bak`, `*.log`, `.artifacts/` | local artifacts | never publish |
| `__pycache__/`, `*.pyc` | Python cache | never publish |

Recommended release asset:

```text
faster-whisper-base.zip
  faster-whisper-base/
    model.bin
    config.json
    tokenizer.json
    vocabulary.txt
```

Users unzip it under `dsh-vhcu/models/`.

---

## 6. Before pushing to GitHub

- [x] Repository URL set in README: `https://github.com/qiaozy-16/dsh-vhcu`.
- [ ] Create the GitHub repository (if not already created).
- [ ] Confirm the `dsh-better-sidebar` installation link/instructions for users.
- [ ] Create a `v1.0.0` tag.
- [ ] Upload the offline `faster-whisper-base` pack as a Release asset.
- [ ] Review the MIT copyright holder if you want a different attribution.
- [ ] Optionally add screenshots/GIFs under `docs/` (not required).
- [ ] Do **not** commit `models/`, `node_modules/`, `macros.json`, or `.bak` files.

Suggested commands:

```powershell
git init
git add .
git status
git commit -m "release: VHCU v1.0.0"
git branch -M main
git remote add origin https://github.com/qiaozy-16/dsh-vhcu.git
git push -u origin main
git tag v1.0.0
git push origin v1.0.0
```

Before committing, verify:

```powershell
git status --short
git ls-files | Select-String -Pattern 'models/|node_modules/|macros\.json|\.bak$'
```

The second command should return no tracked private artifacts.

---

## 8. Plugin icons removed (P26.7)

- [x] `package.json` has **no** `icon` field.
- [x] `dsh.plugin.json` has **no** `icon` field.
- [x] `lib/client.js` `registerTab` has **no** icon object; the tab renders text `VHCU` only.
- [x] `lib/assets/` removed; no icon file is tracked.
- [x] DSH plugin management uses its default artwork.
- [x] `/vhcu/icon.png` route and its tests removed.

Verification after DSH restart:

1. Sidebar tab list: the VHCU tab shows **text only**.
2. Settings → Plugins: VHCU shows the DSH default artwork, with no custom icon or error.
3. No `icon exceeds 256 KiB` warning.

---

## 9. Final status

**Ready to publish.** The only remaining steps are repository creation, the real clone URL, the model Release asset, and the optional license attribution review.
