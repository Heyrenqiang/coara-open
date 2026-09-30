/**
 * ConfigField — typed config panel field with type dispatch.
 *
 * 参考 Flowise EditNodeDialog 的字段类型分发理念，
 * 但代码原创：支持 text/textarea/chip-list/number/readonly 五种类型，
 * 统一布局与验证反馈。
 */

import { memo, useState } from 'react';
import { cx } from './dom';

interface TextFieldProps {
  value?: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  placeholder?: string;
}

function TextField({ value, onChange, readOnly, placeholder }: TextFieldProps) {
  return (
    <input
      type="text"
      className="config-input"
      value={value || ''}
      readOnly={readOnly}
      placeholder={placeholder || ''}
      onChange={readOnly ? undefined : (e) => onChange && onChange(e.target.value)}
    />
  );
}

interface TextAreaProps {
  value?: string;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  placeholder?: string;
}

function TextArea({ value, onChange, readOnly, placeholder }: TextAreaProps) {
  return (
    <textarea
      className="config-input config-textarea"
      value={value || ''}
      readOnly={readOnly}
      rows={3}
      placeholder={placeholder || ''}
      onChange={readOnly ? undefined : (e) => onChange && onChange(e.target.value)}
    />
  );
}

interface NumberFieldProps {
  value: number | null;
  onChange?: (value: number | null) => void;
}

function NumberField({ value, onChange }: NumberFieldProps) {
  return (
    <input
      type="number"
      className="config-input"
      value={value ?? ''}
      onChange={(e) => onChange && onChange(e.target.value === '' ? null : Number(e.target.value))}
    />
  );
}

interface ChipListFieldProps {
  value?: string[];
  onChange?: (value: string[]) => void;
  suggestions?: string[];
  placeholder?: string;
  readOnly?: boolean;
}

function ChipListField({ value, onChange, suggestions, placeholder, readOnly }: ChipListFieldProps) {
  const [input, setInput] = useState('');
  const [showSuggest, setShowSuggest] = useState(false);
  const items = Array.isArray(value) ? value : [];

  const add = (item: string) => {
    const trimmed = item.trim();
    if (!trimmed || items.includes(trimmed)) return;
    onChange && onChange([...items, trimmed]);
    setInput('');
    setShowSuggest(false);
  };

  const remove = (item: string) => {
    onChange && onChange(items.filter((i) => i !== item));
  };

  const filtered = (suggestions || []).filter(
    (s) => !items.includes(s) && s.toLowerCase().includes(input.toLowerCase()),
  );

  return (
    <div className="chip-list-field">
      <div className="chip-list-items">
        {items.map((item) => (
          <span key={item} className="chip-list-chip">
            {item}
            {!readOnly && (
              <button
                type="button"
                className="chip-list-remove"
                aria-label={`移除 ${item}`}
                onClick={() => remove(item)}
              >
                ×
              </button>
            )}
          </span>
        ))}
      </div>
      {!readOnly && (
        <div className="chip-list-input-wrap">
          <input
            type="text"
            className="config-input chip-list-input"
            value={input}
            placeholder={placeholder || '输入后回车添加'}
            onChange={(e) => { setInput(e.target.value); setShowSuggest(true); }}
            onFocus={() => setShowSuggest(true)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); add(input); }
              else if (e.key === 'Backspace' && !input && items.length) {
                remove(items[items.length - 1]);
              }
            }}
          />
          {showSuggest && filtered.length > 0 && (
            <ul className="chip-list-suggestions">
              {filtered.slice(0, 8).map((s) => (
                <li
                  key={s}
                  className="chip-list-suggestion"
                  onClick={() => add(s)}
                >
                  {s}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

type ConfigFieldType = 'text' | 'textarea' | 'number' | 'chip-list' | 'readonly' | 'select';

interface ConfigFieldOption {
  value: string;
  label: string;
}

interface ConfigFieldProps {
  label?: string;
  type?: ConfigFieldType;
  value?: unknown;
  onChange?: (value: unknown) => void;
  error?: string;
  hint?: string;
  readOnly?: boolean;
  suggestions?: string[];
  placeholder?: string;
  options?: ConfigFieldOption[];
}

interface SelectFieldProps {
  value?: string;
  onChange?: (value: string) => void;
  options?: ConfigFieldOption[];
  readOnly?: boolean;
  placeholder?: string;
}

function SelectField({ value, onChange, options, readOnly, placeholder }: SelectFieldProps) {
  const opts = options || [];
  return (
    <select
      className="config-input"
      value={value || ''}
      disabled={readOnly}
      onChange={(e) => onChange && onChange(e.target.value)}
    >
      {placeholder && !opts.some((o) => o.value === (value || '')) && (
        <option value="">{placeholder}</option>
      )}
      {opts.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
}

function ConfigFieldComponent({
  label,
  type = 'text',
  value,
  onChange,
  error,
  hint,
  readOnly,
  suggestions,
  placeholder,
  options,
}: ConfigFieldProps) {
  let field: JSX.Element;
  switch (type) {
    case 'textarea':
      field = (
        <TextArea
          value={value as string | undefined}
          onChange={onChange}
          readOnly={readOnly}
          placeholder={placeholder}
        />
      );
      break;
    case 'number':
      field = (
        <NumberField
          value={value as number | null}
          onChange={onChange}
        />
      );
      break;
    case 'chip-list':
      field = (
        <ChipListField
          value={value as string[] | undefined}
          onChange={onChange}
          suggestions={suggestions}
          placeholder={placeholder}
          readOnly={readOnly}
        />
      );
      break;
    case 'select':
      field = (
        <SelectField
          value={value as string | undefined}
          onChange={onChange as ((v: string) => void) | undefined}
          options={options}
          readOnly={readOnly}
          placeholder={placeholder}
        />
      );
      break;
    case 'readonly':
      field = (
        <TextField
          value={value as string | undefined}
          onChange={() => {}}
          readOnly
          placeholder={placeholder}
        />
      );
      break;
    case 'text':
    default:
      field = (
        <TextField
          value={value as string | undefined}
          onChange={onChange}
          readOnly={readOnly}
          placeholder={placeholder}
        />
      );
      break;
  }

  return (
    <div className={cx('config-field', !label && 'config-field-nolabel', error && 'config-field-error')}>
      {label ? <label className="config-label">{label}</label> : null}
      {field}
      {hint && !error && <div className="config-hint">{hint}</div>}
      {error && <div className="config-error">{error}</div>}
    </div>
  );
}

export const ConfigField = memo(ConfigFieldComponent);
