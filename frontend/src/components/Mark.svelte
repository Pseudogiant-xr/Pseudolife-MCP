<script lang="ts">
  // The Pseudolife mark: one silhouette, one gyrus, one die, two tendrils.
  let { size = 26 }: { size?: number } = $props();
  const uid = $props.id();
  const grad = `mk-${uid}`;
</script>

<svg width={size} height={Math.round((size * 24) / 26)} viewBox="0 0 32 28" fill="none" aria-hidden="true" focusable="false">
  <path
    d="M4 14C4 8 8 4 14 3.5c5-.5 10 .5 13 3.5 3 3 3.5 8 1 11.5-1.5 2-3.5 3-5.5 3 0 2-1.5 3.5-3.5 3-1 0-1.7-.8-1.8-1.7l-.4 3.9h-2l.2-4.1c-3 .1-6-.9-8-2.4-2-1.5-3-3.5-3-6Z"
    fill="var(--ground)"
    stroke="url(#{grad})"
    stroke-width="1.5"
    stroke-linejoin="round"
  />
  <path d="M15.5 5c.5 5-.5 10 0 15" stroke="#A78BFA" stroke-width="1" stroke-linecap="round" opacity=".7" />
  <path d="M7 12c2-3 5-2 5 1 0 2-2 3-3.5 2M9 18c2-1 4 0 4.5 2M11 6.5c2 .5 2 3.5 2.5 5" stroke="#C084FC" stroke-width="1.3" stroke-linecap="round" />
  <rect x="19" y="10" width="6" height="6" rx="1" stroke="#818CF8" stroke-width="1.3" />
  <path d="M25 12h2.5V9M25 15h3M22 10V7h2M22 16v3.5h-3" stroke="#818CF8" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
  <path d="M19 12h-3V9.5M19 15h-4.5v2" stroke="#A78BFA" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round" />
  <circle cx="16" cy="9.5" r="1" fill="#C084FC" />
  <circle cx="14.5" cy="17" r="1" fill="#C084FC" />
  <circle cx="27.5" cy="9" r="1" fill="#818CF8" />
  <defs>
    <linearGradient id={grad} x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#D8B4FE" />
      <stop offset="1" stop-color="#818CF8" />
    </linearGradient>
  </defs>
</svg>
