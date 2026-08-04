interface Props {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
}

/** Minimal dependency-free trend line for widgets like Top CPU/Memory --
 * plots the last-hour history next to the current value so a device that
 * just spiked to 90% reads differently than one that's been steady at
 * 90% all hour. Renders nothing (caller should fall back to just the
 * current value) when there's fewer than 2 points to draw a line
 * between. */
export default function Sparkline({ values, width = 72, height = 24, color = "#2563eb" }: Props) {
  if (!values || values.length < 2) return null;

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const step = width / (values.length - 1);

  const points = values
    .map((v, i) => {
      const x = i * step;
      const y = height - ((v - min) / range) * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");

  const lastX = (values.length - 1) * step;
  const lastY = height - ((values[values.length - 1] - min) / range) * height;

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} className="shrink-0 overflow-visible">
      <polyline points={points} fill="none" stroke={color} strokeWidth={1.5} strokeLinejoin="round" strokeLinecap="round" />
      <circle cx={lastX} cy={lastY} r={1.75} fill={color} />
    </svg>
  );
}