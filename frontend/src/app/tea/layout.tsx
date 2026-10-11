import { type Metadata } from "next";
import "@/components/tea/tea.css";

export const metadata: Metadata = {
  title: "茶渡 · 茶叶出口工作台",
  description: "从一片茶叶，到更远的市场。可追溯的出口办理工作台。",
};
export default function TeaLayout({ children }: { children: React.ReactNode }) {
  return children;
}
