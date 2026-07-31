interface Props {
  score: number;
}

export default function RiskBadge({ score }: Props) {
  let color = "bg-risklow";
  let label = "Low Risk";
  if (score > 70) {
    color = "bg-riskcrit";
    label = "Critical Risk";
  } else if (score > 30) {
    color = "bg-riskmed";
    label = "Medium Risk";
  }

  return (
    <div className="inline-flex items-center gap-2">
      <span className={`px-2.5 py-1 rounded-full text-white text-xs font-semibold ${color}`}>
        {score} / 100
      </span>
      <span className="text-xs text-slate-500">{label}</span>
    </div>
  );
}
