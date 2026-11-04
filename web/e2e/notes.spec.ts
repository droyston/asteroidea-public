// The top-bar Note: the dialog opens from the button with focus in the textarea,
// traps Tab, closes on Esc with focus back on the button, and sends the text with the page address and
// route state to a stubbed `POST /notes`. A 201 shows "Sent"; a 400 shows the server's sentence and
// keeps the typed text. Runs against the sign-in env build; nothing leaves the machine.
import { expect, type Page } from "@playwright/test";
import { API, APP, CORS, stubBase, stubEmptyUploads, test } from "./support";

test.skip(
  process.env.E2E_LIVE === "1",
  "needs the fixture-mode auth build (no live server has the auth env)",
);

interface Sent {
  bodies: unknown[];
  auth: (string | undefined)[];
}

async function stub(page: Page, reply: { status: number; body: unknown }): Promise<Sent> {
  await stubBase(page);
  await stubEmptyUploads(page);
  const sent: Sent = { bodies: [], auth: [] };
  await page.route(`${API}/notes`, (route) => {
    const req = route.request();
    if (req.method() === "OPTIONS") return route.fulfill({ status: 204, headers: CORS });
    sent.bodies.push(JSON.parse(req.postData() ?? "null"));
    sent.auth.push(req.headers().authorization);
    return route.fulfill({
      status: reply.status,
      contentType: "application/json",
      headers: CORS,
      body: JSON.stringify(reply.body),
    });
  });
  return sent;
}

test("the Note dialog opens, traps focus, closes on Esc and returns focus", async ({ page }) => {
  await stub(page, { status: 201, body: { note_id: "n1" } });
  await page.goto(APP);
  const open = page.getByRole("button", { name: "Note", exact: true });
  await expect(open).toBeVisible();
  await open.click();
  const dialog = page.getByRole("dialog", { name: "Leave a note" });
  await expect(dialog).toBeVisible();
  const text = dialog.getByTestId("note-text");
  await expect(text).toBeFocused();
  await text.fill("draft");
  // textarea -> Send -> Cancel -> back to the textarea.
  await page.keyboard.press("Tab");
  await expect(dialog.getByTestId("note-send")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialog.getByTestId("note-cancel")).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(text).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialog.getByTestId("note-cancel")).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toBeHidden();
  await expect(open).toBeFocused();
  // Cancel keeps the draft.
  await open.click();
  await expect(page.getByTestId("note-text")).toHaveValue("draft");
});

test("a note is sent with the page address and view, and the dialog says Sent", async ({
  page,
}) => {
  const sent = await stub(page, { status: 201, body: { note_id: "n1" } });
  await page.goto(`${APP}discover?q=amygdala`);
  await page.getByRole("button", { name: "Note", exact: true }).click();
  await page.getByTestId("note-text").fill("the filter chips wrap badly");
  await page.getByTestId("note-send").click();
  await expect(page.getByTestId("note-outcome")).toHaveText("Sent");
  await expect(page.getByTestId("note-text")).toHaveValue("");
  expect(sent.bodies).toEqual([
    {
      text: "the filter chips wrap badly",
      url: `${APP}discover?q=amygdala`,
      view: { path: "/discover", query: { q: "amygdala" }, hash: "" },
    },
  ]);
  // The ID token goes, so the server can store the author's email.
  expect(sent.auth).toEqual(["Bearer id-1"]);
});

test("a 400 shows the server's sentence and keeps the typed text", async ({
  page,
  expectError,
}) => {
  expectError("the stubbed POST /notes answers 400");
  const detail = "text is 4001 characters; the limit is 4000.";
  const sent = await stub(page, { status: 400, body: { error: "invalid_request", detail } });
  await page.goto(APP);
  await page.getByRole("button", { name: "Note", exact: true }).click();
  await page.getByTestId("note-text").fill("keep me");
  await page.getByTestId("note-send").click();
  await expect(page.getByTestId("note-outcome")).toHaveText(detail);
  await expect(page.getByTestId("note-text")).toHaveValue("keep me");
  await expect(page.getByTestId("note-send")).toBeEnabled();
  expect(sent.bodies).toHaveLength(1);
});
