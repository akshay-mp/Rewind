// Top-level shell. State machine: list ↔ timeline; search overlay can be
// opened from anywhere. Selected span shows up as inspector side panel on
// the timeline view.

import { useState } from "react";
import { TraceList } from "./components/TraceList";
import { Timeline } from "./components/Timeline";
import { SpanInspector } from "./components/SpanInspector";
import { SearchOverlay } from "./components/SearchOverlay";

type View =
  | { kind: "list" }
  | { kind: "trace"; traceId: string; selectedRewindId: string | null };

export default function App(): JSX.Element {
  const [view, setView] = useState<View>({ kind: "list" });
  const [searchOpen, setSearchOpen] = useState(false);

  return (
    <div className="app">
      <header className="app__bar">
        <h1>
          <button
            type="button"
            className="brand"
            onClick={() => setView({ kind: "list" })}
          >
            ⏮ rewind
          </button>
        </h1>
        <nav className="app__nav">
          <button
            type="button"
            className="link-button"
            onClick={() => setSearchOpen(true)}
          >
            search
          </button>
          <span className="muted">v0.1.0</span>
        </nav>
      </header>

      <main className="app__main">
        {view.kind === "list" && (
          <TraceList
            onOpenTrace={(traceId) =>
              setView({ kind: "trace", traceId, selectedRewindId: null })
            }
          />
        )}
        {view.kind === "trace" && (
          <Timeline
            traceId={view.traceId}
            onBack={() => setView({ kind: "list" })}
            onSelectSpan={(rewindId) =>
              setView({
                kind: "trace",
                traceId: view.traceId,
                selectedRewindId: rewindId,
              })
            }
            selectedRewindId={view.selectedRewindId}
          />
        )}
      </main>

      {view.kind === "trace" && view.selectedRewindId !== null && (
        <SpanInspector
          rewindId={view.selectedRewindId}
          onClose={() =>
            setView({
              kind: "trace",
              traceId: view.traceId,
              selectedRewindId: null,
            })
          }
        />
      )}

      {searchOpen && (
        <SearchOverlay
          onClose={() => setSearchOpen(false)}
          onSelectResult={(traceId, rewindId) => {
            setSearchOpen(false);
            setView({ kind: "trace", traceId, selectedRewindId: rewindId });
          }}
        />
      )}
    </div>
  );
}
