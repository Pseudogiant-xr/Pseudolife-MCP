// Agent- and model-authored strings reach this console (source URLs, entity
// names, memory text). Svelte renders text as text; the one remaining sink is
// an href, so a URL is linked only when it is plain http(s).

/** The URL if it is http(s), else null (javascript:, data:, relative, junk). */
export function safeHttpUrl(url: string | null | undefined): string | null {
  if (typeof url !== "string") return null;
  const u = url.trim();
  return /^https?:\/\//i.test(u) ? u : null;
}
