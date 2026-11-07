/**
 * @file client.js — dsh-vhcu client half (prebuilt browser module).
 *
 * Loaded by DSH's client module loader. Hand-written `__ModuleLoader__` module:
 * no bundler, no build step, `require("react")` comes from the host runtime.
 *
 * P8: the single panel now has three inner data tabs (Vision / Action / Hear)
 * plus a gear popover for debug, macros and the IME shortcut. It still registers
 * exactly ONE sidebar tab.
 */

window.__ModuleLoader__.load({
  id: 'dsh-vhcu',
  factory: (require) => {
    const module = { exports: {} };
    const React = require('react');
    const h = React.createElement;
    const { useCallback, useEffect, useRef, useState } = React;

    const API = '/vhcu';
    const STYLE_ID = 'dsh-vhcu-style';
    const DATA_KINDS = new Set(['vision', 'action', 'hear', 'multimodal']);

    const CSS = `
      .vhcu-root { display:flex; flex-direction:column; height:100%; min-height:0;
        background:#0d1016; color:#d7dee8; font:12px/1.5 "Segoe UI", system-ui, sans-serif; position:relative; }
      .vhcu-head { display:flex; align-items:center; gap:4px; padding:8px 8px; border-bottom:1px solid #1e2530; }
      .vhcu-title { font-weight:600; letter-spacing:.02em; color:#e8eef7; }
      .vhcu-dot { width:8px; height:8px; border-radius:50%; flex:0 0 auto; box-shadow:0 0 8px currentColor; }
      .vhcu-phase { color:#8b98a9; text-transform:uppercase; font-size:10px; letter-spacing:.08em; }
      .vhcu-spacer { flex:1; }
      .vhcu-btn { padding:5px 10px; border-radius:6px; border:1px solid #2b3442; background:#161c25;
        color:#d7dee8; cursor:pointer; font:inherit; }
      .vhcu-btn:hover:not(:disabled) { border-color:#4d6bfe; }
      .vhcu-btn:disabled { opacity:.45; cursor:default; }
      .vhcu-btn.on { border-color:#2f6b4f; background:#12251c; color:#9be3bd; }
      .vhcu-icon-btn { width:28px; height:28px; border-radius:6px; border:1px solid #2b3442;
        background:#161c25; color:#b9c6d8; cursor:pointer; font-size:14px; line-height:1; }
      .vhcu-icon-btn:hover { border-color:#4d6bfe; }
      .vhcu-meta { display:flex; gap:14px; padding:6px 12px; color:#77859a; font-size:11px;
        border-bottom:1px solid #141a22; white-space:nowrap; overflow:hidden; }
      .vhcu-meta b { color:#b9c6d8; font-weight:500; }
      .vhcu-privacy-banner { display:flex; align-items:center; gap:8px; padding:6px 10px;
        background:#3a2a12; border-bottom:1px solid #8a6d1f; color:#fde68a;
        font-size:11px; font-weight:600; white-space:nowrap; overflow:hidden; }
      .vhcu-privacy-sub { color:#fbbf24; font-weight:400; overflow:hidden; text-overflow:ellipsis; }
      .vhcu-unprotected-banner { padding:6px 10px; background:#3a2a12; border-bottom:1px solid #8a6d1f;
        color:#fde68a; font-size:11px; font-weight:600; }
      .vhcu-autoapprove-banner { padding:6px 10px; background:#3a1315; border-bottom:1px solid #b91c1c;
        color:#fecaca; font-size:11px; font-weight:700; }
      .vhcu-takeover-banner { padding:6px 10px; background:#10243a; border-bottom:1px solid #2563eb;
        color:#bfdbfe; font-size:11px; font-weight:600; }
      .vhcu-takeover-notice { padding:6px 10px; background:#101c2e; border-bottom:1px solid #1d4ed8;
        color:#93c5fd; font-size:11px; }
      .vhcu-approval-backdrop { position:absolute; inset:0; z-index:70; background:rgba(5,8,13,.78);
        display:flex; align-items:center; justify-content:center; padding:16px; }
      .vhcu-approval { width:100%; max-width:300px; padding:14px; border:1px solid #b91c1c;
        border-radius:8px; background:#1a1113; box-shadow:0 12px 32px rgba(0,0,0,.6); }
      .vhcu-approval-title { color:#fca5a5; font-weight:700; margin-bottom:8px; }
      .vhcu-approval-text { color:#fecaca; font-size:12px; line-height:1.6; margin-bottom:8px;
        overflow-wrap:anywhere; }
      .vhcu-approval .vhcu-btn-row { display:flex; gap:8px; margin-top:10px; }
      .vhcu-inline-check { display:flex; align-items:center; gap:6px; color:#fca5a5; font-size:11px; }
      .vhcu-privacy-confirm { background:#2a1712; border:1px solid #7f1d1d; border-radius:6px;
        padding:6px; margin-top:4px; }
      .vhcu-memory-banner { padding:6px 10px; background:#3a1315; border-bottom:1px solid #7f1d1d;
        color:#fecaca; font-size:11px; font-weight:600; }
      .vhcu-record-strip { display:flex; align-items:center; gap:8px; padding:6px 10px;
        border-bottom:1px solid #1e2530; background:#111722; }
      .vhcu-record-strip.active { background:#3a1315; border-bottom-color:#7f1d1d; }
      .vhcu-record-btn { padding:4px 9px; font-size:11px; color:#fecaca;
        border-color:#7f1d1d; background:#241316; white-space:nowrap; }
      .vhcu-record-btn:hover:not(:disabled) { border-color:#ef4444; }
      .vhcu-record-btn.on { background:#b91c1c; border-color:#ef4444; color:#fff;
        animation:vhcu-pulse 1.2s ease-in-out infinite; }
      .vhcu-record-text { font-size:11px; color:#8b98a9; overflow:hidden;
        text-overflow:ellipsis; white-space:nowrap; }
      .vhcu-record-strip.active .vhcu-record-text { color:#fca5a5; font-weight:600; }
      .vhcu-record-form { display:flex; flex-wrap:wrap; align-items:flex-end; gap:7px;
        padding:8px 10px; background:#161c25; border-bottom:1px solid #1e2530; }
      .vhcu-record-summary { flex:1 0 100%; color:#9aa8ba; font-size:10px;
        overflow-wrap:anywhere; }
      .vhcu-record-field { display:flex; flex-direction:column; gap:2px; color:#77859a;
        font-size:10px; flex:1 1 120px; min-width:0; }
      .vhcu-record-field span { color:#8b98a9; }
      .vhcu-record-input { width:100%; box-sizing:border-box; padding:4px 6px;
        font-size:11px; }
      .vhcu-record-actions { display:flex; gap:5px; margin-left:auto; }
      .vhcu-record-actions .vhcu-btn { padding:4px 9px; font-size:11px; }
      @keyframes vhcu-pulse {
        0%, 100% { box-shadow:0 0 0 0 rgba(239,68,68,.55); }
        50% { box-shadow:0 0 0 5px rgba(239,68,68,0); }
      }
      .vhcu-emergency-btn { padding:4px 8px; color:#f87171; border-color:#5b1a1a;
        background:#1a1113; font-size:11px; }
      .vhcu-emergency-btn:hover:not(:disabled) { border-color:#dc2626; color:#fecaca; background:#241316; }
      .vhcu-tabs { display:flex; gap:4px; padding:8px 10px 0; border-bottom:1px solid #1e2530; }
      .vhcu-tab { display:flex; align-items:center; gap:6px; padding:6px 10px; border:1px solid transparent;
        border-bottom:none; border-radius:6px 6px 0 0; background:transparent; color:#8b98a9;
        cursor:pointer; font:inherit; }
      .vhcu-tab:hover { color:#d7dee8; background:#111722; }
      .vhcu-tab.active { color:#e8eef7; background:#111722; border-color:#2b3442; }
      .vhcu-badge { width:18px; height:18px; border-radius:4px; background:#1b2330; color:#9fb0c7;
        display:inline-flex; align-items:center; justify-content:center; font-size:10px; font-weight:600; }
      .vhcu-tab.active .vhcu-badge { background:#22304a; color:#8fb0ff; }
      .vhcu-count { color:#5f6c80; font-size:10px; font-variant-numeric:tabular-nums; }
      .vhcu-tab-dot { width:6px; height:6px; border-radius:50%; flex:0 0 auto; }
      .vhcu-hear-bar { display:flex; align-items:center; gap:8px; padding:8px 12px; border-bottom:1px solid #1e2530;
        background:#101620; }
      .vhcu-hear-meta { flex:1; min-width:0; color:#c6d2e2; }
      .vhcu-hear-meta b { font-weight:500; display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
      .vhcu-hear-bar .vhcu-hint { margin-top:1px; color:#5f6c80; font-size:10px; overflow-wrap:anywhere;
        white-space:pre-wrap; max-height:52px; overflow:auto; }
      .vhcu-list { flex:1; min-height:0; overflow:auto; padding:4px 0; }
      .vhcu-row { display:flex; gap:8px; padding:6px 12px; border-bottom:1px solid #121821; }
      .vhcu-row:hover { background:#111722; }
      .vhcu-row.error { background:#1a1214; }
      .vhcu-time { color:#5f6c80; font-variant-numeric:tabular-nums; flex:0 0 auto; }
      .vhcu-col { flex:1; min-width:0; }
      .vhcu-line { display:flex; gap:8px; align-items:flex-start; }
      .vhcu-kind { color:#7f8ea3; flex:0 0 auto; }
      .vhcu-text { color:#c6d2e2; flex:1; min-width:0; white-space:pre-wrap;
        word-break:break-word; line-height:1.5; }
      .vhcu-text.collapsed { display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical;
        overflow:hidden; }
      .vhcu-hear-audio { color:#f0c674; font-style:italic; }
      .vhcu-more { align-self:flex-start; margin-top:2px; padding:0; border:0; background:none;
        color:#7aa2f7; font-size:11px; cursor:pointer; }
      .vhcu-more:hover { color:#9bb8ff; }
      .vhcu-sub { color:#77859a; font-size:11px; margin-top:2px; overflow-wrap:anywhere; }
      .vhcu-ok { color:#4ade80; }
      .vhcu-fail { color:#f28b82; }
      .vhcu-thumbs { display:flex; gap:6px; margin-top:5px; flex-wrap:wrap; }
      .vhcu-fusion { display:flex; gap:6px; margin-top:4px; flex-wrap:wrap; font-size:11px; }
      .vhcu-chip { display:inline-block; max-width:100%; padding:1px 6px; border-radius:8px;
        border:1px solid #2b3442; background:#0f141c; color:#aab6c8; overflow-wrap:anywhere; }
      .vhcu-chip-v { border-color:#223a4d; color:#8ec5e8; }
      .vhcu-chip-h { border-color:#3d3350; color:#c8b0f0; }
      .vhcu-thumb { width:84px; height:52px; object-fit:cover; border-radius:4px; border:1px solid #2b3442;
        background:#0a0d12; }
      .vhcu-empty { padding:26px 14px; color:#5f6c80; text-align:center; line-height:1.8; }
      .vhcu-foot { padding:6px 12px; border-top:1px solid #1e2530; color:#5f6c80; font-size:10px; display:flex; gap:10px; }
      .vhcu-note { margin:6px 12px; padding:6px 8px; border:1px solid #3a2b1a; border-radius:5px;
        background:#1d1811; color:#f0c674; font-size:11px; }
      .vhcu-backdrop { position:fixed; inset:0; z-index:40; background:transparent; }
      .vhcu-pop { position:absolute; top:44px; right:10px; z-index:50; width:288px; max-height:calc(100% - 56px);
        overflow:auto; padding:10px; border:1px solid #2b3442; border-radius:8px; background:#121821;
        box-shadow:0 12px 28px rgba(0,0,0,.45); }
      .vhcu-pop h4 { margin:0 0 8px; font-size:11px; color:#8b98a9; text-transform:uppercase; letter-spacing:.08em; }
      .vhcu-field { display:flex; flex-direction:column; align-items:stretch; gap:5px;
        padding:7px 0; margin:8px 0; color:#c6d2e2; }
      .vhcu-settings-tip { color:#8b98a9; font-size:10px; line-height:1.5; margin:2px 0 8px; }
      .vhcu-settings-section { border:1px solid #2b3442; border-radius:6px; margin:8px 0;
        overflow:hidden; }
      .vhcu-settings-danger { border-color:#7f1d1d; }
      .vhcu-section-head { width:100%; text-align:left; border:0; background:#111722;
        color:#c6d2e2; padding:7px 8px; cursor:pointer; font:inherit; font-weight:600; }
      .vhcu-section-head:hover { background:#161c25; }
      .vhcu-section-body { padding:0 8px; }
      .vhcu-field + .vhcu-field { margin-top:12px; }
      .vhcu-field label { flex:none; font-size:11px; color:#aab6c8; font-weight:600; }
      .vhcu-field input[type=checkbox] { align-self:flex-start; width:16px; height:16px; accent-color:#4d6bfe; }
      .vhcu-select { width:100%; box-sizing:border-box; background:#0d1016; color:#d7dee8; border:1px solid #2b3442;
        border-radius:4px; padding:3px 4px; font:inherit; }
      .vhcu-macro-list { margin:2px 0 6px; }
      .vhcu-group { margin-bottom:4px; }
      .vhcu-group-row { display:flex; gap:8px; align-items:center; padding:7px 4px; border-bottom:1px solid #1b2330;
        cursor:pointer; border-radius:4px; }
      .vhcu-group-row:hover { background:#111722; }
      .vhcu-group-arrow { color:#5f6c80; flex:0 0 auto; font-size:10px; }
      .vhcu-mini-btn { border:1px solid #2b3442; background:#161c25; color:#9fb0c7; border-radius:4px;
        cursor:pointer; padding:2px 6px; font-size:11px; }
      .vhcu-mini-btn:hover { border-color:#5a2a2a; color:#f28b82; }
      .vhcu-group-head { width:100%; text-align:left; padding:4px 2px; border:none; border-bottom:1px solid #1b2330;
        background:transparent; color:#9fb0c7; cursor:pointer; font:inherit; font-size:11px; }
      .vhcu-group-head:hover { color:#d7dee8; }
      .vhcu-macro-row { display:flex; gap:6px; align-items:center; padding:5px 2px; border-bottom:1px solid #121821;
        cursor:pointer; border-radius:4px; }
      .vhcu-macro-row:hover { background:#111722; }
      .vhcu-macro-row input { margin-top:0; accent-color:#4d6bfe; }
      .vhcu-macro-name { color:#e8eef7; font-weight:500; overflow-wrap:anywhere; }
      .vhcu-chevron { color:#5f6c80; flex:0 0 auto; }
      .vhcu-form { display:flex; flex-direction:column; gap:5px; margin:6px 0; }
      .vhcu-btn-row { display:flex; gap:6px; margin:5px 0; }
      .vhcu-btn-row .vhcu-btn { width:auto; flex:1; margin-top:0; }
      .vhcu-danger { border-color:#7f1d1d !important; color:#fecaca !important;
        background:#241316; font-weight:600; }
      .vhcu-danger:hover:not(:disabled) { border-color:#ef4444 !important; background:#3a1315; }
      .vhcu-danger.armed { background:#b91c1c; border-color:#ef4444 !important; color:#fff !important;
        animation:vhcu-pulse 1.2s ease-in-out infinite; }
      .vhcu-input, .vhcu-textarea { width:100%; box-sizing:border-box; background:#0d1016; color:#d7dee8;
        border:1px solid #2b3442; border-radius:4px; padding:4px 5px; font:inherit; }
      .vhcu-textarea { font-family:Consolas, monospace; font-size:11px; resize:vertical; }
      .vhcu-pop .vhcu-btn { width:100%; margin-top:6px; }
      .vhcu-pop .vhcu-btn-row .vhcu-btn { width:auto; flex:1; margin-top:0; }
      .vhcu-pop .vhcu-hint { margin-top:4px; color:#5f6c80; font-size:10px; overflow-wrap:anywhere; }
      .vhcu-list::-webkit-scrollbar { width:8px; }
      .vhcu-list::-webkit-scrollbar-thumb { background:#232c3a; border-radius:4px; }
    `;

    const DOT = {
      idle: '#5f6c80',
      starting: '#f0c674',
      running: '#4ade80',
      stopping: '#f0c674',
      error: '#f28b82',
    };

    const PHASE_TEXT = {
      idle: '已停止',
      starting: '启动中',
      running: '运行中',
      recovering: '恢复中',
      stopping: '停止中',
      error: '异常',
    };

    const TABS = [
      { key: 'vision', badge: 'V', label: 'Vision', hint: '视觉差分与变化缩略图' },
      { key: 'action', badge: 'A', label: 'Action', hint: '工具动作、耗时与结果' },
      { key: 'hear', badge: 'H', label: 'Hear', hint: '语音转写与音频事件（预留）' },
      { key: 'multimodal', badge: 'M', label: '融合', hint: '同一 1.5 秒窗口内的视觉 + 听觉' },
    ];

    const IME_OPTIONS = [
      { value: 'ctrl+space', label: 'Ctrl + Space（默认）' },
      { value: 'shift', label: 'Shift' },
      { value: 'none', label: '不自动切换' },
    ];

    function ensureStyle() {
      if (document.getElementById(STYLE_ID)) return;
      const el = document.createElement('style');
      el.id = STYLE_ID;
      el.textContent = CSS;
      document.head.appendChild(el);
    }

    function clock(ts) {
      const date = new Date(ts || Date.now());
      const pad = (value) => String(value).padStart(2, '0');
      return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    }

    function shortParams(entry) {
      const params = entry.params;
      if (!params || typeof params !== 'object') return '';
      const parts = [];
      for (const key of ['x', 'y', 'amount', 'keys', 'text', 'chars', 'from', 'to']) {
        if (params[key] === undefined) continue;
        if (key === 'text') parts.push(`text=${JSON.stringify(String(params[key]).slice(0, 24))}`);
        else if (key === 'keys') parts.push(`keys=${(params[key] || []).join('+')}`);
        else if (key === 'from') parts.push(`from=${(params[key] || []).join(',')}`);
        else if (key === 'to') parts.push(`to=${(params[key] || []).join(',')}`);
        else parts.push(`${key}=${params[key]}`);
      }
      return parts.join(' · ');
    }

    function VisionRow({ entry, debug }) {
      const rois = Array.isArray(entry.rois) ? entry.rois : [];
      const thumbs = rois
        .filter((roi) => roi && roi.frame_url)
        .slice(0, 3)
        .map((roi, index) =>
          h('img', {
            key: `${entry.id}-${index}`,
            className: 'vhcu-thumb',
            src: roi.frame_url,
            alt: `变化区域 ${index + 1}`,
            loading: 'lazy',
          }),
        );
      return h(
        'div',
        { className: 'vhcu-row' },
        h('span', { className: 'vhcu-time' }, clock(entry.ts)),
        h(
          'div',
          { className: 'vhcu-col' },
          h(
            'div',
            { className: 'vhcu-line' },
            h('span', { className: 'vhcu-kind' }, 'V'),
            h('span', { className: 'vhcu-text' }, entry.text || '检测到画面变化'),
          ),
          h(
            'div',
            { className: 'vhcu-sub' },
            `ROI ${entry.roiCount ?? rois.length} · ${Number(entry.diffMs ?? 0).toFixed(2)}ms`,
            entry.screen ? ` · ${entry.screen.width}x${entry.screen.height}` : '',
            rois.length ? ` · 坐标 ${rois.map((roi) => `(${roi.x},${roi.y})`).join(' ')}` : '',
          ),
          thumbs.length ? h('div', { className: 'vhcu-thumbs' }, thumbs) : null,
          debug ? h('div', { className: 'vhcu-sub' }, JSON.stringify(entry)) : null,
        ),
      );
    }

    function ActionRow({ entry, debug }) {
      const ok = entry.ok !== false;
      return h(
        'div',
        { className: `vhcu-row ${ok ? '' : 'error'}` },
        h('span', { className: 'vhcu-time' }, clock(entry.ts)),
        h(
          'div',
          { className: 'vhcu-col' },
          h(
            'div',
            { className: 'vhcu-line' },
            h('span', { className: 'vhcu-kind' }, ok ? 'A' : 'A!'),
            h('span', { className: 'vhcu-text' }, entry.action || 'action'),
            h('span', { className: ok ? 'vhcu-ok' : 'vhcu-fail' }, ok ? '成功' : '失败'),
            h('span', { className: 'vhcu-count' }, `${entry.durationMs ?? 0}ms`),
          ),
          h(
            'div',
            { className: 'vhcu-sub' },
            shortParams(entry) || entry.text || '',
            entry.error ? ` · ${entry.error}` : '',
          ),
          debug ? h('div', { className: 'vhcu-sub' }, JSON.stringify(entry)) : null,
        ),
      );
    }

    function MultimodalRow({ entry }) {
      const rois = Array.isArray(entry.vision_rois) ? entry.vision_rois.slice(0, 3) : [];
      return h(
        'div',
        { className: 'vhcu-row' },
        h('span', { className: 'vhcu-time' }, clock(entry.ts)),
        h(
          'div',
          { className: 'vhcu-col' },
          h(
            'div',
            { className: 'vhcu-line' },
            h('span', { className: 'vhcu-kind' }, 'M'),
            h('span', { className: 'vhcu-text' }, entry.text || '融合事件'),
          ),
          h(
            'div',
            { className: 'vhcu-fusion' },
            h('span', { className: 'vhcu-chip vhcu-chip-v' }, `👁 ${entry.vision || '无视觉变化'}`),
            h('span', { className: 'vhcu-chip vhcu-chip-h' }, `👂 ${entry.hear_text || '无语音转写'}`),
          ),
          rois.length
            ? h(
                'div',
                { className: 'vhcu-thumbs' },
                rois.map((roi, index) =>
                  h('img', {
                    key: roi.id ?? index,
                    className: 'vhcu-thumb',
                    src: roi.frame_url,
                    alt: `ROI ${roi.id ?? index}`,
                    loading: 'lazy',
                  }),
                ),
              )
            : null,
        ),
      );
    }

    function HearRow({ entry, debug }) {
      const text = String(entry.text || '音频事件');
      const [expanded, setExpanded] = useState(false);
      const [canExpand, setCanExpand] = useState(text.length > 100);
      const textRef = useRef(null);

      // P13.2: raw whisper text only — no LLM summary. Collapse long lines to
      // 3 lines by default; the expand button reveals the original string.
      useEffect(() => {
        if (expanded) return;
        const el = textRef.current;
        if (!el) return;
        if (el.scrollHeight > el.clientHeight + 2) setCanExpand(true);
      }, [text, expanded]);

      return h(
        'div',
        { className: 'vhcu-row' },
        h('span', { className: 'vhcu-time' }, clock(entry.ts)),
        h(
          'div',
          { className: 'vhcu-col' },
          h(
            'div',
            { className: 'vhcu-line' },
            h('span', { className: 'vhcu-kind' }, 'H'),
            h(
              'span',
              {
                ref: textRef,
                className: `vhcu-text${entry.audio ? ' vhcu-hear-audio' : ''}${expanded ? '' : ' collapsed'}`,
                title: text,
              },
              text,
            ),
          ),
          canExpand
            ? h(
                'button',
                {
                  type: 'button',
                  className: 'vhcu-more',
                  onClick: () => setExpanded((value) => !value),
                },
                expanded ? '收起' : '展开',
              )
            : null,
          debug ? h('div', { className: 'vhcu-sub' }, JSON.stringify(entry)) : null,
        ),
      );
    }

    function SettingsPanel({
      settings,
      macros,
      categories,
      onSave,
      onToggleMacro,
      onCreateMacro,
      onUpdateMacro,
      onDeleteMacro,
      onAddCategory,
      onDeleteCategory,
      onClose,
    }) {
      const [draft, setDraft] = useState(settings);
      const [saving, setSaving] = useState(false);
      const [group, setGroup] = useState(null);
      const [selected, setSelected] = useState(null);
      const [detail, setDetail] = useState({
        category: '',
        description: '',
        target_app: '',
        is_destructive: false,
        steps: '',
        builtin: false,
      });
      const [creatingMacro, setCreatingMacro] = useState(false);
      const [creatingGroup, setCreatingGroup] = useState(false);
      const [macroForm, setMacroForm] = useState({ name: '', description: '', target_app: '', steps: '[\n  {"op":"wait","ms":300}\n]' });
      const [importingMacro, setImportingMacro] = useState(false);
      const [importText, setImportText] = useState('');
      const [exportText, setExportText] = useState('');
      const [exportNote, setExportNote] = useState('');
      const [confirmDelete, setConfirmDelete] = useState(false);
      const [groupName, setGroupName] = useState('');
      const [confirmUnprotected, setConfirmUnprotected] = useState(false);
      const [confirmAutoApprove, setConfirmAutoApprove] = useState(false);
      const [openSections, setOpenSections] = useState({ general: true, advanced: false, danger: false });
      const [error, setError] = useState('');
      useEffect(() => setDraft(settings), [settings]);
      const patch = (value) => setDraft((previous) => ({ ...previous, ...value }));

      const groups = (categories && categories.length ? categories : []).map((category) => ({
        ...category,
        count: macros.filter((macro) => macro.category === category.name).length,
      }));
      const current = group ? macros.filter((macro) => macro.category === group) : [];

      const openDetail = (macro) => {
        setSelected(macro.name);
        setDetail({
          category: macro.category,
          description: macro.description || '',
          target_app: macro.target_app || '',
          is_destructive: Boolean(macro.is_destructive),
          steps: JSON.stringify(macro.steps, null, 2),
          builtin: Boolean(macro.builtin),
        });
        setExportText('');
        setExportNote('');
        setConfirmDelete(false);
        setError('');
      };
      const saveDetail = async () => {
        setError('');
        try {
          const steps = JSON.parse(detail.steps);
          const result = await onUpdateMacro(selected, {
            category: detail.category,
            description: detail.description,
            target_app: detail.target_app,
            is_destructive: Boolean(detail.is_destructive),
            steps,
          });
          if (result?.ok) setSelected(null);
          else setError(result?.error || '保存失败');
        } catch (parseError) {
          setError(`steps JSON 无效：${parseError?.message ?? parseError}`);
        }
      };
      const deleteDetail = async () => {
        setError('');
        if (!confirmDelete) {
          setConfirmDelete(true);
          return;
        }
        const result = await onDeleteMacro(selected);
        if (result?.ok) {
          setSelected(null);
          setConfirmDelete(false);
        } else {
          setConfirmDelete(false);
          setError(result?.error || '删除失败');
        }
      };
      const exportDetail = () => {
        const macro = macros.find((item) => item.name === selected);
        if (!macro) {
          setError('找不到该宏，无法导出');
          return;
        }
        const payload = {
          name: macro.name,
          description: macro.description || '',
          category: macro.category,
          target_app: macro.target_app || '',
          steps: macro.steps,
        };
        setExportText(JSON.stringify(payload, null, 2));
        setExportNote('');
      };
      const copyExport = async () => {
        try {
          if (typeof navigator === 'undefined' || !navigator.clipboard?.writeText) {
            throw new Error('clipboard unavailable');
          }
          await navigator.clipboard.writeText(exportText);
          setExportNote('已复制到剪贴板');
        } catch {
          setExportNote('复制失败，请选中上方 JSON 手动复制');
        }
      };
      const importMacros = async () => {
        setError('');
        let items;
        try {
          const parsed = JSON.parse(importText);
          items = Array.isArray(parsed)
            ? parsed
            : Array.isArray(parsed?.macros)
              ? parsed.macros
              : [parsed?.macro ?? parsed];
        } catch (parseError) {
          setError(`导入 JSON 无效：${parseError?.message ?? parseError}`);
          return;
        }
        if (!items.length || items.length > 20) {
          setError('导入内容必须是 1..20 个宏');
          return;
        }
        const category = group && group !== '系统基础宏' ? group : '我的自定义宏';
        const macrosToImport = [];
        try {
          items.forEach((item, index) => {
            if (!item || typeof item !== 'object' || Array.isArray(item)) {
              throw new Error(`第 ${index + 1} 个不是对象`);
            }
            const name = String(item.name ?? '').trim();
            if (!name) throw new Error(`第 ${index + 1} 个缺少 name`);
            if (!Array.isArray(item.steps) || item.steps.length === 0) {
              throw new Error(`第 ${index + 1} 个缺少非空 steps`);
            }
            if (item.steps.length > 100) {
              throw new Error(`第 ${index + 1} 个超过 100 步`);
            }
            macrosToImport.push({
              name,
              description: String(item.description ?? '').slice(0, 200),
              target_app: String(item.target_app ?? '').trim(),
              is_destructive: Boolean(item.is_destructive),
              category,
              steps: item.steps,
            });
          });
        } catch (validationError) {
          setError(`导入校验失败：${validationError?.message ?? validationError}`);
          return;
        }
        for (let index = 0; index < macrosToImport.length; index += 1) {
          const result = await onCreateMacro(macrosToImport[index]);
          if (!result?.ok) {
            setError(`第 ${index + 1} 个导入失败：${result?.error || '保存失败'}`);
            return;
          }
        }
        setImportingMacro(false);
        setImportText('');
      };
      const submitMacro = async () => {
        setError('');
        try {
          const steps = JSON.parse(macroForm.steps);
          const result = await onCreateMacro({
            name: macroForm.name.trim(),
            description: macroForm.description.trim(),
            target_app: macroForm.target_app.trim(),
            is_destructive: Boolean(macroForm.is_destructive),
            category: group,
            steps,
          });
          if (result?.ok) {
            setCreatingMacro(false);
            setMacroForm({ name: '', description: '', target_app: '', is_destructive: false, steps: '[\n  {"op":"wait","ms":300}\n]' });
          } else {
            setError(result?.error || '新建宏失败');
          }
        } catch (parseError) {
          setError(`steps JSON 无效：${parseError?.message ?? parseError}`);
        }
      };
      const submitGroup = async () => {
        setError('');
        const result = await onAddCategory(groupName.trim());
        if (result?.ok) {
          setCreatingGroup(false);
          setGroupName('');
        } else {
          setError(result?.error || '新建宏组失败');
        }
      };
      const removeGroup = async (name) => {
        setError('');
        const result = await onDeleteCategory(name);
        if (!result?.ok) setError(result?.error || '删除宏组失败');
      };
      const saveSettings = async () => {
        setSaving(true);
        try {
          await onSave(draft);
        } finally {
          setSaving(false);
        }
      };
      const section = (key, title, children, extraClass = '') =>
        h(
          'div',
          { className: `vhcu-settings-section ${extraClass}`.trim() },
          h(
            'button',
            {
              type: 'button',
              className: 'vhcu-section-head',
              onClick: () => setOpenSections((previous) => ({ ...previous, [key]: !previous[key] })),
            },
            `${openSections[key] ? '▾' : '▸'} ${title}`,
          ),
          openSections[key] ? h('div', { className: 'vhcu-section-body' }, children) : null,
        );

      // ── 宏详情 ────────────────────────────────────────────────
      if (selected) {
        const readOnly = detail.builtin;
        return h(
          'div',
          { className: 'vhcu-pop', onClick: (event) => event.stopPropagation() },
          h('h4', null, `${readOnly ? '内置宏' : '编辑宏'} · ${selected}`),
          readOnly ? h('div', { className: 'vhcu-hint' }, '系统基础宏只读，可启用/禁用；如需修改请复制为自定义宏。') : null,
          h('input', {
            className: 'vhcu-input',
            value: detail.category,
            disabled: readOnly,
            placeholder: '分类',
            onChange: (event) => setDetail((previous) => ({ ...previous, category: event.target.value })),
          }),
          h('input', {
            className: 'vhcu-input',
            value: detail.description,
            disabled: readOnly,
            placeholder: '描述',
            onChange: (event) => setDetail((previous) => ({ ...previous, description: event.target.value })),
          }),
          h('input', {
            className: 'vhcu-input',
            value: detail.target_app,
            disabled: readOnly,
            placeholder: 'target_app（可选，如 notepad / chrome）',
            onChange: (event) => setDetail((previous) => ({ ...previous, target_app: event.target.value })),
          }),
          h(
            'label',
            { className: 'vhcu-inline-check' },
            h('input', {
              type: 'checkbox',
              disabled: readOnly,
              checked: Boolean(detail.is_destructive),
              onChange: (event) =>
                setDetail((previous) => ({ ...previous, is_destructive: event.target.checked })),
            }),
            ' 破坏性宏（模型执行前必须由用户审批）',
          ),
          h('textarea', {
            className: 'vhcu-textarea',
            rows: 9,
            spellCheck: false,
            readOnly,
            value: detail.steps,
            onChange: (event) => setDetail((previous) => ({ ...previous, steps: event.target.value })),
          }),
          error ? h('div', { className: 'vhcu-hint', style: { color: '#f28b82' } }, error) : null,
          readOnly
            ? null
            : h('button', { type: 'button', className: 'vhcu-btn', onClick: saveDetail }, '保存修改'),
          h('button', { type: 'button', className: 'vhcu-btn', onClick: exportDetail }, '📤 导出'),
          exportText
            ? h(
                'div',
                { className: 'vhcu-form' },
                h('textarea', {
                  className: 'vhcu-textarea',
                  rows: 10,
                  readOnly: true,
                  spellCheck: false,
                  value: exportText,
                  onFocus: (event) => event.target.select(),
                  onClick: (event) => event.target.select(),
                }),
                h('div', { className: 'vhcu-hint' }, exportNote || '选中上方 JSON 复制，或点「复制」'),
                h(
                  'div',
                  { className: 'vhcu-btn-row' },
                  h('button', { type: 'button', className: 'vhcu-btn', onClick: copyExport }, '📋 复制'),
                  h(
                    'button',
                    {
                      type: 'button',
                      className: 'vhcu-btn',
                      onClick: () => {
                        setExportText('');
                        setExportNote('');
                      },
                    },
                    '收起导出',
                  ),
                ),
              )
            : null,
          readOnly
            ? null
            : h(
                'button',
                {
                  type: 'button',
                  className: `vhcu-btn vhcu-danger${confirmDelete ? ' armed' : ''}`,
                  onClick: deleteDetail,
                },
                confirmDelete ? '⚠ 再点一次确认删除' : '🗑 删除此宏',
              ),
          confirmDelete
            ? h(
                'button',
                {
                  type: 'button',
                  className: 'vhcu-btn',
                  onClick: () => setConfirmDelete(false),
                },
                '取消删除',
              )
            : null,
          h('button', { type: 'button', className: 'vhcu-btn', onClick: () => setSelected(null) }, '← 返回宏列表'),
        );
      }

      // ── 一级：宏组列表 ────────────────────────────────────────
      if (!group) {
        return h(
          'div',
          { className: 'vhcu-pop', onClick: (event) => event.stopPropagation() },
          h('h4', null, '设置'),
          h(
            'div',
            { className: 'vhcu-hint', style: { marginBottom: '8px' } },
            '紧急情况可按 Ctrl+Shift+X 强制停止',
          ),
          h(
            'div',
            { className: 'vhcu-settings-tip' },
            '💡 建议保持默认设置。如需调整高级选项，请确保您了解其风险。',
          ),
          section('general', '【常规设置】', [
            h(
              'div',
              { className: 'vhcu-field' },
              h('label', { htmlFor: 'vhcu-ime' }, '输入法切换'),
              h(
                'select',
                {
                  id: 'vhcu-ime',
                  className: 'vhcu-select',
                  value: draft.imeShortcut,
                  onChange: (event) => patch({ imeShortcut: event.target.value }),
                },
                IME_OPTIONS.map((option) => h('option', { key: option.value, value: option.value }, option.label)),
              ),
            ),
            h(
              'div',
              { className: 'vhcu-field' },
              h('label', { htmlFor: 'vhcu-sensitivity' }, '听觉灵敏度'),
              h(
                'select',
                {
                  id: 'vhcu-sensitivity',
                  className: 'vhcu-select',
                  value: draft.hearSensitivity ?? 'normal',
                  onChange: (event) => patch({ hearSensitivity: event.target.value }),
                },
                h('option', { value: 'strict' }, '严格（过滤噪音优先）'),
                h('option', { value: 'normal' }, '标准（默认平衡）'),
                h('option', { value: 'sensitive' }, '灵敏（保留说话优先）'),
              ),
              h('div', { className: 'vhcu-hint' }, '调整后立即热更新 Hear，无需重启 DSH'),
            ),
            h(
              'div',
              { className: 'vhcu-field' },
              h('label', { htmlFor: 'vhcu-turbo' }, '启用 Turbo 极速模式'),
              h('input', {
                id: 'vhcu-turbo',
                type: 'checkbox',
                checked: Boolean(draft.turboMode),
                onChange: (event) => patch({ turboMode: event.target.checked }),
              }),
              h(
                'div',
                { className: 'vhcu-hint' },
                '开启后速度大幅提升，但在极不稳定的界面可能因缺乏回读而产生误操作。',
              ),
            ),
          ]),
          section('advanced', '【高级调试】', [
            h(
              'div',
              { className: 'vhcu-field' },
              h('label', { htmlFor: 'vhcu-debug' }, '调试模式（显示原始 JSON）'),
              h('input', {
                id: 'vhcu-debug',
                type: 'checkbox',
                checked: Boolean(draft.debug),
                onChange: (event) => patch({ debug: event.target.checked }),
              }),
            ),
            h(
              'div',
              { className: 'vhcu-field' },
              h('label', { htmlFor: 'vhcu-hear-raw' }, '显示原始转写 (仅调试用)'),
              h('input', {
                id: 'vhcu-hear-raw',
                type: 'checkbox',
                checked: Boolean(draft.hearRawTranscripts),
                onChange: (event) => patch({ hearRawTranscripts: event.target.checked }),
              }),
              h('div', { className: 'vhcu-hint' }, '仅用于排查误杀；开启后可能输出更多幻听文本'),
            ),
            h(
              'div',
              { className: 'vhcu-field' },
              h('label', { htmlFor: 'vhcu-privacy' }, '隐私黑名单（逗号分隔）'),
              h('textarea', {
                id: 'vhcu-privacy',
                className: 'vhcu-textarea',
                rows: 2,
                value: (draft.privacyBlacklist ?? []).join(', '),
                onChange: (event) =>
                  patch({
                    privacyBlacklist: event.target.value
                      .split(/[,，\n]/)
                      .map((value) => value.trim())
                      .filter(Boolean),
                  }),
              }),
              h('div', { className: 'vhcu-hint' }, '活动窗口标题包含这些词时，暂停视觉截屏'),
            ),
          ]),
          section('danger', '【危险区域】', [
            h(
              'div',
              { className: 'vhcu-field' },
              h('label', { htmlFor: 'vhcu-privacy-unprotected' }, '关闭隐私保护 (极高风险)'),
              h('input', {
                id: 'vhcu-privacy-unprotected',
                type: 'checkbox',
                checked: Boolean(draft.privacyUnprotected),
                onChange: (event) => {
                  if (event.target.checked) {
                    setConfirmUnprotected(true);
                  } else {
                    setConfirmUnprotected(false);
                    patch({ privacyUnprotected: false });
                  }
                },
              }),
              h(
                'div',
                { className: 'vhcu-hint' },
                '开启后敏感窗口截图与键鼠注入均放行；仅在高危调试场景使用',
              ),
              confirmUnprotected
                ? h(
                    'div',
                    { className: 'vhcu-privacy-confirm' },
                    h('div', { className: 'vhcu-hint' }, '⚠ 高危：敏感窗口截图与键鼠注入均放行，确认开启后请点底部「保存设置」'),
                    h(
                      'div',
                      { className: 'vhcu-btn-row' },
                      h(
                        'button',
                        {
                          type: 'button',
                          className: 'vhcu-btn vhcu-danger',
                          onClick: () => {
                            patch({ privacyUnprotected: true });
                            setConfirmUnprotected(false);
                          },
                        },
                        '确认开启',
                      ),
                      h(
                        'button',
                        {
                          type: 'button',
                          className: 'vhcu-btn',
                          onClick: () => setConfirmUnprotected(false),
                        },
                        '取消',
                      ),
                    ),
                  )
                : null,
            ),
            h(
              'div',
              { className: 'vhcu-field' },
              h('label', { htmlFor: 'vhcu-auto-approve' }, '自动允许危险操作 (无人值守)'),
              h('input', {
                id: 'vhcu-auto-approve',
                type: 'checkbox',
                checked: Boolean(draft.autoApproveDestructive),
                onChange: (event) => {
                  if (event.target.checked) {
                    setConfirmAutoApprove(true);
                  } else {
                    setConfirmAutoApprove(false);
                    patch({ autoApproveDestructive: false });
                  }
                },
              }),
              h(
                'div',
                { className: 'vhcu-hint' },
                '开启后 AI 执行 is_destructive 宏时不再弹窗审批；仅用于无人值守长任务',
              ),
              confirmAutoApprove
                ? h(
                    'div',
                    { className: 'vhcu-privacy-confirm' },
                    h(
                      'div',
                      { className: 'vhcu-hint' },
                      '⚠ 高危：此操作将允许 AI 自动执行关闭、删除等操作，可能导致数据丢失。确认开启后请点底部「保存设置」',
                    ),
                    h(
                      'div',
                      { className: 'vhcu-btn-row' },
                      h(
                        'button',
                        {
                          type: 'button',
                          className: 'vhcu-btn vhcu-danger',
                          onClick: () => {
                            patch({ autoApproveDestructive: true });
                            setConfirmAutoApprove(false);
                          },
                        },
                        '确认开启',
                      ),
                      h(
                        'button',
                        {
                          type: 'button',
                          className: 'vhcu-btn',
                          onClick: () => setConfirmAutoApprove(false),
                        },
                        '取消',
                      ),
                    ),
                  )
                : null,
            ),
          ], 'vhcu-settings-danger'),
          h('h4', { style: { marginTop: '10px' } }, '宏管理 · 宏组'),
          h(
            'div',
            { className: 'vhcu-macro-list' },
            groups.length === 0
              ? h('div', { className: 'vhcu-hint' }, '还没有宏组。点下方按钮新建。')
              : groups.map((category) =>
                  h(
                    'div',
                    {
                      key: category.name,
                      className: 'vhcu-group-row',
                      role: 'button',
                      tabIndex: 0,
                      onClick: () => {
                        setGroup(category.name);
                        setCreatingMacro(false);
                        setError('');
                      },
                      onKeyDown: (event) => {
                        if (event.key === 'Enter' || event.key === ' ') setGroup(category.name);
                      },
                    },
                    h('span', { className: 'vhcu-group-arrow' }, '▶'),
                    h(
                      'div',
                      { style: { flex: 1, minWidth: 0 } },
                      h('div', { className: 'vhcu-macro-name' }, category.name),
                      h(
                        'div',
                        { className: 'vhcu-hint' },
                        `${category.count} 个宏${category.builtin ? ' · 内置不可删除' : ''}`,
                      ),
                    ),
                    category.builtin
                      ? null
                      : h(
                          'button',
                          {
                            type: 'button',
                            className: 'vhcu-mini-btn',
                            title: '删除宏组',
                            onClick: (event) => {
                              event.stopPropagation();
                              removeGroup(category.name);
                            },
                          },
                          '🗑',
                        ),
                  ),
                ),
          ),
          h(
            'button',
            { type: 'button', className: 'vhcu-btn', onClick: () => setCreatingGroup((value) => !value) },
            creatingGroup ? '取消新建宏组' : '+ 新建宏组',
          ),
          creatingGroup
            ? h(
                'div',
                { className: 'vhcu-form' },
                h('input', {
                  className: 'vhcu-input',
                  placeholder: '宏组名（如 我的自定义宏）',
                  value: groupName,
                  onChange: (event) => setGroupName(event.target.value),
                }),
                h('button', { type: 'button', className: 'vhcu-btn', onClick: submitGroup }, '保存宏组'),
              )
            : null,
          error ? h('div', { className: 'vhcu-hint', style: { color: '#f28b82' } }, error) : null,
          h('button', { type: 'button', className: 'vhcu-btn', disabled: saving, onClick: saveSettings }, saving ? '保存中…' : '保存设置'),
          h('div', { className: 'vhcu-hint' }, '设置保存在插件进程内；宏与宏组保存在 macros.json。'),
          h('button', { type: 'button', className: 'vhcu-btn', onClick: onClose }, '关闭'),
        );
      }

      // ── 二级：组内宏列表 ──────────────────────────────────────
      return h(
        'div',
        { className: 'vhcu-pop', onClick: (event) => event.stopPropagation() },
        h(
          'button',
          { type: 'button', className: 'vhcu-btn', style: { marginTop: 0 }, onClick: () => setGroup(null) },
          '← 返回组列表',
        ),
        h('h4', { style: { marginTop: '10px' } }, `${group} (${current.length})`),
        h(
          'div',
          { className: 'vhcu-macro-list' },
          current.length === 0
            ? h('div', { className: 'vhcu-hint' }, '这个宏组还没有宏。')
            : current.map((macro) =>
                h(
                  'div',
                  {
                    key: macro.name,
                    className: 'vhcu-macro-row',
                    role: 'button',
                    tabIndex: 0,
                    onClick: () => openDetail(macro),
                    onKeyDown: (event) => {
                      if (event.key === 'Enter' || event.key === ' ') openDetail(macro);
                    },
                  },
                  h('input', {
                    type: 'checkbox',
                    checked: Boolean(macro.enabled),
                    title: macro.enabled ? '禁用' : '启用',
                    onClick: (event) => event.stopPropagation(),
                    onChange: (event) => onToggleMacro(macro.name, event.target.checked),
                  }),
                  h(
                    'div',
                    { style: { flex: 1, minWidth: 0 } },
                    h('div', { className: 'vhcu-macro-name' }, macro.name),
                    h(
                      'div',
                      { className: 'vhcu-hint' },
                      `${macro.steps?.length ?? 0} 步 · ${macro.description || '无描述'}` +
                        (macro.target_app ? ` · 适用 ${macro.target_app}` : ' · 通用') +
                        (macro.variables?.length ? ` · 参数 ${macro.variables.join(', ')}` : '') +
                        (macro.builtin ? ' · 内置' : ''),
                    ),
                  ),
                  h('span', { className: 'vhcu-chevron' }, '›'),
                ),
              ),
        ),
        h(
          'div',
          { className: 'vhcu-btn-row' },
          h(
            'button',
            {
              type: 'button',
              className: 'vhcu-btn',
              onClick: () => {
                setCreatingMacro((value) => !value);
                setImportingMacro(false);
              },
            },
            creatingMacro ? '取消新建宏' : '+ 新建宏',
          ),
          h(
            'button',
            {
              type: 'button',
              className: 'vhcu-btn',
              onClick: () => {
                setImportingMacro((value) => !value);
                setCreatingMacro(false);
              },
            },
            importingMacro ? '取消导入' : '📥 导入宏',
          ),
        ),
        importingMacro
          ? h(
              'div',
              { className: 'vhcu-form' },
              h(
                'div',
                { className: 'vhcu-hint' },
                `粘贴单个宏 JSON 或宏数组；将导入到「${group && group !== '系统基础宏' ? group : '我的自定义宏'}」`,
              ),
              h('textarea', {
                className: 'vhcu-textarea',
                rows: 8,
                spellCheck: false,
                placeholder: '{"name":"my_macro","steps":[{"op":"wait","ms":300}]}',
                value: importText,
                onChange: (event) => setImportText(event.target.value),
              }),
              h(
                'div',
                { className: 'vhcu-btn-row' },
                h('button', { type: 'button', className: 'vhcu-btn', onClick: importMacros }, '确认导入'),
                h(
                  'button',
                  {
                    type: 'button',
                    className: 'vhcu-btn',
                    onClick: () => {
                      setImportingMacro(false);
                      setImportText('');
                    },
                  },
                  '取消导入',
                ),
              ),
            )
          : null,
        creatingMacro
          ? h(
              'div',
              { className: 'vhcu-form' },
              h('div', { className: 'vhcu-hint' }, `将创建在「${group}」组`),
              h('input', {
                className: 'vhcu-input',
                placeholder: '宏名（如 open_settings）',
                value: macroForm.name,
                onChange: (event) => setMacroForm((previous) => ({ ...previous, name: event.target.value })),
              }),
              h('input', {
                className: 'vhcu-input',
                placeholder: '描述（可选）',
                value: macroForm.description,
                onChange: (event) => setMacroForm((previous) => ({ ...previous, description: event.target.value })),
              }),
              h('input', {
                className: 'vhcu-input',
                placeholder: 'target_app（可选，如 notepad / chrome）',
                value: macroForm.target_app,
                onChange: (event) => setMacroForm((previous) => ({ ...previous, target_app: event.target.value })),
              }),
              h(
                'label',
                { className: 'vhcu-inline-check' },
                h('input', {
                  type: 'checkbox',
                  checked: Boolean(macroForm.is_destructive),
                  onChange: (event) =>
                    setMacroForm((previous) => ({ ...previous, is_destructive: event.target.checked })),
                }),
                ' 破坏性宏（执行前需用户审批）',
              ),
              h('textarea', {
                className: 'vhcu-textarea',
                rows: 5,
                spellCheck: false,
                value: macroForm.steps,
                onChange: (event) => setMacroForm((previous) => ({ ...previous, steps: event.target.value })),
              }),
              h('button', { type: 'button', className: 'vhcu-btn', onClick: submitMacro }, '保存宏'),
            )
          : null,
        error ? h('div', { className: 'vhcu-hint', style: { color: '#f28b82' } }, error) : null,
        h('button', { type: 'button', className: 'vhcu-btn', onClick: onClose }, '关闭'),
      );
    }

    function Panel({ initialTab = 'vision' }) {
      const [state, setState] = useState({
        phase: 'idle',
        running: false,
        counters: { frames: 0, diffs: 0 },
        recording: { active: false, steps: 0, eventCount: 0, truncated: false },
      });
      const [events, setEvents] = useState([]);
      const [busy, setBusy] = useState(false);
      const [hearBusy, setHearBusy] = useState(false);
      const [recordingBusy, setRecordingBusy] = useState(false);
      const [approvalBusy, setApprovalBusy] = useState(false);
      const [pendingRecording, setPendingRecording] = useState(null);
      const [macroForm, setMacroForm] = useState({ name: '', category: '我的自定义宏', is_destructive: false });
      const [saveBusy, setSaveBusy] = useState(false);
      const [link, setLink] = useState('connecting');
      const [error, setError] = useState(null);
      const [tab, setTab] = useState(initialTab);
      const [gear, setGear] = useState(false);
      const [settings, setSettings] = useState({
        debug: false,
        imeShortcut: 'ctrl+space',
        hearSensitivity: 'normal',
        hearRawTranscripts: false,
        privacyUnprotected: false,
        autoApproveDestructive: false,
        turboMode: false,
        privacyBlacklist: ['密码', '银行', '隐私', 'Password', 'Login', '私人'],
        macros: { open_notepad: true, save: true },
      });
      const [macros, setMacros] = useState([]);
      const [categories, setCategories] = useState([]);
      const sourceRef = useRef(null);
      const listRef = useRef(null);
      const gearRef = useRef(false);
      const macrosRevRef = useRef(-1);

      // P13: keep the Hear timeline pinned to the newest subtitle line while
      // the user is already near the bottom; do not yank them if they scrolled up.
      useEffect(() => {
        if (tab !== 'hear' && tab !== 'multimodal') return undefined;
        const el = listRef.current;
        if (!el) return undefined;
        const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
        if (!nearBottom) return undefined;
        const timer = setTimeout(() => {
          el.scrollTop = el.scrollHeight;
        }, 0);
        return () => clearTimeout(timer);
      }, [tab, events]);

      const applyMacroPayload = useCallback((payload) => {
        setMacros(payload.macros ?? []);
        setCategories(payload.categories ?? []);
        macrosRevRef.current = payload.revision ?? 0;
      }, []);

      const fetchMacros = useCallback(async () => {
        try {
          const response = await fetch(`${API}/macros`, { headers: { accept: 'application/json' } });
          const payload = await response.json();
          if (payload?.ok) applyMacroPayload(payload);
        } catch {
          // leave the last list in place
        }
      }, [applyMacroPayload]);

      useEffect(() => {
        gearRef.current = gear;
        if (gear) fetchMacros();
      }, [gear, fetchMacros]);

      const applyPayload = useCallback((payload) => {
        if (payload?.state) {
          setState(payload.state ?? {});
          if (payload.state.settings) setSettings(payload.state.settings);
        }
        if (Array.isArray(payload?.events)) {
          setEvents(payload.events.filter((entry) => DATA_KINDS.has(entry.kind)).slice(-200).reverse());
        }
      }, []);

      useEffect(() => {
        ensureStyle();
        let disposed = false;

        fetch(`${API}/state`, { headers: { accept: 'application/json' } })
          .then((response) => response.json())
          .then((payload) => {
            if (disposed || !payload?.ok) return;
            applyPayload(payload);
            if (payload.state?.settings) setSettings(payload.state.settings);
          })
          .catch(() => setLink('offline'));

        fetch(`${API}/settings`, { headers: { accept: 'application/json' } })
          .then((response) => response.json())
          .then((payload) => {
            if (!disposed && payload?.ok && payload.settings) setSettings(payload.settings);
          })
          .catch(() => {});

        const source = new EventSource(`${API}/events`);
        sourceRef.current = source;
        source.onopen = () => {
          setLink('live');
          setError(null);
        };
        source.onerror = () => setLink('retry');
        source.addEventListener('hello', (message) => {
          try {
            applyPayload(JSON.parse(message.data));
            setLink('live');
          } catch {
            // status frames will recover
          }
        });
        source.addEventListener('status', (message) => {
          try {
            const payload = JSON.parse(message.data);
            setState(payload);
            if (payload.settings) setSettings(payload.settings);
            if (
              gearRef.current &&
              payload.macrosRevision !== undefined &&
              payload.macrosRevision !== macrosRevRef.current
            ) {
              fetchMacros();
            }
          } catch {
            // ignore
          }
        });
        source.addEventListener('event', (message) => {
          try {
            const entry = JSON.parse(message.data);
            if (!DATA_KINDS.has(entry.kind)) return;
            if (entry.kind === 'hear') {
              // P13.1: text received => force the header/status to listening even
              // if a status frame was lost or arrived before the panel mounted.
              setState((previous) => {
                const hear = previous.hear ?? {};
                if (hear.state === 'listening' && hear.running) return previous;
                return {
                  ...previous,
                  hear: { ...hear, state: 'listening', running: true, lastError: null },
                };
              });
            }
            setEvents((previous) => {
              if (previous.some((item) => item.id === entry.id)) return previous;
              return [entry, ...previous].slice(0, 200);
            });
          } catch {
            // ignore
          }
        });

        return () => {
          disposed = true;
          source.close();
          sourceRef.current = null;
        };
      }, [applyPayload, fetchMacros]);

      const toggle = useCallback(async () => {
        if (busy) return;
        const action = state.running ? 'stop' : 'start';
        setBusy(true);
        setError(null);
        try {
          const response = await fetch(`${API}/control`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action }),
          });
          const payload = await response.json();
          if (!payload.ok) {
            setError(payload.error?.hint || payload.error?.message || `HTTP ${response.status}`);
          } else if (payload.state) {
            setState(payload.state);
          }
        } catch (fetchError) {
          setError(fetchError?.message || 'control request failed');
        } finally {
          setBusy(false);
        }
      }, [busy, state.running]);

      const saveSettings = useCallback(async (draft) => {
        try {
          const response = await fetch(`${API}/settings`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ settings: draft }),
          });
          const payload = await response.json();
          if (payload?.ok && payload.settings) {
            setSettings(payload.settings);
            setGear(false);
          } else {
            setError(payload?.error || `HTTP ${response.status}`);
          }
        } catch (fetchError) {
          setError(fetchError?.message || 'settings request failed');
        }
      }, []);

      const resolveApproval = useCallback(async (decision) => {
        const approval = state.approval;
        if (!approval?.id) return;
        setApprovalBusy(true);
        setError(null);
        try {
          const response = await fetch(`${API}/approval`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ id: approval.id, decision }),
          });
          const payload = await response.json();
          if (!payload?.ok) {
            setError(payload?.error || `HTTP ${response.status}`);
          } else {
            setState((previous) => ({ ...previous, approval: null }));
          }
        } catch (fetchError) {
          setError(fetchError?.message || 'approval request failed');
        } finally {
          setApprovalBusy(false);
        }
      }, [state.approval]);

      const toggleMacro = useCallback(
        async (name, enabled) => {
          try {
            const response = await fetch(`${API}/macros`, {
              method: 'POST',
              headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
              body: JSON.stringify({ action: 'toggle', name, enabled }),
            });
            const payload = await response.json();
            if (payload?.ok) {
              applyMacroPayload(payload);
            } else {
              setError(payload?.error || `HTTP ${response.status}`);
            }
          } catch (fetchError) {
            setError(fetchError?.message || 'macro toggle failed');
          }
        },
        [],
      );

      const createMacro = useCallback(async (macro) => {
        try {
          const response = await fetch(`${API}/macros`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action: 'save', macro }),
          });
          const payload = await response.json();
          if (payload?.ok) {
            applyMacroPayload(payload);
          }
          return payload;
        } catch (fetchError) {
          return { ok: false, error: fetchError?.message || 'macro save failed' };
        }
      }, []);

      const updateMacro = useCallback(async (name, patch) => {
        try {
          const response = await fetch(`${API}/macros`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action: 'update', name, macro: patch }),
          });
          const payload = await response.json();
          if (payload?.ok) {
            applyMacroPayload(payload);
          }
          return payload;
        } catch (fetchError) {
          return { ok: false, error: fetchError?.message || 'macro update failed' };
        }
      }, []);

      const deleteMacro = useCallback(async (name) => {
        try {
          const response = await fetch(`${API}/macros`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action: 'delete', name }),
          });
          const payload = await response.json();
          if (payload?.ok) {
            applyMacroPayload(payload);
          }
          return payload;
        } catch (fetchError) {
          return { ok: false, error: fetchError?.message || 'macro delete failed' };
        }
      }, []);

      const addCategory = useCallback(async (name) => {
        try {
          const response = await fetch(`${API}/macros`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action: 'addCategory', name }),
          });
          const payload = await response.json();
          if (payload?.ok) applyMacroPayload(payload);
          return payload;
        } catch (fetchError) {
          return { ok: false, error: fetchError?.message || 'category add failed' };
        }
      }, [applyMacroPayload]);

      const deleteCategory = useCallback(async (name) => {
        try {
          const response = await fetch(`${API}/macros`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action: 'deleteCategory', name }),
          });
          const payload = await response.json();
          if (payload?.ok) applyMacroPayload(payload);
          return payload;
        } catch (fetchError) {
          return { ok: false, error: fetchError?.message || 'category delete failed' };
        }
      }, [applyMacroPayload]);

      const toggleHear = useCallback(async () => {
        if (hearBusy) return;
        const hear = state.hear ?? {};
        const active = Boolean(hear.running) || ['starting', 'loading', 'listening'].includes(hear.state);
        const action = active ? 'hear.stop' : 'hear.start';
        setHearBusy(true);
        setError(null);
        try {
          const response = await fetch(`${API}/control`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action }),
          });
          const payload = await response.json();
          if (!payload.ok) {
            if (payload.state) setState(payload.state);
            setError(payload.error?.hint || payload.error?.message || `HTTP ${response.status}`);
          } else if (payload.state) {
            setState(payload.state);
          }
        } catch (fetchError) {
          setError(fetchError?.message || 'hear control failed');
        } finally {
          setHearBusy(false);
        }
      }, [hearBusy, state.hear]);

      const phase = state.phase ?? 'idle';
      const dot = DOT[phase] ?? DOT.idle;
      const hearState = state.hear?.state ?? 'idle';
      const hearActive =
        Boolean(state.hear?.running) ||
        ['starting', 'loading', 'listening', 'reconnecting'].includes(hearState);
      const hearColor =
        hearState === 'listening'
          ? '#4ade80'
          : hearState === 'loading' || hearState === 'starting' || hearState === 'reconnecting'
            ? '#f0c674'
            : hearState === 'error'
              ? '#f28b82'
              : '#5f6c80';
      const hearText =
        hearState === 'loading' || hearState === 'starting'
          ? '模型加载中…'
          : hearState === 'reconnecting'
            ? '音频设备断开，重连中…'
            : hearState === 'listening'
              ? '正在监听系统声音'
              : hearState === 'error'
                ? `错误：${state.hear?.lastError ?? '未知'}`
                : '未启动';
      const hearHint =
        hearState === 'loading' || hearState === 'starting'
          ? state.hear?.message ||
            (state.hear?.offline && state.hear?.loadedPath
              ? `使用本地离线模型 ${state.hear.loadedPath}`
              : '首次运行会通过 hf-mirror.com 下载 base 模型到插件目录 models/，请稍候')
          : hearState === 'reconnecting'
            ? state.hear?.message || '音频设备断开，尝试重连中'
            : hearState === 'error'
            ? state.hear?.stderr || state.hear?.message || '查看 daemon 日志/模型路径'
            : state.hear?.offline && state.hear?.loadedPath
              ? `使用本地离线模型 ${state.hear.loadedPath}`
              : state.hear?.modelPath
                ? `模型目录 ${state.hear.modelPath}`
                : 'Hear 独立于视觉，可单独开启系统内录';
      const hearBar = h(
        'div',
        { className: 'vhcu-hear-bar' },
        h('span', { className: 'vhcu-dot', style: { color: hearColor, background: hearColor } }),
        h(
          'div',
          { className: 'vhcu-hear-meta' },
          h('div', null, h('b', null, hearText)),
          h('div', { className: 'vhcu-hint' }, hearHint),
          h('div', { className: 'vhcu-hint' }, '音量偏低时请调高系统音量（软件内录无增益）'),
        ),
        h(
          'button',
          {
            type: 'button',
            className: `vhcu-btn${hearActive ? ' on' : ''}`,
            disabled: hearBusy,
            onClick: toggleHear,
            title: hearActive ? '停止系统内录并释放模型' : '独立开启系统内录',
          },
          hearBusy ? '处理中…' : hearActive ? (hearState === 'listening' || hearState === 'reconnecting' ? '关闭听觉' : '取消加载') : '开启听觉',
        ),
      );
      const filtered = events.filter((entry) => entry.kind === tab);
      const counts = {
        vision: events.filter((entry) => entry.kind === 'vision').length,
        action: events.filter((entry) => entry.kind === 'action').length,
        hear: events.filter((entry) => entry.kind === 'hear').length,
        multimodal: events.filter((entry) => entry.kind === 'multimodal').length,
      };

      let rows;
      if (tab === 'hear') {
        rows = filtered.length
          ? filtered.map((entry) => h(HearRow, { key: entry.id, entry, debug: settings.debug }))
          : [
              h(
                'div',
                { key: 'hear-empty', className: 'vhcu-empty' },
                '系统内录（Loopback）',
                h('br'),
                '点击上方「开启听觉」开始监听电脑正在播放的声音；识别结果会实时出现在这里。',
              ),
            ];
      } else if (tab === 'multimodal') {
        rows = filtered.length
          ? filtered.map((entry) => h(MultimodalRow, { key: entry.id, entry }))
          : [
              h(
                'div',
                { key: 'multimodal-empty', className: 'vhcu-empty' },
                '视觉 + 听觉融合时间轴',
                h('br'),
                '同一 1.5 秒窗口内的画面变化与语音转写会合并成一条 M 事件；' +
                  '只有视觉或只有听觉时也会保留单模态。',
              ),
            ];
      } else if (filtered.length === 0) {
        rows = [
          h(
            'div',
            { key: 'empty', className: 'vhcu-empty' },
            tab === 'vision' ? '还没有视觉事件。点击右上角「开启视觉」开始监控。' : '还没有动作事件。模型执行 click/type/宏后会出现在这里。',
          ),
        ];
      } else {
        rows = filtered.map((entry) =>
          entry.kind === 'vision'
            ? h(VisionRow, { key: entry.id, entry, debug: settings.debug })
            : h(ActionRow, { key: entry.id, entry, debug: settings.debug }),
        );
      }

      const fpsLabel = !state.running
        ? '—'
        : (state.counters?.diffs ?? 0) === 0
          ? '-- 等待中'
          : Number(state.fps) > 0
            ? state.fps
            : '--';

      const emergencyStop = useCallback(async () => {
        if (busy) return;
        setBusy(true);
        setError(null);
        try {
          const response = await fetch(`${API}/emergency-stop`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: '{}',
          });
          const payload = await response.json();
          if (!payload.ok) {
            setError(payload.error?.hint || payload.error?.message || `HTTP ${response.status}`);
          } else if (payload.state) {
            setState(payload.state);
          }
        } catch (fetchError) {
          setError(fetchError?.message || 'emergency stop failed');
        } finally {
          setBusy(false);
        }
      }, [busy]);

      const startRecording = useCallback(async () => {
        if (recordingBusy || pendingRecording) return;
        setRecordingBusy(true);
        setError(null);
        try {
          const response = await fetch(`${API}/recording`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action: 'start' }),
          });
          const payload = await response.json();
          if (!payload.ok) {
            setError(payload.error?.hint || payload.error?.message || `HTTP ${response.status}`);
            return;
          }
          if (payload.recording) {
            setState((previous) => ({ ...previous, recording: payload.recording }));
          }
          setPendingRecording(null);
          setMacroForm({ name: '', category: '我的自定义宏' });
          setTab('action');
        } catch (fetchError) {
          setError(fetchError?.message || 'start recording failed');
        } finally {
          setRecordingBusy(false);
        }
      }, [pendingRecording, recordingBusy]);

      const stopRecording = useCallback(async () => {
        if (recordingBusy) return;
        setRecordingBusy(true);
        setError(null);
        try {
          const response = await fetch(`${API}/recording`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({ action: 'stop' }),
          });
          const payload = await response.json();
          if (!payload.ok) {
            setError(payload.error?.hint || payload.error?.message || `HTTP ${response.status}`);
            return;
          }
          if (payload.recording) {
            setState((previous) => ({ ...previous, recording: payload.recording }));
          }
          const steps = Array.isArray(payload.steps) ? payload.steps : [];
          if (!steps.length) {
            setError('录制已停止，但没有捕获到可保存的步骤。');
            return;
          }
          setPendingRecording({
            steps,
            step_count: payload.step_count ?? steps.length,
            event_count: payload.event_count ?? 0,
            duration_ms: payload.duration_ms ?? 0,
            truncated: Boolean(payload.truncated),
          });
          setMacroForm({ name: `macro_recorded_${Date.now()}`, category: '我的自定义宏' });
        } catch (fetchError) {
          setError(fetchError?.message || 'stop recording failed');
        } finally {
          setRecordingBusy(false);
        }
      }, [recordingBusy]);

      const saveRecording = useCallback(async () => {
        if (!pendingRecording || saveBusy) return;
        const name = macroForm.name.trim();
        const category = macroForm.category.trim() || '我的自定义宏';
        if (!name) {
          setError('宏名称不能为空');
          return;
        }
        setSaveBusy(true);
        setError(null);
        try {
          const saved = await fetch(`${API}/macros`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', 'x-vhcu': '1' },
            body: JSON.stringify({
              action: 'save',
              macro: {
                name,
                description: `录制宏（${pendingRecording.step_count} 步）`,
                category,
                steps: pendingRecording.steps,
              },
            }),
          });
          const savedPayload = await saved.json();
          if (!savedPayload.ok) {
            setError(savedPayload.error?.hint || savedPayload.error?.message || `HTTP ${saved.status}`);
            return;
          }
          await fetchMacros();
          setPendingRecording(null);
          setMacroForm({ name: '', category: '我的自定义宏' });
          setError(null);
        } catch (fetchError) {
          setError(fetchError?.message || 'save recording failed');
        } finally {
          setSaveBusy(false);
        }
      }, [fetchMacros, macroForm, pendingRecording, saveBusy]);

      const cancelRecordingSave = useCallback(() => {
        if (saveBusy) return;
        setPendingRecording(null);
        setMacroForm({ name: '', category: '我的自定义宏' });
        setError('已取消保存，本次录制未写入。');
      }, [saveBusy]);

      useEffect(() => {
        if (typeof window === 'undefined') return undefined;
        const onKeyDown = (event) => {
          const key = String(event.key || '').toLowerCase();
          if ((event.ctrlKey || event.metaKey) && event.shiftKey && key === 'x') {
            event.preventDefault();
            if (!event.repeat) void emergencyStop();
          }
        };
        window.addEventListener('keydown', onKeyDown);
        return () => window.removeEventListener('keydown', onKeyDown);
      }, [emergencyStop]);

      const recording = state.recording ?? { active: false, steps: 0, eventCount: 0, truncated: false };
      const recordStrip = h(
        'div',
        {
          className: `vhcu-record-strip${recording.active ? ' active' : ''}`,
          role: 'status',
          'aria-live': 'polite',
        },
        h(
          'button',
          {
            type: 'button',
            className: `vhcu-btn vhcu-record-btn${recording.active ? ' on' : ''}`,
            disabled: recordingBusy || Boolean(pendingRecording),
            onClick: recording.active ? stopRecording : startRecording,
            title: recording.active ? '停止录制并保存为宏' : '开始全局键鼠录制',
            'aria-pressed': recording.active,
          },
          recordingBusy ? '处理中…' : recording.active ? '⏹ 停止并保存' : '🔴 开始录制宏',
        ),
        h(
          'span',
          { className: 'vhcu-record-text' },
          recording.active
            ? `正在录制中，请操作屏幕 · ${recording.steps ?? 0}/100 步${
                recording.truncated ? ' · ⚠ 已到上限，后续动作不再记录' : ''
              }`
            : '录制键鼠为宏（最多 100 步，间隔 >500ms 自动加 wait）',
        ),
      );

      const recordForm = pendingRecording
        ? h(
            'form',
            {
              className: 'vhcu-record-form',
              onSubmit: (event) => {
                event.preventDefault();
                void saveRecording();
              },
            },
            h(
              'div',
              { className: 'vhcu-record-summary' },
              `已捕获 ${pendingRecording.step_count} 步 · ${pendingRecording.event_count} 个输入事件` +
                (pendingRecording.duration_ms
                  ? ` · ${(pendingRecording.duration_ms / 1000).toFixed(1)} 秒`
                  : '') +
                (pendingRecording.truncated || pendingRecording.step_count >= 100
                  ? ' · ⚠ 已达到 100 步上限，后续动作被截断'
                  : ''),
            ),
            h(
              'label',
              { className: 'vhcu-record-field' },
              h('span', null, '宏名称'),
              h('input', {
                className: 'vhcu-input vhcu-record-input',
                value: macroForm.name,
                maxLength: 64,
                autoFocus: true,
                placeholder: '例如 macro_recorded_demo',
                'aria-label': '宏名称',
                onChange: (event) => setMacroForm((previous) => ({ ...previous, name: event.target.value })),
              }),
            ),
            h(
              'label',
              { className: 'vhcu-record-field' },
              h('span', null, '分类'),
              h('input', {
                className: 'vhcu-input vhcu-record-input',
                value: macroForm.category,
                maxLength: 40,
                placeholder: '我的自定义宏',
                'aria-label': '宏分类',
                onChange: (event) =>
                  setMacroForm((previous) => ({ ...previous, category: event.target.value })),
              }),
            ),
            h(
              'div',
              { className: 'vhcu-record-actions' },
              h(
                'button',
                { type: 'submit', className: 'vhcu-btn on', disabled: saveBusy },
                saveBusy ? '保存中…' : '确认保存',
              ),
              h(
                'button',
                { type: 'button', className: 'vhcu-btn', disabled: saveBusy, onClick: cancelRecordingSave },
                '取消',
              ),
            ),
          )
        : null;

      return h(
        'div',
        { className: 'vhcu-root' },
        state.approval?.status === 'pending'
          ? h(
              'div',
              { className: 'vhcu-approval-backdrop' },
              h(
                'div',
                { className: 'vhcu-approval', role: 'dialog', 'aria-modal': 'true' },
                h('div', { className: 'vhcu-approval-title' }, '⚠️ 破坏性操作审批'),
                h(
                  'div',
                  { className: 'vhcu-approval-text' },
                  `AI 请求执行破坏性操作（${state.approval.macro}），是否允许？`,
                ),
                state.approval.description
                  ? h('div', { className: 'vhcu-hint' }, state.approval.description)
                  : null,
                h(
                  'div',
                  { className: 'vhcu-btn-row' },
                  h(
                    'button',
                    {
                      type: 'button',
                      className: 'vhcu-btn vhcu-danger',
                      disabled: approvalBusy,
                      onClick: () => void resolveApproval('allow'),
                    },
                    approvalBusy ? '处理中…' : '允许',
                  ),
                  h(
                    'button',
                    {
                      type: 'button',
                      className: 'vhcu-btn',
                      disabled: approvalBusy,
                      onClick: () => void resolveApproval('deny'),
                    },
                    '拒绝',
                  ),
                ),
              ),
            )
          : null,
        h(
          'div',
          { className: 'vhcu-head' },
          h('span', { className: 'vhcu-dot', style: { color: dot, background: dot } }),
          h('span', { className: 'vhcu-title' }, 'VHCU'),
          h('span', { className: 'vhcu-phase' }, PHASE_TEXT[phase] ?? phase),
          h('span', { className: 'vhcu-spacer' }),
          h(
            'button',
            {
              type: 'button',
              className: `vhcu-btn${state.running ? ' on' : ''}`,
              disabled: busy || phase === 'starting' || phase === 'stopping' || phase === 'recovering',
              onClick: toggle,
              title: state.running ? '停止视觉并退出守护进程' : '按需启动视觉守护',
            },
            busy ? '处理中…' : state.running ? '关闭视觉' : '开启视觉',
          ),
          h(
            'button',
            {
              type: 'button',
              className: 'vhcu-btn vhcu-emergency-btn',
              title: '紧急制动（Ctrl+Shift+X）：停止视觉/听觉并强制结束 Python',
              'aria-label': '紧急制动',
              disabled: busy,
              onClick: emergencyStop,
            },
            '紧急制动',
          ),
          h(
            'button',
            {
              type: 'button',
              className: 'vhcu-icon-btn',
              title: '设置',
              'aria-label': '设置',
              onClick: (event) => {
                event.stopPropagation();
                setGear((value) => !value);
              },
            },
            '⚙',
          ),
        ),
        h(
          'div',
          { className: 'vhcu-meta' },
          h('span', null, '源 ', h('b', null, state.source ?? '—')),
          h('span', null, 'pid ', h('b', null, state.pid ?? '—')),
          h('span', null, '帧 ', h('b', null, state.counters?.frames ?? 0)),
          h('span', null, '变化 ', h('b', null, state.counters?.diffs ?? 0)),
          h('span', null, 'fps ', h('b', null, fpsLabel)),
        ),
        state.privacy?.blocked
          ? h(
              'div',
              { className: 'vhcu-privacy-banner' },
              h(
                'span',
                null,
                settings.privacyUnprotected
                  ? '🔓 当前处于无保护裸奔模式：敏感窗口已放行，AI 可继续操作'
                  : '🔒 隐私保护触发：AI 已暂停，请处理完毕后切换窗口',
              ),
              settings.privacyUnprotected
                ? h('span', { className: 'vhcu-privacy-sub' }, '截图与键鼠均放行（高危）')
                : null,
            )
          : null,
        settings.privacyUnprotected && !state.privacy?.blocked
          ? h(
              'div',
              { className: 'vhcu-unprotected-banner' },
              '🔓 当前处于无保护裸奔模式：敏感窗口将完全放行（高危）',
            )
          : null,
        settings.autoApproveDestructive
          ? h(
              'div',
              { className: 'vhcu-autoapprove-banner' },
              '⚠️ 自动批准破坏性宏已开启，请随时注意 AI 操作。',
            )
          : null,
        state.interaction?.mode === 'paused_by_user'
          ? h('div', { className: 'vhcu-takeover-banner' }, '👀 您正在接管键鼠，AI 已暂停')
          : state.interaction?.notice
            ? h('div', { className: 'vhcu-takeover-notice' }, `👀 ${state.interaction.notice.text}`)
            : null,
        state.lastError?.code === 'E_MEMORY'
          ? h('div', { className: 'vhcu-memory-banner' }, `⚠ ${state.lastError.message}`)
          : null,
        recordStrip,
        recordForm,
        h(
          'div',
          { className: 'vhcu-tabs', role: 'tablist' },
          TABS.map((item) =>
            h(
              'button',
              {
                key: item.key,
                type: 'button',
                role: 'tab',
                'aria-selected': tab === item.key,
                className: `vhcu-tab${tab === item.key ? ' active' : ''}`,
                title: item.hint,
                onClick: () => setTab(item.key),
              },
              h('span', { className: 'vhcu-badge' }, item.badge),
              h('span', null, item.label),
              item.key === 'hear'
                ? h('span', {
                    className: 'vhcu-tab-dot',
                    title: `Hear ${hearState}`,
                    style: { background: hearColor },
                  })
                : null,
              h('span', { className: 'vhcu-count' }, counts[item.key] ?? 0),
            ),
          ),
        ),
        error ? h('div', { className: 'vhcu-note' }, `⚠ ${error}`) : null,
        h('div', { className: 'vhcu-list', role: 'tabpanel', ref: listRef }, tab === 'hear' ? hearBar : null, ...rows),
        h(
          'div',
          { className: 'vhcu-foot' },
          h('span', null, `SSE ${link}`),
          h('span', null, state.lastError ? `上次错误 ${state.lastError.code ?? ''}` : '按需唤醒 · 不空转'),
        ),
        gear
          ? h(
              'div',
              null,
              h('div', { className: 'vhcu-backdrop', onClick: () => setGear(false) }),
              h(SettingsPanel, {
                settings,
                macros,
                categories,
                onSave: saveSettings,
                onToggleMacro: toggleMacro,
                onCreateMacro: createMacro,
                onUpdateMacro: updateMacro,
                onDeleteMacro: deleteMacro,
                onAddCategory: addCategory,
                onDeleteCategory: deleteCategory,
                onClose: () => setGear(false),
              }),
            )
          : null,
      );
    }

    const name = 'dsh-vhcu';
    const inject = ['betterSidebar'];

    function apply(ctx) {
      const sidebar = ctx.betterSidebar;
      if (!sidebar || typeof sidebar.registerTab !== 'function') {
        return;
      }
      const register = () =>
        sidebar.registerTab({
          id: 'vhcu:monitor',
          title: 'VHCU',
          description: '视觉/动作/听觉三栏监控、设置与实时事件流',
          order: 120,
          single: true,
          component: (props) => h(Panel, props),
        });
      if (typeof ctx.effect === 'function') ctx.effect(register, 'vhcu: sidebar tab');
      else register();
    }

    module.exports = { name, inject, apply };
    return module.exports;
  },
});
