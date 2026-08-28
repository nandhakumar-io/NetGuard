import React, { useEffect, useRef, useState } from 'react';
import { Terminal } from 'xterm';
import { FitAddon } from 'xterm-addon-fit';
import 'xterm/css/xterm.css';
import { api, getAccessToken } from '../lib/api';
import { useAuth } from '../lib/auth';

interface WebTerminalProps {
  deviceId: string;
  // Whether this terminal's tab is the currently visible one. Only
  // matters when several WebTerminals stay mounted at once (see
  // TerminalWindowManager) with inactive ones hidden via display:none --
  // xterm's FitAddon can't measure a zero-size hidden container, so a
  // terminal that was resized (window resize, or the floating window's
  // own drag-resize handle) while its tab was in the background comes
  // back undersized/misaligned until re-fit. Standalone callers (a single
  // WebTerminal, always visible) can ignore this -- it defaults to true.
  active?: boolean;
}

export const WebTerminal: React.FC<WebTerminalProps> = ({ deviceId, active = true }) => {
  const terminalRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);
  const { user } = useAuth();
  const fitAddonRef = useRef<FitAddon | null>(null);

  // Security PIN step-up (see backend app.core.deps.check_pin_step_up_ws).
  // Only relevant when the current user has both set a PIN and turned on
  // enforcement (user.pin_required) -- everyone else skips straight past
  // this and the terminal connects the same way it always did.
  const [pinToken, setPinToken] = useState<string | null>(null);
  const [pinInput, setPinInput] = useState('');
  const [pinBusy, setPinBusy] = useState(false);
  const [pinError, setPinError] = useState<string | null>(null);
  const needsPinPrompt = !!user?.pin_required && !pinToken;

  const verifyPin = async (e: React.FormEvent) => {
    e.preventDefault();
    setPinError(null);
    setPinBusy(true);
    try {
      const res = await api.post('/auth/security-pin/verify', { pin: pinInput });
      setPinToken(res.data.pin_token);
      setPinInput('');
    } catch (err: any) {
      setPinError(err?.response?.data?.detail || 'Incorrect PIN.');
    } finally {
      setPinBusy(false);
    }
  };

  useEffect(() => {
    if (!terminalRef.current) return;
    // Wait for PIN step-up before opening the socket at all when it's
    // required -- avoids a doomed connection attempt (and the resulting
    // confusing 1008 close) when we already know it'll be rejected.
    if (needsPinPrompt) return;

    const term = new Terminal({
      cursorBlink: true,
      fontFamily: 'Menlo, Monaco, "Courier New", monospace',
      fontSize: 14,
      theme: {
        background: '#1e1e2e',
        foreground: '#cdd6f4',
        cursor: '#f38ba8',
      },
    });

    const fitAddon = new FitAddon();
    fitAddonRef.current = fitAddon;
    term.loadAddon(fitAddon);
    term.open(terminalRef.current);
    
    // Small delay to ensure container is fully rendered before fitting
    setTimeout(() => fitAddon.fit(), 50);

    // Derive the WS host from the same VITE_API_BASE_URL the rest of the
    // app's API client (lib/api.ts) uses -- this used to read a
    // VITE_API_URL var that was never actually set anywhere (.env only
    // defines VITE_API_BASE_URL), so it silently fell through to a
    // hardcoded ":8000" guess on window.location.hostname every time,
    // which only happened to work for the default local-dev setup.
    let wsHost = `${window.location.hostname}:8000`;
    if (import.meta.env.VITE_API_BASE_URL) {
      try {
          wsHost = new URL(import.meta.env.VITE_API_BASE_URL).host;
      } catch {
          // Malformed VITE_API_BASE_URL -- fall back to the wsHost default set above.
      }
    }
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const token = getAccessToken() || '';

    const pinParam = pinToken ? `&pin_token=${encodeURIComponent(pinToken)}` : '';
    const wsUrl = `${protocol}//${wsHost}/api/v1/devices/${deviceId}/terminal?token=${token}${pinParam}`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      term.writeln('\x1b[32m*** Establishing secure terminal bridge to NetGuard API ***\x1b[0m');
    };

    ws.onmessage = (event) => {
      term.write(event.data);
    };

    // Browsers fire 'error' for practically any abnormal socket condition
    // with zero detail (no code, no reason) -- it's always followed by a
    // 'close' event, which DOES carry the reason the backend sent. So we
    // no longer render a banner from onerror itself (it was showing a
    // generic "Connection dropped by server" even for perfectly normal,
    // server-explained disconnects); we just log it and let onclose,
    // which fires right after, report what actually happened.
    ws.onerror = (e) => {
      console.error('WebSocket Error:', e);
    };

    ws.onclose = (event) => {
      if (event.reason === 'Security PIN verification required') {
        // Step-up token was rejected or expired between verifying and
        // connecting (e.g. it timed out) -- clear it so needsPinPrompt
        // flips back on and the user gets the prompt again instead of a
        // silent dead terminal.
        setPinToken(null);
        setPinError('PIN verification expired -- enter your PIN again.');
        return;
      }
      if (event.wasClean && event.reason) {
        // Backend already sent a human-readable reason as terminal output
        // right before closing (see terminal.py's _run_pumped_session) --
        // just note the session ended, don't duplicate the reason text.
        term.writeln('\r\n\x1b[33m*** Session Closed ***\x1b[0m');
      } else if (!event.wasClean) {
        const detail = event.reason ? `: ${event.reason}` : ` (code ${event.code})`;
        setError(`Connection dropped by server${detail}.`);
        term.writeln(`\r\n\x1b[31m*** Backend connection dropped${detail} ***\x1b[0m`);
      } else {
        term.writeln('\r\n\x1b[33m*** Session Closed ***\x1b[0m');
      }
    };

    // When a user types in xterm, immediately cast those literal byte chunks over WS
    term.onData((data) => {
      if (ws.readyState === WebSocket.OPEN) {
        ws.send(data);
      }
    });

    const handleResize = () => fitAddon.fit();
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      ws.close();
      term.dispose();
      fitAddonRef.current = null;
    };
  }, [deviceId, needsPinPrompt, pinToken]);

  // Re-fit whenever this tab becomes the visible one -- a hidden
  // (display:none) container reports zero size, so any resize that
  // happened while this tab was in the background needs to be re-applied
  // once it's actually on screen again.
  useEffect(() => {
    if (!active) return;
    const id = requestAnimationFrame(() => fitAddonRef.current?.fit());
    return () => cancelAnimationFrame(id);
  }, [active]);

  if (needsPinPrompt) {
    return (
      <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
        <form onSubmit={verifyPin} className="bg-slate-800 rounded-xl p-6 w-full max-w-xs space-y-3">
          <p className="text-sm text-slate-200 font-semibold">Enter your Security PIN to open a terminal.</p>
          {pinError && <p className="text-xs text-red-400">{pinError}</p>}
          <input
            autoFocus
            type="password"
            inputMode="numeric"
            maxLength={8}
            value={pinInput}
            onChange={(e) => setPinInput(e.target.value.replace(/\D/g, ''))}
            className="w-full px-3 py-2 rounded-lg bg-slate-900 border border-slate-600 text-slate-100 text-sm tracking-widest text-center"
            placeholder="PIN"
            required
          />
          <button
            type="submit"
            disabled={pinBusy || pinInput.length < 4}
            className="w-full bg-brandblue text-white rounded-lg px-4 py-2 text-sm font-semibold hover:opacity-90 disabled:opacity-50"
          >
            {pinBusy ? 'Verifying…' : 'Verify & Continue'}
          </button>
        </form>
      </div>
    );
  }

  return (
    <div style={{ width: '100%', height: '100%', display: 'flex', flexDirection: 'column' }}>
      {error && <div className="text-red-500 mb-2 p-2 bg-red-900/20 rounded">{error}</div>}
      <div 
        ref={terminalRef} 
        style={{ flexGrow: 1, minHeight: '400px', backgroundColor: '#1e1e2e', padding: '10px', borderRadius: '4px' }} 
      />
    </div>
  );
};