/**
 * Expression input with {{}} autocomplete dropdown and quick-insert chips.
 */

import { useState, useRef, useEffect, useMemo } from 'react';
import {
  buildExpressionSuggestions,
  expressionQueryAtCursor,
  filterSuggestions,
  insertExpressionAtCursor,
} from './expression-suggestions';
import type { SuggestionRow, ReactFlowNode, ReactFlowEdge } from './types';

const CHIP_LIMIT = 8;

interface ExpressionFieldProps {
  label: string;
  value?: string;
  onChange: (value: string) => void;
  hint?: string;
  suggestions?: SuggestionRow[];
  nodes?: ReactFlowNode[];
  edges?: ReactFlowEdge[];
  selectedNodeId?: string;
  multiline?: boolean;
  /** 不展示常驻快捷芯片（避免每个输入字段下重复一堆） */
  hideChips?: boolean;
  placeholder?: string;
}

function applyInsert(
  inputRef: React.RefObject<HTMLInputElement | HTMLTextAreaElement>,
  value: string,
  onChange: (value: string) => void,
  template: string,
): void {
  const el = inputRef.current;
  const cursor = el?.selectionStart ?? value?.length ?? 0;
  const { value: next, cursor: nextCursor } = insertExpressionAtCursor(value, cursor, template);
  onChange(next);
  requestAnimationFrame(() => {
    if (!el) return;
    el.focus();
    el.setSelectionRange(nextCursor, nextCursor);
  });
}

export function ExpressionField({
  label,
  value,
  onChange,
  hint,
  suggestions: externalSuggestions,
  nodes,
  edges,
  selectedNodeId,
  multiline = false,
  hideChips = false,
  placeholder,
}: ExpressionFieldProps) {
  const inputRef = useRef<HTMLInputElement | HTMLTextAreaElement>(null);
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(0);

  const allSuggestions = useMemo(() => {
    if (externalSuggestions?.length) return externalSuggestions;
    if (!selectedNodeId) return [];
    return buildExpressionSuggestions({ nodes, edges, selectedNodeId });
  }, [externalSuggestions, nodes, edges, selectedNodeId]);

  const chips = allSuggestions.slice(0, CHIP_LIMIT);

  function refreshDropdown() {
    const el = inputRef.current;
    const cursor = el?.selectionStart ?? String(value ?? '').length;
    const query = expressionQueryAtCursor(value, cursor);
    if (query !== null) {
      setOpen(true);
      setActiveIdx(0);
    } else {
      setOpen(false);
    }
  }

  function filteredList(): SuggestionRow[] {
    const el = inputRef.current;
    const cursor = el?.selectionStart ?? String(value ?? '').length;
    const query = expressionQueryAtCursor(value, cursor);
    if (query === null) return [];
    return filterSuggestions(allSuggestions, query);
  }

  function pickSuggestion(template: string) {
    applyInsert(inputRef, value ?? '', onChange, template);
    setOpen(false);
  }

  function onInputChange(e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) {
    onChange(e.target.value);
    requestAnimationFrame(refreshDropdown);
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>) {
    if (!open) {
      if (e.key === '{') {
        requestAnimationFrame(refreshDropdown);
      }
      return;
    }
    const list = filteredList();
    if (!list.length) {
      if (e.key === 'Escape') setOpen(false);
      return;
    }
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIdx((i) => (i + 1) % list.length);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIdx((i) => (i - 1 + list.length) % list.length);
    } else if (e.key === 'Enter' && !multiline) {
      e.preventDefault();
      pickSuggestion(list[activeIdx]?.template || list[0].template);
    } else if (e.key === 'Tab') {
      e.preventDefault();
      pickSuggestion(list[activeIdx]?.template || list[0].template);
    } else if (e.key === 'Escape') {
      setOpen(false);
    } else {
      requestAnimationFrame(refreshDropdown);
    }
  }

  useEffect(() => {
    if (!open) return;
    const list = filteredList();
    if (activeIdx >= list.length) setActiveIdx(0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value, open, activeIdx]);

  const dropdownItems = open ? filteredList() : [];

  const inputEl = multiline ? (
    <textarea
      ref={inputRef as React.RefObject<HTMLTextAreaElement>}
      value={value ?? ''}
      placeholder={placeholder || '可写 {{…}} 引用上游'}
      rows={3}
      onChange={onInputChange}
      onFocus={refreshDropdown}
      onBlur={() => setTimeout(() => setOpen(false), 120)}
      onKeyDown={onKeyDown}
    />
  ) : (
    <input
      ref={inputRef as React.RefObject<HTMLInputElement>}
      type="text"
      value={value ?? ''}
      placeholder={placeholder || '可写 {{…}} 引用上游'}
      onChange={onInputChange}
      onFocus={refreshDropdown}
      onBlur={() => setTimeout(() => setOpen(false), 120)}
      onKeyDown={onKeyDown}
    />
  );

  return (
    <div className="form-group expr-field">
      {label ? <label>{label}</label> : null}
      {!hideChips && chips.length > 0 && (
        <div className="expr-chips" aria-label="快速插入">
          {chips.map((s) => (
            <button
              key={s.template}
              type="button"
              className="expr-chip"
              title={s.template}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => pickSuggestion(s.template)}
            >
              {s.label}
            </button>
          ))}
        </div>
      )}
      <div className="expr-input-wrap">
        {inputEl}
        {dropdownItems.length > 0 && (
          <ul className="expr-dropdown" role="listbox">
            {dropdownItems.map((s, idx) => (
              <li
                key={s.template}
                role="option"
                className={`expr-dropdown-item${idx === activeIdx ? ' active' : ''}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => pickSuggestion(s.template)}
                onMouseEnter={() => setActiveIdx(idx)}
              >
                <span className="expr-dd-template">{s.template}</span>
                <span className="expr-dd-group">{s.group}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
      {hint && <div className="form-hint expr-hint">{hint}</div>}
    </div>
  );
}
