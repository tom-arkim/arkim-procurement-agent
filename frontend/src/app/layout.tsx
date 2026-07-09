import type { Metadata } from "next";
import { Inter, JetBrains_Mono, Mulish, Cormorant_Garamond } from "next/font/google";
import "./globals.css";
import "@/styles/procurement.css";
import { Providers } from "./providers";
import { BRAND_NAME } from "@/lib/brand";

const inter = Inter({
  subsets: ["latin"],
  variable: "--font-inter",
  display: "swap",
});

const jetbrainsMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-jetbrains",
  display: "swap",
  weight: ["400", "500", "600"],
});

// Customer "Parts & Orders" surface fonts (mockup design system). Scoped to the
// .proc-theme wrapper via procurement.css; the internal app keeps Inter/JetBrains.
const mulish = Mulish({
  subsets: ["latin"],
  variable: "--font-mulish",
  display: "swap",
  weight: ["300", "400", "500", "600", "700"],
  style: ["normal", "italic"],
});

const cormorant = Cormorant_Garamond({
  subsets: ["latin"],
  variable: "--font-cormorant",
  display: "swap",
  weight: ["300", "400", "500"],
  style: ["italic", "normal"],
});

export const metadata: Metadata = {
  title: `${BRAND_NAME} · Sourcing Engine`,
  description: "Maintenance sourcing from work order to approved purchase.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html
      lang="en"
      className={`${inter.variable} ${jetbrainsMono.variable} ${mulish.variable} ${cormorant.variable}`}
    >
      <body className="bg-bg-1 text-fg-1 antialiased">
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
