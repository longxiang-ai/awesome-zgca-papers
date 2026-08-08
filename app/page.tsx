import works from "../data/works.json";
import stats from "../public/data/stats.json";
import { ResearchIndex } from "./ResearchIndex";

export default function Home() {
  return <ResearchIndex works={works} stats={stats} />;
}
