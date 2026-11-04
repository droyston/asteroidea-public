// Button: primary, secondary or quiet. type="button" unless "submit" is passed; focus ring comes from kit.css.
import type { ButtonHTMLAttributes } from "react";

export type ButtonVariant = "primary" | "secondary" | "quiet";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  type?: "button" | "submit";
}

export function Button({
  variant = "secondary",
  className,
  type = "button",
  ...rest
}: ButtonProps) {
  const cls = `btn btn-${variant}${className === undefined ? "" : ` ${className}`}`;
  return <button type={type} className={cls} {...rest} />;
}
