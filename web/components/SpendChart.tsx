"use client";
import { useState } from "react";

type Day = { day: string; cost: number; calls: number };

/** Daily spend, single series (title names it, so no legend). Hover any column for the exact value. */
export function SpendChart({ days }: { days: Day[] }) {
  const [hover, setHover] = useState<number | null>(null);
  const W = 640, H = 160, pad = { t: 12, r: 8, b: 22, l: 44 };
  const max = Math.max(0.0001, ...days.map((d) => d.cost));
  const top = niceCeil(max);
  const bw = (W - pad.l - pad.r) / days.length;
  const y = (v: number) => pad.t + (H - pad.t - pad.b) * (1 - v / top);
  const fmt = (v: number) => (v < 0.01 ? `$${v.toFixed(3)}` : `$${v.toFixed(2)}`);

  return (
    <div className="relative">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Daily LLM spend, last 30 days">
        {[0, top / 2, top].map((v) => (
          <g key={v}>
            <line x1={pad.l} x2={W - pad.r} y1={y(v)} y2={y(v)} stroke="var(--line)" strokeWidth={1} />
            <text x={pad.l - 6} y={y(v) + 4} textAnchor="end" fontSize={11} fill="var(--muted)">{fmt(v)}</text>
          </g>
        ))}
        {days.map((d, i) => {
          const x = pad.l + i * bw + 1;
          const h = Math.max(0, y(0) - y(d.cost));
          const w = Math.max(1, bw - 2);
          const r = Math.min(4, w / 2, h);
          return (
            <g key={d.day} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}>
              <rect x={pad.l + i * bw} y={pad.t} width={bw} height={H - pad.t - pad.b} fill="transparent" />
              {h > 0 && (
                <path
                  d={`M${x},${y(0)} v${-(h - r)} q0,${-r} ${r},${-r} h${w - 2 * r} q${r},0 ${r},${r} v${h - r} z`}
                  fill="var(--ink)" opacity={hover === null || hover === i ? 1 : 0.45}
                />
              )}
            </g>
          );
        })}
        {[0, Math.floor(days.length / 2), days.length - 1].map((i) => days[i] && (
          <text key={i} x={pad.l + i * bw + bw / 2} y={H - 6} textAnchor="middle" fontSize={11} fill="var(--muted)">
            {days[i].day.slice(5)}
          </text>
        ))}
      </svg>
      {hover !== null && days[hover] && (
        <div className="pointer-events-none absolute top-0 rounded-md border border-line bg-card px-2 py-1 text-xs shadow-sm"
          style={{ left: `${((pad.l + hover * bw + bw / 2) / W) * 100}%`, transform: "translateX(-50%)" }}>
          <div className="text-ink-2">{days[hover].day}</div>
          <div className="font-semibold num">{fmt(days[hover].cost)} · {days[hover].calls} calls</div>
        </div>
      )}
    </div>
  );
}

function niceCeil(v: number) {
  const exp = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * exp >= v) return m * exp;
  return 10 * exp;
}
