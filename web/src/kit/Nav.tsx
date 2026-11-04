// Nav: links through the router (interceptLinks handles the click); the current route
// carries aria-current="page".
export interface NavItem {
  to: string;
  label: string;
  current: boolean;
}

export function Nav({ items, label = "Main" }: { items: readonly NavItem[]; label?: string }) {
  return (
    <nav className="nav" aria-label={label}>
      {items.map((item) => (
        <a key={item.to} href={item.to} aria-current={item.current ? "page" : undefined}>
          {item.label}
        </a>
      ))}
    </nav>
  );
}
