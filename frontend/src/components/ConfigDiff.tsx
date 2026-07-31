interface Props {
  diffText: string | null | undefined;
}

export default function ConfigDiff({ diffText }: Props) {
  if (!diffText) {
    return <p className="text-sm text-slate-400 italic">No diff available.</p>;
  }

  const lines = diffText.split("\n");

  return (
    <pre className="bg-slate-900 text-xs rounded-lg p-4 overflow-x-auto leading-relaxed">
      {lines.map((line, i) => {
        let cls = "text-slate-300";
        if (line.startsWith("+") && !line.startsWith("+++")) cls = "text-risklow bg-green-950/40 block";
        else if (line.startsWith("-") && !line.startsWith("---")) cls = "text-riskcrit bg-red-950/40 block";
        else if (line.startsWith("@@")) cls = "text-accent block";
        return (
          <span key={i} className={cls}>
            {line || " "}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
