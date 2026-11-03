import AskAI from "../components/AskAI";
import { Page } from "../components/ui";

export default function Ask() {
  return (
    <Page
      title="Ask across meetings"
      subtitle="Searches the summaries and decisions of every analysed meeting."
    >
      <AskAI
        endpoint="/api/chat/search"
        placeholder="What did we decide about pricing last month?"
        suggestions={[
          "What are we still blocked on?",
          "Which commitments are overdue?",
          "What has changed about the roadmap?",
          "Where did we disagree?",
        ]}
      />
    </Page>
  );
}
