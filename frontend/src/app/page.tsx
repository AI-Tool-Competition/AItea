import "streamdown/styles.css";
import "@/components/tea/tea.css";

import { TeaApp } from "@/components/tea/tea-app";

export { metadata } from "./tea/layout";

export default function Home() {
  return <TeaApp />;
}
