/**
 * Shared Schedule module editor — same WDL `schedule` shape for document /
 * step.
 */

import { memo } from 'react';

export type ScheduleValue = {
  kind?: string;
  cron?: string;
  interval_seconds?: number;
  event_id?: string;
  webhook_path?: string;
  webhook_secret?: string;
  enabled?: boolean;
  [key: string]: unknown;
};

const KIND_OPTIONS: { value: string; label: string }[] = [
  { value: 'manual', label: '手动' },
  { value: 'cron', label: '定时' },
  { value: 'interval', label: '每隔多久' },
  { value: 'event', label: '事件来了' },
  { value: 'webhook', label: 'Webhook' },
];

export interface SchedulePanelProps {
  value?: ScheduleValue | null;
  onChange: (next: ScheduleValue | null) => void;
  variant?: 'document' | 'step';
  title?: string;
}

export const SchedulePanel = memo(function SchedulePanel({
  value,
  onChange,
  variant = 'step',
  title = '何时启动',
}: SchedulePanelProps) {
  const sched: ScheduleValue = value && typeof value === 'object' ? value : { kind: 'manual' };
  const kind = String(sched.kind || 'manual');

  const patch = (partial: ScheduleValue) => {
    const next = { ...sched, ...partial };
    onChange(next);
  };

  const body = (
    <>
      <div className="form-group">
        <label>方式</label>
        <select value={kind} onChange={(e) => patch({ kind: e.target.value })}>
          {KIND_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      {kind === 'cron' && (
        <div className="form-group">
          <label>时间表达式</label>
          <input
            value={String(sched.cron || '')}
            placeholder="例如每天早上 8 点：0 8 * * *"
            onChange={(e) => patch({ cron: e.target.value })}
          />
        </div>
      )}
      {kind === 'interval' && (
        <div className="form-group">
          <label>间隔（秒）</label>
          <input
            type="number"
            min={1}
            value={Number(sched.interval_seconds) || ''}
            onChange={(e) => patch({ interval_seconds: Number(e.target.value) || 0 })}
          />
        </div>
      )}
      {(kind === 'event' || kind === 'webhook') && (
        <div className="form-group">
          <label>事件源</label>
          <input
            value={String(sched.event_id || '')}
            placeholder="事件源 id"
            onChange={(e) => patch({ event_id: e.target.value })}
          />
        </div>
      )}
      {kind === 'webhook' && (
        <>
          <div className="form-group">
            <label>路径</label>
            <input
              value={String(sched.webhook_path || '')}
              onChange={(e) => patch({ webhook_path: e.target.value })}
            />
          </div>
          <div className="form-group">
            <label>密钥（可选）</label>
            <input
              value={String(sched.webhook_secret || '')}
              onChange={(e) => patch({ webhook_secret: e.target.value })}
            />
          </div>
        </>
      )}
      {variant === 'document' && kind !== 'manual' && (
        <div className="form-group">
          <label>
            <input
              type="checkbox"
              checked={sched.enabled !== false}
              onChange={(e) => patch({ enabled: e.target.checked })}
            />{' '}
            保存后自动监听
          </label>
        </div>
      )}
    </>
  );

  return (
    <div className="config-section">
      <div className="config-section-header" style={{ cursor: 'default' }}>
        <span>{title}</span>
      </div>
      <div className="config-section-body">{body}</div>
    </div>
  );
});
