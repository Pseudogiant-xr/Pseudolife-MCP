// App-wide overlays every view shares: toasts (outcome of an action) and a
// promise-based confirmation for anything that changes or removes data.
// <Toasts/> and <ConfirmDialog/> are mounted once in App.svelte.

export type Tone = "ok" | "warn" | "danger" | "info";

export interface Toast {
  id: number;
  message: string;
  tone: Tone;
}

export const toasts = $state<Toast[]>([]);
let nextId = 1;

/** Show a short outcome line. Name the result ("Fact forgotten"), not the click. */
export function toast(message: string, tone: Tone = "ok", ms = 4200): void {
  const id = nextId++;
  toasts.push({ id, message, tone });
  setTimeout(() => dismissToast(id), ms);
}

export function dismissToast(id: number): void {
  const i = toasts.findIndex((t) => t.id === id);
  if (i !== -1) toasts.splice(i, 1);
}

export interface ConfirmRequest {
  title: string;
  /** Plain text; rendered as text, never as HTML. */
  message: string;
  /** The verb on the button, e.g. "Forget the fact". Defaults to "Confirm". */
  confirmLabel?: string;
  cancelLabel?: string;
  /** Destructive: the button turns red. */
  danger?: boolean;
}

interface Pending extends ConfirmRequest {
  resolve: (ok: boolean) => void;
}

export const confirmState = $state<{ current: Pending | null }>({ current: null });

/** Ask before acting. Resolves false on Cancel, Escape or a second request. */
export function confirm(req: ConfirmRequest): Promise<boolean> {
  confirmState.current?.resolve(false);
  return new Promise((resolve) => {
    confirmState.current = { ...req, resolve };
  });
}

export function settleConfirm(ok: boolean): void {
  const p = confirmState.current;
  confirmState.current = null;
  p?.resolve(ok);
}
