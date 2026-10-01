<script lang="ts">
  // The Observatory hero: one brain in profile, gyri on the left, a circuit
  // board on the right. Once per product (design sheet). Captions are live
  // counts; a missing count drops its caption rather than inventing one.
  import { fmtNum } from "../lib/format";

  let { entries, facts }: { entries?: number; facts?: number } = $props();
  const uid = $props.id();
  const ids = { meld: `meld-${uid}`, ga: `ga-${uid}`, gb: `gb-${uid}`, brain: `brain-${uid}` };
</script>

<svg
  class="hero-art"
  viewBox="0 0 400 300"
  width="400"
  height="300"
  fill="none"
  role="img"
  aria-label="Half brain, half chip: circuitry melding into the organic hemisphere"
>
    <defs>
    <linearGradient id="{ids.meld}" x1="0" y1="0" x2="1" y2="0"><stop offset="0" stop-color="#D8B4FE"/><stop offset=".48" stop-color="#A78BFA"/><stop offset=".52" stop-color="#A5B4FC"/><stop offset="1" stop-color="#818CF8"/></linearGradient>
    <radialGradient id="{ids.ga}" cx="30%" cy="45%" r="40%"><stop offset="0" stop-color="#A855F7" stop-opacity=".38"/><stop offset="1" stop-color="#A855F7" stop-opacity="0"/></radialGradient>
    <radialGradient id="{ids.gb}" cx="70%" cy="48%" r="38%"><stop offset="0" stop-color="#6366F1" stop-opacity=".34"/><stop offset="1" stop-color="#6366F1" stop-opacity="0"/></radialGradient>
    <clipPath id="{ids.brain}"><path d="M58 150c-2-38 30-84 84-100 42-14 98-14 148 4 40 14 66 56 60 106-4 28-22 48-48 54 2 22-18 44-48 38-16-4-26-14-28-24l-4 42h-22l4-44c-28 2-60-6-84-20-34-10-58-30-62-56Z"/></clipPath>
    </defs>
    <rect width="400" height="300" fill="url(#{ids.ga})"/><rect width="400" height="300" fill="url(#{ids.gb})"/>
    <g clip-path="url(#{ids.brain})">
    <rect x="0" y="0" width="204" height="300" fill="#2E1065" opacity=".28"/>
    <rect x="204" y="0" width="200" height="300" fill="#1E1B4B" opacity=".38"/>
    <path d="M204 0v300" stroke="#A78BFA" stroke-width="1" opacity=".35"/>
    </g>
    <path d="M58 150c-2-38 30-84 84-100 42-14 98-14 148 4 40 14 66 56 60 106-4 28-22 48-48 54 2 22-18 44-48 38-16-4-26-14-28-24l-4 42h-22l4-44c-28 2-60-6-84-20-34-10-58-30-62-56Z" stroke="url(#{ids.meld})" stroke-width="3" stroke-linejoin="round"/>
    <path d="M200 64c8 30-2 60 6 92-6 30 4 54 0 72" stroke="#A78BFA" stroke-width="1.6" stroke-linecap="round" opacity=".6"/>
    <path d="M84 120c16-24 44-20 50 4 6 22-14 36-30 26M70 160c22-10 44 6 48 30M110 70c20-4 40 14 36 38-4 22 14 34 34 26M150 60c20-4 36 10 34 32-2 20 12 28 16 12M126 178c20-12 42-2 50 18M96 190c16 6 44 16 66 22M160 148c14 8 16 28 30 34M92 94c10-12 24-10 30 2M138 122c10 6 14 22 6 32M118 150c6 10 2 24-8 28" stroke="url(#{ids.meld})" stroke-width="2.2" stroke-linecap="round" opacity=".9"/>
    <path d="M176 116c10-4 18-14 20-26M172 176c8 4 18 2 26-8" stroke="#C084FC" stroke-width="1.6" stroke-linecap="round" opacity=".7"/>
    <circle cx="104" cy="150" r="3" fill="#D8B4FE"/><circle cx="118" cy="190" r="3" fill="#D8B4FE"/><circle cx="180" cy="134" r="3" fill="#D8B4FE"/><circle cx="200" cy="104" r="2.6" fill="#D8B4FE"/><circle cx="176" cy="196" r="2.6" fill="#D8B4FE"/><circle cx="162" cy="212" r="2.4" fill="#D8B4FE"/><circle cx="122" cy="96" r="2.4" fill="#D8B4FE"/><circle cx="84" cy="120" r="2.4" fill="#D8B4FE"/><circle cx="110" cy="178" r="2.2" fill="#D8B4FE"/>
    <rect x="246" y="112" width="60" height="60" rx="8" fill="#000" stroke="#818CF8" stroke-width="2.4"/>
    <rect x="260" y="126" width="32" height="32" rx="5" fill="#818CF8" opacity=".2"/>
    <rect x="270" y="136" width="12" height="12" rx="2" fill="#C4B5FD"/>
    <path d="M254 112v-6M266 112v-6M278 112v-6M290 112v-6M298 112v-6M254 172v6M266 172v6M278 172v6M290 172v6M298 172v6M306 120h6M306 132h6M306 144h6M306 156h6M306 164h6M246 120h-6M246 132h-6M246 144h-6M246 156h-6M246 164h-6" stroke="#818CF8" stroke-width="1.8" stroke-linecap="round"/>
    <path d="M312 128h18v-32h14M312 156h30v26M278 106v-24h20v-16M266 106v-14h-20v-22M278 178v28h-26M290 178v40h14M312 144h36M330 96v-22M342 198h-12v18M322 66h14v18M240 84h-14v-18" stroke="#818CF8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <rect x="327" y="93" width="6" height="6" rx="1" fill="#818CF8"/><rect x="339" y="179" width="6" height="6" rx="1" fill="#818CF8"/><rect x="295" y="63" width="6" height="6" rx="1" fill="#818CF8"/><rect x="243" y="67" width="6" height="6" rx="1" fill="#818CF8"/>
    <circle cx="344" cy="96" r="3" fill="#818CF8"/><circle cx="348" cy="144" r="3" fill="#818CF8"/><circle cx="330" cy="74" r="2.6" fill="#818CF8"/><circle cx="336" cy="84" r="2.6" fill="#818CF8"/><circle cx="252" cy="206" r="3" fill="#818CF8"/><circle cx="304" cy="218" r="3" fill="#818CF8"/><circle cx="330" cy="216" r="2.6" fill="#818CF8"/><circle cx="226" cy="66" r="2.6" fill="#818CF8"/>
    <path d="M240 128h-26v-22h-30M240 150h-40v24h-22M240 140h-58M260 178v16h-40v22h-16M222 106v-24h10M240 160h-14v30" stroke="url(#{ids.meld})" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <path d="M184 106c-6-2-10-8-12-14M178 174c-8 2-14 8-18 14M182 140c-8-4-14-10-16-18M204 216c-10-2-20-6-28-12" stroke="#C084FC" stroke-width="1.6" stroke-linecap="round" opacity=".8"/>
    <circle cx="184" cy="106" r="3.2" fill="#C4B5FD"/><circle cx="178" cy="174" r="3.2" fill="#C4B5FD"/><circle cx="182" cy="140" r="3.2" fill="#C4B5FD"/><circle cx="204" cy="216" r="3.2" fill="#C4B5FD"/><circle cx="232" cy="82" r="2.6" fill="#A78BFA"/><circle cx="226" cy="190" r="2.6" fill="#A78BFA"/>
    <circle cx="214" cy="128" r="2" fill="#A5B4FC"/><circle cx="200" cy="174" r="2" fill="#A5B4FC"/><circle cx="220" cy="194" r="2" fill="#A5B4FC"/>
  {#if entries !== undefined}
    <text x="68" y="282" fill="#C084FC" font-family="Geist Mono, monospace" font-size="10.5">{fmtNum(entries)} associative entries</text>
  {/if}
  {#if facts !== undefined}
    <text x="252" y="282" fill="#A5B4FC" font-family="Geist Mono, monospace" font-size="10.5">{fmtNum(facts)} canonical facts</text>
  {/if}
</svg>

<style>
  .hero-art {
    justify-self: end;
    align-self: center;
    max-width: 100%;
    height: auto;
  }
</style>
