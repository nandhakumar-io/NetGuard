interface Props {
  label: string;
  value: number | string;
  accent?: "blue" | "green" | "red" | "amber";
}

const accentMap: Record<string, string> = {
  blue: "text-brandblue",
  green: "text-risklow",
  red: "text-riskcrit",
  amber: "text-riskmed",
};

export default function StatCard({ label, value, accent = "blue" }: Props) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-5 shadow-sm">
      <p className="text-xs font-medium text-slate-500 uppercase tracking-wide">{label}</p>
      <p className={`text-3xl font-bold mt-2 ${accentMap[accent]}`}>{value}</p>
    </div>
  );
}
