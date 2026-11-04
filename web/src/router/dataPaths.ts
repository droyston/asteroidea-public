// Data paths and the spine trail for dataset > subject > session > file. Pure; routes are wired separately.
// The session key is `<dataset>/<subject>/<session or _>`; `_` stands for a subject with no session.

export const NO_SESSION = "_";
/** The study segment of a session path until studies exist: the session page ignores it. */
export const NO_STUDY = "_";

export interface Crumb {
  readonly label: string;
  /** Absent on the last crumb: the page the reader is on. */
  readonly href?: string;
}

const enc = encodeURIComponent;

export function dataPath(): string {
  return "/";
}

/** The retired dataset path (`/data/:dataset`); `redirectFor` sends it to `datasetViewPath`. Subject paths still build on it. */
export function dataDatasetPath(dataset: string): string {
  return `/data/${enc(dataset)}`;
}

/** The dataset page, `/d/:dataset`. */
export function datasetViewPath(dataset: string): string {
  return `/d/${enc(dataset)}`;
}

/** The retired subject path (`/data/:dataset/:subject`); `redirectFor` sends it to `participantPath`. */
export function dataSubjectPath(dataset: string, subject: string): string {
  return `${dataDatasetPath(dataset)}/${enc(subject)}`;
}

/**
 * The participant segment for a subject record that no link ties to another: `<dataset>:<subject>`
 *. A linked person's segment is their `subject_key`.
 */
export function unlinkedKey(dataset: string, subject: string): string {
  return `${dataset}:${subject}`;
}

/** The participant page, `/s/:study/p/:participant` (`study` is `_` outside a study). */
export function participantPath(participant: string, study: string = NO_STUDY): string {
  return `/s/${enc(study)}/p/${enc(participant)}`;
}

/** A participant segment as a dataset and subject when it is the unlinked form; null for a `subject_key`. */
export function parseUnlinkedKey(key: string): { dataset: string; subject: string } | null {
  const at = key.indexOf(":");
  if (at <= 0 || at === key.length - 1) return null;
  return { dataset: key.slice(0, at), subject: key.slice(at + 1) };
}

function sessionPart(session: string | null): string {
  return session === null || session === "" ? NO_SESSION : session;
}

/** The session page, `/s/:study/:dataset/:subject/:session`. A null or empty session is the `_` placeholder; so is the study (`study` omitted). */
export function dataSessionPath(
  dataset: string,
  subject: string,
  session: string | null,
  study: string = NO_STUDY,
): string {
  return `/s/${enc(study)}/${enc(dataset)}/${enc(subject)}/${enc(sessionPart(session))}`;
}

export function sessionKey(dataset: string, subject: string, session: string | null): string {
  return `${dataset}/${subject}/${sessionPart(session)}`;
}

/** Inverse of `sessionKey`; null unless the key has exactly three non-empty parts. */
export function parseSessionKey(
  key: string,
): { dataset: string; subject: string; session: string | null } | null {
  const parts = key.split("/");
  const [dataset, subject, session] = parts;
  if (parts.length !== 3 || !dataset || !subject || !session) return null;
  return { dataset, subject, session: session === NO_SESSION ? null : session };
}

export interface CrumbTarget {
  readonly dataset?: string;
  readonly subject?: string;
  /** null means the subject has no session; undefined means the level is not shown. */
  readonly session?: string | null;
  /** The file's display name; makes the file the last crumb. */
  readonly file?: string;
}

/** Studies > dataset > subject > session > file, cut at the first level the target omits. Every crumb but the last links. */
export function dataCrumbs(t: CrumbTarget): Crumb[] {
  const linked: { label: string; href: string }[] = [{ label: "Studies", href: dataPath() }];
  if (t.dataset !== undefined) {
    linked.push({ label: t.dataset, href: datasetViewPath(t.dataset) });
    if (t.subject !== undefined) {
      linked.push({ label: t.subject, href: dataSubjectPath(t.dataset, t.subject) });
      if (t.session !== undefined) {
        linked.push({
          label: sessionPart(t.session),
          href: dataSessionPath(t.dataset, t.subject, t.session),
        });
      }
    }
  }
  if (t.file !== undefined) return [...linked, { label: t.file }];
  const last = linked.pop();
  return [...linked, ...(last ? [{ label: last.label }] : [])];
}
