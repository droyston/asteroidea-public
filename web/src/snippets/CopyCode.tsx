// The "Copy code" button: copies the page's notebook code to the clipboard and says so
// in a toast; a refusal says why. A Tooltip names what it copies.
import { Button } from "../kit/Button";
import { toast } from "../kit/Toast";
import { Tooltip } from "../kit/Tooltip";
import { renderSnippet, type SnippetPage, type SnippetValues, snippetWhat } from "./render";

/** Copy `text`; returns the toast sentence and whether it is an error. */
export async function copyText(text: string): Promise<{ text: string; error: boolean }> {
  try {
    await navigator.clipboard.writeText(text);
    return { text: "Copied — paste into a notebook cell", error: false };
  } catch (err) {
    const why = err instanceof Error && err.message !== "" ? err.message : "the browser refused";
    return {
      text: `Could not copy the code: ${why}. Open the notebook to copy it by hand.`,
      error: true,
    };
  }
}

export function CopyCode<P extends SnippetPage>({
  page,
  values,
}: {
  page: P;
  values: SnippetValues[P];
}) {
  const onClick = () => {
    void copyText(renderSnippet(page, values)).then((r) =>
      toast(r.text, r.error ? "error" : "info"),
    );
  };
  return (
    <div className="copy-code-slot">
      <Tooltip
        text={`Copies ${snippetWhat(page)}, ready to paste into a notebook cell`}
        trigger={
          <Button variant="secondary" data-testid="copy-code" data-ab="copy-code" onClick={onClick}>
            Copy code
          </Button>
        }
      />
    </div>
  );
}
