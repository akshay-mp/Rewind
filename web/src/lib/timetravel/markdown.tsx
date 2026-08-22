"use client";

/**
 * Shared lightweight markdown-ish renderer.
 *
 * Extracted from span-detail.tsx so both the SpanDetail output card and the
 * live ThinkingPanel can reuse it. Handles headings, bold, bullets, numbered
 * lists, and paragraphs — enough for model prose without pulling a full
 * markdown dependency into the streaming hot path.
 *
 * Pass `variant="thinking"` to render in the muted/italic style used for the
 * model's chain-of-thought (distinct from the normal answer prose).
 */
export function MarkdownPreview({
  text,
  variant = "default",
}: {
  text: string;
  variant?: "default" | "thinking";
}) {
  const thinking = variant === "thinking";
  const base = thinking
    ? "space-y-1 text-[13px] leading-relaxed italic text-slate-500 dark:text-slate-400"
    : "space-y-1 text-sm leading-relaxed";

  const lines = text.split("\n");
  return (
    <div className={base}>
      {lines.map((line, i) => {
        const stripped = (s: string) => s.replace(/\*\*(.+?)\*\*/g, "$1");
        if (/^###\s+/.test(line))
          return (
            <h4 key={i} className="mt-2 text-sm font-semibold not-italic">
              {stripped(line.replace(/^###\s+/, ""))}
            </h4>
          );
        if (/^##\s+/.test(line))
          return (
            <h3 key={i} className="mt-3 text-base font-semibold not-italic">
              {stripped(line.replace(/^##\s+/, ""))}
            </h3>
          );
        if (/^#\s+/.test(line))
          return (
            <h2 key={i} className="mt-3 text-lg font-semibold not-italic">
              {stripped(line.replace(/^#\s+/, ""))}
            </h2>
          );
        if (/^\s*[-*]\s+/.test(line))
          return (
            <div key={i} className="flex gap-2 pl-2">
              <span className="text-muted-foreground">•</span>
              <span>{stripped(line.replace(/^\s*[-*]\s+/, ""))}</span>
            </div>
          );
        if (/^\s*\d+\.\s+/.test(line))
          return (
            <div key={i} className="flex gap-2 pl-2">
              <span className="text-muted-foreground">
                {line.match(/^\s*(\d+)\./)?.[1]}.
              </span>
              <span>{stripped(line.replace(/^\s*\d+\.\s+/, ""))}</span>
            </div>
          );
        if (line.trim() === "") return <div key={i} className="h-2" />;
        return (
          <p key={i} className="whitespace-pre-wrap">
            {stripped(line)}
          </p>
        );
      })}
    </div>
  );
}
