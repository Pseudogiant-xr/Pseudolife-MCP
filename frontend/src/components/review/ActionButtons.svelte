<script lang="ts">
  // The decision buttons of one review row. Clicking hands the button to the
  // view, which runs its ReviewAction through runReviewAction(). While any
  // decision is in flight every button is disabled, so two decisions on the
  // same graph never race.
  import type { ActionButton } from "../../lib/review";

  let {
    buttons,
    busy,
    onact,
    size = "sm",
  }: {
    buttons: ActionButton[];
    /** Id of the decision in flight, or null. */
    busy: string | null;
    onact: (b: ActionButton) => void;
    size?: "sm" | "md";
  } = $props();

  const CLS: Record<ActionButton["tone"], string> = {
    primary: "btn-primary",
    secondary: "btn-secondary",
    danger: "btn-danger",
    ghost: "btn-ghost",
  };
</script>

{#each buttons as b (b.id)}
  <button
    type="button"
    class="btn {CLS[b.tone]}"
    class:btn-sm={size === "sm"}
    disabled={busy !== null || b.disabled}
    aria-busy={busy === b.id}
    onclick={() => onact(b)}
  >
    {b.label}
  </button>
{/each}
