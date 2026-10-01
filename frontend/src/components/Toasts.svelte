<script lang="ts">
  import Icon from "./Icon.svelte";
  import { dismissToast, toasts } from "../lib/overlay.svelte";
</script>

<div class="toasts" role="status" aria-live="polite">
  {#each toasts as t (t.id)}
    <div class="toast {t.tone}">
      <span class="dot {t.tone === 'info' ? 'assoc' : t.tone}" aria-hidden="true"></span>
      <span class="msg">{t.message}</span>
      <button type="button" class="close" aria-label="Dismiss" onclick={() => dismissToast(t.id)}>
        <Icon name="close" size={12} />
      </button>
    </div>
  {/each}
</div>

<style>
  .toasts {
    position: fixed;
    right: 20px;
    bottom: 20px;
    z-index: 60;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 8px;
    pointer-events: none;
  }
  .toast {
    pointer-events: auto;
    display: flex;
    align-items: center;
    gap: 10px;
    max-width: min(440px, calc(100vw - 32px));
    padding: 10px 10px 10px 14px;
    border-radius: 14px;
    border: 1px solid var(--panel-border);
    background: var(--bar);
    box-shadow: var(--highlight), 0 18px 40px -20px rgba(0, 0, 0, 0.8);
    -webkit-backdrop-filter: blur(24px) saturate(1.4);
    backdrop-filter: blur(24px) saturate(1.4);
    font-size: 13px;
  }
  .msg {
    flex: 1 1 auto;
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .close {
    flex: none;
    width: 24px;
    height: 24px;
    border-radius: 999px;
    border: 0;
    background: transparent;
    color: var(--ink-3);
    display: inline-flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
  }
  .close:hover {
    color: var(--ink);
    background: var(--fill);
  }
  @media (max-width: 860px) {
    .toasts {
      left: 16px;
      right: 16px;
      bottom: calc(84px + env(safe-area-inset-bottom));
      align-items: stretch;
    }
  }
  @media (prefers-reduced-motion: no-preference) {
    .toast {
      animation: rise 0.18s ease-out;
    }
    @keyframes rise {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
    }
  }
</style>
