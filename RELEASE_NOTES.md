# v1.1.0 - Multimodal CUA & Background Capture

**Release date:** 2026-11-22  
**Platform:** Windows 10/11 x64, DSH Desktop App  
**Engine:** `dist/vhcu-daemon/vhcu-daemon.exe` (PyInstaller onedir)  
**Plugin version:** 1.1.0

V1.1.0 turns VHCU into a guarded multimodal computer-use layer: precision
background capture, semantic UIA control, a non-intrusive virtual cursor,
system-audio transcription, and macro recovery behind one contract. The public
surface remains 28 tools, 10 routes, and one sidebar panel.

## Features

- **Intelligent Background Capture.** `precision_mode` on
  `vhcu_get_full_frame` / `vhcu_capture_target` returns native physical pixels
  when the global precision switch is enabled. The panel exposes a live
  high-precision indicator; denied requests return `precision_denied: true`
  rather than silently downscaling.
- **Multimodal CUA timeline.** Vision diffs and loopback-audio transcripts are
  fused into a 1.5-second event stream. `vhcu_get_multimodal_events` returns
  `vision`, `hear`, `both`, or `hear`-only records with stable IDs.
- **UIA semantic control.** Opaque `element_id` tokens, InvokePattern-first
  clicks, configurable `uiaDefaultDepth` (1-10, default 6), stale fingerprints,
  and an isolated COM worker. Password fields are redacted from model output.
- **Virtual cursor over PostMessage.** `virtual` mode posts mouse messages
  without moving the physical pointer. `hybrid` selects physical, virtual, or
  UIA input per call and falls back to physical input on blocked surfaces.
- **Macro engine with breakpoint recovery.** `session_state.json` records queue
  progress; Resume/Discard survives restarts. Image-relative queues are discarded
  across restarts instead of being replayed blindly.
- **Macro governance.** Model-side self-generation is opt-in. Until enabled,
  `vhcu_save_macro` returns `E_MACRO_GENERATION_DISABLED`. When enabled, the
  model can persist parameterized macros and portable semantic metadata without
  per-application adapters.
- **Onedir release engine.** PyInstaller onedir output, no onefile extraction
  into `%TEMP%`. The Hear model remains an optional external asset.

### Phase 1-11 consolidation

- **Phase 1-2:** contract-first NDJSON, monotonic sequence numbers, absolute
  `deadlineMs`, signed frame URLs, and fail-closed frame handling.
- **Phase 3-4:** isolated UIA worker, element registry, stale-bounds checks,
  privacy gates on discovery and invocation, explicit `E_PRIVACY_BLOCKED`
  taxonomy.
- **Phase 5:** in-flight injection counter, modifier/button force-release, and
  the PostMessage blocklist with Chromium support.
- **Phase 6:** coordinate transforms and image-origin mapping, readback gate,
  stale-frame refusal, DPI-aware UIA bounds.
- **Phase 7-8:** atomic macro store, opt-in self-generation, reusable-action
  validation, 100-step cap, 20-macro queue cap, and destructive-action approval.
- **Phase 9:** memory watermark, frame retention limits, cache cleanup, PID
  reclamation, and orphan-daemon detection.
- **Phase 10:** layered virtual-cursor overlay, glow/ripple rendering, and
  click-through behavior.
- **Phase 11:** shared settings watcher, field-level settings merge, clipboard
  renderer, configurable UIA depth, extended navigation keys, and semantic macro
  metadata.

## Bug Fixes

- `vhcu_get_memory_clipboard` now returns the stored text to the model instead
  of only a character count.
- Settings are refreshed from disk and merged field-by-field; stale sidebar
  windows can no longer overwrite privacy or managed-mode changes.
- `alt+left` works in Explorer: arrow and navigation keys carry
  `KEYEVENTF_EXTENDEDKEY` and no longer look like numeric-keypad input.
- The virtual cursor glow no longer draws an outer white outline; the stray
  white line under the pointer is gone.
- UIA `value` is rendered for non-password fields when `allowUiaValueRead` is
  enabled; password fields remain redacted even when the switch is on.
- UIA depth is no longer hard-coded. `uiaDefaultDepth` defaults to 6 and accepts
  1-10; per-call `max_depth` still wins.
- Input privacy violations use `E_PRIVACY_BLOCKED` consistently across capture,
  UIA, and keyboard/mouse tools.
- Expired commands are dropped before dispatch; orphaned daemons are reclaimed
  through process identity checks rather than broad process-name kills.
- The virtual-cursor overlay is excluded from model-facing captures and restored
  afterward, so the overlay is never mistaken for application content.

## Security

- **Privacy hard block.** A blacklisted foreground title returns
  `E_PRIVACY_BLOCKED`; capture, UIA discovery, UIA invocation, and input all fail
  closed. The developer bypass is explicit and leaves a permanent warning.
- **Password redaction.** UIA never calls `ValuePattern` for password fields; the
  model-facing renderer applies a second redaction guard.
- **Forced readback.** Click, type, scroll, and drag require visual confirmation
  before the next action. Turbo mode relaxes only explicitly supported tools.
- **Coordinate and focus guards.** Out-of-bounds points are rejected; the
  foreground window is asserted before injection; the four screen corners act as
  an operator emergency stop.
- **Macro approval.** Destructive macros require user approval unless managed
  approval is enabled, which keeps a persistent red warning.
- **Untrusted data.** Screen text, UIA names, and audio transcripts are wrapped
  as `<untrusted_screen_data>` and treated as data.
- **Local-only data.** No telemetry, remote capture service, or cloud audio path.
  The optional Hear model download is the only expected network access.

## Known Issues

- **Edge/Chromium web content is not exposed through the current UIA tree.**
  Page controls must use visual coordinates; browser web-content UIA is deferred
  to V1.2.
- **UWP/CEF PostMessage injection is limited.** Use `physical` or `hybrid`
  fallback; some surfaces reject posted messages by design.
- **Multi-monitor and complex DPI layouts are not fully validated in V1.1.**
- **The bundled Hear model (`base`) has limited accuracy under strong background
  noise.**
- Windows 10/11 x64 only; no macOS or Linux engine in V1.1.

## Upgrade Notes

1. Replace the plugin folder or junction target with the V1.1.0 build.
2. Replace `dist/vhcu-daemon/` as a whole; do not mix `_internal/` files from an
   older engine.
3. Ensure `dsh-better-sidebar` is enabled in the same DSH profile.
4. Restart DSH so the host plugin and sidebar bundle reload.
5. New settings default to `uiaDefaultDepth: 6`,
   `allowHighPrecisionVision: true`, and `enableMacroAutoGeneration: false`.
6. Contract consumers should use `E_PRIVACY_BLOCKED` for privacy refusals and
   `E_MACRO_GENERATION_DISABLED` for disabled self-growth.

## Release Artifact

```text
dist/
  vhcu-daemon/
    vhcu-daemon.exe
    _internal/...
```

The engine is a PyInstaller onedir build. The Hear model is not bundled; use the
offline model pack or allow the one-time download.

## License

MIT. See `LICENSE`.
