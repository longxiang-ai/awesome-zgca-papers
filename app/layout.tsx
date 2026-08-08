import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  metadataBase: new URL("https://longxiang-ai.github.io/awesome-zgca-papers/"),
  title: "Awesome ZGCA Papers | 中关村两院研究成果索引",
  description: "A bilingual, traceable index of research outputs from Zhongguancun Academy and the Zhongguancun Institute of Artificial Intelligence.",
  keywords: ["Zhongguancun Academy", "ZGCA", "ZGCI", "research papers", "北京中关村学院", "中关村人工智能研究院"],
  authors: [{ name: "Awesome ZGCA Papers contributors" }],
  openGraph: {
    type: "website",
    locale: "zh_CN",
    alternateLocale: "en_US",
    title: "Awesome ZGCA Papers",
    description: "Discover every research output from ZGCA × ZGCI, with inspectable affiliation evidence.",
    url: "https://longxiang-ai.github.io/awesome-zgca-papers/",
    siteName: "Awesome ZGCA Papers",
    images: [{ url: "og.png", width: 1732, height: 909, alt: "Awesome ZGCA Papers — Open, Traceable, Updated daily" }],
  },
  twitter: { card: "summary_large_image", title: "Awesome ZGCA Papers", description: "An open, traceable research index for ZGCA × ZGCI.", images: ["og.png"] },
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-CN"><body>{children}</body></html>;
}
