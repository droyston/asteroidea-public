import { TARGETS_SCHEMA, type Target, type TargetsDoc } from "./schema";

export const EXPORT_FILENAME = "targets.json";

/** The exported document text: schema, export time and the entries in list order. */
export function buildExport(targets: readonly Target[], now: Date = new Date()): string {
  const doc: TargetsDoc = {
    schema: TARGETS_SCHEMA,
    exported_at: now.toISOString(),
    targets: targets.map((t) => ({ ...t })),
  };
  return `${JSON.stringify(doc, null, 2)}\n`;
}

/** Offer the document as a browser download named targets.json. */
export function downloadExport(targets: readonly Target[]): void {
  const blob = new Blob([buildExport(targets)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = EXPORT_FILENAME;
  document.body.append(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
