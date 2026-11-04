import { parseTargets, type Target } from "./schema";

export type ImportResult = { ok: true; targets: Target[] } | { ok: false; reason: string };

/** Validate a whole file; an invalid file is rejected whole with the first reason. */
export function parseImport(text: string, filename: string): ImportResult {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    return {
      ok: false,
      reason: `${filename}: not valid JSON: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
  try {
    return { ok: true, targets: parseTargets(raw).targets };
  } catch (err) {
    return {
      ok: false,
      reason: `${filename}: ${err instanceof Error ? err.message : String(err)}`,
    };
  }
}

/** The replace-confirm text: both counts. */
export function replaceMessage(current: number, incoming: number, filename: string): string {
  return `Replace ${current} picks with ${incoming} from ${filename}?`;
}
