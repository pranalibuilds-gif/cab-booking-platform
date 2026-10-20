import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Ridewell — your city, your ride",
  description: "A smoother way to move around your city.",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
