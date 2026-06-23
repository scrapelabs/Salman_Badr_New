import { useId } from "react";

interface LogoMarkProps {
  height?: number;
  className?: string;
}

export default function LogoMark({ height = 38, className }: LogoMarkProps) {
  const raw = useId();
  const p = raw.replace(/[:]/g, "");
  const gBG = `${p}-gBG`;
  const gDS = `${p}-gDS`;
  const gN = `${p}-gN`;
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
          id={gBG}
          x1="128"
          y1="90"
          x2="350"
          y2="360"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0" stopColor="#4DB8FF" />
          <stop offset=".55" stopColor="#2B8CFF" />
          <stop offset="1" stopColor="#63D95C" />
        </linearGradient>
        <linearGradient
          id={gDS}
          x1="40"
          y1="160"
          x2="230"
          y2="360"
          gradientUnits="userSpaceOnUse"
        >
          <stop offset="0" stopColor="#27B8D8" />
          <stop offset="1" stopColor="#77E34E" />
        </linearGradient>
        <linearGradient id={gN} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#1F347E" />
          <stop offset="1" stopColor="#162A6A" />
        </linearGradient>
        <filter id={sh} x="-20%" y="-20%" width="140%" height="160%">
          <feDropShadow
            dx="0"
            dy="12"
            stdDeviation="12"
            floodColor="#16306b"
            floodOpacity="0.14"
          />
        </filter>
      </defs>
      <g transform="translate(44,58)" filter={`url(#${sh})`}>
        <rect
          x="0"
          y="82"
          width="172"
          height="232"
          rx="26"
          fill={`url(#${gDS})`}
          opacity=".96"
        />
        <path
          d="M52 24H238C252.359 24 264 35.641 264 50V250L204 314H52C37.641 314 26 302.359 26 288V50C26 35.641 37.641 24 52 24Z"
          fill="white"
        />
        <path
          d="M52 24H238C252.359 24 264 35.641 264 50V250L204 314H52C37.641 314 26 302.359 26 288V50C26 35.641 37.641 24 52 24Z"
          stroke="#BFD2F7"
          strokeWidth="12"
        />
        <path
          d="M264 250L204 314L148 314L214 242C226 229 245 236 264 250Z"
          fill={`url(#${gN})`}
        />
        <path
          d="M84 106L110 132L155 85"
          stroke="#39C38B"
          strokeWidth="17"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <rect x="86" y="170" width="76" height="12" rx="6" fill="#CFD9F2" />
        <rect x="86" y="208" width="92" height="12" rx="6" fill="#CFD9F2" />
        <rect x="86" y="246" width="70" height="12" rx="6" fill="#CFD9F2" />
        <g transform="translate(112,48)">
          <path
            d="M148 8C82 8 28 61 28 127C28 212 128 298 140 313C144 319 153 319 157 313C169 298 268 215 268 127C268 61 214 8 148 8Z"
            fill={`url(#${gBG})`}
          />
          <circle cx="149" cy="125" r="54" fill="white" />
          <path
            d="M148 8C82 8 28 61 28 127C28 212 128 298 140 313C144 319 153 319 157 313C168 300 235 229 258 158C225 142 194 134 161 135C132 136 103 146 77 165C84 201 111 240 148 276C183 234 216 198 241 157C225 118 192 89 152 82C150 57 149 33 148 8Z"
            fill="#1B77E8"
            fillOpacity=".35"
          />
          <path
            d="M259 153C234 133 202 123 166 125C138 127 109 136 82 154C95 211 146 259 184 293C208 269 247 223 265 169C262 163 261 158 259 153Z"
            fill="#9EE75A"
            fillOpacity=".55"
          />
        </g>
      </g>
    </svg>
  );
}
