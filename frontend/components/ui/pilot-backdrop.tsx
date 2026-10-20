type PilotBackdropProps = { variant: "navigation" | "trajectory" | "heading" };

const backdropClassNames = {
  navigation: "pilot-backdrop-navigation",
  trajectory: "pilot-backdrop-trajectory",
  heading: "pilot-backdrop-heading"
};

/** Purely decorative flight charts; never represent live task or system state. */
export function PilotBackdrop({ variant }: PilotBackdropProps) {
  return (
    <div className={`pilot-backdrop ${backdropClassNames[variant]}`} aria-hidden="true">
      {variant === "navigation" ? (
        <svg viewBox="0 0 300 320" fill="none">
          <g stroke="currentColor" strokeWidth="1">
            <circle cx="150" cy="144" r="104" strokeOpacity=".6" />
            <circle cx="150" cy="144" r="88" strokeDasharray="1 8" />
            <path d="M150 25v22m0 194v22M31 144h22m194 0h22" />
            <path d="m70 64 9 9m142 142 9 9M70 224l9-9M221 73l9-9" strokeOpacity=".6" />
            <path d="M150 56v12m0 152v12m-88-88h12m152 0h12" />
            <path d="m145 39 5-8 5 8m-10 211 5 8 5-8M45 139l-8 5 8 5m210-10 8 5-8 5" />
            <path d="M116 147v-22a34 34 0 0 1 68 0v22M115 168v-27m70 0v27" strokeWidth="1.5" />
            <path d="M120 131c18-11 42-11 60 0v22c-16 11-44 11-60 0Z" strokeWidth="1.5" />
            <path d="m132 136 18-4m-24 32 9 18h30l9-18m-39 18-10 14h50l-10-14M112 139h-6v24h6m76-24h6v24h-6" />
            <path d="M190 163v8h-22m-3-3h-8v6h8" />
            <path d="M8 221c47 28 84-9 118-45m49-78c41-41 79-44 122-27" strokeDasharray="4 6" strokeOpacity=".7" />
            <path d="M20 102V84h18m224 120h18v-18" strokeOpacity=".6" />
          </g>
          <g fill="currentColor">
            <circle cx="48" cy="171" r="3" />
            <circle cx="243" cy="114" r="3" />
            <path d="m212 67 8-2-3 8Z" />
            <circle cx="150" cy="40" r="1.5" />
          </g>
          <g fill="currentColor" fontFamily="monospace" fontSize="8" letterSpacing="2" textAnchor="middle">
            <text x="150" y="16">N</text>
            <text x="150" y="285">REPOPILOT AI</text>
            <text x="150" y="302" fontSize="6" letterSpacing="3">CHART THE NEXT BRANCH</text>
          </g>
        </svg>
      ) : variant === "trajectory" ? (
        <svg viewBox="0 0 360 600" fill="none">
          <g stroke="currentColor" strokeWidth="1">
            <circle cx="264" cy="390" r="126" strokeOpacity=".45" />
            <circle cx="264" cy="390" r="104" strokeDasharray="2 10" />
            <circle cx="264" cy="390" r="70" strokeOpacity=".5" />
            <path d="M264 246v31m0 226v31M120 390h31m226 0h31" strokeOpacity=".65" />
            <path d="M336 44C198 96 341 224 249 300S106 464 201 568" strokeDasharray="5 8" />
            <path d="m239 411 25-67 25 67-25-14Z" strokeWidth="1.5" />
            <path d="m264 344 0 53m-14 31 14 19 14-19M192 99h20m-10-10v20M122 275h14m-7-7v14M201 547h18m-9-9v18" />
            <circle cx="269" cy="174" r="5" />
            <circle cx="249" cy="300" r="4" />
            <circle cx="161" cy="454" r="5" />
            <path d="m298 263 6 6m-130 235 6 6m-17-207 6-6" strokeOpacity=".6" />
          </g>
          <g fill="currentColor">
            <circle cx="269" cy="174" r="1.5" />
            <circle cx="249" cy="300" r="1.5" />
            <circle cx="161" cy="454" r="1.5" />
          </g>
        </svg>
      ) : (
        <svg viewBox="0 0 180 84" fill="none">
          <g stroke="currentColor" strokeWidth="1">
            <circle cx="130" cy="42" r="34" strokeOpacity=".55" />
            <circle cx="130" cy="42" r="25" strokeDasharray="1 5" />
            <path d="M130 1v10m0 62v10M89 42h10m62 0h10m-41-56v10" />
            <path d="m120 54 10-30 10 30-10-6Zm10-30v24" />
            <path d="M3 65h28c25 0 22-46 47-46h20" strokeDasharray="3 5" />
            <circle cx="31" cy="65" r="3" />
            <circle cx="78" cy="19" r="3" />
          </g>
        </svg>
      )}
    </div>
  );
}
