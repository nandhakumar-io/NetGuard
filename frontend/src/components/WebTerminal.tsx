import React, { useEffect, useRef, useState } from 'react';
import { Terminal } from 'xterm';
import { FitAddon } from 'xterm-addon-fit';
import 'xterm/css/xterm.css';
import { getAccessToken } from '../lib/api';

interface WebTerminalProps {
  deviceId: string;
}

export const WebTerminal: React.FC<WebTerminalProps> = ({ deviceId }) => {
  const terminalRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!terminalRef.current) return;

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
    
    const wsUrl = `${protocol}//${wsHost}/api/v1/devices/${deviceId}/terminal?token=${token}`;
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
    };
  }, [deviceId]);

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