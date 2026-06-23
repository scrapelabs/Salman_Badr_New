import { useId } from "react";

interface LogoMarkProps {
  height?: number;
  className?: string;
}

export default function LogoMark({ height = 38, className }: LogoMarkProps) {
  const raw = useId();
  const p = raw.replace(/[:]/g, "");
  const gBall = `${p}-gBall`;
  const sh = `${p}-sh`;

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      height={height}
      viewBox="0 0 512 512"
      fill="none"
      className={className}
      style={{ width: "auto", flexShrink: 0 }}
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient
          id={gBall}
          x1="110"
          y1="90"
          x2="402"
          y2="420"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0" stopColor="#4DB8FF" />
          <stop offset=".5" stopColor="#2B8CFF" />
          <stop offset="1" stopColor="#5FD957" />
        </linearGradient>
        <filter id={sh} x="-20%" y="-20%" width="140%" height="160%">
          <feDropShadow
            dx="0"
            dy="12"
            stdDeviation="14"
            floodColor="#16306b"
            floodOpacity="0.18"
          />
        </filter>
      </defs>
      <g filter={`url(#${sh})`}>
        <circle cx="256" cy="252" r="196" fill={`url(#${gBall})`} />
      </g>
      <ellipse cx="194" cy="166" rx="84" ry="52" fill="#ffffff" opacity=".14" />
      <g stroke="#ffffff" strokeWidth="26" strokeLinecap="round" fill="none">
        <path d="M86 154 C236 180 236 324 86 350" />
        <path d="M426 154 C276 180 276 324 426 350" />
      </g>
    </svg>
  );
}
