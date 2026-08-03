import { useMemo, useState } from "react";

interface ConfigViewerProps {
  title: string;
  config: string | null | undefined;
  configPretty?: string | null;
  isXml?: boolean;
  emptyText: string;
}

/** Renders a device config blob. NETCONF devices return raw, unindented
 * XML (Cisco IOS-XE native/YANG models nest 6-8 levels deep) -- readable
 * to a parser, not a person. When the backend detected XML and could
 * pretty-print it (`configPretty`), default to that formatted view with a
 * toggle back to the untouched raw text (e.g. to copy exactly what the
 * device returned, or to compare byte-for-byte against a diff/backup).
 * Plain CLI-style config (SSH/NAPALM-sourced) has no `configPretty` and
 * just renders as-is -- no toggle shown. */
export default function ConfigViewer({ title, config, configPretty, isXml, emptyText }: ConfigViewerProps) {
  const hasFormatted = isXml && !!configPretty;
  const [mode, setMode] = useState<"formatted" | "raw">(hasFormatted ? "formatted" : "raw");
  const [copied, setCopied] = useState(false);

  const displayed = useMemo(() => {
    if (!config) return null;
    return mode === "formatted" && configPretty ? configPretty : config;
  }, [config, configPretty, mode]);

  const lineCount = displayed ? displayed.split("\n").length : 0;

  const copy = async () => {
    if (!displayed) return;
    try {
      await navigator.clipboard.writeText(displayed);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // Clipboard API unavailable (e.g. insecure context) -- fail silently,
      // the text is still fully selectable/copyable by hand.
    }
  };

  return (
    <div>
      <div className="flex items-center justify-between gap-2 mb-2">
        <h4 className="text-xs font-bold uppercase text-slate-500 dark:text-slate-400">{title}</h4>
        {config && (
          <div className="flex items-center gap-2">
            {hasFormatted && (
              <div className="flex text-[10px] font-bold uppercase rounded-md overflow-hidden border border-slate-300 dark:border-slate-600">
                <button
                  onClick={() => setMode("formatted")}
                  className={`px-2 py-1 ${
                    mode === "formatted"
                      ? "bg-brandblue text-white"
                      : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400"
                  }`}
                >
                  Formatted
                </button>
                <button
                  onClick={() => setMode("raw")}
                  className={`px-2 py-1 ${
                    mode === "raw"
                      ? "bg-brandblue text-white"
                      : "bg-white dark:bg-slate-800 text-slate-500 dark:text-slate-400"
                  }`}
                >
                  Raw XML
                </button>
              </div>
            )}
            <button
              onClick={copy}
              className="text-[10px] font-bold uppercase text-slate-500 dark:text-slate-400 hover:text-brandblue border border-slate-300 dark:border-slate-600 rounded-md px-2 py-1"
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
        )}
      </div>
      {displayed ? (
        <>
          <pre className="bg-slate-900 border border-slate-700 text-slate-300 text-[11px] rounded-lg p-3 overflow-x-auto max-h-[420px] whitespace-pre-wrap leading-relaxed shadow-inner">
            {displayed}
          </pre>
          <p className="text-[10px] text-slate-400 dark:text-slate-500 mt-1">
            {lineCount} line{lineCount === 1 ? "" : "s"}
            {hasFormatted && mode === "formatted" ? " (re-indented for readability — toggle Raw XML for the exact device output)" : ""}
          </p>
        </>
      ) : (
        <p className="text-xs text-slate-400 dark:text-slate-500 italic mt-4">{emptyText}</p>
      )}
    </div>
  );
}