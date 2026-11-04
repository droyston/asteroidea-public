// Error text shared across the app: the error's name and message, or the thrown value as a string.
export function errorText(err: unknown): string {
  return err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}
