/**
 * Deep Research agent prompt templates.
 *
 * Mirrors src/open_deep_research/prompts.py from langchain-ai/open_deep_research.
 * Each span in the agent flow has a system prompt that is editable when the user
 * branches from that span — this is the "fix the prompt" surface Rewind exposes.
 *
 * The flow (8 spans):
 *   1. clarify_with_user      — optional clarifying question
 *   2. write_research_brief    — convert query → research brief
 *   3. supervisor_think (1)    — plan first research topics (think_tool reflection)
 *   4. conduct_research (1)    — researcher subgraph: search → compress
 *   5. supervisor_think (2)    — review, plan next topics
 *   6. conduct_research (2)    — more research
 *   7. research_complete       — supervisor summary
 *   8. final_report            — writer synthesizes the report
 */

export interface SpanPrompt {
  name: string;
  kind:
    | "clarify_with_user"
    | "write_research_brief"
    | "supervisor_think"
    | "conduct_research"
    | "research_complete"
    | "final_report";
  systemPrompt: string;
  /**
   * Template for the user-side input. May reference prior span outputs via
   * {output:INDEX} (e.g. {output:1} = the output of span at index 1) and the
   * original query via {query}.
   */
  userInputTemplate: string;
}

export const DEFAULT_PROMPTS: SpanPrompt[] = [
  {
    name: "clarify_with_user",
    kind: "clarify_with_user",
    systemPrompt:
      "You are a research intake assistant. Decide whether the user's research query needs clarification. " +
      "If it does, ask ONE focused clarifying question. If it is already specific enough, output exactly PROCEED. " +
      "Be concise — one short paragraph at most.",
    userInputTemplate: "Research query: {query}",
  },
  {
    name: "write_research_brief",
    kind: "write_research_brief",
    systemPrompt:
      "You are a lead researcher. Convert the conversation into a detailed research brief. " +
      "Cover: scope, 3-5 key sub-questions, intended audience, and success criteria. " +
      "Output as a short markdown document with the headings: Scope, Key Questions, Audience, Success Criteria.",
    userInputTemplate:
      "Original query: {query}\n\nClarify step output:\n{output:0}\n\nWrite the research brief now.",
  },
  {
    name: "supervisor_think",
    kind: "supervisor_think",
    systemPrompt:
      "You are the research supervisor. Reflect on the brief and identify the first 2 specific research topics to investigate. " +
      "For each topic, state the topic and one sentence on why it matters. Output as a numbered list.",
    userInputTemplate: "Research brief:\n{output:1}",
  },
  {
    name: "conduct_research",
    kind: "conduct_research",
    systemPrompt:
      "You are a researcher. Given a research topic, produce compressed research notes: 3-5 key findings, " +
      "each one sentence, with a plausible citation in the form [Source: description]. Do not browse the web — " +
      "reason from your training knowledge.",
    userInputTemplate:
      "Research brief:\n{output:1}\n\nSupervisor topics:\n{output:2}\n\nInvestigate topic #1 in depth.",
  },
  {
    name: "supervisor_think",
    kind: "supervisor_think",
    systemPrompt:
      "You are the research supervisor. Review the findings so far. Decide whether more research is needed. " +
      "If yes, identify ONE follow-up topic to investigate. If no, output exactly COMPLETE.",
    userInputTemplate:
      "Brief:\n{output:1}\n\nFindings from topic #1:\n{output:3}",
  },
  {
    name: "conduct_research",
    kind: "conduct_research",
    systemPrompt:
      "You are a researcher. Given a research topic, produce compressed research notes: 3-5 key findings, " +
      "each one sentence, with a plausible citation in the form [Source: description]. Do not browse the web — " +
      "reason from your training knowledge.",
    userInputTemplate:
      "Research brief:\n{output:1}\n\nSupervisor follow-up:\n{output:4}\n\nInvestigate the follow-up topic in depth.",
  },
  {
    name: "research_complete",
    kind: "research_complete",
    systemPrompt:
      "You are the research supervisor. Summarize the research that was conducted. List the consolidated key " +
      "findings that will feed into the final report. Output as a bulleted list.",
    userInputTemplate:
      "Brief:\n{output:1}\n\nFindings #1:\n{output:3}\n\nFindings #2:\n{output:5}",
  },
  {
    name: "final_report",
    kind: "final_report",
    systemPrompt:
      "You are a senior research writer. Synthesize the brief and findings into a structured markdown research report. " +
      "Use the sections: Executive Summary, Key Findings, Analysis, Conclusion. Keep it under 400 words.",
    userInputTemplate:
      "Research brief:\n{output:1}\n\nConsolidated findings:\n{output:6}\n\nWrite the final report now.",
  },
];

/**
 * Suggested "fixed" prompts the user can one-click apply when branching —
 * this is what makes the demo immediately show the "fix the prompt, see the
 * new output" loop without the user needing to invent a fix themselves.
 *
 * Each suggestion targets a span and provides an improved system prompt plus
 * a short human-readable rationale.
 */
export interface PromptSuggestion {
  spanIndex: number;
  title: string;
  rationale: string;
  newSystemPrompt: string;
}

export const PROMPT_SUGGESTIONS: PromptSuggestion[] = [
  {
    spanIndex: 2,
    title: "Force 3 specific, non-overlapping topics",
    rationale:
      "The default supervisor prompt is vague — it asks for '2 specific topics' which often produces overlapping or generic ones. " +
      "This fix asks for 3 explicitly non-overlapping topics with a sharper framing, which makes the downstream research more comprehensive.",
    newSystemPrompt:
      "You are the research supervisor. Reflect on the brief and identify exactly 3 specific, NON-OVERLAPPING research topics " +
      "to investigate first. The three topics must cover different facets of the brief (e.g. one historical, one technical, one comparative). " +
      "For each topic, state the topic in one line and one sentence on why it matters and what specifically will be investigated. " +
      "Output as a numbered list (1., 2., 3.).",
  },
  {
    spanIndex: 7,
    title: "Require citations + lengthen the report",
    rationale:
      "The default final-report prompt caps the report at 400 words and does not explicitly require inline citations. " +
      "This fix raises the cap and requires every Key Finding to carry a citation — much closer to what a real deep-research deliverable looks like.",
    newSystemPrompt:
      "You are a senior research writer. Synthesize the brief and findings into a structured markdown research report. " +
      "Use the sections: Executive Summary, Key Findings (each finding MUST include an inline citation in [Source: ...] form), " +
      "Analysis (compare and contrast the findings), Conclusion, and References (list every cited source). " +
      "Target 600-800 words. Be specific and concrete — avoid hedging.",
  },
  {
    spanIndex: 3,
    title: "Require quantitative findings",
    rationale:
      "The default researcher prompt asks for 'key findings' which tend to be qualitative. " +
      "This fix requires at least two quantitative data points per topic, which makes the findings more substantive and citable.",
    newSystemPrompt:
      "You are a researcher. Given a research topic, produce compressed research notes: 3-5 key findings. " +
      "AT LEAST TWO of the findings MUST include a specific number, date, or quantitative comparison. " +
      "Each finding should be one sentence with a plausible citation in the form [Source: description]. " +
      "Do not browse the web — reason from your training knowledge.",
  },
];

export const DEFAULT_QUERY =
  "Compare RLHF vs DPO for aligning large language models, with citations.";
