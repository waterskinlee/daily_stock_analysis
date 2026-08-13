import { useEffect, useRef, useState } from 'react';
import { scheduledRunApi, type ScheduledRunStatus } from '../../api/analysis';
import { useUiLanguage } from '../../contexts/UiLanguageContext';

const POLL_INTERVAL_MS = 3000;

/**
 * Live progress banner for background scheduled analysis batches written by
 * the analyzer container. Polls the read-only scheduled-run endpoint; renders
 * nothing when no batch is currently running.
 */
export const ScheduledRunBanner: React.FC = () => {
  const { t } = useUiLanguage();
  const [runs, setRuns] = useState<ScheduledRunStatus[]>([]);
  const [failed, setFailed] = useState(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    let active = true;

    const poll = async () => {
      try {
        const next = await scheduledRunApi.listActiveRuns();
        if (active) {
          setRuns(next);
          setFailed(false);
        }
      } catch {
        if (active) {
          setFailed(true);
        }
      }
    };

    void poll();
    timerRef.current = window.setInterval(() => {
      void poll();
    }, POLL_INTERVAL_MS);

    return () => {
      active = false;
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  if (failed) {
    return (
      <div className="mb-3 rounded-lg border border-[var(--settings-border)] bg-[var(--settings-surface)] px-4 py-3 text-xs text-muted-text">
        {t('home.scheduledRunRefreshFailed')}
      </div>
    );
  }

  if (runs.length === 0) {
    return null;
  }

  return (
    <div className="mb-3 space-y-2">
      {runs.map((run) => (
        <div
          key={run.runId}
          className="rounded-lg border border-[var(--settings-border)] bg-[var(--settings-surface)] px-4 py-3"
        >
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="text-sm font-semibold text-foreground">
              {t('home.scheduledRunBannerTitle')}
            </span>
            <span className="text-xs text-muted-text">
              {t('home.scheduledRunTotal', { total: String(run.stockCount) })}
            </span>
          </div>
          <p className="mt-1 font-mono text-[11px] text-muted-text">
            {t('home.scheduledRunId')}: {run.runId}
          </p>
        </div>
      ))}
    </div>
  );
};
