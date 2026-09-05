import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Clearledger | Reconciliation control room",
  description: "Deterministic reconciliation review, exception resolution, and close controls.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
