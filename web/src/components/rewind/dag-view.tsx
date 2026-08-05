"use client";

import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { api } from "@/api";

/**
 * Phase 5.1 — execution DAG renderer.
 *
 * Fetches the parent → children tree from ``GET /api/v1/traces/{id}/dag``
 * and renders it as a collapsible nested list so the developer can see the
 * causal structure of an agent run (which LLM call spawned which tool call).
 */

interface DagNode {
  span_id: string;
  name: string;
  kind: string;
  status: string;
  parent_span_id: string | null;
  start_time: string;
  children: DagNode[];
}

function DagNodeRow({ node, depth }: { node: DagNode; depth: number }) {
  const [expanded, setExpanded] = useState(true);
  const hasChildren = node.children.length > 0;
  return (
    <div style={{ paddingLeft: depth * 16 }}>
      <div className="flex items-center gap-2 py-1">
        {hasChildren ? (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-xs text-muted-foreground hover:text-foreground"
          >
            {expanded ? "▾" : "▸"}
          </button>
        ) : (
          <span className="w-3 text-xs text-muted-foreground">•</span>
        )}
        <span className="font-mono text-xs">{node.name}</span>
        <Badge variant="outline" className="text-[10px] font-mono">
          {node.kind}
        </Badge>
        {node.status === "error" && (
          <Badge className="bg-rose-100 text-rose-900 text-[10px] hover:bg-rose-100 dark:bg-rose-900/40 dark:text-rose-200">
            error
          </Badge>
        )}
      </div>
      {expanded &&
        node.children.map((child) => (
          <DagNodeRow key={child.span_id} node={child} depth={depth + 1} />
        ))}
    </div>
  );
}

export function DagView({ traceId }: { traceId: string }) {
  const [roots, setRoots] = useState<DagNode[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .getDag(traceId)
      .then((data) => {
        if (!cancelled) setRoots(data as DagNode[]);
      })
      .catch((err: Error) => {
        if (!cancelled) setError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [traceId]);

  if (error) {
    return (
      <div className="p-4 text-sm text-rose-600 dark:text-rose-400">
        Failed to load DAG: {error}
      </div>
    );
  }
  if (!roots) {
    return <div className="p-4 text-sm text-muted-foreground">Loading DAG…</div>;
  }
  if (roots.length === 0) {
    return (
      <div className="p-4 text-sm text-muted-foreground">
        No spans to render.
      </div>
    );
  }
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">Execution DAG</CardTitle>
      </CardHeader>
      <CardContent>
        {roots.map((node) => (
          <DagNodeRow key={node.span_id} node={node} depth={0} />
        ))}
      </CardContent>
    </Card>
  );
}
