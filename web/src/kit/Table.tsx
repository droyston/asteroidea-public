// Table: one look for every data table on the `.table` / `.table-wrap` style.
// Numeric columns right-align; an empty table shows its message; Previous / Next bind to the
// API's `page` and `more`.
import type { Key, ReactNode } from "react";
import { Button } from "./Button";

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  numeric?: boolean;
  /** Monospace, no wrap (paths and ids). */
  what?: boolean;
}

export interface Paging {
  page: number;
  more: boolean;
  onPage: (page: number) => void;
}

export interface TableProps<T> {
  columns: readonly Column<T>[];
  rows: readonly T[];
  rowKey: (row: T, index: number) => Key;
  /** Accessible name of the table. */
  label: string;
  empty?: string;
  paging?: Paging;
  testId?: string;
}

function cls(c: { numeric?: boolean; what?: boolean }): string | undefined {
  const parts = [c.numeric === true ? "num" : "", c.what === true ? "what" : ""].filter(
    (p) => p !== "",
  );
  return parts.length === 0 ? undefined : parts.join(" ");
}

export function Table<T>({
  columns,
  rows,
  rowKey,
  label,
  empty = "Nothing to show.",
  paging,
  testId,
}: TableProps<T>) {
  return (
    <div className="table-block">
      {rows.length === 0 ? (
        <p
          className="table-empty"
          data-testid={testId === undefined ? undefined : `${testId}-empty`}
        >
          {empty}
        </p>
      ) : (
        <div className="table-wrap">
          <table className="table" aria-label={label} data-testid={testId}>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c.key} className={c.numeric === true ? "num" : undefined}>
                    {c.header}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={rowKey(row, i)}>
                  {columns.map((c) => (
                    <td key={c.key} className={cls(c)}>
                      {c.render(row)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {paging === undefined ? null : (
        <p className="table-paging">
          <Button disabled={paging.page === 0} onClick={() => paging.onPage(paging.page - 1)}>
            Previous
          </Button>
          <span className="table-page">Page {paging.page + 1}</span>
          <Button disabled={!paging.more} onClick={() => paging.onPage(paging.page + 1)}>
            Next
          </Button>
        </p>
      )}
    </div>
  );
}
