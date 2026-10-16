// The top bar's writer chip. The daemon's writer id (PSEUDOLIFE_WRITER_ID in
// ops/.env) is the provenance stamp on writes that arrive without a writer id
// of their own; the installer sets it from the MCP client chosen at install.
// A bare "codex" there read as the dream model (2026-10-16), so the chip
// names what the value is.

export interface WriterChip {
  label: string;
  value: string;
  title: string;
}

export function writerChip(id: string | null | undefined): WriterChip | null {
  if (!id) return null;
  return {
    label: "writer",
    value: id,
    title:
      `Default writer id: ${id}. The daemon stamps it on writes from clients that send ` +
      `no writer id of their own; the installer set it from the MCP client chosen at ` +
      `install. It is not the dream or extractor model (see Settings).`,
  };
}
