import { useEffect, useRef, useState } from 'react';
import { Button } from '../common';
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
  const [cancellingRunId, setCancellingRunId] = useState<string | null>(null);
  const [cancelErrorRunId, setCancelErrorRunId] = useState<string | null>(null);
  const timerRef = useRef<number | null>(null);
  const activeRef = useRef(true);

  useEffect(() => {
    activeRef.current = true;

    const poll = async () => {
      try {
        const next = await scheduledRunApi.listActiveRuns();
        if (activeRef.current) {
          setRuns(next);
          setFailed(false);
        }
      } catch {
        if (activeRef.current) {
          setFailed(true);
        }
      }
    };

    void poll();
    timerRef.current = window.setInterval(() => {
      void poll();
    }, POLL_INTERVAL_MS);

    return () => {
      activeRef.current = false;
      if (timerRef.current !== null) {
        window.clearInterval(timerRef.current);
        timerRef.current = null;
      }
    };
  }, []);

  const handleCancel = async (run: ScheduledRunStatus) => {
    if (run.status !== 'running' || cancellingRunId !== null) {
      return;
    }
    setCancellingRunId(run.runId);
    setCancelErrorRunId(null);
    try {
      const updated = await scheduledRunApi.cancelRun(run.runId);
      if (activeRef.current) {
        setRuns((current) => current.map((item) => item.runId === updated.runId ? updated : item));
      }
    } catch {
      if (activeRef.current) {
        setCancelErrorRunId(run.runId);
      }
    } finally {
      if (activeRef.current) {
        setCancellingRunId(null);
      }
    }
  };

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
              {run.status === 'cancel_requested' ? t('home.scheduledRunCancelRequested') : t('home.scheduledRunBannerTitle')}
            </span>
            <span className="text-xs text-muted-text">
              {t('home.scheduledRunTotal', { total: String(run.stockCount) })}
            </span>
          </div>
          <p className="mt-1 font-mono text-[11px] text-muted-text">
            {t('home.scheduledRunId')}: {run.runId}
          </p>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
            {cancelErrorRunId === run.runId ? (
              <p className="text-xs text-danger" role="status">
                {t('home.scheduledRunCancelFailed')}
              </p>
            ) : <span />}
            <Button
              type="button"
              variant="danger-subtle"
              size="sm"
              className="h-10 shrink-0"
              disabled={run.status !== 'running' || cancellingRunId !== null}
              isLoading={cancellingRunId === run.runId}
              aria-label={t(
                run.status === 'cancel_requested'
                  ? 'home.scheduledRunCancelRequested'
                  : 'home.scheduledRunCancel',
              )}
              onClick={() => void handleCancel(run)}
            >
              {t('home.scheduledRunCancel')}
            </Button>
          </div>
        </div>
      ))}
    </div>
  );
};
