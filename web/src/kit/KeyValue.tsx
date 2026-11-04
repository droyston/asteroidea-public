// KeyValue: a definition list of label / value pairs. A null or empty value shows a dash.
import type { ReactNode } from "react";

export interface KeyValueItem {
  label: string;
  value: ReactNode;
}

export function KeyValue({ items, testId }: { items: readonly KeyValueItem[]; testId?: string }) {
  return (
    <dl className="kv-list" data-testid={testId}>
      {items.map((item) => (
        <div key={item.label}>
          <dt>{item.label}</dt>
          <dd>{item.value === null || item.value === "" ? "—" : item.value}</dd>
        </div>
      ))}
    </dl>
  );
}
