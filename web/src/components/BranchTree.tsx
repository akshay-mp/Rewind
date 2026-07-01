// Branch tree — collapsible view of every branch in a trace.
//
// This is the Phase 5 navigation surface. Users pick two branches (one left,
// one right) which drives the DiffView. Each row has two explicit pick
// buttons — "use as left" and "use as right" — so the gesture is
// unambiguous (a meta-click shortcut was tried first but proved fragile:
// holding ⌘ across two clicks silently re-picked the wrong slot).
//
// The tree itself is rendered recursively: a single root node with N
// children, each of which may recurse. Indentation is explicit so the tree
// shape is readable at any depth.

import { useEffect, useState } from "react";
import { ApiError, api } from "../api";
import { formatTimestamp } from "../styles";
import type { BranchNodeView } from "../types";

interface Props {
  traceId: string;
  /** Active left pick for diff. ``null`` until the user selects one. */
  leftBranchId: string | null;
  /** Active right pick for diff. */
  rightBranchId: string | null;
  /** Notifies the parent when a branch row is clicked. */
  onPickLeft: (branchId: string) => void;
  onPickRight: (branchId: string) => void;
  /**
   * Click handler to fork a new branch off the given branch at its last
   * span index. ``undefined`` disables the action (e.g. while a fork is
   * already pending). Provided by the parent so the inspector/branch-from
   * modal can share state with this list.
   */
  onBranchFrom?: ((branch: BranchNodeView) => void) | undefined;
  /** Optional: hide the per-row branch-from button. */
  hideBranchFromAction?: boolean | undefined;
}

export function BranchTree({
  traceId,
  leftBranchId,
  rightBranchId,
  onPickLeft,
  onPickRight,
  onBranchFrom,
  hideBranchFromAction = false,
}: Props): JSX.Element {
  const [root, setRoot] = useState<BranchNodeView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getBranchTree(traceId)
      .then((tree) => {
        if (!cancelled) setRoot(tree);
      })
      .catch((err: ApiError | Error) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [traceId]);

  return (
    <section className="branch-tree" aria-label="Branch tree">
      <header className="branch-tree__header">
        <h3>branches</h3>
        <span className="muted">use each row's ← / → buttons to pick</span>
      </header>

      {error !== null && <div className="banner banner--error">{error}</div>}
      {loading && <div className="muted">loading…</div>}
      {!loading && error === null && root !== null && (
        <ul className="branch-tree__list" role="tree">
          <BranchRow
            node={root}
            depth={0}
            leftBranchId={leftBranchId}
            rightBranchId={rightBranchId}
            onPickLeft={onPickLeft}
            onPickRight={onPickRight}
            onBranchFrom={onBranchFrom}
            hideBranchFromAction={hideBranchFromAction}
          />
        </ul>
      )}
      {!loading && error === null && root === null && (
        <div className="muted">no branches in this trace.</div>
      )}
    </section>
  );
}

interface RowProps {
  node: BranchNodeView;
  depth: number;
  leftBranchId: string | null;
  rightBranchId: string | null;
  onPickLeft: (branchId: string) => void;
  onPickRight: (branchId: string) => void;
  onBranchFrom: ((branch: BranchNodeView) => void) | undefined;
  hideBranchFromAction: boolean | undefined;
}

function BranchRow({
  node,
  depth,
  leftBranchId,
  rightBranchId,
  onPickLeft,
  onPickRight,
  onBranchFrom,
  hideBranchFromAction,
}: RowProps): JSX.Element {
  const isLeft = leftBranchId === node.branch_id;
  const isRight = rightBranchId === node.branch_id;
  const isRoot = node.parent_branch_id === null;

  const className =
    "branch-tree__row" +
    (isLeft ? " branch-tree__row--left" : "") +
    (isRight ? " branch-tree__row--right" : "") +
    (isLeft && isRight ? " branch-tree__row--both" : "");

  return (
    <li role="treeitem" aria-selected={isLeft || isRight}>
      <div
        className={className}
        style={{ paddingLeft: `${0.75 + depth * 1.2}rem` }}
      >
        <span className="branch-tree__label">
          <span className="branch-tree__mode">{node.mode}</span>
          <span className="branch-tree__name">{node.label}</span>
          {node.branch_at_index !== null && (
            <span className="muted"> @ {node.branch_at_index}</span>
          )}
        </span>
        <time className="muted branch-tree__when">
          {formatTimestamp(node.created_at)}
        </time>
        <div className="branch-tree__picks">
          <button
            type="button"
            className={
              "branch-tree__pick" +
              (isLeft ? " branch-tree__pick--active" : "")
            }
            onClick={() => onPickLeft(node.branch_id)}
            title={
              isRoot
                ? "Use this branch as the LEFT side of the diff"
                : `forked off ${node.parent_branch_id?.slice(0, 8)}… at index ${node.branch_at_index} — use as LEFT`
            }
            aria-pressed={isLeft}
          >
            ← left
          </button>
          <button
            type="button"
            className={
              "branch-tree__pick" +
              (isRight ? " branch-tree__pick--active" : "")
            }
            onClick={() => onPickRight(node.branch_id)}
            title="Use this branch as the RIGHT side of the diff"
            aria-pressed={isRight}
          >
            right →
          </button>
          {onBranchFrom !== undefined && !hideBranchFromAction && (
            <button
              type="button"
              className="link-button branch-tree__fork"
              onClick={() => onBranchFrom(node)}
            >
              fork ⎇
            </button>
          )}
        </div>
      </div>
      {node.children.length > 0 && (
        <ul role="group">
          {node.children.map((child) => (
            <BranchRow
              key={child.branch_id}
              node={child}
              depth={depth + 1}
              leftBranchId={leftBranchId}
              rightBranchId={rightBranchId}
              onPickLeft={onPickLeft}
              onPickRight={onPickRight}
              onBranchFrom={onBranchFrom}
              hideBranchFromAction={hideBranchFromAction}
            />
          ))}
        </ul>
      )}
    </li>
  );
}
