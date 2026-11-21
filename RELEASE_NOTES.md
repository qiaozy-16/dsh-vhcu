# VHCU V1.1.0 Release Notes

**Release date:** 2026-11-20
**Platform:** Windows 10/11 x64, DSH Desktop App only
**Engine:** `dist/vhcu-daemon/vhcu-daemon.exe` (PyInstaller onedir)
**Plugin version:** 1.1.0

V1.1.0 is the release-hardening and interaction-quality release. It keeps the
local-first architecture and the existing safety model, and adds precision
capture, semantic UIA control, a non-intrusive virtual cursor, and stronger
execution integrity.

## 2026-11-21 Post-acceptance hardening

- `vhcu_get_memory_clipboard` now renders the stored text to the model instead
  of only showing its character count.
- `vhcu_find_elements` defaults to UIA depth 10, so Edge/Chromium page controls
  are visible without an explicit `max_depth`.
- Arrow and navigation keys are emitted with `KEYEVENTF_EXTENDEDKEY`; this fixes
  `alt+left` navigation in Explorer.
- The virtual cursor no longer strokes the enlarged glow triangle, removing the
  stray white line below the pointer while retaining the solid cursor rim.
- The shared settings store refreshes from disk and watches its directory, and
  updates merge only fields that changed locally; stale windows no longer
  overwrite another window's privacy/managed-mode choice.

---

## Four Defining Capabilities

### Game War intelligent see-through

- `vhcu_get_full_frame` and `vhcu_capture_target` accept `precision_mode`
  (default `false`).
- With the global **Allow high-precision vision** switch enabled (default on),
  the capture returns 100% physical pixels: no PNG downscale,
  `image_width == width`, `scale_factor == 1`.
- A green **High-precision capture** indicator appears in the panel and clears
  after 15 seconds, on the next normal capture, or when vision stops.
- If the global switch is off, the tool returns normally with
  `precision_denied: true` and an explicit message instead of pretending.
- Intended for dense, fast-changing surfaces: game HUDs/combat views, CAD and
  Blender viewports, charts, and small labels. No overlay, no injection into
  protected processes, no anti-cheat bypass.

### UIA semantic-level control

- `vhcu_find_elements` returns opaque element tokens for Windows UI Automation
  elements, with stale fingerprints and bounds checks.
- `vhcu_click_element` prefers **InvokePattern**; elements without Invoke fall
  back to their physical center.
- New `uia` injection mode resolves the element under a screen point and invokes
  it directly, falling back to a coordinate click only when required.
- Password fields never expose `Value`; UIA discovery and invocation are blocked
  on privacy-blacklisted target windows before any thread starts.
- UIA timeouts mark the worker unhealthy and require a daemon restart instead of
  retrying a stuck COM call.

### Background, non-intrusive virtual cursor

- `virtual` mode posts mouse messages to the target window without moving the
  physical cursor; the layered click-through overlay shows the AI's virtual
  position.
- Chromium/Electron child surfaces are supported after real-world verification;
  CEF and UWP shells stay in the PostMessage blocklist and `hybrid` falls back to
  physical input.
- Virtual mode no longer triggers the user-takeover soft pause, because physical
  mouse movement is unrelated to the injected messages.
- `hybrid` supports a per-call `preferred_method` on `vhcu_mouse_click`
  (`physical` / `virtual` / `uia`).

### Resilience triad: recovery, precision, macro governance

- **Recovery:** `session_state.json` records macro-queue progress; the panel
  offers Resume/Discard, now also on the web client. Image-relative queues are
  discarded across restarts.
- **Precision governance:** a global permission plus a live indicator, so the
  user always sees when the model is using physical-pixel capture.
- **Macro governance:** model-side macro self-generation is off by default;
  `vhcu_save_macro` returns `E_MACRO_GENERATION_DISABLED` until the user enables
  it, and the panel shows the interception notice.
- **Execution integrity:** expired commands are refused before dispatch; orphaned
  daemons are reclaimed through per-process PID records with strict identity
  checks; modifiers and mouse buttons are force-released on startup and shutdown.

---

## Additional Improvements

- Full virtual-desktop capture and injection, including monitors at negative
  coordinates, with image-origin-aware scaling.
- DPI-aware UIA bounds normalization and explicit `bounds_space` handling.
- Safer concurrent input state: an in-flight injection counter replaces the
  previous boolean, so overlapping actions cannot leave injection state stuck.
- Chromium `PostMessage` support relaxed; CEF/UWP remain blocked.
- Privacy gates extended to UIA discovery and invocation, with a single
  `E_PRIVACY_BLOCKED` taxonomy.
- Web recovery buttons no longer disabled by the web platform flag.
- `deadlineMs` is now an absolute epoch deadline end-to-end; expired commands are
  dropped instead of executed later.
- Orphan daemon reclamation before binding, with parent-liveness and process
  creation-time checks.
- Plugin-list icon restored (512x512 `assets/icon.png`).
- Four plain-language injection-mode labels in the settings panel.
- First-run onboarding, offline wheelhouse, and path-sandbox guarantees retained.

---

## Upgrade Notes

1. Replace the plugin folder (or junction target) with the 1.1.0 build.
2. Ensure `dsh-better-sidebar` is enabled in the same DSH profile.
3. Restart DSH so the host plugin and sidebar bundle reload.
4. Replace `dist/vhcu-daemon/` with the rebuilt 1.1.0 engine; do not mix
   `_internal/` files from older builds.
5. Settings are migrated automatically; new keys default to:
   `allowHighPrecisionVision: true`, `enableMacroAutoGeneration: false`.
6. External consumers of the contract should add
   `E_MACRO_GENERATION_DISABLED` to their error table.

---

## Release Artifact

```
dist/
  vhcu-daemon/
    vhcu-daemon.exe
    _internal/...
```

The engine is a PyInstaller **onedir** build (no onefile extraction into
`%TEMP%`). The Hear model is not bundled; use the offline model pack or allow the
one-time download.

---

## Known Limitations

- Windows 10/11 x64 only; no macOS/Linux engine in V1.1.
- UWP/WinUI3 capture may lack a GDI back buffer; WGC fallback is deferred.
- CEF/UWP windows reject PostMessage; use `physical` or `hybrid`.
- Visual diff remains cell-sampled; precision mode improves PNG fidelity, not the
  diff grid.
- High-precision 4K frames are large; use them only when necessary.
