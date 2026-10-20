export function BrandMark({ className = "h-5 w-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 24 24" fill="none" className={className} aria-hidden="true">
      <path d="M20.5 11.5a8.5 8.5 0 0 1-13 7.2M3.5 12.5a8.5 8.5 0 0 1 13-7.2" stroke="currentColor" strokeOpacity=".35" strokeWidth=".8" strokeLinecap="round" />
      <path d="M6 18V6m0 12 12-12M6 12h6a6 6 0 0 0 6-6" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="6" cy="6" r="2.5" fill="currentColor" />
      <circle cx="6" cy="18" r="2.5" fill="currentColor" />
      <circle cx="18" cy="6" r="2.5" fill="currentColor" />
    </svg>
  );
}
