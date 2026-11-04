// Test helper: the harvester's golden index (one source, read across trees).
import { readFileSync } from "node:fs";

export const GOLDEN_PATH = new URL(
  "../../../services/tests/fixtures/harvest/expected-index.json",
  import.meta.url,
);

export function readGolden(): unknown {
  return JSON.parse(readFileSync(GOLDEN_PATH, "utf8"));
}
