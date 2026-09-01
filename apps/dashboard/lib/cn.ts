import clsx, { type ClassValue } from "clsx";

/** Thin conditional-classname helper (shadcn/ui convention). */
export function cn(...inputs: ClassValue[]): string {
  return clsx(inputs);
}
